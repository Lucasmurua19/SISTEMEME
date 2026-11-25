"""Apertura, valoración y cierre simulado de posiciones.

Invariantes:
- Reservar capital y crear la posición ocurre en una sola transacción con la fila de la sesión
  bloqueada: dos workers no pueden abrir dos veces la misma señal ni exceder el presupuesto.
- Cada aplicación de un intento escribe asientos con clave única ``(intento, tipo)``: reaplicar un
  intento no duplica capital.
- Las llamadas de red (cotizaciones, FX) se hacen fuera de las transacciones y la vigencia se
  revalida al escribir.
- Una venta fallida deja la posición abierta y programa un reintento según la política.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timedelta
from decimal import Decimal

from app.application.evaluate_pool import unavailable_quote
from app.application.ports import (
    Clock,
    FxProvider,
    FxRate,
    QuoteProvider,
    TransientSourceError,
    UnitOfWorkFactory,
)
from app.domain.config import StrategyConfig
from app.domain.costs import adverse_exit_proceeds, net_exit_proceeds
from app.domain.models import LAMPORTS_PER_SOL, PoolInfo, Quote
from app.domain.reasons import (
    AttemptKind,
    AttemptStatus,
    LedgerKind,
    PositionStatus,
    ReasonCode,
)
from app.domain.risk import RiskLimits, check_entry
from app.domain.simulation import ExitSignal, check_exit_trigger, decide_entry, decide_exit_fill
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
) -> list[AdmissionOutcome]:
    """Procesa las señales vigentes que la sesión todavía no vio.

    El orden depende de la política: ``ranking`` (puntaje) o ``liquidity`` (referencia simple). Las
    dos sesiones ven el mismo universo elegible en el mismo instante, con idénticos límites.
    """
    now = clock.now()
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

        for item in ordered:
            sig = by_id[item.key]
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
                              {"signal_id": sig.id, "session_id": session_id}, now)  # fmt: skip
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
                              {"code": code.value if code else None, "signal_id": sig.id}, now)  # fmt: skip
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
    """Ejecuta el intento de entrada tras el retraso simulado, con cotizaciones nuevas."""
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

    buy = _safe_buy(quotes, pool, sig.amount_lamports, cfg.slippage_bps, now)  # red
    sell = _safe_sell(quotes, pool, buy.min_out_amount, cfg.slippage_bps, now) if buy.ok else None  # red
    fx_rate = _safe_fx(fx, now)  # red
    decided_at = clock.now()
    decision = decide_entry(signal=sig, buy=buy, sell=sell, latest=latest, now=decided_at, cfg=cfg)

    with uow_factory() as uow:
        uow.sessions.lock(pos.session_id)
        att = uow.attempts.get(attempt_id, lock=True)
        position = uow.positions.get(pos.id, lock=True)
        if att is None or att.status != AttemptStatus.PENDING.value or position is None:
            return "noop"
        if position.status != PositionStatus.PENDING_ENTRY.value:
            return "noop"
        session = uow.sessions.get(position.session_id)
        assert session is not None

        buy_id = uow.quotes.save(buy, pool=pool.address, purpose="entry")
        sell_id = uow.quotes.save(sell, pool=pool.address, purpose="entry") if sell else None
        if fx_rate:
            uow.quotes.save_fx(fx_rate)

        accepted = decision.accepted
        code = decision.code
        message = decision.message
        detail = dict(decision.detail)
        reserved = position.reserved_lamports

        # La reserva se libera siempre: o se convierte en débito real, o se devuelve.
        uow.ledger.add(
            session_id=session.id, position_id=position.id, attempt_id=att.id,
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
                session_id=session.id, position_id=position.id, attempt_id=att.id,
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
                att.id,
                status=AttemptStatus.APPLIED,
                executed_at=decided_at,
                quote_id=buy_id,
                requested_amount=buy.in_amount,
                result_amount=rt.tokens_expected,
                detail={"sell_quote_id": sell_id, "round_trip": rt.to_dict(), "message": message},
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
            att.id,
            status=AttemptStatus.REJECTED,
            executed_at=decided_at,
            quote_id=buy_id,
            requested_amount=buy.in_amount or sig.amount_lamports,
            failure_cause=code.value if code else None,
            detail={"sell_quote_id": sell_id, "message": message, **detail},
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
    """Valora cada posición abierta con una cotización de salida y dispara intentos de salida.

    Se ejecuta aunque la sesión esté pausada o haya llegado a sus límites: el seguimiento no se corta.
    """
    now = clock.now()
    with uow_factory() as uow:
        opened = uow.positions.list([PositionStatus.OPEN])
    outcomes: list[MonitorOutcome] = []

    for pos in opened:
        with uow_factory() as uow:
            if uow.attempts.pending(pos.id, AttemptKind.EXIT.value):
                continue  # ya hay una salida en curso
            session = uow.sessions.get(pos.session_id)
            pool = uow.chain.get_pool(pos.pool)
            assert session is not None and pool is not None
            snaps = uow.snapshots.list_for_pool(pool.address, now)
            latest = snaps[-1] if snaps else None
        cfg = cfg_loader(session.strategy_version_id)

        sell = _safe_sell(quotes, pool, pos.tokens_expected, cfg.exit_slippage_bps, now)  # red
        valued_at = clock.now()
        if sell.ok:
            recoverable = net_exit_proceeds(sell) + pos.immobilized_lamports
            adverse = adverse_exit_proceeds(sell) + pos.immobilized_lamports
            valuation_status = "ok"
        else:
            recoverable = adverse = 0  # escenario conservador: recuperación cero
            valuation_status = sell.status.value

        trigger: ExitSignal | None = check_exit_trigger(
            position=pos, latest=latest, recoverable_lamports=recoverable if sell.ok else None, now=valued_at, cfg=cfg
        )

        with uow_factory() as uow:
            p = uow.positions.get(pos.id, lock=True)
            if p is None or p.status != PositionStatus.OPEN.value or uow.attempts.pending(p.id, AttemptKind.EXIT.value):
                continue
            if trigger is None and p.exit_trigger:  # disparador ya fijado y sin intento pendiente: se retoma
                trigger = ExitSignal(ReasonCode(p.exit_trigger), "disparador previo sin intento pendiente")
            if not sell.ok:
                uow.quotes.save(sell, pool=pool.address, purpose="valuation")  # evidencia de fallas de cotización

            fields: dict = {
                "last_recoverable_lamports": recoverable,
                "last_recoverable_adverse_lamports": adverse,
                "last_valued_at": valued_at,
                "valuation_status": valuation_status,
                "illiquid": (not sell.ok) or p.exit_failures >= cfg.exit_retry_max,
            }
            attempt_id = None
            if trigger is not None:
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
                    uow.audit.log("paper_position", str(p.id), "exit_triggered", PositionStatus.OPEN.value,
                                  PositionStatus.OPEN.value, {"trigger": trigger.code.value, "detail": trigger.detail}, valued_at)  # fmt: skip
            uow.positions.update(p.id, valued_at, **fields)
        outcomes.append(
            MonitorOutcome(pos.id, True, trigger.code.value if trigger else None, attempt_id)
        )
    return outcomes


def execute_exit(
    *,
    uow_factory: UnitOfWorkFactory,
    quotes: QuoteProvider,
    fx: FxProvider | None,
    clock: Clock,
    cfg_loader: ConfigLoader,
    attempt_id: int,
) -> str:
    """Intenta vender con una cotización nueva. Si falla, la posición sigue abierta y se reintenta."""
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
        session = uow.sessions.get(pos.session_id)
        pool = uow.chain.get_pool(pos.pool)
        assert session is not None and pool is not None
    cfg = cfg_loader(session.strategy_version_id)

    from app.domain.simulation import exit_slippage_bps

    quote = _safe_sell(quotes, pool, pos.tokens_expected, exit_slippage_bps(cfg, att.number), now)  # red
    fx_rate = _safe_fx(fx, now)  # red
    decided_at = clock.now()
    decision = decide_exit_fill(
        quote=quote, reference_out_lamports=att.reference_out_lamports, attempt_number=att.number, now=decided_at, cfg=cfg
    )

    with uow_factory() as uow:
        uow.sessions.lock(pos.session_id)
        att = uow.attempts.get(attempt_id, lock=True)
        position = uow.positions.get(pos.id, lock=True)
        if att is None or att.status != AttemptStatus.PENDING.value or position is None:
            return "noop"
        if position.status != PositionStatus.OPEN.value:
            return "noop"
        quote_id = uow.quotes.save(quote, pool=pool.address, purpose="exit")
        if fx_rate:
            uow.quotes.save_fx(fx_rate)

        if decision.filled:
            proceeds = decision.proceeds_lamports + position.immobilized_lamports
            adverse = decision.adverse_proceeds_lamports + position.immobilized_lamports
            uow.ledger.add(
                session_id=session.id, position_id=position.id, attempt_id=att.id,
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
                att.id,
                status=AttemptStatus.APPLIED,
                executed_at=decided_at,
                quote_id=quote_id,
                requested_amount=position.tokens_expected,
                result_amount=proceeds,
                detail={"slippage_bps": decision.slippage_bps, "message": decision.message},
            )
            uow.audit.log("paper_position", str(position.id), "closed", PositionStatus.OPEN.value,
                          PositionStatus.CLOSED.value,
                          {"trigger": att.trigger, "pnl_lamports": str(proceeds - position.debited_lamports)}, decided_at)  # fmt: skip
            return "closed"

        # Venta fallida: la posición sigue abierta, se registra el fallo y se programa el reintento.
        failures = position.exit_failures + 1
        retry_at = decided_at + timedelta(seconds=cfg.exit_retry_interval_s)
        uow.attempts.finish(
            att.id,
            status=AttemptStatus.FAILED,
            executed_at=decided_at,
            quote_id=quote_id,
            requested_amount=position.tokens_expected,
            failure_cause=decision.cause.value if decision.cause else None,
            detail={"slippage_bps": decision.slippage_bps, "message": decision.message},
        )
        next_id = uow.attempts.create(
            position_id=position.id,
            kind=AttemptKind.EXIT.value,
            number=att.number + 1,
            scheduled_at=retry_at,
            trigger=att.trigger,
            reference_out_lamports=att.reference_out_lamports,
            detail={"retry_of": att.id},
        )
        if next_id is not None:
            uow.jobs.enqueue(
                type="exit_attempt", payload={"attempt_id": next_id},
                key=f"exit_attempt:{next_id}", run_at=retry_at, now=decided_at,
            )  # fmt: skip
        uow.positions.update(
            position.id,
            decided_at,
            exit_failures=failures,
            illiquid=failures >= cfg.exit_retry_max or not quote.ok,
            next_exit_at=retry_at,
            **({"last_recoverable_lamports": 0, "last_recoverable_adverse_lamports": 0} if not quote.ok else {}),
        )
        uow.audit.log("paper_position", str(position.id), "exit_failed", PositionStatus.OPEN.value,
                      PositionStatus.OPEN.value,
                      {"cause": decision.cause.value if decision.cause else None, "failures": failures}, decided_at)  # fmt: skip
        return f"failed:{decision.cause.value if decision.cause else 'unknown'}"
