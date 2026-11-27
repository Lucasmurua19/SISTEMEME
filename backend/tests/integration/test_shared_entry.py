"""Ranking y Liquidez entran con la misma cotización cuando admiten la misma señal en el mismo ciclo."""

from __future__ import annotations

from dataclasses import replace
from datetime import datetime

import pytest
from sqlalchemy import select

from app.application.process_position import admit_signals
from app.infrastructure import orm
from tests.harness import Harness


class MovingQuotes:
    """Cada consulta tarda 2 s y el precio de compra empeora un poco: dos cotizaciones nunca son iguales."""

    def __init__(self, inner, clock) -> None:
        self.inner, self.clock = inner, clock
        self.buys = self.sells = 0

    def quote_buy(self, pool, lamports_in, slippage_bps, now):
        self.clock.advance(2)
        self.buys += 1
        quote = self.inner.quote_buy(pool, lamports_in, slippage_bps, now)
        return replace(quote, out_amount=quote.out_amount - self.buys * 1_000)

    def quote_sell(self, pool, tokens_in, slippage_bps, now):
        self.clock.advance(2)
        self.sells += 1
        return self.inner.quote_sell(pool, tokens_in, slippage_bps, now)

    def __getattr__(self, name):
        return getattr(self.inner, name)


def run_until_signal(harness: Harness) -> None:
    """Avanza hasta el ciclo en que ambas sesiones admiten la señal (la entrada todavía está pendiente)."""
    for _ in range(80):
        if harness.run(15)[0].admitted == 2:
            return
    raise AssertionError("el escenario no produjo una señal admitida por las dos sesiones")


def entries(session_factory) -> dict[str, tuple[orm.PaperPosition, orm.PaperAttempt]]:
    with session_factory() as s:
        rows = s.execute(
            select(orm.PaperSession.policy, orm.PaperPosition, orm.PaperAttempt)
            .join(orm.PaperPosition, orm.PaperPosition.session_id == orm.PaperSession.id)
            .join(orm.PaperAttempt, orm.PaperAttempt.position_id == orm.PaperPosition.id)
            .where(orm.PaperAttempt.kind == "entry")
        ).all()
    return {policy: (position, attempt) for policy, position, attempt in rows}


@pytest.fixture()
def pending(uow, session_factory):
    harness = Harness(uow, only={"alpha_valid"})
    run_until_signal(harness)
    quotes = MovingQuotes(harness.worker.quotes, harness.clock)
    harness.worker.quotes = quotes
    return harness, quotes


def test_both_sessions_enter_with_one_quote_at_the_same_instant(pending, session_factory):
    harness, quotes = pending
    assert {a.status for _, a in entries(session_factory).values()} == {"pending"}
    harness.clock.advance(5)
    result = harness.worker.jobs.run_due()

    assert (result.done, result.retried, result.dead) == (2, 0, 0)  # el segundo trabajo no encuentra nada pendiente
    assert (quotes.buys, quotes.sells) == (1, 1)
    got = entries(session_factory)
    (rank_pos, rank), (liq_pos, liq) = got["ranking"], got["liquidity"]
    assert rank_pos.status == liq_pos.status == "open"
    for field in ("tokens_expected", "debited_lamports", "entry_price", "opened_at", "last_recoverable_lamports"):
        assert getattr(rank_pos, field) == getattr(liq_pos, field), field
    assert rank.quote_id == liq.quote_id and rank.detail["sell_quote_id"] == liq.detail["sell_quote_id"]
    assert rank.detail["shared_quote_with"] == [liq.id] and liq.detail["shared_quote_with"] == [rank.id]
    with session_factory() as s:
        assert len(list(s.scalars(select(orm.QuoteRow).where(orm.QuoteRow.purpose == "entry")))) == 2  # compra y venta
        ledger = list(s.scalars(select(orm.PaperLedger)))
    for position in (rank_pos, liq_pos):
        assert sorted(r.kind for r in ledger if r.position_id == position.id) == ["debit", "release", "reserve"]


def test_each_attempt_records_when_the_signal_appeared_was_due_and_was_quoted(pending, session_factory):
    harness, _ = pending
    harness.clock.advance(12)  # el worker llega 7 s tarde a un turno programado 5 s después de admitir
    harness.worker.jobs.run_due()

    got = entries(session_factory)
    timings = [attempt.detail["timing"] for _, attempt in got.values()]
    assert timings[0] == timings[1]
    at = {k: datetime.fromisoformat(v) for k, v in timings[0].items()}
    assert (at["due_at"] - at["signal_at"]).total_seconds() == harness.cfg.entry_delay_s
    assert (at["requested_at"] - at["due_at"]).total_seconds() == 7
    assert (at["received_at"] - at["requested_at"]).total_seconds() == 4  # compra y venta, 2 s cada una


def test_each_session_still_applies_its_own_limits_to_the_shared_quote(pending, session_factory):
    harness, quotes = pending
    with session_factory() as s:
        s.scalar(select(orm.PaperSession).where(orm.PaperSession.policy == "liquidity")).paused = True
        s.commit()
    harness.clock.advance(5)
    harness.worker.jobs.run_due()

    got = entries(session_factory)
    assert got["ranking"][0].status == "open"
    assert got["liquidity"][0].status == "rejected" and got["liquidity"][0].reject_reason == "SESSION_PAUSED"
    assert got["ranking"][1].quote_id == got["liquidity"][1].quote_id
    assert quotes.buys == 1


def test_admissions_in_different_cycles_are_quoted_separately(uow, session_factory):
    harness = Harness(uow, only={"alpha_valid"})
    liquidity_id = None
    with uow() as u:
        liquidity_id = next(s.id for s in u.sessions.list_for_version(harness.worker.version_id) if s.policy == "liquidity")
        u.sessions.set_paused(liquidity_id, True, "prueba", harness.clock.now())
    for _ in range(80):
        if harness.run(15)[0].admitted:
            break
    with session_factory() as s:  # la sesión en pausa ya registró su rechazo: se borra para admitirla después
        for position in s.scalars(select(orm.PaperPosition).where(orm.PaperPosition.session_id == liquidity_id)):
            s.delete(position)
        s.get(orm.PaperSession, liquidity_id).paused = False
        s.commit()
    harness.clock.advance(1)
    admit_signals(uow_factory=uow, clock=harness.clock, session_id=liquidity_id, cfg=harness.cfg)
    quotes = MovingQuotes(harness.worker.quotes, harness.clock)
    harness.worker.quotes = quotes
    harness.clock.advance(6)
    harness.worker.jobs.run_due()

    got = entries(session_factory)
    assert quotes.buys == 2
    assert got["ranking"][1].quote_id != got["liquidity"][1].quote_id
    assert got["ranking"][1].detail["shared_quote_with"] == [] == got["liquidity"][1].detail["shared_quote_with"]
