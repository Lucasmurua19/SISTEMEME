"""Ejecutor con proveedor falso: de una señal de fixtures a una compra y una venta, con reinicios en el medio."""

from __future__ import annotations

from dataclasses import replace
from datetime import timedelta
from decimal import Decimal

import pytest
from sqlalchemy import select

from app.application.execution.executor import Executor
from app.application.execution.intents import reserve_buy, reserve_sell
from app.application.execution.ports import GatewayError, NoRouteError
from app.bootstrap import check_execution_mode
from app.domain.execution import AttemptState, ExecutionLimits, check_transition
from app.domain.models import QuoteStatus
from app.infrastructure import orm
from app.settings import Settings
from tests.fake_gateway import FEE, FakeGateway, ProcessCrash
from tests.harness import Harness

CLAIM_TTL_S = 20  # menor que la vigencia de la orden falsa (60 s): al reintentar, la transacción sigue vigente

LIMITS = ExecutionLimits(
    max_order_lamports=100_000_000, max_open_positions=1, max_session_loss_lamports=50_000_000,
    fee_reserve_lamports=5_000_000, max_fee_lamports=500_000, max_slippage_bps=300, max_exit_slippage_bps=3000,
    max_price_impact_bps=Decimal(200), max_sell_attempts=3,
)  # fmt: skip


class FrozenQuotes:
    """Cotizaciones del instante de la señal, con la vigencia de ahora: el precio no se mueve durante la prueba."""

    def __init__(self, inner, clock) -> None:
        self.inner, self.clock, self.at = inner, clock, clock.now()

    def _fresh(self, quote):
        now = self.clock.now()
        return replace(quote, observed_at=now, expires_at=now + timedelta(seconds=20))

    def quote_buy(self, pool, lamports_in, slippage_bps, now):
        return self._fresh(self.inner.quote_buy(pool, lamports_in, slippage_bps, self.at))

    def quote_sell(self, pool, tokens_in, slippage_bps, now):
        return self._fresh(self.inner.quote_sell(pool, tokens_in, slippage_bps, self.at))


class World:
    def __init__(self, uow, session_factory) -> None:
        self.uow = uow
        self.sessions = session_factory
        self.h = Harness(uow, only={"alpha_valid"})
        for _ in range(80):
            if self.h.run(15)[0].signals:
                break
        with session_factory() as s:
            signal = s.scalars(select(orm.Signal)).first()
            self.signal_id, self.pool, amount = signal.id, signal.pool, signal.amount_lamports
        self.quotes = FrozenQuotes(self.h.worker.quotes, self.h.clock)
        self.gateway = FakeGateway(self.h.clock)
        with uow() as u:
            info = u.chain.get_pool(self.pool)
        quote = self.quotes.quote_buy(info, amount, self.h.cfg.slippage_bps, self.h.clock.now())
        self.tokens = quote.out_amount
        self.gateway.expected_out = self.tokens

    def executor(self, limits: ExecutionLimits = LIMITS, quotes=None) -> Executor:
        """Una instancia nueva equivale a un proceso recién arrancado: todo el estado está en la base."""
        return Executor(
            uow_factory=self.uow, gateway=self.gateway, quotes=quotes or self.quotes, clock=self.h.clock,
            limits=limits, cfg_loader=self.h.worker.cfg_for, claim_ttl_s=CLAIM_TTL_S,
        )  # fmt: skip

    def buy(self, limits: ExecutionLimits = LIMITS, signal_id: int | None = None):
        return reserve_buy(
            uow_factory=self.uow, gateway=self.gateway, clock=self.h.clock, limits=limits,
            signal_id=signal_id or self.signal_id, policy="ranking",
        )  # fmt: skip

    def sell(self, position_id: int, trigger: str = "EXIT_TAKE_PROFIT"):
        self.gateway.expected_out = 120_000_000  # SOL que devuelve la venta
        return reserve_sell(
            uow_factory=self.uow, clock=self.h.clock, position_id=position_id, trigger=trigger, policy="ranking"
        )

    def rows(self, model) -> list:
        with self.sessions() as s:
            return list(s.scalars(select(model).order_by(*model.__table__.primary_key.columns)))

    def one(self, model):
        (row,) = self.rows(model)
        return row

    def reserved(self) -> int:
        with self.uow() as u:
            return u.execution.wallet_state(self.gateway.wallet, self.gateway.balance).reserved_lamports

    def bought(self) -> orm.LivePosition:
        assert self.executor().advance(self.buy().intent_id) == "done"
        return self.one(orm.LivePosition)


@pytest.fixture()
def world(uow, session_factory):
    return World(uow, session_factory)


def test_a_fixture_signal_goes_through_buy_and_sell_with_full_trace(world):
    reservation = world.buy()
    assert reservation.created and reservation.status == "active"
    assert world.reserved() == 100_000_000 + LIMITS.fee_reserve_lamports
    position = world.bought()
    assert (position.status, position.tokens_open, position.sol_debited_lamports) == ("open", world.tokens, 100_000_000 + FEE)
    assert world.reserved() == 0

    sale = world.sell(position.id)
    assert world.executor().run_pending() == {sale.intent_id: "done"}
    position = world.one(orm.LivePosition)
    assert position.status == "closed" and position.tokens_open == 0
    assert position.pnl_lamports == (120_000_000 - FEE) - (100_000_000 + FEE)

    buy, sell = world.rows(orm.ExecutionIntent)
    assert (buy.status, sell.status, sell.trigger) == ("done", "done", "EXIT_TAKE_PROFIT")
    for attempt in world.rows(orm.ExecutionAttempt):
        assert attempt.state == "confirmed" and attempt.signature and attempt.request_id
        assert attempt.created_at <= attempt.prepared_at <= attempt.signed_at <= attempt.resolved_at
        assert attempt.min_out_amount >= attempt.min_out_authorized > 0
    assert [e.kind for e in world.rows(orm.LiveLedger)] == ["reserve", "release", "debit", "credit"]
    assert len(world.gateway.signed) == len(world.gateway.sent) == 2


def test_repeating_the_reservation_or_the_advance_changes_nothing(world):
    first, second = world.buy(), world.buy()
    assert first.intent_id == second.intent_id and not second.created
    executor = world.executor()
    assert executor.advance(first.intent_id) == "done" and executor.advance(first.intent_id) == "skipped"
    assert len(world.gateway.signed) == 1 and len(world.rows(orm.LivePosition)) == 1


@pytest.mark.parametrize("crash", ["crash_before", "crash_after"], ids=["antes_de_enviar", "despues_de_enviar"])
def test_restart_in_the_middle_of_the_send_applies_the_movement_once(world, crash):
    intent_id = world.buy().intent_id
    world.gateway.submits = [crash]
    with pytest.raises(ProcessCrash):
        world.executor().advance(intent_id)
    attempt = world.one(orm.ExecutionAttempt)
    assert attempt.state == "signed" and attempt.signature  # la firma quedó guardada antes de enviar
    assert world.executor().advance(intent_id) == "skipped"  # el reclamo del proceso muerto todavía vale

    world.h.clock.advance(CLAIM_TTL_S + 1)
    assert world.executor().advance(intent_id) == "done"
    assert len(world.gateway.signed) == 1 and set(world.gateway.sent) == {attempt.signature}
    assert len(world.gateway.sent) == (2 if crash == "crash_before" else 1)  # solo se reenvía si no había llegado
    assert [e.kind for e in world.rows(orm.LiveLedger)] == ["reserve", "release", "debit"]


def test_a_send_without_answer_blocks_a_new_order_until_the_first_one_expires(world):
    intent_id = world.buy().intent_id
    world.gateway.submits = ["timeout_lost", "timeout_lost", "timeout_lost"]
    assert world.executor().advance(intent_id) == "pending"
    assert world.one(orm.ExecutionAttempt).state == "unknown"
    for _ in range(2):
        world.h.clock.advance(10)
        assert world.executor().advance(intent_id) == "pending"
    assert len(world.gateway.signed) == 1 and len(set(world.gateway.sent)) == 1  # siempre la misma transacción
    assert world.reserved() > 0 and world.buy().intent_id == intent_id

    world.h.clock.advance(60)  # ya no puede incluirse
    assert world.executor().advance(intent_id) == "cancelled:ORDER_EXPIRED"
    assert world.one(orm.ExecutionAttempt).state == "expired" and world.reserved() == 0
    assert world.rows(orm.LivePosition) == []


def test_a_timeout_that_did_land_is_reconciled_without_a_second_send(world):
    intent_id = world.buy().intent_id
    world.gateway.submits = ["timeout_landed"]
    assert world.executor().advance(intent_id) == "done"
    assert len(world.gateway.sent) == 1 and world.one(orm.LivePosition).status == "open"


def test_a_crash_while_preparing_never_sends_the_abandoned_order(world):
    intent_id = world.buy().intent_id

    def die() -> None:
        raise ProcessCrash

    world.gateway.prepare_hook = die
    with pytest.raises(ProcessCrash):
        world.executor().advance(intent_id)
    world.gateway.prepare_hook = None
    world.h.clock.advance(CLAIM_TTL_S + 1)
    assert world.executor().advance(intent_id) == "done"
    first, second = world.rows(orm.ExecutionAttempt)
    assert (first.state, first.error, first.signature) == ("expired", "ABANDONED", None)
    assert second.state == "confirmed" and world.gateway.sent == [second.signature]


def test_after_another_worker_claims_the_intent_the_first_one_cannot_sign_or_send(world):
    """El cruce más delicado: el segundo worker ya reclamó pero todavía no escribió nada. Solo el número de
    reclamo impide que el primero guarde su firma y envíe una orden que el segundo daría por abandonada."""
    intent_id = world.buy().intent_id

    def another_worker_takes_over() -> None:
        world.h.clock.advance(CLAIM_TTL_S + 1)
        with world.uow() as u:
            assert u.execution.claim(intent_id, "otro", world.h.clock.now(), CLAIM_TTL_S) is not None

    world.gateway.prepare_hook = another_worker_takes_over
    assert world.executor().advance(intent_id) == "claim_lost"
    assert world.gateway.signed == [] and world.gateway.sent == []
    attempt = world.one(orm.ExecutionAttempt)
    assert (attempt.state, attempt.signature) == ("preparing", None)


@pytest.mark.parametrize(
    "setup, code",
    [
        ({"prepare_error": NoRouteError()}, "NO_ROUTE"),
        ({"prepare_error": GatewayError("caído")}, "PROVIDER_ERROR"),
        ({"order_changes": {"taker": "otra"}}, "ORDER_MISMATCH"),
        ({"order_changes": {"in_amount": 100_000_001}}, "ORDER_MISMATCH"),
        ({"order_changes": {"min_out_amount": 1}}, "BELOW_AUTHORIZED"),
        ({"order_changes": {"price_impact_bps": Decimal(900)}}, "PRICE_IMPACT_LIMIT"),
        ({"order_changes": {"fee_lamports": 900_000}}, "FEE_LIMIT"),
    ],
    ids=["sin_ruta", "proveedor", "firmante", "monto", "minimo", "impacto", "comision"],
)
def test_an_order_out_of_bounds_is_never_signed(world, setup, code):
    for name, value in setup.items():
        setattr(world.gateway, name, value)
    assert world.executor().advance(world.buy().intent_id) == f"cancelled:{code}"
    assert world.gateway.signed == [] and world.gateway.sent == [] and world.reserved() == 0
    assert world.one(orm.ExecutionAttempt).state == "rejected"


def test_an_expired_signal_and_a_missing_exit_route_stop_before_the_provider(world):
    intent_id = world.buy().intent_id

    class NoExit:
        def __init__(self, inner) -> None:
            self.inner = inner

        def quote_buy(self, *args):
            return self.inner.quote_buy(*args)

        def quote_sell(self, *args):
            return replace(self.inner.quote_sell(*args), status=QuoteStatus.NO_ROUTE, out_amount=0, min_out_amount=0)

    assert world.executor(quotes=NoExit(world.quotes)).advance(intent_id) == "cancelled:NO_SELL_ROUTE"
    assert world.gateway.prepared == [] and world.reserved() == 0

    world.h.clock.advance(600)
    late = reserve_buy(
        uow_factory=world.uow, gateway=FakeGatewayWithOtherWallet(world.h.clock), clock=world.h.clock, limits=LIMITS,
        signal_id=world.signal_id, policy="ranking",
    )  # fmt: skip
    assert (late.status, late.code) == ("cancelled", "SIGNAL_EXPIRED")


class FakeGatewayWithOtherWallet(FakeGateway):
    wallet = "OtraBi11etera11111111111111111111111111111"


@pytest.mark.parametrize(
    "changes, balance, code",
    [
        ({"max_order_lamports": 99_999_999}, None, "ORDER_TOO_LARGE"),
        ({"max_open_positions": 0}, None, "POSITION_LIMIT"),
        ({"max_session_loss_lamports": 0}, None, "SESSION_LIMIT"),
        ({}, 104_000_000, "INSUFFICIENT_FUNDS"),
    ],
)
def test_wallet_limits_reject_the_intent_without_reserving(world, changes, balance, code):
    if balance:
        world.gateway.balance = balance
    reservation = world.buy(replace(LIMITS, **changes))
    assert (reservation.status, reservation.code) == ("cancelled", code)
    assert world.reserved() == 0 and world.executor().run_pending() == {}


def test_a_failed_sale_keeps_the_position_retries_later_and_asks_for_attention(world):
    position = world.bought()
    sale = world.sell(position.id)
    world.gateway.submits = ["fail", "fail", "fail"]
    executor = world.executor()
    assert executor.advance(sale.intent_id) == "retry:EXECUTION_FAILED"
    assert executor.advance(sale.intent_id) == "waiting" and executor.run_pending() == {}
    assert world.sell(position.id).intent_id == sale.intent_id  # no se abre una segunda venta

    for wait, expected in ((15, "retry:EXECUTION_FAILED"), (30, "needs_attention")):
        world.h.clock.advance(wait)
        assert executor.advance(sale.intent_id) == expected
    again = world.sell(position.id)
    assert again.intent_id == sale.intent_id and again.status == "needs_attention"
    open_position = world.one(orm.LivePosition)
    assert open_position.status == "open" and open_position.tokens_open == world.tokens
    fees = [e.amount_lamports for e in world.rows(orm.LiveLedger) if e.kind == "fee"]
    assert fees == [5_000] * 3 and len(world.gateway.signed) == 4


def test_a_sale_that_fails_once_then_fills_counts_the_failed_fee_in_the_pnl(world):
    position = world.bought()
    sale = world.sell(position.id)
    world.gateway.submits = ["fail"]
    assert world.executor().advance(sale.intent_id) == "retry:EXECUTION_FAILED"
    world.h.clock.advance(15)
    assert world.executor().advance(sale.intent_id) == "done"
    assert world.one(orm.LivePosition).pnl_lamports == (120_000_000 - FEE) - (100_000_000 + FEE) - 5_000


def test_a_confirmation_that_does_not_match_pauses_buys_and_books_nothing(world):
    intent_id = world.buy().intent_id
    original = world.gateway.land

    def short_fill(signature: str) -> None:
        original(signature)
        result = world.gateway.chain.landed[signature]
        world.gateway.chain.landed[signature] = replace(result, fill=replace(result.fill, token_delta=1))

    world.gateway.land = short_fill
    assert world.executor().advance(intent_id) == "needs_reconciliation"
    assert world.rows(orm.LivePosition) == [] and [e.kind for e in world.rows(orm.LiveLedger)] == ["reserve"]
    assert world.one(orm.ExecutionAttempt).state == "submitted" and world.one(orm.ExecutionWallet).buys_paused
    assert world.executor().advance(intent_id) == "needs_reconciliation" and len(world.gateway.signed) == 1


@pytest.mark.parametrize("moment", ["antes_de_pedir", "mientras_prepara"])
def test_pausing_buys_stops_a_reserved_buy_before_it_is_sent(world, moment):
    intent_id = world.buy().intent_id

    def pause() -> None:
        with world.uow() as u:
            u.execution.pause_buys(world.gateway.wallet, "manual", world.h.clock.now())

    if moment == "antes_de_pedir":
        pause()
    else:
        world.gateway.prepare_hook = pause
    assert world.executor().advance(intent_id) == "cancelled:WALLET_PAUSED"
    assert world.gateway.sent == [] and world.reserved() == 0
    assert len(world.gateway.prepared) == (0 if moment == "antes_de_pedir" else 1)


def test_paused_buys_still_allow_selling(world):
    position = world.bought()
    with world.uow() as u:
        u.execution.pause_buys(world.gateway.wallet, "manual", world.h.clock.now())
    assert world.executor().advance(world.sell(position.id).intent_id) == "done"


def test_a_signed_attempt_can_only_be_closed_by_the_chain():
    for state in (AttemptState.SIGNED, AttemptState.SUBMITTED, AttemptState.UNKNOWN):
        with pytest.raises(ValueError):
            check_transition(state, AttemptState.REJECTED)


@pytest.mark.parametrize("mode", ["manual", "auto"])
def test_the_worker_refuses_to_start_in_a_real_execution_mode(mode):
    check_execution_mode(Settings(execution_mode="paper", _env_file=None))
    with pytest.raises(RuntimeError, match="solo existe la simulación"):
        check_execution_mode(Settings(execution_mode=mode, _env_file=None))
