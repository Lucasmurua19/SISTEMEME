"""Estados y códigos de motivo estables.

Los códigos son parte del contrato público (API y panel): no se renombran, solo se agregan.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any


class CandidateStatus(StrEnum):
    OBSERVING = "observing"
    PENDING_DATA = "pending_data"
    ELIGIBLE = "eligible"
    REJECTED = "rejected"
    EXPIRED = "expired"


class ReasonCode(StrEnum):
    # Identidad y migración
    MIGRATION_UNVERIFIED = "MIGRATION_UNVERIFIED"
    IDENTITY_MISMATCH = "IDENTITY_MISMATCH"
    # Datos
    NO_MARKET_DATA = "NO_MARKET_DATA"
    STALE_MARKET_DATA = "STALE_MARKET_DATA"
    LOW_DATA_COVERAGE = "LOW_DATA_COVERAGE"
    CONCENTRATION_UNKNOWN = "CONCENTRATION_UNKNOWN"
    CONCENTRATION_TOO_HIGH = "CONCENTRATION_TOO_HIGH"
    # Patrón
    PATTERN_IN_PROGRESS = "PATTERN_IN_PROGRESS"
    RECOVERY_NOT_CONFIRMED = "RECOVERY_NOT_CONFIRMED"
    DRAWDOWN_TOO_DEEP = "DRAWDOWN_TOO_DEEP"
    PATTERN_INVALIDATED = "PATTERN_INVALIDATED"
    PATTERN_EXPIRED = "PATTERN_EXPIRED"
    INSUFFICIENT_NET_BUYERS = "INSUFFICIENT_NET_BUYERS"
    INSUFFICIENT_DEPTH = "INSUFFICIENT_DEPTH"
    LIQUIDITY_DETERIORATING = "LIQUIDITY_DETERIORATING"
    # Cotización
    QUOTE_PENDING = "QUOTE_PENDING"
    NO_BUY_ROUTE = "NO_BUY_ROUTE"
    NO_SELL_ROUTE = "NO_SELL_ROUTE"
    QUOTE_ERROR = "QUOTE_ERROR"
    QUOTE_STALE = "QUOTE_STALE"
    ROUND_TRIP_COST_EXCEEDED = "ROUND_TRIP_COST_EXCEEDED"
    # Señal y riesgo
    SIGNAL_EXPIRED = "SIGNAL_EXPIRED"
    SESSION_PAUSED = "SESSION_PAUSED"
    SESSION_LIMIT = "SESSION_LIMIT"
    POSITION_LIMIT = "POSITION_LIMIT"
    EXPOSURE_LIMIT = "EXPOSURE_LIMIT"
    INSUFFICIENT_CAPITAL = "INSUFFICIENT_CAPITAL"
    SOURCE_DEGRADED = "SOURCE_DEGRADED"
    # Ejecución simulada
    ENTRY_QUOTE_UNAVAILABLE = "ENTRY_QUOTE_UNAVAILABLE"
    SLIPPAGE_EXCEEDED = "SLIPPAGE_EXCEEDED"
    # Salidas
    EXIT_INVALIDATION = "EXIT_INVALIDATION"
    EXIT_LIQUIDITY = "EXIT_LIQUIDITY"
    EXIT_TAKE_PROFIT = "EXIT_TAKE_PROFIT"
    EXIT_MAX_HOLD = "EXIT_MAX_HOLD"
    EXIT_FAILED = "EXIT_FAILED"


class FilterOutcome(StrEnum):
    PASS = "pass"
    FAIL = "fail"
    PENDING = "pending"  # dato crítico incompleto o aún no disponible
    SKIPPED = "skipped"  # no evaluado porque un filtro previo lo hace innecesario


class PositionStatus(StrEnum):
    PENDING_ENTRY = "pending_entry"
    OPEN = "open"
    CLOSED = "closed"
    REJECTED = "rejected"


class AttemptKind(StrEnum):
    ENTRY = "entry"
    EXIT = "exit"


class AttemptStatus(StrEnum):
    PENDING = "pending"
    APPLIED = "applied"
    FAILED = "failed"
    REJECTED = "rejected"


class LedgerKind(StrEnum):
    RESERVE = "reserve"
    RELEASE = "release"
    DEBIT = "debit"
    CREDIT = "credit"


class SignalStatus(StrEnum):
    ACTIVE = "active"
    EXPIRED = "expired"


@dataclass(frozen=True)
class Reason:
    code: ReasonCode
    message: str
    data: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {"code": self.code.value, "message": self.message, "data": self.data}
