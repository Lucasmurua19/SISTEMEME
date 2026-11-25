"""Riesgo de la sesión: exposición, límites y capital disponible.

Al superar un límite se bloquean nuevas posiciones simuladas; el seguimiento de las abiertas y el
registro de señales continúan.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from .models import SessionState
from .reasons import ReasonCode


@dataclass(frozen=True)
class RiskLimits:
    trade_size_lamports: int
    max_exposure_lamports: int
    max_positions: int
    max_session_loss_lamports: int


@dataclass(frozen=True)
class RiskDecision:
    allowed: bool
    code: ReasonCode | None = None
    message: str = ""
    numbers: dict[str, str] = field(default_factory=dict)


def position_deficit(cost_basis_lamports: int, conservative_recoverable_lamports: int | None) -> int:
    """Déficit de una posición abierta frente a una salida conservadora (0 si no hay ruta)."""
    recoverable = conservative_recoverable_lamports or 0
    return max(0, cost_basis_lamports - recoverable)


def check_entry(limits: RiskLimits, state: SessionState, size_lamports: int | None = None) -> RiskDecision:
    """Decide si una nueva posición cabe en el presupuesto de la sesión.

    Orden de verificación: pausa → pérdida de sesión → cantidad de posiciones → exposición → capital.
    """
    size = size_lamports if size_lamports is not None else limits.trade_size_lamports
    numbers = {
        "size_lamports": str(size),
        "available_lamports": str(state.available_lamports),
        "reserved_lamports": str(state.reserved_lamports),
        "open_cost_basis_lamports": str(state.open_cost_basis_lamports),
        "active_positions": str(state.active_positions),
        "net_session_pnl_lamports": str(state.net_session_pnl_lamports),
    }
    if state.paused:
        return RiskDecision(False, ReasonCode.SESSION_PAUSED, "la sesión está en pausa", numbers)
    if -state.net_session_pnl_lamports >= limits.max_session_loss_lamports:
        return RiskDecision(
            False,
            ReasonCode.SESSION_LIMIT,
            "se alcanzó la pérdida máxima de la sesión (incluye el déficit de las posiciones abiertas)",
            numbers,
        )
    if state.active_positions >= limits.max_positions:
        return RiskDecision(False, ReasonCode.POSITION_LIMIT, "máximo de posiciones simultáneas", numbers)
    exposure_after = state.open_cost_basis_lamports + state.reserved_lamports + size
    if exposure_after > limits.max_exposure_lamports:
        return RiskDecision(
            False,
            ReasonCode.EXPOSURE_LIMIT,
            "la exposición total superaría el máximo",
            {**numbers, "exposure_after_lamports": str(exposure_after)},
        )
    if state.available_lamports < size:
        return RiskDecision(False, ReasonCode.INSUFFICIENT_CAPITAL, "capital simulado insuficiente", numbers)
    return RiskDecision(True, None, "dentro del presupuesto de riesgo", numbers)
