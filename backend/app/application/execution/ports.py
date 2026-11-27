"""Contratos del ejecutor: el proveedor de órdenes y la persistencia de intenciones."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from typing import Any, Protocol

from app.domain.execution import AttemptState, Fill, OrderRequest, PreparedOrder, Side, WalletState


class NoRouteError(Exception):
    """El proveedor no tiene ruta para la orden."""


class GatewayError(Exception):
    """El proveedor no pudo preparar la orden."""


@dataclass(frozen=True)
class SignedOrder:
    signature: str  # identificador on-chain: se conoce antes de enviar
    payload: str  # transacción firmada; acceso restringido, nunca se expone ni se registra en logs
    valid_until: datetime


class SubmitOutcome(StrEnum):
    SENT = "sent"
    UNKNOWN = "unknown"  # ante cualquier duda: tiempo agotado, error del proveedor o rechazo sin garantía


class ChainStatus(StrEnum):
    CONFIRMED = "confirmed"
    FAILED = "failed"  # incluida con error
    PENDING = "pending"  # vista por la red, sin resolver
    NOT_FOUND = "not_found"  # no se la ve, pero todavía puede incluirse
    EXPIRED = "expired"  # no se la ve y ya no puede incluirse: es definitivo


@dataclass(frozen=True)
class ChainResult:
    status: ChainStatus
    fill: Fill | None = None  # en ``confirmed``; en ``failed``, la comisión cobrada


class OrderGateway(Protocol):
    """Proveedor de órdenes de una billetera. ``sign`` no envía; ``submit`` debe ser seguro de repetir."""

    name: str
    wallet: str

    def balance_lamports(self) -> int: ...
    def prepare(self, request: OrderRequest) -> PreparedOrder: ...
    def sign(self, order: PreparedOrder) -> SignedOrder: ...
    def submit(self, signed: SignedOrder) -> SubmitOutcome: ...
    def lookup(self, signature: str) -> ChainResult: ...


# ------------------------------------------------------------------ registros persistidos
@dataclass(frozen=True)
class IntentInfo:
    id: int
    wallet: str
    side: Side
    signal_id: int
    position_id: int | None
    pool: str
    mint: str
    amount: int
    strategy_version_id: int
    policy: str
    trigger: str | None
    status: str
    reason: str | None
    failures: int
    expires_at: datetime | None
    next_attempt_at: datetime | None
    claim_token: int


@dataclass(frozen=True)
class ExecutionAttemptInfo:
    id: int
    intent_id: int
    number: int
    state: AttemptState
    signature: str | None
    signed_payload: str | None
    valid_until: datetime | None
    min_out_amount: int | None


@dataclass(frozen=True)
class LivePositionInfo:
    id: int
    wallet: str
    buy_intent_id: int
    signal_id: int
    pool: str
    mint: str
    strategy_version_id: int
    status: str
    tokens_acquired: int
    tokens_open: int
    sol_debited_lamports: int
    sol_received_lamports: int
    pnl_lamports: int | None


class ExecutionRepository(Protocol):
    def lock_wallet(self, wallet: str, now: datetime) -> bool: ...  # serializa; devuelve si las compras están en pausa
    def pause_buys(self, wallet: str, reason: str, now: datetime) -> None: ...
    def wallet_state(self, wallet: str, balance_lamports: int) -> WalletState: ...

    def create_intent(self, **fields: Any) -> int | None: ...
    def get_intent(self, intent_id: int, *, lock: bool = False) -> IntentInfo | None: ...
    def intent_by_key(self, key: str) -> IntentInfo | None: ...
    def blocking_sell(self, position_id: int) -> IntentInfo | None: ...
    def sell_count(self, position_id: int) -> int: ...
    def due_intents(self, wallet: str, now: datetime, limit: int) -> list[int]: ...
    def claim(self, intent_id: int, worker_id: str, now: datetime, ttl_s: int) -> IntentInfo | None: ...
    def release_claim(self, intent_id: int, token: int, now: datetime) -> None: ...
    def update_intent(self, intent_id: int, now: datetime, **fields: Any) -> None: ...

    def open_attempt(self, intent_id: int) -> ExecutionAttemptInfo | None: ...
    def create_attempt(self, intent_id: int, gateway: str, now: datetime) -> ExecutionAttemptInfo: ...
    def get_attempt(self, attempt_id: int, *, lock: bool = False) -> ExecutionAttemptInfo | None: ...
    def move_attempt(self, attempt_id: int, state: AttemptState, now: datetime, **fields: Any) -> None: ...
    def failed_fees(self, position_id: int) -> int: ...

    def create_position(self, **fields: Any) -> int: ...
    def get_position(self, position_id: int, *, lock: bool = False) -> LivePositionInfo | None: ...
    def update_position(self, position_id: int, **fields: Any) -> None: ...
    def add_ledger(self, **fields: Any) -> bool: ...
