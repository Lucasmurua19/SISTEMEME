"""Avance de una intención: preparar → firmar → guardar la firma → enviar → conciliar.

Invariantes:
- Antes de llamar al proveedor se **reclama** la intención en la base. Cada reclamo recibe un número creciente y toda
  escritura lo verifica: un worker que perdió el reclamo no puede guardar nada, y por lo tanto no puede enviar.
- El lugar del intento se guarda antes de pedir la orden, y la firma antes de enviarla.
- Tras un reinicio, una firma guardada se consulta en la cadena. Mientras pueda incluirse se reenvía esa misma
  transacción; nunca se prepara otra hasta saber que la anterior quedó resuelta.
- Las llamadas de red se hacen fuera de las transacciones.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timedelta

from app.application.execution.ports import (
    ChainResult,
    ChainStatus,
    ExecutionAttemptInfo,
    GatewayError,
    IntentInfo,
    NoRouteError,
    OrderGateway,
    SignedOrder,
    SubmitOutcome,
)
from app.application.ports import Clock, QuoteProvider, UnitOfWork, UnitOfWorkFactory
from app.domain.config import StrategyConfig
from app.domain.execution import (
    AttemptState,
    ExecutionCode,
    ExecutionLimits,
    Fill,
    IntentStatus,
    OrderRequest,
    Side,
    check_order,
    fill_matches,
    order_request,
)
from app.domain.simulation import decide_entry, exit_retry_delay_s, exit_slippage_bps

log = logging.getLogger(__name__)


class ClaimLost(Exception):
    """Otro worker tomó la intención: este ya no puede escribir ni enviar."""


@dataclass
class Executor:
    uow_factory: UnitOfWorkFactory
    gateway: OrderGateway
    quotes: QuoteProvider
    clock: Clock
    limits: ExecutionLimits
    cfg_loader: Callable[[int], StrategyConfig]
    worker_id: str = "executor-1"
    claim_ttl_s: int = 60

    def run_pending(self, limit: int = 25) -> dict[int, str]:
        with self.uow_factory() as uow:
            due = uow.execution.due_intents(self.gateway.wallet, self.clock.now(), limit)
        return {intent_id: self.advance(intent_id) for intent_id in due}

    def advance(self, intent_id: int) -> str:
        """Avanza la intención todo lo posible sin esperar. Devuelve en qué quedó."""
        with self.uow_factory() as uow:
            intent = uow.execution.claim(intent_id, self.worker_id, self.clock.now(), self.claim_ttl_s)
        if intent is None:
            return "skipped"
        try:
            outcome = self._advance(intent)
        except ClaimLost:
            log.warning("intención %s: se perdió el reclamo; no se escribe ni se envía nada más", intent_id)
            return "claim_lost"
        except Exception:  # lo persistido alcanza para retomar: se libera el reclamo y sigue con las demás
            log.exception("intención %s: falló el avance", intent_id)
            outcome = "error"
        with self.uow_factory() as uow:
            uow.execution.release_claim(intent.id, intent.claim_token, self.clock.now())
        return outcome

    # ------------------------------------------------------------------ escritura protegida
    def _owned(self, uow: UnitOfWork, intent: IntentInfo) -> IntentInfo:
        current = uow.execution.get_intent(intent.id, lock=True)
        if current is None or current.claim_token != intent.claim_token or current.status != IntentStatus.ACTIVE.value:
            raise ClaimLost
        return current

    def _move(self, intent: IntentInfo, attempt_id: int, state: AttemptState, **fields) -> None:
        with self.uow_factory() as uow:
            self._owned(uow, intent)
            uow.execution.move_attempt(attempt_id, state, self.clock.now(), **fields)

    # ------------------------------------------------------------------ pasos
    def _advance(self, intent: IntentInfo) -> str:
        with self.uow_factory() as uow:
            attempt = uow.execution.open_attempt(intent.id)
        if attempt is not None and attempt.signature is not None:
            return self._reconcile(intent, attempt)
        if attempt is not None:  # quedó sin firma guardada: nunca se pudo haber enviado
            with self.uow_factory() as uow:
                self._owned(uow, intent)
                current = uow.execution.get_attempt(attempt.id, lock=True)
                assert current is not None and current.signature is None  # jamás se abandona un intento firmado
                uow.execution.move_attempt(
                    attempt.id, AttemptState.EXPIRED, self.clock.now(), error=ExecutionCode.ABANDONED.value
                )
        now = self.clock.now()
        if intent.side is Side.BUY and intent.expires_at is not None and now >= intent.expires_at:
            return self._give_up(intent, None, "SIGNAL_EXPIRED")
        if intent.next_attempt_at is not None and now < intent.next_attempt_at:
            return "waiting"
        with self.uow_factory() as uow:
            self._owned(uow, intent)
            if intent.side is Side.BUY and uow.execution.lock_wallet(intent.wallet, now):
                attempt = None
            else:
                attempt = uow.execution.create_attempt(intent.id, self.gateway.name, now)
        if attempt is None:
            return self._give_up(intent, None, ExecutionCode.WALLET_PAUSED.value)
        return self._prepare_and_send(intent, attempt)

    def _prepare_and_send(self, intent: IntentInfo, attempt: ExecutionAttemptInfo) -> str:
        cfg = self.cfg_loader(intent.strategy_version_id)
        checked = self._check_market(intent, cfg)
        if isinstance(checked, str):
            return self._give_up(intent, attempt.id, checked)
        request, min_out = checked
        try:
            order = self.gateway.prepare(request)  # red
        except NoRouteError:
            return self._give_up(intent, attempt.id, ExecutionCode.NO_ROUTE.value)
        except GatewayError as exc:
            return self._give_up(intent, attempt.id, ExecutionCode.PROVIDER_ERROR.value, str(exc))
        rejected = check_order(self.limits, request, order, min_out_authorized=min_out, now=self.clock.now())
        if rejected:
            return self._give_up(intent, attempt.id, rejected.value)
        self._move(
            intent, attempt.id, AttemptState.PREPARED, request_id=order.request_id, in_amount=order.in_amount,
            out_amount=order.out_amount, min_out_amount=order.min_out_amount, min_out_authorized=min_out,
            valid_until=order.valid_until, price_impact_bps=order.price_impact_bps,
            fee_estimate_lamports=order.fee_lamports,
        )  # fmt: skip
        signed = self.gateway.sign(order)
        # Si esta escritura no se hace (reclamo perdido o compras en pausa), la firma se descarta sin salir del proceso.
        paused = False
        with self.uow_factory() as uow:
            self._owned(uow, intent)
            now = self.clock.now()
            if intent.side is Side.BUY and uow.execution.lock_wallet(intent.wallet, now):
                paused = True
            else:
                uow.execution.move_attempt(
                    attempt.id, AttemptState.SIGNED, now, signature=signed.signature, signed_payload=signed.payload,
                    valid_until=signed.valid_until,
                )  # fmt: skip
        if paused:
            return self._give_up(intent, attempt.id, ExecutionCode.WALLET_PAUSED.value)
        self._submit(intent, attempt.id, signed)
        with self.uow_factory() as uow:
            current = uow.execution.get_attempt(attempt.id)
        assert current is not None
        return self._reconcile(intent, current, resend=False)

    def _check_market(self, intent: IntentInfo, cfg: StrategyConfig) -> tuple[OrderRequest, int] | str:
        """Revisa el mercado antes de pedir la orden. Devuelve la orden a pedir y el mínimo autorizado, o el motivo."""
        now = self.clock.now()
        with self.uow_factory() as uow:
            pool = uow.chain.get_pool(intent.pool)
            signal = uow.signals.get(intent.signal_id)
            snapshots = uow.snapshots.list_for_pool(intent.pool, now)
        assert pool is not None and signal is not None
        if intent.side is Side.BUY:
            buy = self.quotes.quote_buy(pool, intent.amount, cfg.slippage_bps, now)  # red
            sell = self.quotes.quote_sell(pool, buy.min_out_amount, cfg.slippage_bps, now) if buy.ok else None
            decision = decide_entry(
                signal=signal, buy=buy, sell=sell, latest=snapshots[-1] if snapshots else None,
                now=self.clock.now(), cfg=cfg,
            )  # fmt: skip
            if not decision.accepted:
                assert decision.code is not None
                return decision.code.value
            slippage, min_out = cfg.slippage_bps, buy.min_out_amount
        else:
            slippage = exit_slippage_bps(cfg, intent.failures + 1)
            quote = self.quotes.quote_sell(pool, intent.amount, slippage, now)  # red
            if not quote.ok:
                return ExecutionCode.NO_ROUTE.value
            min_out = quote.min_out_amount
        return order_request(intent.side, intent.wallet, intent.mint, intent.amount, slippage), min_out

    def _submit(self, intent: IntentInfo, attempt_id: int, signed: SignedOrder) -> None:
        try:
            outcome = self.gateway.submit(signed)  # red
        except Exception as exc:  # no se sabe si salió: queda como desconocido
            log.warning("intención %s: envío sin respuesta (%s)", intent.id, type(exc).__name__)
            outcome = SubmitOutcome.UNKNOWN
        state = AttemptState.SUBMITTED if outcome is SubmitOutcome.SENT else AttemptState.UNKNOWN
        self._move(intent, attempt_id, state)

    # ------------------------------------------------------------------ conciliación
    def _reconcile(self, intent: IntentInfo, attempt: ExecutionAttemptInfo, *, resend: bool = True) -> str:
        assert attempt.signature is not None and attempt.signed_payload is not None and attempt.valid_until is not None
        result = self.gateway.lookup(attempt.signature)  # red
        if result.status is ChainStatus.CONFIRMED:
            return self._apply_fill(intent, attempt, result)
        if result.status is ChainStatus.FAILED:
            fee = result.fill.fee_lamports if result.fill else 0
            return self._give_up(
                intent, attempt.id, ExecutionCode.EXECUTION_FAILED.value, state=AttemptState.FAILED, fee=fee
            )
        if result.status is ChainStatus.EXPIRED:
            return self._give_up(intent, attempt.id, ExecutionCode.ORDER_EXPIRED.value, state=AttemptState.EXPIRED)
        # puede incluirse todavía: si no consta enviada, se reenvía exactamente la misma transacción
        unsent = attempt.state in (AttemptState.SIGNED, AttemptState.UNKNOWN)
        if resend and unsent and result.status is ChainStatus.NOT_FOUND:
            self._submit(intent, attempt.id, SignedOrder(attempt.signature, attempt.signed_payload, attempt.valid_until))
            with self.uow_factory() as uow:
                current = uow.execution.get_attempt(attempt.id)
            assert current is not None
            return self._reconcile(intent, current, resend=False)
        if attempt.state is AttemptState.SIGNED:
            state = AttemptState.SUBMITTED if result.status is ChainStatus.PENDING else AttemptState.UNKNOWN
            self._move(intent, attempt.id, state)
        return "pending"

    def _apply_fill(self, intent: IntentInfo, attempt: ExecutionAttemptInfo, result: ChainResult) -> str:
        request = order_request(intent.side, intent.wallet, intent.mint, intent.amount, 0)
        fill = result.fill
        if fill is None or attempt.min_out_amount is None or not fill_matches(
            request, fill, min_out_amount=attempt.min_out_amount
        ):
            now = self.clock.now()
            with self.uow_factory() as uow:
                self._owned(uow, intent)
                uow.execution.pause_buys(intent.wallet, f"intención {intent.id}: movimiento sin conciliar", now)
                uow.audit.log("execution_intent", str(intent.id), "discrepancy", "active", "active",
                              {"attempt_id": attempt.id, "has_fill": fill is not None}, now)  # fmt: skip
            log.error("intención %s: la confirmación no coincide con la orden; compras pausadas", intent.id)
            return "needs_reconciliation"

        now = self.clock.now()
        with self.uow_factory() as uow:
            current = self._owned(uow, intent)
            uow.execution.lock_wallet(intent.wallet, now)
            uow.execution.move_attempt(
                attempt.id, AttemptState.CONFIRMED, now, sol_delta_lamports=fill.sol_delta_lamports,
                token_delta=fill.token_delta, fee_paid_lamports=fill.fee_lamports,
            )  # fmt: skip
            if current.side is Side.BUY:
                self._open_position(uow, current, attempt.id, fill, now)
            else:
                self._reduce_position(uow, current, attempt.id, fill, now)
            uow.execution.update_intent(intent.id, now, status=IntentStatus.DONE.value, reason=None)
            uow.audit.log("execution_intent", str(intent.id), "done", "active", "done", {"attempt_id": attempt.id}, now)
        return "done"

    def _open_position(self, uow: UnitOfWork, intent: IntentInfo, attempt_id: int, fill: Fill, now: datetime) -> None:
        position_id = uow.execution.create_position(
            wallet=intent.wallet, buy_intent_id=intent.id, signal_id=intent.signal_id, pool=intent.pool,
            mint=intent.mint, strategy_version_id=intent.strategy_version_id, status="open",
            tokens_acquired=fill.token_delta, tokens_open=fill.token_delta,
            sol_debited_lamports=-fill.sol_delta_lamports, sol_received_lamports=0, opened_at=now,
        )  # fmt: skip
        self._release_reservation(uow, intent, now)
        uow.execution.add_ledger(
            wallet=intent.wallet, intent_id=intent.id, attempt_id=attempt_id, position_id=position_id, kind="debit",
            amount_lamports=-fill.sol_delta_lamports, reference=f"attempt:{attempt_id}:fill", created_at=now,
        )  # fmt: skip

    def _reduce_position(self, uow: UnitOfWork, intent: IntentInfo, attempt_id: int, fill: Fill, now: datetime) -> None:
        assert intent.position_id is not None
        position = uow.execution.get_position(intent.position_id, lock=True)
        assert position is not None
        remaining = position.tokens_open + fill.token_delta
        received = position.sol_received_lamports + fill.sol_delta_lamports
        changes: dict = {"tokens_open": remaining, "sol_received_lamports": received}
        if remaining <= 0:
            pnl = received - position.sol_debited_lamports - uow.execution.failed_fees(position.id)
            changes |= {"status": "closed", "closed_at": now, "pnl_lamports": pnl}
        uow.execution.update_position(position.id, **changes)
        uow.execution.add_ledger(
            wallet=intent.wallet, intent_id=intent.id, attempt_id=attempt_id, position_id=position.id, kind="credit",
            amount_lamports=fill.sol_delta_lamports, reference=f"attempt:{attempt_id}:fill", created_at=now,
        )  # fmt: skip

    def _release_reservation(self, uow: UnitOfWork, intent: IntentInfo, now: datetime) -> None:
        uow.execution.add_ledger(
            wallet=intent.wallet, intent_id=intent.id, kind="release",
            amount_lamports=intent.amount + self.limits.fee_reserve_lamports,
            reference=f"intent:{intent.id}:release", created_at=now,
        )  # fmt: skip

    def _give_up(
        self,
        intent: IntentInfo,
        attempt_id: int | None,
        code: str,
        error: str | None = None,
        *,
        state: AttemptState = AttemptState.REJECTED,
        fee: int = 0,
    ) -> str:
        """Cierra el intento sin movimiento. La compra se cancela; la venta espera y reintenta dentro de su presupuesto."""
        now = self.clock.now()
        with self.uow_factory() as uow:
            current = self._owned(uow, intent)
            uow.execution.lock_wallet(intent.wallet, now)
            if attempt_id is not None:
                uow.execution.move_attempt(attempt_id, state, now, error=(error or code)[:300], fee_paid_lamports=fee)
                if fee:
                    uow.execution.add_ledger(
                        wallet=intent.wallet, intent_id=intent.id, attempt_id=attempt_id,
                        position_id=current.position_id, kind="fee", amount_lamports=fee,
                        reference=f"attempt:{attempt_id}:fee", created_at=now,
                    )  # fmt: skip
            if current.side is Side.BUY:
                uow.execution.update_intent(intent.id, now, status=IntentStatus.CANCELLED.value, reason=code)
                self._release_reservation(uow, current, now)
                uow.audit.log("execution_intent", str(intent.id), "cancelled", "active", "cancelled", {"code": code}, now)
                return f"cancelled:{code}"
            assert current.position_id is not None
            failures = current.failures + 1
            cfg = self.cfg_loader(current.strategy_version_id)
            exhausted = (
                failures >= self.limits.max_sell_attempts
                or uow.execution.failed_fees(current.position_id) >= self.limits.max_failed_sell_fees_lamports
            )
            status = IntentStatus.NEEDS_ATTENTION if exhausted else IntentStatus.ACTIVE
            retry_at = None if exhausted else now + timedelta(seconds=exit_retry_delay_s(cfg, failures))
            uow.execution.update_intent(
                intent.id, now, status=status.value, reason=code, failures=failures, next_attempt_at=retry_at
            )
            uow.audit.log("execution_intent", str(intent.id), "sell_failed", "active", status.value,
                          {"code": code, "failures": failures}, now)  # fmt: skip
            return status.value if exhausted else f"retry:{code}"
