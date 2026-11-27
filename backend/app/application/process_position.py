"""Apertura, valoración y cierre simulado de posiciones.

Invariantes:
- Reservar capital y crear la posición ocurre en una sola transacción con la fila de la sesión
  bloqueada: dos workers no pueden abrir dos veces la misma señal ni exceder el presupuesto.
- Cada aplicación de un intento escribe asientos con clave única ``(intento, tipo)``: reaplicar un
  intento no duplica capital.
- Las llamadas de red (cotizaciones, FX) se hacen fuera de las transacciones y la vigencia se
  revalida al escribir.
- Las sesiones que admiten la misma señal en el mismo ciclo entran con la misma cotización; posiciones del mismo token
  y la misma cantidad que venden a la vez también comparten la de salida.
- Una venta fallida deja la posición abierta, actualiza su valoración y habilita un reintento tras una espera
  creciente. Cada intento se compara con la cotización tomada al programarlo, nunca con la de un intento anterior.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from dataclasses import dataclass, replace
from datetime import datetime, timedelta
from decimal import Decimal

from app.application.evaluate_pool import unavailable_quote
from app.application.ports import (
    Clock,
    FxProvider,
    FxRate,
    QuoteProvider,
    TransientSourceError,
    UnitOfWork,
    UnitOfWorkFactory,
)
from app.domain.config import StrategyConfig
from app.domain.costs import adverse_exit_proceeds, net_exit_proceeds
from app.domain.models import (
    LAMPORTS_PER_SOL,
    AttemptInfo,
    MarketSnapshot,
    PoolInfo,
    PositionInfo,
    Quote,
    SignalInfo,
)
from app.domain.reasons import (
    AttemptKind,
    AttemptStatus,
    LedgerKind,
    PositionStatus,
    ReasonCode,
)
from app.domain.risk import RiskLimits, check_entry
from app.domain.simulation import (
    EntryDecision,
    ExitSignal,
    check_exit_trigger,
    decide_entry,
    decide_exit_fill,
    exit_retry_delay_s,
    exit_slippage_bps,
)
from app.domain.strategy import RankItem, liquidity_order, ranking_order

log = logging.getLogger(__name__)

ConfigLoader = Callable[[int], StrategyConfig]


# --------------------------------------------------------------------------- utilidades
def _safe_buy(quotes: QuoteProvider, pool: PoolInfo, lamports: int, slippage_bps: int, now: datetime) -> Quote:
    try:
        return quotes.quote_buy(pool, lamports, slippage_bps, now)
    except Exception as exc:
        return unavailable_quote("buy", pool, now, f"{type(exc).__name__}: {exc}")


def _safe_sell(quotes: QuoteProvider, pool: PoolInfo, tokens: int, slippage_bps: int, now: datetime) -> Quote:
    try:
        return quotes.quote_sell(pool, tokens, slippage_bps, now)
    except Exception as exc:
        return unavailable_quote("sell", pool, now, f"{type(exc).__name__}: {exc}")


def _safe_fx(fx: FxProvider | None, now: datetime) -> FxRate | None:
    if fx is None:
        return None
    try:
        return fx.sol_usd(now)
    except Exception:
        log.warning("no se pudo obtener la referencia SOL/USD", exc_info=True)
        return None


def _entry_price(lamports_in: int, tokens_out: int, decimals: int) -> Decimal:
    """Precio de entrada en SOL por token (unidad entera)."""
    return (Decimal(lamports_in) / LAMPORTS_PER_SOL) / (Decimal(tokens_out) / Decimal(10**decimals))


# --------------------------------------------------------------------------- admisión
@dataclass(frozen=True)
class AdmissionOutcome:
    signal_id: int
    position_id: int | None
    admitted: bool
    code: str | None


def admit_signals(
    *,
    uow_factory: UnitOfWorkFactory,
    clock: Clock,
    session_id: int,
    cfg: StrategyConfig,
    blocked_sources: frozenset[str] = frozenset(),
    at: datetime | None = None,
) -> list[AdmissionOutcome]:
    """Procesa las señales vigentes que la sesión todavía no vio.

    El orden depende de la política: ``ranking`` (puntaje) o ``liquidity`` (referencia simple). Las
    dos sesiones ven el mismo universo elegible en el mismo instante, con idénticos límites. ``at`` es el
    instante del ciclo: con el mismo valor, las entradas de ambas sesiones quedan programadas a la vez y
    comparten la cotización.
    """
    now = at or clock.now()
    outcomes: list[AdmissionOutcome] = []
    with uow_factory() as uow:
        sess = uow.sessions.lock(session_id)  # serializa las reservas de esta sesión
        if sess is None:
            return outcomes
        pending = uow.signals.list_unprocessed(session_id, sess.strategy_version_id, now)
        if not pending:
            return outcomes

        by_id = {s.id: s for s in pending}
        items = [RankItem(s.id, s.score, s.round_trip_cost_pct, s.issued_at, s.liquidity_lamports) for s in pending]
        ordered = ranking_order(items) if sess.policy == "ranking" else liquidity_order(items)
        limits = RiskLimits(
            sess.trade_size_lamports, sess.max_exposure_lamports, sess.max_positions, sess.max_session_loss_lamports
        )

        candidates = sorted(by_id)
        for rank, item in enumerate(ordered, start=1):
            sig = by_id[item.key]
            # queda en la auditoría para saber, entre sesiones, cuándo el orden de selección cambió lo elegido
            selection = {"cycle": now.isoformat(), "rank": rank, "candidates": candidates}
            if blocked_sources:
                code, numbers, allowed = ReasonCode.SOURCE_DEGRADED, {"sources": sorted(blocked_sources)}, False
            else:
                decision = check_entry(limits, uow.sessions.state(session_id), sig.amount_lamports)
                code, numbers, allowed = decision.code, decision.numbers, decision.allowed

            if allowed:
                position_id = uow.positions.create(
                    session_id=session_id,
                    signal=sig,
                    status=PositionStatus.PENDING_ENTRY,
                    reserved_lamports=sig.amount_lamports,
                    now=now,
                )
                if position_id is None:
                    continue  # otro worker ya procesó esta señal en la sesión
                scheduled = now + timedelta(seconds=cfg.entry_delay_s)
                attempt_id = uow.attempts.create(
                    position_id=position_id,
                    kind=AttemptKind.ENTRY.value,
                    number=1,
                    scheduled_at=scheduled,
                    detail={"reserved_lamports": str(sig.amount_lamports)},
                )
                assert attempt_id is not None
                uow.ledger.add(
                    session_id=session_id, position_id=position_id, attempt_id=attempt_id,
                    kind=LedgerKind.RESERVE, amount=sig.amount_lamports, now=now,
                )  # fmt: skip
                uow.jobs.enqueue(
                    type="entry_attempt", payload={"attempt_id": attempt_id},
                    key=f"entry_attempt:{attempt_id}", run_at=scheduled, now=now,
                )  # fmt: skip
                uow.audit.log("paper_position", str(position_id), "reserved", None, PositionStatus.PENDING_ENTRY.value,
                              {"signal_id": sig.id, "session_id": session_id, "selection": selection}, now)  # fmt: skip
                outcomes.append(AdmissionOutcome(sig.id, position_id, True, None))
            else:
                position_id = uow.positions.create(
                    session_id=session_id,
                    signal=sig,
                    status=PositionStatus.REJECTED,
                    reserved_lamports=0,
                    now=now,
                    reject_reason=code.value if code else None,
                    reject_detail=numbers,
                )
                if position_id is None:
                    continue
                uow.audit.log("paper_position", str(position_id), "rejected", None, PositionStatus.REJECTED.value,
                              {"code": code.value if code else None, "signal_id": sig.id, "selection": selection}, now)  # fmt: skip
                outcomes.append(AdmissionOutcome(sig.id, position_id, False, code.value if code else None))
    return outcomes


# --------------------------------------------------------------------------- entrada simulada
def execute_entry(
    *,
    uow_factory: UnitOfWorkFactory,
    quotes: QuoteProvider,
    fx: FxProvider | None,
    clock: Clock,
    cfg_loader: ConfigLoader,
    attempt_id: int,
) -> str:
    """Ejecuta el intento de entrada tras el retraso simulado, con cotizaciones nuevas.

    Los intentos de la misma señal programados para el mismo instante (uno por sesión) se resuelven juntos: una sola
    cotización, un solo momento de decisión y una sola transacción. Así una diferencia entre sesiones no puede deberse
    a que una haya entrado más tarde. El trabajo de los demás intentos ya no encuentra nada pendiente.

    Cada intento guarda en ``detail["timing"]`` cuándo apareció la señal, cuándo tocaba cotizar, cuándo se pidió la
    cotización y cuándo llegó.
    """
    now = clock.now()
    with uow_factory() as uow:
        att = uow.attempts.get(attempt_id)
        if att is None or att.status != AttemptStatus.PENDING.value or att.kind != AttemptKind.ENTRY.value:
            return "noop"
        if att.scheduled_at > now:
            raise TransientSourceError("el intento de entrada todavía no está vencido")
        pos = uow.positions.get(att.position_id)
        if pos is None or pos.status != PositionStatus.PENDING_ENTRY.value:
            return "noop"
        sig = uow.signals.get(pos.signal_id)
        pool = uow.chain.get_pool(pos.pool)
        assert sig is not None and pool is not None
        snaps = uow.snapshots.list_for_pool(pool.address, now)
        latest = snaps[-1] if snaps else None
    cfg = cfg_loader(sig.strategy_version_id)

    requested_at = clock.now()
    buy = _safe_buy(quotes, pool, sig.amount_lamports, cfg.slippage_bps, now)  # red
    sell = _safe_sell(quotes, pool, buy.min_out_amount, cfg.slippage_bps, now) if buy.ok else None  # red
    received_at = clock.now()
    fx_rate = _safe_fx(fx, now)  # red
    decided_at = clock.now()
    decision = decide_entry(signal=sig, buy=buy, sell=sell, latest=latest, now=decided_at, cfg=cfg)
    timing = {
        "signal_at": sig.issued_at.isoformat(),
        "due_at": att.scheduled_at.isoformat(),
        "requested_at": requested_at.isoformat(),
        "received_at": received_at.isoformat(),
    }

    with uow_factory() as uow:
        batch = uow.attempts.entry_batch(sig.id, att.scheduled_at)
        for session_id, _ in batch:  # vienen ordenadas por sesión: dos workers bloquean en el mismo orden
            uow.sessions.lock(session_id)
        ready: list[tuple[AttemptInfo, PositionInfo]] = []
        for _, batch_id in batch:
            item = uow.attempts.get(batch_id, lock=True)
            if item is None or item.status != AttemptStatus.PENDING.value:
                continue
            position = uow.positions.get(item.position_id, lock=True)
            if position is not None and position.status == PositionStatus.PENDING_ENTRY.value:
                ready.append((item, position))
        if all(item.id != attempt_id for item, _ in ready):
            return "noop"

        buy_id = uow.quotes.save(buy, pool=pool.address, purpose="entry")
        sell_id = uow.quotes.save(sell, pool=pool.address, purpose="entry") if sell else None
        if fx_rate:
            uow.quotes.save_fx(fx_rate)

        results: dict[int, str] = {}
        for item, position in ready:
            shared = [other.id for other, _ in ready if other.id != item.id]
            results[item.id] = _apply_entry(
                uow, attempt=item, position=position, signal=sig, pool=pool, latest=latest, cfg=cfg,
                decision=decision, buy=buy, sell=sell, buy_id=buy_id, sell_id=sell_id, fx_rate=fx_rate,
                decided_at=decided_at, trace={"timing": timing, "shared_quote_with": shared},
            )  # fmt: skip
        return results[attempt_id]


def _apply_entry(
    uow: UnitOfWork,
    *,
    attempt: AttemptInfo,
    position: PositionInfo,
    signal: SignalInfo,
    pool: PoolInfo,
    latest: MarketSnapshot | None,
    cfg: StrategyConfig,
    decision: EntryDecision,
    buy: Quote,
    sell: Quote | None,
    buy_id: int,
    sell_id: int | None,
    fx_rate: FxRate | None,
    decided_at: datetime,
    trace: dict,
) -> str:
    """Aplica una decisión de entrada a la posición de una sesión (pausa y capital se revisan por sesión)."""
    session = uow.sessions.get(position.session_id)
    assert session is not None
    accepted = decision.accepted
    code = decision.code
    message = decision.message
    detail = dict(decision.detail)
    reserved = position.reserved_lamports

    # La reserva se libera siempre: o se convierte en débito real, o se devuelve.
    uow.ledger.add(
        session_id=session.id, position_id=position.id, attempt_id=attempt.id,
        kind=LedgerKind.RELEASE, amount=reserved, now=decided_at,
    )  # fmt: skip

    if accepted and session.paused:
        accepted, code, message = False, ReasonCode.SESSION_PAUSED, "la sesión se pausó antes de la entrada"
    rt = decision.round_trip
    if accepted:
        assert rt is not None
        if uow.sessions.state(session.id).available_lamports < rt.total_debited_lamports:
            accepted, code, message = False, ReasonCode.INSUFFICIENT_CAPITAL, "capital insuficiente tras revalidar"

    if accepted:
        assert rt is not None and sell is not None
        uow.ledger.add(
            session_id=session.id, position_id=position.id, attempt_id=attempt.id,
            kind=LedgerKind.DEBIT, amount=rt.total_debited_lamports, now=decided_at,
        )  # fmt: skip
        immobilized = rt.immobilized_lamports if cfg.ata_deposit_refundable else 0
        uow.positions.update(
            position.id,
            decided_at,
            status=PositionStatus.OPEN.value,
            tokens_expected=rt.tokens_expected,
            tokens_min=rt.tokens_min,
            debited_lamports=rt.total_debited_lamports,
            reserved_lamports=0,
            immobilized_lamports=immobilized,
            entry_price=_entry_price(buy.in_amount, buy.out_amount, pool.decimals),
            entry_liquidity_lamports=latest.liquidity_lamports if latest else None,
            opened_at=decided_at,
            last_recoverable_lamports=net_exit_proceeds(sell) + immobilized,
            last_recoverable_adverse_lamports=adverse_exit_proceeds(sell) + immobilized,
            last_valued_at=decided_at,
            valuation_status="ok",
            sol_usd_open=fx_rate.rate if fx_rate else None,
        )
        uow.attempts.finish(
            attempt.id,
            status=AttemptStatus.APPLIED,
            executed_at=decided_at,
            quote_id=buy_id,
            requested_amount=buy.in_amount,
            result_amount=rt.tokens_expected,
            detail={"sell_quote_id": sell_id, "round_trip": rt.to_dict(), "message": message, **trace},
        )
        uow.audit.log("paper_position", str(position.id), "opened", PositionStatus.PENDING_ENTRY.value,
                      PositionStatus.OPEN.value, {"cost_pct": str(rt.cost_pct)}, decided_at)  # fmt: skip
        return "opened"

    uow.positions.update(
        position.id,
        decided_at,
        status=PositionStatus.REJECTED.value,
        reserved_lamports=0,
        reject_reason=code.value if code else None,
        reject_detail={"message": message, **detail},
    )
    uow.attempts.finish(
        attempt.id,
        status=AttemptStatus.REJECTED,
        executed_at=decided_at,
        quote_id=buy_id,
        requested_amount=buy.in_amount or signal.amount_lamports,
        failure_cause=code.value if code else None,
        detail={"sell_quote_id": sell_id, "message": message, **detail, **trace},
    )
    uow.audit.log("paper_position", str(position.id), "entry_rejected", PositionStatus.PENDING_ENTRY.value,
                  PositionStatus.REJECTED.value, {"code": code.value if code else None}, decided_at)  # fmt: skip
    return f"rejected:{code.value if code else 'unknown'}"


# --------------------------------------------------------------------------- monitoreo y salida
@dataclass(frozen=True)
class MonitorOutcome:
    position_id: int
    valued: bool
    trigger: str | None = None
    attempt_id: int | None = None


def monitor_positions(
    *,
    uow_factory: UnitOfWorkFactory,
    quotes: QuoteProvider,
    clock: Clock,
    cfg_loader: ConfigLoader,
) -> list[MonitorOutcome]:
    """Valora cada posición abierta con una cotización de salida y programa los intentos de salida.

    Se ejecuta aunque la sesión esté pausada o haya llegado a sus límites: el seguimiento no se corta, y de esta
    valoración depende el límite de pérdida. Una vez disparada la salida, el disparo no cambia; cada reintento es un
    intento nuevo cuya referencia es la cotización de este momento. Mientras espera, la posición se revalora como
    mucho cada ``exit_retry_interval_s`` para no gastar el cupo de cotizaciones.

    Las posiciones del mismo token y la misma cantidad que se revisan en esta pasada comparten la cotización y el
    instante: cada una aplica sus propias reglas sobre el mismo dato y, si salen, quedan programadas a la vez.
    """
    now = clock.now()
    with uow_factory() as uow:
        opened = uow.positions.list([PositionStatus.OPEN])
    groups: dict[tuple[str, int, int], list[tuple[PositionInfo, PoolInfo, MarketSnapshot | None, StrategyConfig]]] = {}

    for pos in opened:
        with uow_factory() as uow:
            session = uow.sessions.get(pos.session_id)
            pool = uow.chain.get_pool(pos.pool)
            assert session is not None and pool is not None
            snaps = uow.snapshots.list_for_pool(pool.address, now)
            latest = snaps[-1] if snaps else None
        cfg = cfg_loader(session.strategy_version_id)
        if (
            pos.exit_trigger
            and pos.next_exit_at is not None
            and now < pos.next_exit_at
            and pos.last_valued_at is not None
            and (now - pos.last_valued_at).total_seconds() < cfg.exit_retry_interval_s
        ):
            continue
        key = (pool.address, pos.tokens_expected, cfg.exit_slippage_bps)
        groups.setdefault(key, []).append((pos, pool, latest, cfg))

    outcomes: list[MonitorOutcome] = []
    for (_, tokens, slippage_bps), members in groups.items():
        pool = members[0][1]
        sell = _safe_sell(quotes, pool, tokens, slippage_bps, now)  # red: una por grupo
        valued_at = clock.now()
        with uow_factory() as uow:
            if not sell.ok:
                uow.quotes.save(sell, pool=pool.address, purpose="valuation")  # evidencia de fallas de cotización
            for pos, _, latest, cfg in members:  # en orden de id: dos workers bloquean en el mismo orden
                outcome = _review_position(uow, pos.id, sell=sell, latest=latest, cfg=cfg, valued_at=valued_at)
                if outcome is not None:
                    outcomes.append(outcome)
    return outcomes


def _review_position(
    uow: UnitOfWork,
    position_id: int,
    *,
    sell: Quote,
    latest: MarketSnapshot | None,
    cfg: StrategyConfig,
    valued_at: datetime,
) -> MonitorOutcome | None:
    """Valora una posición con la cotización dada y, si corresponde, programa su próximo intento de salida."""
    p = uow.positions.get(position_id, lock=True)
    if p is None or p.status != PositionStatus.OPEN.value:
        return None
    if sell.ok:
        recoverable = net_exit_proceeds(sell) + p.immobilized_lamports
        adverse = adverse_exit_proceeds(sell) + p.immobilized_lamports
        valuation_status = "ok"
    else:
        recoverable = adverse = 0  # escenario conservador: recuperación cero
        valuation_status = sell.status.value

    trigger: ExitSignal | None
    if p.exit_trigger:  # la salida ya está decidida: se conserva el disparo que la inició
        trigger = ExitSignal(ReasonCode(p.exit_trigger), "reintento de una salida ya disparada")
    else:
        trigger = check_exit_trigger(
            position=p, latest=latest, recoverable_lamports=recoverable if sell.ok else None, now=valued_at, cfg=cfg
        )
    waiting = p.exit_trigger is not None and p.next_exit_at is not None and valued_at < p.next_exit_at
    busy = bool(uow.attempts.pending(p.id, AttemptKind.EXIT.value))

    fields: dict = {
        "last_recoverable_lamports": recoverable,
        "last_recoverable_adverse_lamports": adverse,
        "last_valued_at": valued_at,
        "valuation_status": valuation_status,
        "illiquid": (not sell.ok) or p.exit_failures >= cfg.exit_retry_max,
    }
    attempt_id = None
    if trigger is not None and not busy and not waiting:
        number = uow.attempts.last_number(p.id, AttemptKind.EXIT.value) + 1
        scheduled = valued_at + timedelta(seconds=cfg.exit_delay_s)
        attempt_id = uow.attempts.create(
            position_id=p.id,
            kind=AttemptKind.EXIT.value,
            number=number,
            scheduled_at=scheduled,
            trigger=trigger.code.value,
            reference_out_lamports=sell.out_amount if sell.ok else None,
            detail={"trigger_detail": trigger.detail},
        )
        if attempt_id is not None:
            fields["exit_trigger"] = trigger.code.value
            fields["next_exit_at"] = scheduled
            uow.jobs.enqueue(
                type="exit_attempt", payload={"attempt_id": attempt_id},
                key=f"exit_attempt:{attempt_id}", run_at=scheduled, now=valued_at,
            )  # fmt: skip
            action = "exit_retried" if p.exit_trigger else "exit_triggered"
            uow.audit.log("paper_position", str(p.id), action, PositionStatus.OPEN.value,
                          PositionStatus.OPEN.value, {"trigger": trigger.code.value, "detail": trigger.detail}, valued_at)  # fmt: skip
    uow.positions.update(p.id, valued_at, **fields)
    return MonitorOutcome(p.id, True, trigger.code.value if trigger else None, attempt_id)


def execute_exit(
    *,
    uow_factory: UnitOfWorkFactory,
    quotes: QuoteProvider,
    fx: FxProvider | None,
    clock: Clock,
    cfg_loader: ConfigLoader,
    attempt_id: int,
) -> str:
    """Intenta vender con una cotización nueva. Si falla, la posición sigue abierta y el monitoreo la reintenta.

    Los intentos del mismo token, la misma cantidad y la misma tolerancia programados para el mismo instante se
    resuelven juntos, con una sola cotización y en una sola transacción. Cada intento guarda en su detalle con quién
    compartió la cotización o, si cotizó solo, el motivo (``separate_quote_reason``).
    """
    now = clock.now()
    with uow_factory() as uow:
        att = uow.attempts.get(attempt_id)
        if att is None or att.status != AttemptStatus.PENDING.value or att.kind != AttemptKind.EXIT.value:
            return "noop"
        if att.scheduled_at > now:
            raise TransientSourceError("el intento de salida todavía no está vencido")
        pos = uow.positions.get(att.position_id)
        if pos is None or pos.status != PositionStatus.OPEN.value:
            return "noop"
        pool = uow.chain.get_pool(pos.pool)
        assert pool is not None
        versions: dict[int, int] = {}
        for session_id, _ in uow.attempts.exit_batch(pool.address, att.scheduled_at):
            session = uow.sessions.get(session_id)
            assert session is not None
            versions[session_id] = session.strategy_version_id
    configs = {session_id: cfg_loader(version) for session_id, version in versions.items()}
    tolerance = exit_slippage_bps(configs[pos.session_id], att.number)

    quote = _safe_sell(quotes, pool, pos.tokens_expected, tolerance, now)  # red
    fx_rate = _safe_fx(fx, now)  # red
    decided_at = clock.now()

    with uow_factory() as uow:
        batch = [row for row in uow.attempts.exit_batch(pool.address, att.scheduled_at) if row[0] in configs]
        for session_id, _ in batch:  # vienen ordenadas por sesión: dos workers bloquean en el mismo orden
            uow.sessions.lock(session_id)
        ready: list[tuple[AttemptInfo, PositionInfo, StrategyConfig]] = []
        for session_id, batch_id in batch:
            item = uow.attempts.get(batch_id, lock=True)
            if item is None or item.status != AttemptStatus.PENDING.value:
                continue
            position = uow.positions.get(item.position_id, lock=True)
            if position is None or position.status != PositionStatus.OPEN.value:
                continue
            cfg = configs[session_id]
            if position.tokens_expected != pos.tokens_expected or exit_slippage_bps(cfg, item.number) != tolerance:
                continue  # otra cantidad u otra tolerancia: su propio trabajo pide su cotización
            ready.append((item, position, cfg))
        if all(item.id != attempt_id for item, _, _ in ready):
            return "noop"

        quote_id = uow.quotes.save(quote, pool=pool.address, purpose="exit")
        if fx_rate:
            uow.quotes.save_fx(fx_rate)

        traces: dict[int, dict] = {}
        for item, position, _ in ready:  # los motivos se leen antes de cambiar el estado de ninguna posición
            shared = [other.id for other, _, _ in ready if other.id != item.id]
            reason = None if shared else _separate_quote_reason(uow, item, position)
            traces[item.id] = {"shared_quote_with": shared, "separate_quote_reason": reason}
        results: dict[int, str] = {}
        for item, position, cfg in ready:
            results[item.id] = _apply_exit(
                uow, attempt=item, position=position, cfg=cfg, quote=quote, quote_id=quote_id, fx_rate=fx_rate,
                decided_at=decided_at, trace=traces[item.id],
            )  # fmt: skip
        return results[attempt_id]


def _separate_quote_reason(uow: UnitOfWork, attempt: AttemptInfo, position: PositionInfo) -> str:
    """Por qué un intento de salida no compartió cotización con la posición de otra sesión sobre la misma señal."""
    others = [
        p
        for p in uow.positions.list_for_signal(position.signal_id)
        if p.id != position.id and p.status in (PositionStatus.OPEN.value, PositionStatus.CLOSED.value)
    ]
    if not others:
        return "sin_otra_posicion"
    other = others[0]
    if other.tokens_expected != position.tokens_expected:
        return "cantidad_distinta"
    if other.status == PositionStatus.CLOSED.value:
        return "la_otra_ya_vendio"
    sibling = uow.attempts.pending(other.id, AttemptKind.EXIT.value)
    if sibling is None:
        return "la_otra_no_vende_en_este_ciclo"
    if sibling.scheduled_at != attempt.scheduled_at:
        return "turno_distinto"
    return "tolerancia_distinta"


def _apply_exit(
    uow: UnitOfWork,
    *,
    attempt: AttemptInfo,
    position: PositionInfo,
    cfg: StrategyConfig,
    quote: Quote,
    quote_id: int,
    fx_rate: FxRate | None,
    decided_at: datetime,
    trace: dict,
) -> str:
    """Decide y aplica un intento de salida con la cotización dada."""
    decision = decide_exit_fill(
        quote=quote, reference_out_lamports=attempt.reference_out_lamports, attempt_number=attempt.number,
        now=decided_at, cfg=cfg,
    )  # fmt: skip
    detail = {"slippage_bps": decision.slippage_bps, "message": decision.message, **trace}

    if decision.filled:
        proceeds = decision.proceeds_lamports + position.immobilized_lamports
        adverse = decision.adverse_proceeds_lamports + position.immobilized_lamports
        uow.ledger.add(
            session_id=position.session_id, position_id=position.id, attempt_id=attempt.id,
            kind=LedgerKind.CREDIT, amount=proceeds, now=decided_at,
        )  # fmt: skip
        uow.positions.update(
            position.id,
            decided_at,
            status=PositionStatus.CLOSED.value,
            closed_at=decided_at,
            proceeds_lamports=proceeds,
            adverse_proceeds_lamports=adverse,
            pnl_lamports=proceeds - position.debited_lamports,
            last_recoverable_lamports=0,
            last_recoverable_adverse_lamports=0,
            illiquid=False,
            next_exit_at=None,
            sol_usd_close=fx_rate.rate if fx_rate else None,
        )
        uow.attempts.finish(
            attempt.id,
            status=AttemptStatus.APPLIED,
            executed_at=decided_at,
            quote_id=quote_id,
            requested_amount=position.tokens_expected,
            result_amount=proceeds,
            detail=detail,
        )
        uow.audit.log("paper_position", str(position.id), "closed", PositionStatus.OPEN.value,
                      PositionStatus.CLOSED.value,
                      {"trigger": attempt.trigger, "pnl_lamports": str(proceeds - position.debited_lamports)}, decided_at)  # fmt: skip
        return "closed"

    # Venta fallida: la posición sigue abierta, se registra el fallo y se revalora con la cotización recién vista.
    failures = position.exit_failures + 1
    retry_at = decided_at + timedelta(seconds=exit_retry_delay_s(cfg, failures))
    recoverable = adverse = 0
    if quote.ok:
        # el escenario adverso usa la tolerancia base, como en el monitoreo, no la ampliada del reintento
        floor = quote.out_amount * (10_000 - cfg.exit_slippage_bps) // 10_000
        recoverable = net_exit_proceeds(quote) + position.immobilized_lamports
        adverse = adverse_exit_proceeds(replace(quote, min_out_amount=floor)) + position.immobilized_lamports
    uow.attempts.finish(
        attempt.id,
        status=AttemptStatus.FAILED,
        executed_at=decided_at,
        quote_id=quote_id,
        requested_amount=position.tokens_expected,
        failure_cause=decision.cause.value if decision.cause else None,
        detail=detail,
    )
    uow.positions.update(
        position.id,
        decided_at,
        exit_failures=failures,
        illiquid=failures >= cfg.exit_retry_max or not quote.ok,
        next_exit_at=retry_at,
        last_recoverable_lamports=recoverable,
        last_recoverable_adverse_lamports=adverse,
        last_valued_at=decided_at,
        valuation_status="ok" if quote.ok else quote.status.value,
    )
    uow.audit.log("paper_position", str(position.id), "exit_failed", PositionStatus.OPEN.value,
                  PositionStatus.OPEN.value,
                  {"cause": decision.cause.value if decision.cause else None, "failures": failures}, decided_at)  # fmt: skip
    return f"failed:{decision.cause.value if decision.cause else 'unknown'}"
