"""Patrón de primera caída con recuperación, filtros obligatorios y puntaje.

Todo es función pura: recibe snapshots fechados, una configuración versionada y el instante de
evaluación (``as_of``), y devuelve estado, filtros, métricas y motivos. Solo se usan observaciones
disponibles hasta ``as_of``, de modo que una reproducción histórica no puede ver el futuro.
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from decimal import Decimal
from enum import StrEnum
from typing import Any

from .config import StrategyConfig
from .costs import compute_round_trip
from .models import HUNDRED, LAMPORTS_PER_SOL, ZERO, MarketSnapshot, Quote, QuoteStatus, RoundTrip
from .reasons import CandidateStatus, FilterOutcome, Reason, ReasonCode

# --------------------------------------------------------------------------- patrón


class PatternState(StrEnum):
    NO_DATA = "no_data"
    TRACKING_PEAK = "tracking_peak"
    DRAWDOWN = "drawdown"
    CONFIRMED = "confirmed"
    TOO_DEEP = "too_deep"
    INVALIDATED = "invalidated"


@dataclass(frozen=True)
class Level:
    price: Decimal
    window_index: int
    at: datetime
    snapshot_id: int | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "price": str(self.price),
            "window_index": self.window_index,
            "at": self.at.isoformat(),
            "snapshot_id": self.snapshot_id,
        }


@dataclass(frozen=True)
class PatternResult:
    state: PatternState
    detail: str = ""
    peak: Level | None = None
    low: Level | None = None
    drawdown_pct: Decimal | None = None
    recovery_pct: Decimal | None = None
    net_buyers: int | None = None
    liquidity_retention_pct: Decimal | None = None
    invalidation_level: Decimal | None = None
    confirmed_window_index: int | None = None
    confirmed_at: datetime | None = None
    invalidated_window_index: int | None = None
    last_window_index: int | None = None
    confirm_snapshot_ids: tuple[int, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        def d(v: Decimal | None) -> str | None:
            return None if v is None else str(v)

        return {
            "state": self.state.value,
            "detail": self.detail,
            "peak": self.peak.to_dict() if self.peak else None,
            "low": self.low.to_dict() if self.low else None,
            "drawdown_pct": d(self.drawdown_pct),
            "recovery_pct": d(self.recovery_pct),
            "net_buyers": self.net_buyers,
            "liquidity_retention_pct": d(self.liquidity_retention_pct),
            "invalidation_level": d(self.invalidation_level),
            "confirmed_window_index": self.confirmed_window_index,
            "confirmed_at": self.confirmed_at.isoformat() if self.confirmed_at else None,
            "invalidated_window_index": self.invalidated_window_index,
            "last_window_index": self.last_window_index,
            "confirm_snapshot_ids": list(self.confirm_snapshot_ids),
        }


def detect_pattern(
    snapshots: Iterable[MarketSnapshot], migrated_at: datetime, cfg: StrategyConfig
) -> PatternResult:
    """Reproduce, ventana por ventana, los estados: máximo → caída → recuperación → confirmación.

    - El máximo inicial es el mayor ``high`` de las ventanas que empiezan dentro de ``peak_window_s``.
    - El mínimo es el menor precio posterior al máximo (el cierre de la ventana del máximo ocurre
      después del máximo; los mínimos de ventanas posteriores también).
    - La recuperación se confirma con ``confirmation_windows`` ventanas cerradas consecutivas,
      posteriores al mínimo, cuyo cierre supera ``min_recovery_pct`` sobre el mínimo.
    - Una vez confirmada, el máximo, el mínimo y el nivel de invalidación quedan congelados: ventanas
      futuras no reescriben la señal. Si un mínimo posterior toca el nivel de invalidación, vence.
    """
    snaps = sorted(snapshots, key=lambda s: s.window_index)
    if not snaps:
        return PatternResult(PatternState.NO_DATA, detail="sin snapshots observados")

    peak_deadline = migrated_at + timedelta(seconds=cfg.peak_window_s)
    peak: Level | None = None
    low: Level | None = None
    state = PatternState.TRACKING_PEAK
    streak: list[MarketSnapshot] = []
    invalidation_level: Decimal | None = None
    confirmed_snap: MarketSnapshot | None = None
    confirm_ids: tuple[int, ...] = ()
    invalidated_idx: int | None = None
    max_liquidity = 0
    buffer = cfg.invalidation_buffer_pct / HUNDRED

    for s in snaps:
        max_liquidity = max(max_liquidity, s.liquidity_lamports)
        if state in (PatternState.TOO_DEEP, PatternState.INVALIDATED):
            continue
        if state is PatternState.CONFIRMED:
            assert invalidation_level is not None
            if s.low <= invalidation_level:
                state = PatternState.INVALIDATED
                invalidated_idx = s.window_index
            continue

        if s.window_start < peak_deadline and (peak is None or s.high > peak.price):
            peak = Level(s.high, s.window_index, s.window_end, s.id)
            low = Level(s.close, s.window_index, s.window_end, s.id)
            streak = []
        elif peak is not None and low is not None and s.low < low.price:
            low = Level(s.low, s.window_index, s.window_end, s.id)
            streak = []
        if peak is None or low is None:
            continue

        drawdown = (peak.price - low.price) / peak.price * HUNDRED
        if drawdown > cfg.max_drawdown_pct:
            state = PatternState.TOO_DEEP
            continue
        if drawdown < cfg.min_drawdown_pct:
            state = PatternState.TRACKING_PEAK
            continue

        state = PatternState.DRAWDOWN
        if s.window_index <= low.window_index:
            continue
        recovery = (s.close - low.price) / low.price * HUNDRED
        if recovery >= cfg.min_recovery_pct:
            if streak and s.window_index != streak[-1].window_index + 1:
                streak = []  # hueco de datos: la confirmación exige ventanas consecutivas
            streak.append(s)
        else:
            streak = []
        if len(streak) >= cfg.confirmation_windows:
            state = PatternState.CONFIRMED
            confirmed_snap = s
            invalidation_level = low.price * (Decimal(1) - buffer)
            confirm_ids = tuple(x.id for x in streak[-cfg.confirmation_windows :] if x.id is not None)

    last = snaps[-1]
    if peak is None:
        return PatternResult(
            PatternState.NO_DATA,
            detail="la ventana del máximo inicial no fue observada",
            last_window_index=last.window_index,
        )
    assert low is not None
    drawdown_pct = (peak.price - low.price) / peak.price * HUNDRED
    recovery_pct = (last.close - low.price) / low.price * HUNDRED
    trailing = snaps[-cfg.confirmation_windows :]
    net_buyers = max(s.net_buyers for s in trailing)  # cota inferior de compradores netos distintos
    retention = Decimal(last.liquidity_lamports) * HUNDRED / Decimal(max_liquidity) if max_liquidity else ZERO

    detail = {
        PatternState.TRACKING_PEAK: f"caída {drawdown_pct:.2f}% menor al mínimo {cfg.min_drawdown_pct}%",
        PatternState.DRAWDOWN: f"caída {drawdown_pct:.2f}%, recuperación {recovery_pct:.2f}% sin confirmar",
        PatternState.CONFIRMED: f"recuperación confirmada en la ventana {confirmed_snap.window_index}"
        if confirmed_snap
        else "",
        PatternState.TOO_DEEP: f"caída {drawdown_pct:.2f}% supera el máximo {cfg.max_drawdown_pct}%",
        PatternState.INVALIDATED: f"mínimo bajo el nivel de invalidación en la ventana {invalidated_idx}",
    }.get(state, "")

    return PatternResult(
        state=state,
        detail=detail,
        peak=peak,
        low=low,
        drawdown_pct=drawdown_pct,
        recovery_pct=recovery_pct,
        net_buyers=net_buyers,
        liquidity_retention_pct=retention,
        invalidation_level=invalidation_level,
        confirmed_window_index=confirmed_snap.window_index if confirmed_snap else None,
        confirmed_at=max(s.observed_at for s in snaps if s.id in confirm_ids)
        if confirmed_snap and confirm_ids
        else (confirmed_snap.observed_at if confirmed_snap else None),
        invalidated_window_index=invalidated_idx,
        last_window_index=last.window_index,
        confirm_snapshot_ids=confirm_ids,
    )


# --------------------------------------------------------------------------- filtros y resultado


@dataclass(frozen=True)
class FilterResult:
    name: str
    code: ReasonCode
    outcome: FilterOutcome
    message: str
    value: str | None = None
    threshold: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "code": self.code.value,
            "outcome": self.outcome.value,
            "message": self.message,
            "value": self.value,
            "threshold": self.threshold,
        }


@dataclass(frozen=True)
class EvaluationInput:
    pool: str
    mint: str
    migrated_at: datetime
    migration_verified: bool
    identity_ok: bool
    snapshots: Sequence[MarketSnapshot]
    as_of: datetime
    buy_quote: Quote | None = None
    sell_quote: Quote | None = None


@dataclass(frozen=True)
class ScoreComponent:
    name: str
    value: Decimal
    score: Decimal
    weight: Decimal

    def to_dict(self) -> dict[str, str]:
        return {
            "name": self.name,
            "value": str(self.value),
            "score": str(self.score),
            "weight": str(self.weight),
            "points": str(self.score * self.weight / HUNDRED),
        }


@dataclass(frozen=True)
class EvaluationResult:
    status: CandidateStatus
    reasons: tuple[Reason, ...]
    filters: tuple[FilterResult, ...]
    pattern: PatternResult
    metrics: dict[str, str]
    score: Decimal | None
    score_components: tuple[ScoreComponent, ...]
    round_trip: RoundTrip | None
    needs_quote: bool
    window_index: int | None
    latest_snapshot: MarketSnapshot | None
    as_of: datetime
    snapshot_ids: tuple[int, ...] = field(default_factory=tuple)

    @property
    def eligible(self) -> bool:
        return self.status is CandidateStatus.ELIGIBLE


_OBSERVING = {ReasonCode.PATTERN_IN_PROGRESS, ReasonCode.RECOVERY_NOT_CONFIRMED}
_STALE_DATA = {ReasonCode.NO_MARKET_DATA, ReasonCode.STALE_MARKET_DATA, ReasonCode.LOW_DATA_COVERAGE}
_PENDING = _STALE_DATA | {
    ReasonCode.CONCENTRATION_UNKNOWN,
    ReasonCode.QUOTE_PENDING,
    ReasonCode.NO_BUY_ROUTE,
    ReasonCode.NO_SELL_ROUTE,
    ReasonCode.QUOTE_ERROR,
    ReasonCode.QUOTE_STALE,
}
_TERMINAL_REJECT = {
    ReasonCode.MIGRATION_UNVERIFIED,
    ReasonCode.IDENTITY_MISMATCH,
    ReasonCode.DRAWDOWN_TOO_DEEP,
}
_EXPIRED = {ReasonCode.PATTERN_EXPIRED, ReasonCode.PATTERN_INVALIDATED}


def resolve_status(failing: Iterable[FilterResult]) -> CandidateStatus:
    """Prioridad: rechazo terminal > vencido > datos vencidos > observando > rechazo > pendiente."""
    codes = {f.code for f in failing}
    if not codes:
        return CandidateStatus.ELIGIBLE
    if codes & _TERMINAL_REJECT:
        return CandidateStatus.REJECTED
    if codes & _EXPIRED:
        return CandidateStatus.EXPIRED
    if codes & _STALE_DATA:
        return CandidateStatus.PENDING_DATA
    if codes & _OBSERVING:
        return CandidateStatus.OBSERVING
    if codes - _PENDING:
        return CandidateStatus.REJECTED
    return CandidateStatus.PENDING_DATA


def _norm(value: Decimal, lo: Decimal, hi: Decimal) -> Decimal:
    if hi == lo:
        return Decimal(1)
    ratio = (value - lo) / (hi - lo)
    return max(ZERO, min(Decimal(1), ratio))


def compute_score(
    cfg: StrategyConfig,
    recovery_pct: Decimal,
    liquidity_retention_pct: Decimal,
    round_trip_cost_pct: Decimal,
    top10_concentration_pct: Decimal,
) -> tuple[Decimal, tuple[ScoreComponent, ...]]:
    """Puntaje 0-100 con rangos fijos por versión: no depende de otros candidatos. Sin redondear."""
    recovery = _norm(recovery_pct, cfg.min_recovery_pct, cfg.score_recovery_full_pct) * HUNDRED
    liquidity = _norm(liquidity_retention_pct, cfg.min_liquidity_retention_pct, HUNDRED) * HUNDRED
    cost = (Decimal(1) - _norm(round_trip_cost_pct, ZERO, cfg.max_round_trip_cost_pct)) * HUNDRED
    holders = (
        Decimal(1)
        - _norm(top10_concentration_pct, cfg.score_concentration_best_pct, cfg.max_top10_concentration_pct)
    ) * HUNDRED
    components = (
        ScoreComponent("recovery", recovery_pct, recovery, cfg.weight_recovery),
        ScoreComponent("liquidity", liquidity_retention_pct, liquidity, cfg.weight_liquidity),
        ScoreComponent("cost", round_trip_cost_pct, cost, cfg.weight_cost),
        ScoreComponent("holders", top10_concentration_pct, holders, cfg.weight_holders),
    )
    total = sum((c.score * c.weight / HUNDRED for c in components), ZERO)
    return total, components


def evaluate(inp: EvaluationInput, cfg: StrategyConfig) -> EvaluationResult:
    """Evalúa un pool en el instante ``inp.as_of``.

    Si todos los filtros previos a la cotización pasan y todavía no hay cotizaciones, devuelve
    ``needs_quote=True``: el caso de uso consulta al proveedor y vuelve a evaluar.
    """
    as_of = inp.as_of
    visible = sorted(
        (s for s in inp.snapshots if s.window_end <= as_of and s.observed_at <= as_of),
        key=lambda s: s.window_index,
    )
    latest = visible[-1] if visible else None
    pattern = detect_pattern(visible, inp.migrated_at, cfg)
    age_s = Decimal((as_of - inp.migrated_at).total_seconds())

    filters: list[FilterResult] = []

    def add(
        name: str,
        code: ReasonCode,
        outcome: FilterOutcome,
        message: str,
        value: object = None,
        threshold: object = None,
    ) -> None:
        filters.append(
            FilterResult(
                name,
                code,
                outcome,
                message,
                None if value is None else str(value),
                None if threshold is None else str(threshold),
            )
        )

    P, F, W, S = FilterOutcome.PASS, FilterOutcome.FAIL, FilterOutcome.PENDING, FilterOutcome.SKIPPED

    # 1-3. identidad, migración y edad
    add(
        "migration_verified",
        ReasonCode.MIGRATION_UNVERIFIED,
        P if inp.migration_verified else F,
        "migración verificada contra el programa publicado"
        if inp.migration_verified
        else "la migración no pudo verificarse",
    )
    add(
        "identity",
        ReasonCode.IDENTITY_MISMATCH,
        P if inp.identity_ok else F,
        "mint y pool coinciden con la migración observada"
        if inp.identity_ok
        else "el mint y el pool no coinciden con la migración observada",
    )
    age_ok = age_s <= Decimal(cfg.max_age_after_migration_s)
    add(
        "age",
        ReasonCode.PATTERN_EXPIRED,
        P if age_ok else F,
        "edad dentro de la ventana de estrategia" if age_ok else "se superó la edad máxima tras la migración",
        value=f"{age_s:.0f}s",
        threshold=f"{cfg.max_age_after_migration_s}s",
    )

    # 4. datos frescos y completos
    if latest is None:
        add("market_data", ReasonCode.NO_MARKET_DATA, W, "todavía no hay ventanas cerradas observadas")
        data_age = None
    else:
        data_age = Decimal((as_of - latest.window_end).total_seconds())
        if data_age > Decimal(cfg.data_max_age_s):
            add(
                "market_data",
                ReasonCode.STALE_MARKET_DATA,
                W,
                "la última ventana cerrada está vencida",
                value=f"{data_age:.0f}s",
                threshold=f"{cfg.data_max_age_s}s",
            )
        elif latest.coverage < cfg.min_coverage:
            add(
                "market_data",
                ReasonCode.LOW_DATA_COVERAGE,
                W,
                "la cobertura de swaps de la ventana es insuficiente",
                value=latest.coverage,
                threshold=cfg.min_coverage,
            )
        else:
            add(
                "market_data",
                ReasonCode.STALE_MARKET_DATA,
                P,
                "datos frescos y con cobertura suficiente",
                value=f"{data_age:.0f}s",
                threshold=f"{cfg.data_max_age_s}s",
            )

    # 5. concentración
    conc: Decimal | None = None
    if latest is None:
        add("concentration", ReasonCode.CONCENTRATION_UNKNOWN, W, "sin datos de concentración")
    else:
        conc = latest.top10_concentration_pct
        conc_age = (
            (as_of - latest.concentration_observed_at).total_seconds()
            if latest.concentration_observed_at
            else None
        )
        if conc is None or (conc_age is not None and conc_age > cfg.concentration_max_age_s):
            add(
                "concentration",
                ReasonCode.CONCENTRATION_UNKNOWN,
                W,
                "concentración de los 10 mayores no evaluable (dato crítico incompleto)",
                value=conc,
            )
            conc = None
        elif conc > cfg.max_top10_concentration_pct:
            add(
                "concentration",
                ReasonCode.CONCENTRATION_TOO_HIGH,
                F,
                "los 10 mayores propietarios concentran demasiado suministro",
                value=f"{conc}%",
                threshold=f"{cfg.max_top10_concentration_pct}%",
            )
        else:
            add(
                "concentration",
                ReasonCode.CONCENTRATION_TOO_HIGH,
                P,
                "concentración aceptable",
                value=f"{conc}%",
                threshold=f"{cfg.max_top10_concentration_pct}%",
            )

    # 6. patrón
    confirmed = pattern.state is PatternState.CONFIRMED
    if pattern.state is PatternState.NO_DATA:
        add("pattern", ReasonCode.NO_MARKET_DATA, W, pattern.detail or "sin datos para el patrón")
    elif pattern.state is PatternState.TRACKING_PEAK:
        add("pattern", ReasonCode.PATTERN_IN_PROGRESS, W, pattern.detail)
    elif pattern.state is PatternState.DRAWDOWN:
        add("pattern", ReasonCode.RECOVERY_NOT_CONFIRMED, W, pattern.detail)
    elif pattern.state is PatternState.TOO_DEEP:
        add("pattern", ReasonCode.DRAWDOWN_TOO_DEEP, F, pattern.detail)
    elif pattern.state is PatternState.INVALIDATED:
        add("pattern", ReasonCode.PATTERN_INVALIDATED, F, pattern.detail)
    else:
        add("pattern", ReasonCode.RECOVERY_NOT_CONFIRMED, P, pattern.detail)

    # 7-8. recuperación vigente y compradores netos (solo con patrón confirmado)
    if confirmed and pattern.recovery_pct is not None:
        holding = pattern.recovery_pct >= cfg.min_recovery_pct
        add(
            "recovery_holding",
            ReasonCode.RECOVERY_NOT_CONFIRMED,
            P if holding else F,
            "la recuperación se mantiene sobre el mínimo"
            if holding
            else "la recuperación se perdió tras la confirmación",
            value=f"{pattern.recovery_pct:.2f}%",
            threshold=f"{cfg.min_recovery_pct}%",
        )
        buyers = pattern.net_buyers or 0
        add(
            "net_buyers",
            ReasonCode.INSUFFICIENT_NET_BUYERS,
            P if buyers >= cfg.min_net_buyers else F,
            "compradores netos distintos suficientes"
            if buyers >= cfg.min_net_buyers
            else "el rebote no tiene suficientes compradores netos distintos",
            value=buyers,
            threshold=cfg.min_net_buyers,
        )
    else:
        add("recovery_holding", ReasonCode.RECOVERY_NOT_CONFIRMED, S, "requiere patrón confirmado")
        add("net_buyers", ReasonCode.INSUFFICIENT_NET_BUYERS, S, "requiere patrón confirmado")

    # 9-10. profundidad y estabilidad de liquidez
    if latest is not None:
        need = cfg.min_depth_required_lamports
        depth_ok = latest.liquidity_lamports >= need
        add(
            "depth",
            ReasonCode.INSUFFICIENT_DEPTH,
            P if depth_ok else F,
            "profundidad suficiente para el tamaño evaluado"
            if depth_ok
            else "el pool no tiene profundidad suficiente para el tamaño evaluado",
            value=f"{Decimal(latest.liquidity_lamports) / LAMPORTS_PER_SOL:.2f} SOL",
            threshold=f"{Decimal(need) / LAMPORTS_PER_SOL:.2f} SOL",
        )
        retention = pattern.liquidity_retention_pct
        if retention is not None:
            stable = retention >= cfg.min_liquidity_retention_pct
            add(
                "liquidity_retention",
                ReasonCode.LIQUIDITY_DETERIORATING,
                P if stable else F,
                "la liquidez se mantiene"
                if stable
                else "la liquidez se deterioró respecto de su máximo observado",
                value=f"{retention:.2f}%",
                threshold=f"{cfg.min_liquidity_retention_pct}%",
            )
        else:
            add("liquidity_retention", ReasonCode.LIQUIDITY_DETERIORATING, S, "sin referencia de liquidez")
    else:
        add("depth", ReasonCode.INSUFFICIENT_DEPTH, S, "sin datos de mercado")
        add("liquidity_retention", ReasonCode.LIQUIDITY_DETERIORATING, S, "sin datos de mercado")

    # 11. cotizaciones (solo si todo lo anterior pasó)
    pre_ok = all(f.outcome is P for f in filters)  # un filtro omitido (SKIPPED) tampoco habilita cotizar
    round_trip: RoundTrip | None = None
    needs_quote = False
    buy, sell = inp.buy_quote, inp.sell_quote

    if not pre_ok:
        for name, code in (
            ("buy_route", ReasonCode.NO_BUY_ROUTE),
            ("sell_route", ReasonCode.NO_SELL_ROUTE),
            ("quote_fresh", ReasonCode.QUOTE_STALE),
            ("round_trip_cost", ReasonCode.ROUND_TRIP_COST_EXCEEDED),
        ):
            add(name, code, S, "requiere que los filtros previos pasen")
    elif buy is None:
        needs_quote = True
        add("buy_route", ReasonCode.QUOTE_PENDING, W, "cotización de compra pendiente")
        add("sell_route", ReasonCode.QUOTE_PENDING, W, "cotización de venta pendiente")
        add("quote_fresh", ReasonCode.QUOTE_PENDING, S, "sin cotización")
        add("round_trip_cost", ReasonCode.QUOTE_PENDING, S, "sin cotización")
    else:
        # ``sell`` puede faltar solo cuando la compra no tiene ruta: no se cotiza una venta sin compra
        for name, code_route, q in (
            ("buy_route", ReasonCode.NO_BUY_ROUTE, buy),
            ("sell_route", ReasonCode.NO_SELL_ROUTE, sell),
        ):
            if q is None:
                add(name, code_route, S, "no se cotiza la venta sin una compra viable")
            elif q.status is QuoteStatus.OK and q.out_amount > 0:
                add(name, code_route, P, f"ruta disponible ({q.route or 'agregador'})")
            elif q.status is QuoteStatus.NO_ROUTE:
                add(name, code_route, W, "no existe ruta cotizable")
            else:
                add(name, ReasonCode.QUOTE_ERROR, W, f"error del proveedor: {q.error or 'desconocido'}")
        if sell is not None and buy.ok and sell.ok:
            fresh = all(
                q.is_fresh(as_of) and (as_of - q.observed_at).total_seconds() <= cfg.quote_max_age_s
                for q in (buy, sell)
            )
            add(
                "quote_fresh",
                ReasonCode.QUOTE_STALE,
                P if fresh else W,
                "cotizaciones vigentes" if fresh else "cotización vencida",
                threshold=f"{cfg.quote_max_age_s}s",
            )
            round_trip = compute_round_trip(buy, sell)
            cost_ok = round_trip.cost_pct <= cfg.max_round_trip_cost_pct
            add(
                "round_trip_cost",
                ReasonCode.ROUND_TRIP_COST_EXCEEDED,
                P if cost_ok else F,
                "costo de ida y vuelta dentro del límite"
                if cost_ok
                else "el costo de ida y vuelta supera el límite",
                value=f"{round_trip.cost_pct:.4f}%",
                threshold=f"{cfg.max_round_trip_cost_pct}%",
            )
        else:
            add("quote_fresh", ReasonCode.QUOTE_STALE, S, "sin ambas cotizaciones")
            add("round_trip_cost", ReasonCode.ROUND_TRIP_COST_EXCEEDED, S, "sin ambas cotizaciones")

    failing = [f for f in filters if f.outcome in (F, W)]
    status = resolve_status(failing)

    # puntaje (se calcula siempre que existan los cuatro insumos, aunque el estado no sea elegible)
    score: Decimal | None = None
    components: tuple[ScoreComponent, ...] = ()
    if confirmed and round_trip is not None and conc is not None and pattern.recovery_pct is not None:
        score, components = compute_score(
            cfg,
            pattern.recovery_pct,
            pattern.liquidity_retention_pct or ZERO,
            round_trip.cost_pct,
            conc,
        )

    reasons = tuple(Reason(f.code, f.message, {"value": f.value, "threshold": f.threshold}) for f in failing)

    metrics: dict[str, str] = {"age_s": str(age_s)}
    if data_age is not None:
        metrics["data_age_s"] = str(data_age)
    if latest is not None:
        metrics["liquidity_lamports"] = str(latest.liquidity_lamports)
        metrics["last_close"] = str(latest.close)
    if conc is not None:
        metrics["top10_concentration_pct"] = str(conc)
    for key, val in (
        ("drawdown_pct", pattern.drawdown_pct),
        ("recovery_pct", pattern.recovery_pct),
        ("liquidity_retention_pct", pattern.liquidity_retention_pct),
    ):
        if val is not None:
            metrics[key] = str(val)
    if pattern.net_buyers is not None:
        metrics["net_buyers"] = str(pattern.net_buyers)
    if round_trip is not None:
        metrics["round_trip_cost_pct"] = str(round_trip.cost_pct)

    return EvaluationResult(
        status=status,
        reasons=reasons,
        filters=tuple(filters),
        pattern=pattern,
        metrics=metrics,
        score=score,
        score_components=components,
        round_trip=round_trip,
        needs_quote=needs_quote,
        window_index=latest.window_index if latest else None,
        latest_snapshot=latest,
        as_of=as_of,
        snapshot_ids=tuple(s.id for s in visible if s.id is not None),
    )


# --------------------------------------------------------------------------- ordenamiento


@dataclass(frozen=True)
class RankItem:
    key: int
    score: Decimal
    cost_pct: Decimal
    quote_observed_at: datetime
    liquidity_lamports: int


def ranking_order(items: Iterable[RankItem]) -> list[RankItem]:
    """Puntaje descendente; desempate por menor costo de salida y luego por cotización más fresca."""
    return sorted(items, key=lambda i: (-i.score, i.cost_pct, -i.quote_observed_at.timestamp(), i.key))


def liquidity_order(items: Iterable[RankItem]) -> list[RankItem]:
    """Referencia simple: mayor liquidez primero; desempate por menor costo y luego por clave."""
    return sorted(items, key=lambda i: (-i.liquidity_lamports, i.cost_pct, i.key))
