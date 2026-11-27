"""Reserva de intenciones: el único punto donde nace una compra o una venta.

Todo ocurre en una transacción con la billetera bloqueada. Hay una sola intención de compra por billetera y señal, y
una sola venta sin resolver por posición; la base lo garantiza con índices únicos.
"""

from __future__ import annotations

from dataclasses import dataclass

from app.application.execution.ports import OrderGateway
from app.application.ports import Clock, UnitOfWorkFactory
from app.domain.execution import ExecutionLimits, IntentStatus, Side, check_buy
from app.domain.reasons import ReasonCode


@dataclass(frozen=True)
class Reservation:
    intent_id: int | None
    status: str | None
    code: str | None = None
    created: bool = False


def reserve_buy(
    *,
    uow_factory: UnitOfWorkFactory,
    gateway: OrderGateway,
    clock: Clock,
    limits: ExecutionLimits,
    signal_id: int,
    policy: str,
) -> Reservation:
    """Aparta el saldo y crea la intención de compra de una señal. Repetirla devuelve la que ya existe.

    Un rechazo por límites también queda guardado: la señal no se vuelve a intentar con esa billetera.
    """
    balance = gateway.balance_lamports()  # red, antes de la transacción
    now = clock.now()
    key = f"buy:{gateway.wallet}:{signal_id}"
    with uow_factory() as uow:
        uow.execution.lock_wallet(gateway.wallet, now)
        existing = uow.execution.intent_by_key(key)
        if existing is not None:
            return Reservation(existing.id, existing.status, existing.reason)
        signal = uow.signals.get(signal_id)
        if signal is None:
            return Reservation(None, None, "UNKNOWN_SIGNAL")
        code: str | None = None
        if now >= signal.expires_at:
            code = ReasonCode.SIGNAL_EXPIRED.value
        else:
            state = uow.execution.wallet_state(gateway.wallet, balance)
            rejected = check_buy(limits, state, signal.amount_lamports)
            code = rejected.value if rejected else None
        status = IntentStatus.CANCELLED if code else IntentStatus.ACTIVE
        intent_id = uow.execution.create_intent(
            wallet=gateway.wallet, idempotency_key=key, side=Side.BUY.value, signal_id=signal.id, pool=signal.pool,
            mint=signal.mint, amount=signal.amount_lamports, strategy_version_id=signal.strategy_version_id,
            policy=policy, status=status.value, reason=code, expires_at=signal.expires_at, created_at=now,
            updated_at=now,
        )  # fmt: skip
        assert intent_id is not None  # la billetera está bloqueada: nadie más pudo crear la misma clave
        if code is None:
            uow.execution.add_ledger(
                wallet=gateway.wallet, intent_id=intent_id, kind="reserve",
                amount_lamports=signal.amount_lamports + limits.fee_reserve_lamports,
                reference=f"intent:{intent_id}:reserve", created_at=now,
            )  # fmt: skip
        uow.audit.log("execution_intent", str(intent_id), status.value, None, status.value,
                      {"side": "buy", "signal_id": signal.id, "code": code}, now)  # fmt: skip
        return Reservation(intent_id, status.value, code, created=True)


def reserve_sell(
    *, uow_factory: UnitOfWorkFactory, clock: Clock, position_id: int, trigger: str, policy: str
) -> Reservation:
    """Crea la intención de venta por todo el saldo confirmado de la posición.

    Si ya hay una venta sin resolver (o que pide atención), la devuelve: no se abre una segunda. Las ventas se
    permiten aunque las compras estén pausadas.
    """
    now = clock.now()
    with uow_factory() as uow:
        position = uow.execution.get_position(position_id)
        if position is None:
            return Reservation(None, None, "UNKNOWN_POSITION")
        uow.execution.lock_wallet(position.wallet, now)
        existing = uow.execution.blocking_sell(position_id)
        if existing is not None:
            return Reservation(existing.id, existing.status, existing.reason)
        position = uow.execution.get_position(position_id, lock=True)
        assert position is not None
        if position.status != "open" or position.tokens_open <= 0:
            return Reservation(None, None, "POSITION_CLOSED")
        key = f"sell:{position_id}:{uow.execution.sell_count(position_id) + 1}"
        intent_id = uow.execution.create_intent(
            wallet=position.wallet, idempotency_key=key, side=Side.SELL.value, signal_id=position.signal_id,
            position_id=position.id, pool=position.pool, mint=position.mint, amount=position.tokens_open,
            strategy_version_id=position.strategy_version_id, policy=policy, trigger=trigger,
            status=IntentStatus.ACTIVE.value, created_at=now, updated_at=now,
        )  # fmt: skip
        assert intent_id is not None  # billetera bloqueada y sin otra venta sin resolver
        uow.audit.log("execution_intent", str(intent_id), "active", None, "active",
                      {"side": "sell", "position_id": position.id, "trigger": trigger}, now)  # fmt: skip
        return Reservation(intent_id, IntentStatus.ACTIVE.value, created=True)
