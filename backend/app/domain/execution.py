"""Reglas puras de ejecución: estados de intención e intento, límites y validación de órdenes.

Regla de seguridad central: una firma guardada solo deja de bloquear su intención cuando la cadena la confirma,
la marca fallida o ya no puede incluirse (venció). Hasta entonces no se prepara otra transacción, así que nunca hay
dos transacciones distintas vivas para la misma compra o venta.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from enum import StrEnum

from .models import WSOL_MINT


class Side(StrEnum):
    BUY = "buy"
    SELL = "sell"


class IntentStatus(StrEnum):
    ACTIVE = "active"  # reservada, en curso o esperando un reintento
    DONE = "done"  # movimiento confirmado y contabilizado una vez
    CANCELLED = "cancelled"  # terminó sin movimiento
    NEEDS_ATTENTION = "needs_attention"  # venta que agotó su presupuesto: no se reintenta sola


class AttemptState(StrEnum):
    PREPARING = "preparing"  # lugar reservado antes de llamar al proveedor
    PREPARED = "prepared"  # orden recibida y validada, sin firma
    SIGNED = "signed"  # firma guardada; puede haberse enviado o no
    SUBMITTED = "submitted"  # el proveedor aceptó el envío
    UNKNOWN = "unknown"  # no se sabe si se envió
    CONFIRMED = "confirmed"
    REJECTED = "rejected"  # descartada antes de firmar
    FAILED = "failed"  # incluida en la cadena con error
    EXPIRED = "expired"  # ya no puede incluirse


A = AttemptState
#: estados en los que el intento todavía ocupa su intención (a lo sumo uno por intención)
OPEN_STATES = (A.PREPARING, A.PREPARED, A.SIGNED, A.SUBMITTED, A.UNKNOWN)
_SIGNED_EXITS = {A.CONFIRMED, A.FAILED, A.EXPIRED}  # una vez firmada, solo la cadena la cierra
TRANSITIONS: dict[AttemptState, set[AttemptState]] = {
    A.PREPARING: {A.PREPARED, A.REJECTED, A.EXPIRED},
    A.PREPARED: {A.SIGNED, A.REJECTED, A.EXPIRED},
    A.SIGNED: {A.SUBMITTED, A.UNKNOWN, *_SIGNED_EXITS},
    A.SUBMITTED: {A.UNKNOWN, *_SIGNED_EXITS},
    A.UNKNOWN: {A.SUBMITTED, A.UNKNOWN, *_SIGNED_EXITS},  # un reenvío puede volver a quedar sin respuesta
}


def check_transition(current: AttemptState, new: AttemptState) -> None:
    if new not in TRANSITIONS.get(current, set()):
        raise ValueError(f"transición de intento no permitida: {current.value} → {new.value}")


class ExecutionCode(StrEnum):
    WALLET_PAUSED = "WALLET_PAUSED"
    ORDER_TOO_LARGE = "ORDER_TOO_LARGE"
    POSITION_LIMIT = "POSITION_LIMIT"
    SESSION_LIMIT = "SESSION_LIMIT"
    INSUFFICIENT_FUNDS = "INSUFFICIENT_FUNDS"
    NO_ROUTE = "NO_ROUTE"
    PROVIDER_ERROR = "PROVIDER_ERROR"
    ORDER_MISMATCH = "ORDER_MISMATCH"
    BELOW_AUTHORIZED = "BELOW_AUTHORIZED"
    SLIPPAGE_LIMIT = "SLIPPAGE_LIMIT"
    PRICE_IMPACT_LIMIT = "PRICE_IMPACT_LIMIT"
    FEE_LIMIT = "FEE_LIMIT"
    ORDER_EXPIRED = "ORDER_EXPIRED"
    ABANDONED = "ABANDONED"  # preparación interrumpida antes de guardar una firma
    EXECUTION_FAILED = "EXECUTION_FAILED"
    DISCREPANCY = "DISCREPANCY"


@dataclass(frozen=True)
class ExecutionLimits:
    max_order_lamports: int
    max_open_positions: int
    max_session_loss_lamports: int
    fee_reserve_lamports: int  # saldo que se aparta además del monto, para comisiones y depósito de cuenta
    max_fee_lamports: int
    max_slippage_bps: int
    max_exit_slippage_bps: int
    max_price_impact_bps: Decimal
    max_sell_attempts: int = 8
    max_failed_sell_fees_lamports: int = 5_000_000


@dataclass(frozen=True)
class WalletState:
    balance_lamports: int  # saldo observado en la cadena
    reserved_lamports: int  # apartado por compras sin resolver
    open_positions: int
    active_buys: int
    loss_lamports: int  # pérdida realizada, comisiones fallidas y déficit conservador de lo abierto
    buys_paused: bool


def check_buy(limits: ExecutionLimits, state: WalletState, amount_lamports: int) -> ExecutionCode | None:
    """¿Cabe una compra nueva? Orden: pausa → tamaño → posiciones → pérdida → saldo."""
    if state.buys_paused:
        return ExecutionCode.WALLET_PAUSED
    if amount_lamports > limits.max_order_lamports:
        return ExecutionCode.ORDER_TOO_LARGE
    if state.open_positions + state.active_buys >= limits.max_open_positions:
        return ExecutionCode.POSITION_LIMIT
    if state.loss_lamports >= limits.max_session_loss_lamports:
        return ExecutionCode.SESSION_LIMIT
    if state.balance_lamports - state.reserved_lamports < amount_lamports + limits.fee_reserve_lamports:
        return ExecutionCode.INSUFFICIENT_FUNDS
    return None


@dataclass(frozen=True)
class OrderRequest:
    side: Side
    wallet: str
    input_mint: str
    output_mint: str
    amount: int  # lamports en una compra, unidades base del token en una venta
    slippage_bps: int


def order_request(side: Side, wallet: str, mint: str, amount: int, slippage_bps: int) -> OrderRequest:
    source, target = (WSOL_MINT, mint) if side is Side.BUY else (mint, WSOL_MINT)
    return OrderRequest(side, wallet, source, target, amount, slippage_bps)


@dataclass(frozen=True)
class PreparedOrder:
    request_id: str
    taker: str
    input_mint: str
    output_mint: str
    in_amount: int
    out_amount: int
    min_out_amount: int
    price_impact_bps: Decimal
    fee_lamports: int
    valid_until: datetime


def check_order(
    limits: ExecutionLimits, request: OrderRequest, order: PreparedOrder, *, min_out_authorized: int, now: datetime
) -> ExecutionCode | None:
    """Valida la orden firmable contra lo autorizado. Cualquier diferencia la descarta antes de firmar."""
    if (order.taker, order.input_mint, order.output_mint, order.in_amount) != (
        request.wallet, request.input_mint, request.output_mint, request.amount,
    ):  # fmt: skip
        return ExecutionCode.ORDER_MISMATCH
    if order.valid_until <= now:
        return ExecutionCode.ORDER_EXPIRED
    if not 0 < order.min_out_amount <= order.out_amount or order.min_out_amount < min_out_authorized:
        return ExecutionCode.BELOW_AUTHORIZED
    cap = limits.max_slippage_bps if request.side is Side.BUY else limits.max_exit_slippage_bps
    if order.min_out_amount < order.out_amount * (10_000 - cap) // 10_000:  # el proveedor redondea hacia abajo
        return ExecutionCode.SLIPPAGE_LIMIT
    if order.price_impact_bps > limits.max_price_impact_bps:
        return ExecutionCode.PRICE_IMPACT_LIMIT
    if order.fee_lamports > limits.max_fee_lamports:
        return ExecutionCode.FEE_LIMIT
    return None


@dataclass(frozen=True)
class Fill:
    """Cambios de saldo de la billetera atribuibles a una transacción confirmada."""

    sol_delta_lamports: int  # negativo si salió SOL; incluye comisiones y depósitos
    token_delta: int
    fee_lamports: int


def fill_matches(request: OrderRequest, fill: Fill, *, min_out_amount: int) -> bool:
    """¿El movimiento confirmado es compatible con la orden firmada?"""
    if request.side is Side.BUY:
        return fill.token_delta >= min_out_amount and fill.sol_delta_lamports < 0
    return fill.token_delta == -request.amount and fill.sol_delta_lamports + fill.fee_lamports >= min_out_amount
