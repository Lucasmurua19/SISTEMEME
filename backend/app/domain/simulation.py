"""Reglas de ejecución estimada y de salida.

Una cotización es una estimación en un instante. Para simular una ejecución se consulta una cotización
nueva después del retraso configurado y se decide con ella. Un stop es un disparador de intento de
salida: el precio conseguido depende de la ruta y de la liquidez disponibles.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal
from typing import Any

from .config import StrategyConfig
from .costs import adverse_exit_proceeds, compute_round_trip, net_exit_proceeds
from .models import MarketSnapshot, PositionInfo, Quote, QuoteStatus, RoundTrip, SignalInfo
from .reasons import ReasonCode

BPS = Decimal(10_000)
MAX_EXIT_RETRY_INTERVAL_S = 120  # no es parte de la configuración versionada para no alterar la huella de las versiones


# --------------------------------------------------------------------------- entrada
@dataclass(frozen=True)
class EntryDecision:
    accepted: bool
    code: ReasonCode | None
    message: str
    round_trip: RoundTrip | None = None
    detail: dict[str, Any] = field(default_factory=dict)


def decide_entry(
    *,
    signal: SignalInfo,
    buy: Quote | None,
    sell: Quote | None,
    latest: MarketSnapshot | None,
    now: datetime,
    cfg: StrategyConfig,
) -> EntryDecision:
    """Revalida la señal con las cotizaciones posteriores al retraso de entrada."""
    if now > signal.expires_at:
        return EntryDecision(False, ReasonCode.SIGNAL_EXPIRED, "la señal venció antes de la entrada simulada")
    if latest is not None and latest.low <= signal.invalidation_level:
        return EntryDecision(
            False,
            ReasonCode.PATTERN_INVALIDATED,
            "el precio tocó el nivel de invalidación antes de la entrada",
            detail={"low": str(latest.low), "invalidation_level": str(signal.invalidation_level)},
        )
    if buy is None or not buy.ok:
        status = buy.status.value if buy else "sin_cotizacion"
        return EntryDecision(
            False,
            ReasonCode.ENTRY_QUOTE_UNAVAILABLE,
            f"cotización de compra no disponible ({status})",
            detail={"error": buy.error if buy else None},
        )
    if not buy.is_fresh(now):
        return EntryDecision(False, ReasonCode.QUOTE_STALE, "la cotización de compra venció")

    new_price = Decimal(buy.in_amount) / Decimal(buy.out_amount)
    ref = signal.buy_price_lamports_per_unit
    if ref is not None and ref > 0:
        limit = ref * (Decimal(1) + Decimal(cfg.slippage_bps) / BPS)
        if new_price > limit:
            return EntryDecision(
                False,
                ReasonCode.SLIPPAGE_EXCEEDED,
                "el precio de compra cambió más que la tolerancia desde la señal",
                detail={
                    "reference_price": str(ref),
                    "new_price": str(new_price),
                    "limit_price": str(limit),
                    "slippage_bps": cfg.slippage_bps,
                },
            )
    if sell is None or not sell.ok:
        return EntryDecision(
            False,
            ReasonCode.NO_SELL_ROUTE,
            "sin ruta de venta al momento de la entrada",
            detail={"error": sell.error if sell else None},
        )
    round_trip = compute_round_trip(buy, sell)
    if round_trip.cost_pct > cfg.max_round_trip_cost_pct:
        return EntryDecision(
            False,
            ReasonCode.ROUND_TRIP_COST_EXCEEDED,
            "el costo de ida y vuelta superó el límite al momento de la entrada",
            round_trip,
            {"cost_pct": str(round_trip.cost_pct), "max_pct": str(cfg.max_round_trip_cost_pct)},
        )
    return EntryDecision(True, None, "entrada simulada aceptada", round_trip, {"new_price": str(new_price)})


# --------------------------------------------------------------------------- salida
@dataclass(frozen=True)
class ExitSignal:
    code: ReasonCode
    detail: str


def check_exit_trigger(
    *,
    position: PositionInfo,
    latest: MarketSnapshot | None,
    recoverable_lamports: int | None,
    now: datetime,
    cfg: StrategyConfig,
) -> ExitSignal | None:
    """Disparadores fijados antes de evaluar resultados. Prioridad: riesgo primero."""
    opened = position.opened_at
    if latest is not None and opened is not None and latest.window_end > opened:
        if position.invalidation_level is not None and latest.low <= position.invalidation_level:
            return ExitSignal(
                ReasonCode.EXIT_INVALIDATION,
                f"mínimo {latest.low} en o bajo el nivel de invalidación {position.invalidation_level}",
            )
        if position.entry_liquidity_lamports:
            ratio = Decimal(latest.liquidity_lamports) * 100 / Decimal(position.entry_liquidity_lamports)
            if ratio <= cfg.liquidity_exit_retention_pct:
                return ExitSignal(
                    ReasonCode.EXIT_LIQUIDITY,
                    f"liquidez al {ratio:.1f}% de la del ingreso (límite {cfg.liquidity_exit_retention_pct}%)",
                )
    if (
        cfg.take_profit_pct is not None
        and recoverable_lamports is not None
        and position.debited_lamports > 0
    ):
        target = Decimal(position.debited_lamports) * (Decimal(1) + cfg.take_profit_pct / 100)
        if Decimal(recoverable_lamports) >= target:
            return ExitSignal(
                ReasonCode.EXIT_TAKE_PROFIT,
                f"recuperable {recoverable_lamports} ≥ objetivo {target:.0f} lamports",
            )
    if opened is not None and (now - opened).total_seconds() >= cfg.max_hold_s:
        return ExitSignal(ReasonCode.EXIT_MAX_HOLD, f"tiempo máximo en posición ({cfg.max_hold_s}s)")
    return None


@dataclass(frozen=True)
class ExitDecision:
    filled: bool
    cause: ReasonCode | None
    message: str
    proceeds_lamports: int = 0
    adverse_proceeds_lamports: int = 0
    slippage_bps: int = 0


def exit_slippage_bps(cfg: StrategyConfig, attempt_number: int) -> int:
    return min(cfg.max_exit_slippage_bps, cfg.exit_slippage_bps + cfg.exit_slippage_step_bps * max(0, attempt_number - 1))


def exit_retry_delay_s(cfg: StrategyConfig, failures: int) -> int:
    """Espera antes del próximo intento: arranca en ``exit_retry_interval_s`` y se duplica en cada fallo, con tope."""
    cap = max(cfg.exit_retry_interval_s, MAX_EXIT_RETRY_INTERVAL_S)
    return min(cap, cfg.exit_retry_interval_s * 2 ** min(max(0, failures - 1), 16))


def decide_exit_fill(
    *,
    quote: Quote | None,
    reference_out_lamports: int | None,
    attempt_number: int,
    now: datetime,
    cfg: StrategyConfig,
) -> ExitDecision:
    """Decide si un intento de salida se ejecuta con la cotización posterior al retraso.

    ``reference_out_lamports`` es la cotización tomada al programar **este** intento: la tolerancia protege el tramo
    entre programar y ejecutar, no la distancia al precio que disparó la salida.

    Una venta fallida deja la posición abierta: el llamador registra el fallo y reintenta según la
    política (la tolerancia de slippage crece en cada reintento hasta un tope).
    """
    tolerance = exit_slippage_bps(cfg, attempt_number)
    if quote is None or quote.status is QuoteStatus.NO_ROUTE:
        return ExitDecision(False, ReasonCode.NO_SELL_ROUTE, "no existe ruta de venta", slippage_bps=tolerance)
    if quote.status is QuoteStatus.ERROR or not quote.ok:
        return ExitDecision(
            False, ReasonCode.QUOTE_ERROR, f"error del proveedor: {quote.error}", slippage_bps=tolerance
        )
    if not quote.is_fresh(now):
        return ExitDecision(False, ReasonCode.QUOTE_STALE, "la cotización de venta venció", slippage_bps=tolerance)
    if reference_out_lamports:
        floor = Decimal(reference_out_lamports) * (Decimal(1) - Decimal(tolerance) / BPS)
        if Decimal(quote.out_amount) < floor:
            return ExitDecision(
                False,
                ReasonCode.SLIPPAGE_EXCEEDED,
                "la salida cayó más que la tolerancia respecto de la cotización con que se programó el intento",
                slippage_bps=tolerance,
            )
    return ExitDecision(
        True,
        None,
        "salida simulada ejecutada",
        proceeds_lamports=net_exit_proceeds(quote),
        adverse_proceeds_lamports=adverse_exit_proceeds(quote),
        slippage_bps=tolerance,
    )
