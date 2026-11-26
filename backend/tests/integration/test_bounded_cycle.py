"""Ciclo acotado: un RPC lento no debe bloquear el latido, los trabajos ni el seguimiento de posiciones abiertas.

Defecto hallado en la primera corrida live real: al entrar una migración, un ciclo tardó 96 s (ingesta y evaluación
contra un RPC público con límite de tasa), sin latido durante todo ese tiempo, y el panel mostraba «worker caído».
"""

from __future__ import annotations

from datetime import timedelta

import pytest
from sqlalchemy import select

from app.application.evaluate_pool import evaluate_pools
from app.application.health import HEARTBEAT_STREAM, WORKER_SOURCE
from app.infrastructure import orm
from tests.harness import Harness


@pytest.fixture()
def busy(uow):
    """Varios pools activos a la vez: se avanza hasta que haya al menos tres sin evaluar la ventana siguiente."""
    h = Harness(uow)
    h.run(240)
    return h


def active_pools(uow, h):
    with uow() as u:
        return u.chain.list_active_pools(h.clock.now(), h.cfg.max_age_after_migration_s + 2 * h.cfg.window_seconds)


def evaluate(uow, h, **kw):
    h.clock.advance(h.cfg.window_seconds)  # una ventana nueva por pool: hay trabajo para todos
    return evaluate_pools(
        uow_factory=uow, market=h.market, quotes=h.quotes, clock=h.clock, version_id=h.worker.version_id, cfg=h.cfg, **kw
    )


class TestEvaluationBudget:
    def test_without_budget_every_active_pool_is_visited_in_a_stable_order(self, uow, busy):
        pools = active_pools(uow, busy)
        assert len(pools) >= 3
        out = evaluate(uow, busy)
        assert len(out) >= len(pools)  # sin tope, recorrido completo (fixtures)

    def test_budget_stops_starting_new_pools_but_always_makes_progress(self, uow, busy):
        ticks = {"n": 0}

        def mono():  # cada consulta de reloj "gasta" 10 s: tras el primer pool el presupuesto de 5 s ya se agotó
            ticks["n"] += 1
            return ticks["n"] * 10.0

        seen: list[int] = []
        out = evaluate(uow, busy, budget_s=5.0, monotonic=mono, shuffle=lambda pools: None, progress=lambda: seen.append(1))
        assert len(out) == 1  # al menos un pool por ciclo: nunca se queda sin avanzar
        assert seen == [1]  # el latido se emitió después de ese pool

    def test_budget_shuffles_so_the_last_pools_are_not_starved(self, uow, busy):
        seen_orders: list[list[str]] = []

        def spy(pools):
            seen_orders.append([p.address for p in pools])
            pools.reverse()

        evaluate(uow, busy, budget_s=3600.0, shuffle=spy)
        assert len(seen_orders) == 1 and len(seen_orders[0]) >= 3

    def test_transient_source_failure_is_a_one_line_warning_and_does_not_stop_other_pools(self, uow, busy, caplog):
        from app.application.ports import TransientSourceError

        inner = busy.market._inner
        real = inner.get_snapshots
        calls = {"n": 0}

        def flaky(pool, now):
            calls["n"] += 1
            if calls["n"] == 1:
                raise TransientSourceError("getTransaction: límite de espera agotado (HTTP 429)")
            return real(pool, now)

        inner.get_snapshots = flaky  # type: ignore[method-assign]
        with caplog.at_level("WARNING", logger="app.application.evaluate_pool"):
            out = evaluate(uow, busy)
        assert out[0].error and out[0].error.startswith("TransientSourceError")
        assert len(out) >= 3 and all(o.error is None for o in out[1:])  # el resto de los pools siguió
        records = [r for r in caplog.records if "fuente transitoria" in r.message]
        assert len(records) == 1 and records[0].exc_info is None  # sin traceback: es un caso esperado
        assert not any(r.levelname == "ERROR" for r in caplog.records)

    def test_no_budget_means_no_shuffle(self, uow, busy):
        def boom(pools):
            raise AssertionError("sin presupuesto el orden debe ser determinista")

        evaluate(uow, busy, shuffle=boom)


class TestHeartbeat:
    def heartbeat(self, uow):
        with uow() as u:
            return u.checkpoints.get(WORKER_SOURCE, HEARTBEAT_STREAM)

    def test_cycle_reports_step_timings_and_beats(self, uow):
        h = Harness(uow)
        rep = h.run(30)[-1]
        assert {"ingest", "evaluate", "jobs", "monitor"} <= set(rep.timings)
        assert all(v >= 0 for v in rep.timings.values())
        assert self.heartbeat(uow) is not None

    def test_heartbeat_is_emitted_between_pools_during_a_long_evaluation(self, uow, session_factory):
        h = Harness(uow)
        h.run(240)
        beats: list[object] = []
        original = h.worker._heartbeat

        def counting():
            original()
            beats.append(1)

        h.worker._heartbeat = counting  # type: ignore[method-assign]
        h.clock.advance(15)
        rep = h.worker.run_cycle()
        pools = len(active_pools(uow, h))
        # ingesta + 1 por pool evaluado + trabajos + cierre del ciclo
        assert len(beats) >= pools + 3
        assert rep.errors == []

    def test_slow_step_is_logged_as_a_warning(self, uow, caplog):
        h = Harness(uow)
        h.worker.slow_step_s = -1.0  # cualquier paso cuenta como lento
        with caplog.at_level("WARNING", logger="radar.worker"):
            h.run(15)
        assert any("tardó" in r.message and "fuente lenta" in r.message for r in caplog.records)


def test_fixtures_replay_is_unchanged_by_the_new_budget_parameters(uow, session_factory):
    """Contrato: sin presupuesto el resultado es el de siempre (la reproducción es determinista)."""
    h = Harness(uow)
    h.run_all()
    with session_factory() as s:
        sessions = {x.policy: x for x in s.scalars(select(orm.PaperSession))}
        assert set(sessions) == {"ranking", "liquidity"}
        closed = s.scalars(select(orm.PaperPosition).where(orm.PaperPosition.status == "closed")).all()
        assert len(closed) >= 4
    assert h.clock.now() - h.library.t0 > timedelta(seconds=h.horizon_s)
