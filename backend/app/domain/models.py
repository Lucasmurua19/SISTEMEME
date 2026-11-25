"""Tipos de dominio compartidos.

Convenciones:
- Lamports y unidades mínimas de token son ``int`` (nunca float).
- Precios y porcentajes son ``Decimal``. El precio se expresa en SOL por token (unidad entera del token).
- Todos los instantes son ``datetime`` con zona UTC.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal
from enum import StrEnum
from typing import Any

LAMPORTS_PER_SOL = 1_000_000_000
WSOL_MINT = "So11111111111111111111111111111111111111112"
USDC_MINT = "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v"
PUMP_PROGRAM_ID = "6EF8rrecthR5Dkzon8Nwu78hRvfCKubJ14M5uBEwF6P"
PUMPSWAP_PROGRAM_ID = "pAMMBay6oceH9fJKBRHGP5D4bD4sWpmSwMn52FMfXEA"

ZERO = Decimal(0)
HUNDRED = Decimal(100)


# --------------------------------------------------------------------------- mercado
@dataclass(frozen=True)
class MarketSnapshot:
    """Resumen de una ventana cerrada de swaps de un pool."""

    pool: str
    window_index: int
    window_start: datetime
    window_end: datetime
    observed_at: datetime
    open: Decimal
    high: Decimal
    low: Decimal
    close: Decimal
    sol_reserve_lamports: int
    token_reserve_base: int
    swap_count: int
    buy_count: int
    sell_count: int
    net_buyers: int
    buy_volume_lamports: int
    sell_volume_lamports: int
    top10_concentration_pct: Decimal | None = None
    concentration_observed_at: datetime | None = None
    coverage: Decimal = Decimal(1)
    source: str = "unknown"
    id: int | None = None

    @property
    def liquidity_lamports(self) -> int:
        """Valor de ambos lados del pool expresado en lamports (2 × reserva de SOL)."""
        return 2 * self.sol_reserve_lamports


# --------------------------------------------------------------------------- cotizaciones
class QuoteStatus(StrEnum):
    OK = "ok"
    NO_ROUTE = "no_route"
    ERROR = "error"


@dataclass(frozen=True)
class QuoteCost:
    """Un cargo asociado a la cotización.

    ``included_in_amount`` indica que el monto de salida ya lo descuenta (no se suma de nuevo).
    ``refundable`` marca fondos inmovilizados que se recuperan al cerrar (depósito de cuenta de token).
    """

    kind: str  # protocol | interface | priority | tip | ata_deposit
    lamports: int
    included_in_amount: bool = False
    refundable: bool = False

    def to_dict(self) -> dict[str, Any]:
        return {
            "kind": self.kind,
            "lamports": str(self.lamports),
            "included_in_amount": self.included_in_amount,
            "refundable": self.refundable,
        }


@dataclass(frozen=True)
class Quote:
    side: str  # "buy" (SOL → token) | "sell" (token → SOL)
    input_mint: str
    output_mint: str
    in_amount: int
    out_amount: int
    min_out_amount: int
    observed_at: datetime
    expires_at: datetime
    status: QuoteStatus = QuoteStatus.OK
    route: str = ""
    price_impact_bps: Decimal = ZERO
    costs: tuple[QuoteCost, ...] = ()
    error: str | None = None
    request_id: str | None = None
    id: int | None = None

    @property
    def ok(self) -> bool:
        return self.status is QuoteStatus.OK and self.out_amount > 0

    def additional_cost_lamports(self) -> int:
        """Cargos que la cotización todavía no incorporó en sus montos."""
        return sum(c.lamports for c in self.costs if not c.included_in_amount)

    def is_fresh(self, now: datetime) -> bool:
        return self.expires_at > now


@dataclass(frozen=True)
class RoundTrip:
    """Costo de ida y vuelta: ``(SOL total debitado - SOL neto recuperable) / SOL total debitado``."""

    total_debited_lamports: int
    net_recoverable_lamports: int
    cost_lamports: int
    cost_pct: Decimal
    buy_in_lamports: int
    buy_extra_costs_lamports: int
    sell_out_lamports: int
    sell_extra_costs_lamports: int
    refunds_lamports: int
    immobilized_lamports: int
    tokens_expected: int
    tokens_min: int

    def to_dict(self) -> dict[str, Any]:
        return {
            "total_debited_lamports": str(self.total_debited_lamports),
            "net_recoverable_lamports": str(self.net_recoverable_lamports),
            "cost_lamports": str(self.cost_lamports),
            "cost_pct": str(self.cost_pct),
            "buy_in_lamports": str(self.buy_in_lamports),
            "buy_extra_costs_lamports": str(self.buy_extra_costs_lamports),
            "sell_out_lamports": str(self.sell_out_lamports),
            "sell_extra_costs_lamports": str(self.sell_extra_costs_lamports),
            "refunds_lamports": str(self.refunds_lamports),
            "immobilized_lamports": str(self.immobilized_lamports),
            "tokens_expected": str(self.tokens_expected),
            "tokens_min": str(self.tokens_min),
        }


# --------------------------------------------------------------------------- cadena
@dataclass(frozen=True)
class MigrationEvent:
    signature: str
    instruction_index: int
    event_index: int
    slot: int
    mint: str
    pool: str
    program: str
    event_at: datetime
    observed_at: datetime
    confirmation: str = "confirmed"
    symbol: str | None = None
    name: str | None = None
    decimals: int = 6
    evidence: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class PoolVerification:
    verified: bool
    identity_ok: bool
    canonical: bool | None
    reason: str | None = None
    evidence: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class MigrationBatch:
    events: tuple[MigrationEvent, ...]
    next_cursor: str | None
    head_slot: int | None = None


# --------------------------------------------------------------------------- registros de aplicación
@dataclass(frozen=True)
class PoolInfo:
    address: str
    mint: str
    symbol: str | None
    name: str | None
    decimals: int
    program: str
    quote_mint: str
    migrated_at: datetime
    migration_signature: str
    migration_instruction_index: int
    migration_slot: int
    verified: bool
    identity_ok: bool
    canonical: bool | None


@dataclass(frozen=True)
class SignalInfo:
    id: int
    evaluation_id: int
    pool: str
    mint: str
    strategy_version_id: int
    status: str
    issued_at: datetime
    expires_at: datetime
    amount_lamports: int
    invalidation_level: Decimal
    entry_ref_price: Decimal
    score: Decimal
    liquidity_lamports: int
    round_trip_cost_pct: Decimal
    buy_quote_id: int | None
    sell_quote_id: int | None
    buy_price_lamports_per_unit: Decimal | None = None


@dataclass(frozen=True)
class SessionInfo:
    id: int
    name: str
    policy: str  # "ranking" | "liquidity"
    strategy_version_id: int
    capital_lamports: int
    trade_size_lamports: int
    max_exposure_lamports: int
    max_positions: int
    max_session_loss_lamports: int
    paused: bool
    pause_reason: str | None
    started_at: datetime


@dataclass(frozen=True)
class PositionInfo:
    id: int
    session_id: int
    signal_id: int
    pool: str
    mint: str
    status: str
    tokens_expected: int
    tokens_min: int
    debited_lamports: int
    reserved_lamports: int
    entry_price: Decimal | None
    entry_liquidity_lamports: int | None
    invalidation_level: Decimal | None
    opened_at: datetime | None
    exit_trigger: str | None
    exit_failures: int
    illiquid: bool
    next_exit_at: datetime | None
    last_recoverable_lamports: int | None
    last_valued_at: datetime | None
    immobilized_lamports: int = 0  # depósito reembolsable de la cuenta de token, vuelve al cerrar
    last_recoverable_adverse_lamports: int | None = None  # valoración conservadora (mínimo garantizado)


@dataclass(frozen=True)
class AttemptInfo:
    id: int
    position_id: int
    kind: str
    number: int
    status: str
    scheduled_at: datetime
    trigger: str | None
    reference_out_lamports: int | None
    detail: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class SessionState:
    """Estado de capital de una sesión, reconstruido desde el libro de asientos y las posiciones."""

    capital_lamports: int
    cash_lamports: int
    reserved_lamports: int
    active_positions: int  # pendientes de entrada + abiertas
    open_cost_basis_lamports: int
    realized_pnl_lamports: int
    open_deficit_lamports: int
    paused: bool

    @property
    def available_lamports(self) -> int:
        return self.cash_lamports - self.reserved_lamports

    @property
    def net_session_pnl_lamports(self) -> int:
        return self.realized_pnl_lamports - self.open_deficit_lamports
