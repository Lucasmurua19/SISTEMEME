"""Métricas de resultados de la simulación (PnL neto, expectativa, caída máxima, etc.)."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from typing import Any

from .models import LAMPORTS_PER_SOL

ZERO = Decimal(0)


@dataclass(frozen=True)
class ClosedTrade:
    position_id: int
    closed_at: datetime
    debited_lamports: int
    proceeds_lamports: int
    adverse_proceeds_lamports: int
    sol_usd_open: Decimal | None = None
    sol_usd_close: Decimal | None = None

    @property
    def pnl_lamports(self) -> int:
        return self.proceeds_lamports - self.debited_lamports

    @property
    def adverse_pnl_lamports(self) -> int:
        return self.adverse_proceeds_lamports - self.debited_lamports

    @property
    def pnl_usd(self) -> Decimal | None:
        if self.sol_usd_open is None or self.sol_usd_close is None:
            return None
        return (
            Decimal(self.proceeds_lamports) / LAMPORTS_PER_SOL * self.sol_usd_close
            - Decimal(self.debited_lamports) / LAMPORTS_PER_SOL * self.sol_usd_open
        )


@dataclass(frozen=True)
class OpenTrade:
    """Posición abierta con su última valoración.

    ``conservative_recoverable_lamports`` es lo recuperable vendiendo al mínimo garantizado, después de costos
    (la valoración que se muestra como cifra principal). ``quoted_recoverable_lamports`` es lo recuperable al precio
    cotizado, después de costos: la referencia optimista que acota el rango.
    """

    position_id: int
    debited_lamports: int
    conservative_recoverable_lamports: int | None
    illiquid: bool
    quoted_recoverable_lamports: int | None = None
    valued_at: datetime | None = None


def max_drawdown(pnls_in_order: list[int]) -> int:
    """Mayor caída pico-valle de la curva de PnL realizado acumulado (en lamports, ≥ 0)."""
    peak = 0
    equity = 0
    worst = 0
    for pnl in pnls_in_order:
        equity += pnl
        peak = max(peak, equity)
        worst = max(worst, peak - equity)
    return worst


def compute_metrics(
    closed: list[ClosedTrade],
    open_: list[OpenTrade],
    *,
    capital_lamports: int,
    exit_attempts_total: int,
    exit_attempts_failed: int,
    rejected_entries: int = 0,
) -> dict[str, Any]:
    """Todas las cifras en lamports se serializan como cadenas; los cocientes, como decimales exactos."""
    ordered = sorted(closed, key=lambda t: (t.closed_at, t.position_id))
    pnls = [t.pnl_lamports for t in ordered]
    n = len(pnls)
    wins = [p for p in pnls if p > 0]
    losses = [p for p in pnls if p < 0]
    realized = sum(pnls)
    usd_values = [t.pnl_usd for t in ordered]
    realized_usd = sum((v for v in usd_values if v is not None), ZERO) if any(v is not None for v in usd_values) else None

    illiquid = [o for o in open_ if o.illiquid]
    liquid_open = [o for o in open_ if not o.illiquid]
    open_valuation = sum((o.conservative_recoverable_lamports or 0) - o.debited_lamports for o in liquid_open)
    zero_recovery_loss = sum(o.debited_lamports for o in illiquid)
    # Posiciones abiertas valoradas: las que tienen ruta de salida, al mínimo garantizado; las ilíquidas, a recuperación
    # cero. Es la cifra que se lee junto al PnL realizado, porque acá el riesgo de lo abierto puede darlo vuelta.
    open_pnl_valued = open_valuation - zero_recovery_loss
    open_pnl_quoted = (
        sum(
            (o.quoted_recoverable_lamports if o.quoted_recoverable_lamports is not None else (o.conservative_recoverable_lamports or 0))
            - o.debited_lamports
            for o in liquid_open
        )
        - zero_recovery_loss
    )
    valued_times = [o.valued_at for o in open_ if o.valued_at is not None]
    dd = max_drawdown(pnls)
    best = max(pnls) if pnls else 0

    gross_win = sum(wins)
    gross_loss = -sum(losses)

    def ratio(a: int, b: int) -> str | None:
        return None if b == 0 else str(Decimal(a) / Decimal(b))

    return {
        "trades_closed": n,
        "trades_open": len(open_),
        "trades_illiquid": len(illiquid),
        "realized_pnl_lamports": str(realized),
        "realized_pnl_sol": str(Decimal(realized) / LAMPORTS_PER_SOL),
        "realized_pnl_usd": None if realized_usd is None else str(realized_usd),
        "open_valuation_lamports": str(open_valuation),
        "illiquid_cost_basis_lamports": str(zero_recovery_loss),
        "open_pnl_valued_lamports": str(open_pnl_valued),
        "open_pnl_quoted_lamports": str(open_pnl_quoted),
        "total_pnl_valued_lamports": str(realized + open_pnl_valued),
        "open_oldest_valuation_at": None if not valued_times else min(valued_times).isoformat(),
        "zero_recovery_scenario_pnl_lamports": str(realized - zero_recovery_loss),
        "expectancy_lamports": None if n == 0 else str(Decimal(realized) / n),
        "hit_rate": None if n == 0 else str(Decimal(len(wins)) / n),
        "avg_win_lamports": None if not wins else str(Decimal(gross_win) / len(wins)),
        "avg_loss_lamports": None if not losses else str(Decimal(sum(losses)) / len(losses)),
        "profit_factor": ratio(gross_win, gross_loss),
        # Solo curva de operaciones cerradas: no incluye la pérdida latente de las abiertas.
        "max_realized_drawdown_lamports": str(dd),
        "max_realized_drawdown_pct_of_capital": None if capital_lamports <= 0 else str(Decimal(dd) * 100 / capital_lamports),
        "pnl_without_best_lamports": None if n == 0 else str(realized - best),
        "best_trade_lamports": None if n == 0 else str(best),
        "adverse_pnl_lamports": str(sum(t.adverse_pnl_lamports for t in ordered)),
        "failed_exit_attempts": exit_attempts_failed,
        "exit_attempts_total": exit_attempts_total,
        "failed_exit_rate": None if exit_attempts_total == 0 else str(Decimal(exit_attempts_failed) / exit_attempts_total),
        "entries_rejected": rejected_entries,
    }
