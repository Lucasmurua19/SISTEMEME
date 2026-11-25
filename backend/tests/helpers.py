"""Constructores de datos para pruebas: series de ventanas, cotizaciones y reloj."""

from __future__ import annotations

from collections.abc import Sequence
from datetime import UTC, datetime, timedelta
from decimal import Decimal

from app.domain.models import (
    WSOL_MINT,
    MarketSnapshot,
    Quote,
    QuoteCost,
    QuoteStatus,
)

T0 = datetime(2026, 1, 1, 12, 0, 0, tzinfo=UTC)
MINT = "MintDemo1111111111111111111111111111111111"
POOL = "PoolDemo1111111111111111111111111111111111"
WINDOW = 15


def dec(x: object) -> Decimal:
    return Decimal(str(x))


def build_series(
    closes: Sequence[float | str],
    *,
    p0: float | str = 1,
    wick: str = "0.005",
    buyers: int | Sequence[int] = 8,
    sol0: int = 60_000_000_000,
    concentration: str | None = "30",
    coverage: str = "1",
    lag_s: int = 1,
    start: datetime = T0,
    first_id: int = 1,
    window: int = WINDOW,
    pool: str = POOL,
    liquidity_scale: bool = True,
) -> list[MarketSnapshot]:
    """Genera ventanas cerradas con OHLC derivado de los cierres.

    La reserva de SOL sigue ``sol0 × sqrt(precio / p0)`` (producto constante), de modo que una caída
    de precio reduce la liquidez de forma coherente.
    """
    out: list[MarketSnapshot] = []
    prev = dec(p0)
    for i, c in enumerate(closes):
        close = dec(c)
        high = max(prev, close) * (1 + dec(wick))
        low = min(prev, close) * (1 - dec(wick))
        ws = start + timedelta(seconds=i * window)
        we = ws + timedelta(seconds=window)
        scale = (close / dec(p0)).sqrt() if liquidity_scale else Decimal(1)
        sol = int(Decimal(sol0) * scale)
        nb = buyers if isinstance(buyers, int) else buyers[i]
        out.append(
            MarketSnapshot(
                pool=pool,
                window_index=i,
                window_start=ws,
                window_end=we,
                observed_at=we + timedelta(seconds=lag_s),
                open=prev,
                high=high,
                low=low,
                close=close,
                sol_reserve_lamports=sol,
                token_reserve_base=int(Decimal(sol) / close * 1_000) if close else 0,
                swap_count=20,
                buy_count=12,
                sell_count=8,
                net_buyers=nb,
                buy_volume_lamports=5_000_000_000,
                sell_volume_lamports=3_000_000_000,
                top10_concentration_pct=dec(concentration) if concentration is not None else None,
                concentration_observed_at=we if concentration is not None else None,
                coverage=dec(coverage),
                source="test",
                id=first_id + i,
            )
        )
        prev = close
    return out


# Trayectoria canónica: máximo ~1.30, mínimo ~0.80 (caída ≈39%), confirmación en la ventana 9.
VALID_CLOSES = ["1.1", "1.2", "1.3", "1.1", "0.9", "0.85", "0.8", "0.82", "0.9", "0.95"]


def make_quote(
    side: str,
    *,
    in_amount: int,
    out_amount: int,
    min_out: int | None = None,
    observed_at: datetime = T0,
    ttl_s: int = 20,
    status: QuoteStatus = QuoteStatus.OK,
    costs: tuple[QuoteCost, ...] = (),
    error: str | None = None,
) -> Quote:
    return Quote(
        side=side,
        input_mint=WSOL_MINT if side == "buy" else MINT,
        output_mint=MINT if side == "buy" else WSOL_MINT,
        in_amount=in_amount,
        out_amount=out_amount,
        min_out_amount=out_amount if min_out is None else min_out,
        observed_at=observed_at,
        expires_at=observed_at + timedelta(seconds=ttl_s),
        status=status,
        route="Pump AMM",
        costs=costs,
        error=error,
    )


def cheap_quotes(now: datetime, *, cost_pct: float = 2.0) -> tuple[Quote, Quote]:
    """Compra de 0.1 SOL y venta inmediata que devuelve ``100 - cost_pct`` por ciento."""
    tokens = 50_000_000
    buy = make_quote("buy", in_amount=100_000_000, out_amount=tokens, observed_at=now)
    sell_out = int(100_000_000 * (100 - cost_pct) / 100)
    sell = make_quote("sell", in_amount=tokens, out_amount=sell_out, observed_at=now)
    return buy, sell
