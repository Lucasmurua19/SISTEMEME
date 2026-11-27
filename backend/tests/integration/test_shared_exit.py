"""Posiciones iguales venden con la misma cotización; si no pueden, cada intento registra por qué cotizó solo."""

from __future__ import annotations

from datetime import timedelta

import pytest
from sqlalchemy import select

from app.application.process_position import monitor_positions
from app.domain.models import WSOL_MINT, Quote, QuoteStatus
from app.infrastructure import orm
from tests.harness import Harness
from tests.integration.test_shared_entry import run_until_signal


class ScriptedSells:
    """Precio fijado por la prueba más un corrimiento por consulta: dos cotizaciones nunca coinciden."""

    def __init__(self, inner) -> None:
        self.inner = inner
        self.value = 0
        self.calls = 0

    def quote_sell(self, pool, tokens_in, slippage_bps, now) -> Quote:
        self.calls += 1
        out = self.value + self.calls * 1_000
        return Quote(
            side="sell", input_mint=pool.mint, output_mint=WSOL_MINT, in_amount=tokens_in, out_amount=out,
            min_out_amount=out * (10_000 - slippage_bps) // 10_000, observed_at=now,
            expires_at=now + timedelta(seconds=20), status=QuoteStatus.OK,
        )  # fmt: skip

    def __getattr__(self, name):
        return getattr(self.inner, name)


class Market:
    def __init__(self, uow, session_factory, *, pause: str | None = None) -> None:
        self.sessions = session_factory
        self.h = Harness(uow, only={"alpha_valid"})
        run_until_signal(self.h)
        if pause:
            with session_factory() as s:
                s.scalar(select(orm.PaperSession).where(orm.PaperSession.policy == pause)).paused = True
                s.commit()
        self.h.clock.advance(5)
        self.h.worker.jobs.run_due()  # ambas entran con la misma cotización, todavía sin revisar salidas
        self.quotes = ScriptedSells(self.h.worker.quotes)
        self.h.worker.quotes = self.quotes
        self.trigger_value = self.position("ranking").debited_lamports * 125 // 100  # +25 %: toma de ganancia

    def position(self, policy: str) -> orm.PaperPosition:
        with self.sessions() as s:
            return s.scalar(
                select(orm.PaperPosition).join(orm.PaperSession, orm.PaperSession.id == orm.PaperPosition.session_id)
                .where(orm.PaperSession.policy == policy)
            )  # fmt: skip

    def exits(self, policy: str) -> list[orm.PaperAttempt]:
        position_id = self.position(policy).id
        with self.sessions() as s:
            return list(s.scalars(
                select(orm.PaperAttempt)
                .where(orm.PaperAttempt.position_id == position_id, orm.PaperAttempt.kind == "exit")
                .order_by(orm.PaperAttempt.number)
            ))  # fmt: skip

    def change(self, policy: str, **fields) -> None:
        position_id = self.position(policy).id
        with self.sessions() as s:
            row = s.get(orm.PaperPosition, position_id)
            for name, value in fields.items():
                setattr(row, name, value)
            s.commit()

    def monitor(self, value: int) -> None:
        self.quotes.value = value
        w = self.h.worker
        monitor_positions(uow_factory=w.uow, quotes=self.quotes, clock=self.h.clock, cfg_loader=w.cfg_for)

    def execute(self, value: int, after_s: int = 5):
        self.quotes.value = value
        self.h.clock.advance(after_s)
        return self.h.worker.jobs.run_due()


@pytest.fixture()
def market(uow, session_factory):
    return Market(uow, session_factory)


def test_same_entry_and_same_exit_end_with_the_same_pnl_after_a_failed_sale(market, session_factory):
    value = market.trigger_value
    market.monitor(value)
    assert market.quotes.calls == 1  # una sola valoración para las dos

    result = market.execute(value * 887 // 1000)  # cae 11 %: supera la tolerancia del primer intento
    assert (result.done, result.retried, result.dead) == (2, 0, 0) and market.quotes.calls == 2
    assert [market.position(p).status for p in ("ranking", "liquidity")] == ["open", "open"]

    market.h.clock.advance(15)  # espera del primer reintento
    market.monitor(value * 90 // 100)
    market.execute(value * 90 // 100)
    assert market.quotes.calls == 4

    rank, liq = market.position("ranking"), market.position("liquidity")
    assert rank.status == liq.status == "closed"
    for field in ("pnl_lamports", "proceeds_lamports", "closed_at", "exit_trigger", "exit_failures"):
        assert getattr(rank, field) == getattr(liq, field), field
    assert rank.exit_failures == 1
    first, second = market.exits("ranking"), market.exits("liquidity")
    assert [a.status for a in first] == [a.status for a in second] == ["failed", "applied"]
    for mine, theirs in zip(first, second, strict=True):
        assert mine.quote_id == theirs.quote_id and mine.reference_out_lamports == theirs.reference_out_lamports
        assert mine.detail["shared_quote_with"] == [theirs.id] and theirs.detail["shared_quote_with"] == [mine.id]
        assert mine.detail["separate_quote_reason"] is None
    with session_factory() as s:
        credits = list(s.scalars(select(orm.PaperLedger).where(orm.PaperLedger.kind == "credit")))
    assert sorted(c.position_id for c in credits) == sorted([rank.id, liq.id])


def test_a_different_amount_is_quoted_separately_and_says_why(market):
    market.change("liquidity", tokens_expected=market.position("liquidity").tokens_expected + 1_000)
    market.monitor(market.trigger_value)
    market.execute(market.trigger_value)

    assert market.quotes.calls == 4  # dos valoraciones y dos ventas
    rank, liq = market.exits("ranking")[0], market.exits("liquidity")[0]
    assert rank.quote_id != liq.quote_id
    for attempt in (rank, liq):
        assert attempt.detail["shared_quote_with"] == []
        assert attempt.detail["separate_quote_reason"] == "cantidad_distinta"


def test_a_position_that_leaves_first_by_its_own_rule_says_why(market):
    market.change("liquidity", debited_lamports=market.position("liquidity").debited_lamports * 2)  # objetivo más lejos
    market.monitor(market.trigger_value)
    assert market.quotes.calls == 1  # se evalúan las dos con el mismo dato; solo una dispara
    market.execute(market.trigger_value)

    assert market.position("ranking").status == "closed" and market.position("liquidity").status == "open"
    assert market.exits("liquidity") == []
    assert market.exits("ranking")[0].detail["separate_quote_reason"] == "la_otra_no_vende_en_este_ciclo"

    later = market.trigger_value * 2
    market.h.clock.advance(15)
    market.monitor(later)
    market.execute(later)
    assert market.exits("liquidity")[0].detail["separate_quote_reason"] == "la_otra_ya_vendio"


def test_a_position_held_by_one_session_only_says_so(uow, session_factory):
    market = Market(uow, session_factory, pause="liquidity")
    market.monitor(market.trigger_value)
    market.execute(market.trigger_value)
    assert market.exits("ranking")[0].detail["separate_quote_reason"] == "sin_otra_posicion"
