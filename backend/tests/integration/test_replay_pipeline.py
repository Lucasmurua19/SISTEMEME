"""Flujo completo con fixtures: migración → snapshots → evaluación → señal → posición simulada → PnL."""

from __future__ import annotations

from collections import defaultdict

import pytest
from sqlalchemy import func, select

from app.infrastructure import orm
from tests.harness import Harness


def latest_evaluations(session_factory):
    """Última evaluación por símbolo."""
    with session_factory() as s:
        rows = s.execute(
            select(orm.Token.symbol, orm.Evaluation)
            .join(orm.Pool, orm.Pool.mint == orm.Token.mint)
            .join(orm.Evaluation, orm.Evaluation.pool == orm.Pool.address)
            .order_by(orm.Evaluation.window_index)
        ).all()
        out = {}
        for symbol, ev in rows:
            out[symbol] = ev
        return out


def positions_by_session(session_factory):
    with session_factory() as s:
        rows = s.execute(
            select(orm.PaperSession.policy, orm.Token.symbol, orm.PaperPosition)
            .join(orm.PaperPosition, orm.PaperPosition.session_id == orm.PaperSession.id)
            .join(orm.Token, orm.Token.mint == orm.PaperPosition.mint)
        ).all()
        out: dict[str, dict[str, orm.PaperPosition]] = defaultdict(dict)
        for policy, symbol, pos in rows:
            out[policy][symbol] = pos
        return out


@pytest.fixture()
def replay(uow, session_factory):
    h = Harness(uow)
    h.run_all()
    return h


class TestFullReplay:
    def test_every_scenario_ends_in_its_expected_state(self, replay, session_factory):
        ev = latest_evaluations(session_factory)
        codes = {sym: {r["code"] for r in e.reasons} for sym, e in ev.items()}

        assert ev["ETA"].status == "rejected" and "DRAWDOWN_TOO_DEEP" in codes["ETA"]
        assert ev["MU"].status == "rejected" and "CONCENTRATION_TOO_HIGH" in codes["MU"]
        assert ev["DELT"].status == "rejected" and "INSUFFICIENT_NET_BUYERS" in codes["DELT"]
        assert ev["LAMB"].status == "rejected" and "ROUND_TRIP_COST_EXCEEDED" in codes["LAMB"]
        assert ev["XI"].status == "rejected" and "MIGRATION_UNVERIFIED" in codes["XI"]
        assert ev["EPSI"].status == "pending_data" and "CONCENTRATION_UNKNOWN" in codes["EPSI"]
        assert ev["ZETA"].status == "pending_data" and "NO_SELL_ROUTE" in codes["ZETA"]

    def test_signals_only_for_tokens_that_passed_every_filter(self, replay, session_factory):
        with session_factory() as s:
            symbols = set(
                s.scalars(
                    select(orm.Token.symbol)
                    .join(orm.Pool, orm.Pool.mint == orm.Token.mint)
                    .join(orm.Signal, orm.Signal.pool == orm.Pool.address)
                )
            )
        assert symbols == {"ALFA", "BETA", "GAMA", "THET", "IOTA", "OMIC", "KAPA"}

    def test_ranking_and_liquidity_sessions_choose_different_tokens(self, replay, session_factory):
        pos = positions_by_session(session_factory)
        ranking_taken = {s for s, p in pos["ranking"].items() if p.status != "rejected" or p.reject_reason != "POSITION_LIMIT"}
        liquidity_taken = {s for s, p in pos["liquidity"].items() if p.status != "rejected" or p.reject_reason != "POSITION_LIMIT"}
        # mismo universo, mismo presupuesto, distinto orden de admisión
        assert pos["ranking"]["ALFA"].reject_reason is None
        assert pos["ranking"]["GAMA"].reject_reason == "POSITION_LIMIT"
        assert pos["liquidity"]["GAMA"].reject_reason is None
        assert pos["liquidity"]["ALFA"].reject_reason == "POSITION_LIMIT"
        assert "BETA" in ranking_taken and "BETA" in liquidity_taken
        assert set(pos["ranking"]) == set(pos["liquidity"])  # ambas sesiones procesaron las mismas señales

    def test_winning_trade_closes_by_take_profit_with_net_pnl(self, replay, session_factory):
        pos = positions_by_session(session_factory)["ranking"]["ALFA"]
        assert pos.status == "closed"
        assert pos.exit_trigger == "EXIT_TAKE_PROFIT"
        assert pos.pnl_lamports > 0
        assert pos.pnl_lamports == pos.proceeds_lamports - pos.debited_lamports
        assert pos.sol_usd_open is not None and pos.sol_usd_close is not None

    def test_invalidation_exits_with_a_loss(self, replay, session_factory):
        pos = positions_by_session(session_factory)["liquidity"]["GAMA"]
        assert pos.status == "closed" and pos.exit_trigger == "EXIT_INVALIDATION"
        assert pos.pnl_lamports < 0

    def test_price_jump_between_signal_and_entry_rejects_the_entry(self, replay, session_factory):
        pos = positions_by_session(session_factory)["ranking"]["IOTA"]
        assert pos.status == "rejected" and pos.reject_reason == "SLIPPAGE_EXCEEDED"
        with session_factory() as s:
            ledger = list(s.scalars(select(orm.PaperLedger).where(orm.PaperLedger.position_id == pos.id)))
        kinds = sorted(r.kind for r in ledger)
        assert kinds == ["release", "reserve"]  # la reserva se libera: no se debita nada
        assert sum(r.amount_lamports for r in ledger if r.kind == "reserve") == sum(
            r.amount_lamports for r in ledger if r.kind == "release"
        )

    def test_failed_sale_keeps_position_open_and_retries_until_route_returns(self, replay, session_factory):
        pos = positions_by_session(session_factory)["ranking"]["OMIC"]
        assert pos.status == "closed"
        assert pos.exit_failures >= 3
        with session_factory() as s:
            attempts = list(
                s.scalars(
                    select(orm.PaperAttempt)
                    .where(orm.PaperAttempt.position_id == pos.id, orm.PaperAttempt.kind == "exit")
                    .order_by(orm.PaperAttempt.number)
                )
            )
        statuses = [a.status for a in attempts]
        assert statuses[-1] == "applied" and set(statuses[:-1]) == {"failed"}
        assert {a.failure_cause for a in attempts if a.status == "failed"} == {"NO_SELL_ROUTE"}

    def test_exit_that_slips_is_retried_with_wider_tolerance_and_then_fills(self, replay, session_factory):
        # THET cae entre el disparo y la ejecución: las primeras ventas exceden la tolerancia y el
        # reintento, con más slippage permitido, cierra la posición.
        pos = positions_by_session(session_factory)["ranking"]["THET"]
        with session_factory() as s:
            attempts = list(
                s.scalars(
                    select(orm.PaperAttempt)
                    .where(orm.PaperAttempt.position_id == pos.id, orm.PaperAttempt.kind == "exit")
                    .order_by(orm.PaperAttempt.number)
                )
            )
        assert pos.status == "closed" and pos.exit_failures == len(attempts) - 1 >= 1
        assert {a.failure_cause for a in attempts if a.status == "failed"} == {"SLIPPAGE_EXCEEDED"}
        slippage = [a.detail["slippage_bps"] for a in attempts]
        assert slippage == sorted(slippage) and slippage[-1] > slippage[0]

    def test_stuck_position_stays_open_illiquid_with_zero_recovery(self, replay, session_factory):
        pos = positions_by_session(session_factory)["ranking"]["KAPA"]
        assert pos.status == "open"
        assert pos.illiquid is True
        assert pos.last_recoverable_lamports == 0
        assert pos.exit_failures >= 3

    def test_capital_ledger_balances_for_every_session(self, replay, session_factory):
        with session_factory() as s:
            sessions = list(s.scalars(select(orm.PaperSession)))
        with replay.worker.uow() as u:
            for sess in sessions:
                state = u.sessions.state(sess.id)
                with session_factory() as s:
                    open_debits = s.scalar(
                        select(func.coalesce(func.count(), 0)).where(
                            orm.PaperPosition.session_id == sess.id, orm.PaperPosition.status == "pending_entry"
                        )
                    )
                assert open_debits == 0, "no deben quedar entradas pendientes al final"
                assert state.reserved_lamports == 0, "no debe quedar capital reservado sin posición"
                # cash = capital + cobros − débitos; coincide con capital + PnL realizado − costo de las abiertas
                assert state.cash_lamports == state.capital_lamports + state.realized_pnl_lamports - state.open_cost_basis_lamports


class TestIdempotency:
    def test_replaying_twice_keeps_one_evaluation_per_window(self, uow, session_factory):
        h = Harness(uow, only={"alpha_valid", "beta_valid", "gamma_liquid_loser"})
        h.run_all()
        with session_factory() as s:
            first = (
                s.scalar(select(func.count()).select_from(orm.Evaluation)),
                s.scalar(select(func.count()).select_from(orm.ChainEvent)),
                s.scalar(select(func.count()).select_from(orm.PaperPosition)),
                s.scalar(select(func.count()).select_from(orm.Signal)),
                s.scalar(select(func.count()).select_from(orm.PaperLedger)),
            )
        assert first[0] > 0 and first[1] == 3 and first[3] == 3

        # reprocesa desde cero con la misma base: se borra el cursor y se vuelve al instante inicial
        with uow() as u:
            u.session.query(orm.SourceCheckpoint).delete()
        h.clock.set(h.library.t0)
        h.run_all()
        with session_factory() as s:
            second = (
                s.scalar(select(func.count()).select_from(orm.Evaluation)),
                s.scalar(select(func.count()).select_from(orm.ChainEvent)),
                s.scalar(select(func.count()).select_from(orm.PaperPosition)),
                s.scalar(select(func.count()).select_from(orm.Signal)),
                s.scalar(select(func.count()).select_from(orm.PaperLedger)),
            )
            dup_windows = s.execute(
                select(orm.Evaluation.pool, orm.Evaluation.window_index, func.count())
                .group_by(orm.Evaluation.pool, orm.Evaluation.window_index, orm.Evaluation.strategy_version_id)
                .having(func.count() > 1)
            ).all()
        assert second == first
        assert dup_windows == []

    def test_one_migration_reprocessed_produces_one_event(self, uow, session_factory):
        h = Harness(uow, only={"alpha_valid"})
        h.run(60)
        with uow() as u:
            u.checkpoints.save_cursor("fixtures", "migrations", None, None, h.clock.now())  # cursor perdido
        h.run(30)
        with session_factory() as s:
            assert s.scalar(select(func.count()).select_from(orm.ChainEvent)) == 1
            assert s.scalar(select(func.count()).select_from(orm.Pool)) == 1
