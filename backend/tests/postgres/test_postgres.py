from __future__ import annotations

import importlib.util
import threading
import time
from collections import defaultdict
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from alembic import command
from sqlalchemy import func, inspect, select, text
from sqlalchemy.exc import IntegrityError

from app.application.evaluate_pool import evaluate_pools
from app.application.ingest_migrations import ingest_migrations
from app.application.process_position import admit_signals, execute_entry, execute_exit
from app.domain.execution import AttemptState
from app.infrastructure import orm
from tests.harness import Harness, fixtures_strategy
from tests.integration.test_execution import CLAIM_TTL_S, World
from tests.integration.test_replay_pipeline import positions_by_session

NOW = datetime(2026, 3, 1, 0, 0, 0, tzinfo=UTC)


def test_migration_creates_native_postgres_types(pg_engine):
    insp = inspect(pg_engine)
    cols = {c["name"]: c for c in insp.get_columns("paper_positions")}
    assert str(cols["debited_lamports"]["type"]) == "NUMERIC(40, 0)"
    assert str(cols["entry_price"]["type"]) == "NUMERIC(38, 18)"
    assert "TIMESTAMP" in str(cols["opened_at"]["type"]) and cols["opened_at"]["type"].timezone is True
    assert "JSONB" in str(insp.get_columns("evaluations")[[c["name"] for c in insp.get_columns("evaluations")].index("filters")]["type"])


def test_big_integers_decimals_and_json_roundtrip_exactly(pg_uow, pg_sessions):
    huge = 18_446_744_073_709_551_615  # u64 máximo: no cabe en float ni en BIGINT con signo
    with pg_uow() as u:
        u.quotes.save(
            __import__("tests.helpers", fromlist=["make_quote"]).make_quote(
                "buy", in_amount=huge, out_amount=huge - 1, observed_at=NOW
            ),
            pool=None,
            purpose="evaluation",
        )
    with pg_sessions() as s:
        row = s.scalar(select(orm.QuoteRow))
        assert row.in_amount == huge and row.out_amount == huge - 1 and isinstance(row.in_amount, int)
        assert s.execute(text("SELECT in_amount::text FROM quotes")).scalar() == str(huge)
        assert row.observed_at == NOW and row.observed_at.tzinfo is not None


def test_full_replay_on_postgres_matches_the_reference_outcomes(pg_uow, pg_sessions):
    h = Harness(pg_uow)
    h.run_all()
    pos = positions_by_session(pg_sessions)
    assert pos["ranking"]["ALFA"].status == "closed" and pos["ranking"]["ALFA"].pnl_lamports > 0
    assert pos["ranking"]["GAMA"].reject_reason == "POSITION_LIMIT"
    assert pos["liquidity"]["GAMA"].status == "closed" and pos["liquidity"]["GAMA"].pnl_lamports < 0
    assert pos["ranking"]["IOTA"].reject_reason == "SLIPPAGE_EXCEEDED"
    assert pos["ranking"]["KAPA"].status == "open" and pos["ranking"]["KAPA"].illiquid is True
    with pg_sessions() as s:
        assert s.scalar(select(func.count()).select_from(orm.Evaluation)) > 100
        dup = s.execute(
            select(orm.Evaluation.pool, orm.Evaluation.window_index)
            .group_by(orm.Evaluation.pool, orm.Evaluation.window_index, orm.Evaluation.strategy_version_id)
            .having(func.count() > 1)
        ).all()
        assert dup == []


def test_two_workers_never_open_the_same_signal_twice(pg_uow, pg_sessions):
    """Etapa 4: el bloqueo de la fila de sesión serializa a los workers; cada señal se admite una vez."""
    h = Harness(pg_uow, only={"alpha_valid", "beta_valid", "gamma_liquid_loser"})
    h.run(150)  # justo antes de la confirmación (ventana 9)
    h.clock.advance(15)
    from app.application.ingest_migrations import ingest_migrations

    ingest_migrations(uow_factory=pg_uow, source=h.worker.migrations, clock=h.clock)
    evaluate_pools(
        uow_factory=pg_uow, market=h.worker.market, quotes=h.worker.quotes, clock=h.clock,
        version_id=h.worker.version_id, cfg=h.cfg,
    )  # fmt: skip
    with pg_sessions() as s:
        assert s.scalar(select(func.count()).select_from(orm.Signal)) == 3
        session_id = s.scalar(select(orm.PaperSession.id).where(orm.PaperSession.policy == "ranking"))

    errors: list[BaseException] = []
    barrier = threading.Barrier(4)

    def worker():
        try:
            barrier.wait(timeout=10)
            admit_signals(uow_factory=pg_uow, clock=h.clock, session_id=session_id, cfg=h.cfg)
        except BaseException as exc:  # noqa: BLE001
            errors.append(exc)

    threads = [threading.Thread(target=worker) for _ in range(4)]
    [t.start() for t in threads]
    [t.join(30) for t in threads]
    assert errors == []

    with pg_sessions() as s:
        positions = list(s.scalars(select(orm.PaperPosition).where(orm.PaperPosition.session_id == session_id)))
        reserves = list(s.scalars(select(orm.PaperLedger).where(orm.PaperLedger.session_id == session_id, orm.PaperLedger.kind == "reserve")))
    assert len(positions) == 3  # una por señal, aunque cuatro workers compitieron
    assert sorted(p.status for p in positions) == ["pending_entry", "pending_entry", "rejected"]
    assert len(reserves) == 2 and sum(r.amount_lamports for r in reserves) == 200_000_000


def test_two_workers_executing_sibling_entries_apply_each_one_once(pg_uow, pg_sessions):
    """Cada worker toma el intento de una sesión: una sola cotización se aplica a las dos, sin duplicar capital."""
    h = Harness(pg_uow, only={"alpha_valid"})
    assert any(h.run(15)[0].admitted == 2 for _ in range(80))
    h.clock.advance(5)
    with pg_sessions() as s:
        attempt_ids = list(s.scalars(select(orm.PaperAttempt.id).where(orm.PaperAttempt.kind == "entry")))
    assert len(attempt_ids) == 2

    errors: list[BaseException] = []
    results: list[str] = []
    barrier = threading.Barrier(2)

    def worker(attempt_id: int):
        try:
            barrier.wait(timeout=10)
            results.append(execute_entry(
                uow_factory=pg_uow, quotes=h.worker.quotes, fx=None, clock=h.clock,
                cfg_loader=h.worker.cfg_for, attempt_id=attempt_id,
            ))  # fmt: skip
        except BaseException as exc:  # noqa: BLE001
            errors.append(exc)

    threads = [threading.Thread(target=worker, args=(i,)) for i in attempt_ids]
    [t.start() for t in threads]
    [t.join(30) for t in threads]
    assert errors == [] and sorted(results) == ["noop", "opened"]

    with pg_sessions() as s:
        attempts = list(s.scalars(select(orm.PaperAttempt).where(orm.PaperAttempt.id.in_(attempt_ids))))
        ledger = list(s.scalars(select(orm.PaperLedger)))
    assert {a.status for a in attempts} == {"applied"} and len({a.quote_id for a in attempts}) == 1
    assert sorted(r.kind for r in ledger) == ["debit", "debit", "release", "release", "reserve", "reserve"]


def run_together(action, workers: int) -> tuple[list, list[BaseException]]:
    results: list = []
    errors: list[BaseException] = []
    barrier = threading.Barrier(workers)

    def run() -> None:
        try:
            barrier.wait(timeout=10)
            results.append(action())
        except BaseException as exc:  # noqa: BLE001
            errors.append(exc)

    threads = [threading.Thread(target=run) for _ in range(workers)]
    [t.start() for t in threads]
    [t.join(30) for t in threads]
    return results, errors


def test_two_workers_executing_sibling_exits_credit_each_one_once(pg_uow, pg_sessions):
    h = Harness(pg_uow, only={"alpha_valid"})
    assert any(h.run(15)[0].admitted == 2 for _ in range(80))
    for _ in range(80):  # hasta que ambas tengan su salida programada
        with pg_sessions() as s:
            attempts = list(s.scalars(select(orm.PaperAttempt).where(
                orm.PaperAttempt.kind == "exit", orm.PaperAttempt.status == "pending")))  # fmt: skip
        if len(attempts) == 2:
            break
        h.run(15)
    assert len(attempts) == 2 and attempts[0].scheduled_at == attempts[1].scheduled_at
    h.clock.advance(5)

    errors: list[BaseException] = []
    results: list[str] = []
    barrier = threading.Barrier(2)

    def worker(attempt_id: int):
        try:
            barrier.wait(timeout=10)
            results.append(execute_exit(
                uow_factory=pg_uow, quotes=h.worker.quotes, fx=None, clock=h.clock,
                cfg_loader=h.worker.cfg_for, attempt_id=attempt_id,
            ))  # fmt: skip
        except BaseException as exc:  # noqa: BLE001
            errors.append(exc)

    threads = [threading.Thread(target=worker, args=(a.id,)) for a in attempts]
    [t.start() for t in threads]
    [t.join(30) for t in threads]
    assert errors == [] and sorted(results) == ["closed", "noop"]

    with pg_sessions() as s:
        done = list(s.scalars(select(orm.PaperAttempt).where(orm.PaperAttempt.id.in_([a.id for a in attempts]))))
        credits = list(s.scalars(select(orm.PaperLedger).where(orm.PaperLedger.kind == "credit")))
        pnl = list(s.scalars(select(orm.PaperPosition.pnl_lamports)))
    assert len({a.quote_id for a in done}) == 1 and len(credits) == 2 and pnl[0] == pnl[1]


def test_two_workers_reserving_the_same_signal_create_one_intent(pg_uow, pg_sessions):
    world = World(pg_uow, pg_sessions)
    results, errors = run_together(lambda: world.buy(), 4)
    assert errors == [] and len({r.intent_id for r in results}) == 1 and sum(r.created for r in results) == 1
    assert len(world.rows(orm.ExecutionIntent)) == 1 and world.reserved() == 105_000_000


def test_two_workers_advancing_one_intent_ask_sign_and_send_once(pg_uow, pg_sessions):
    """El hallazgo de la revisión: sin reclamo previo, ambos pedirían la orden en paralelo."""
    world = World(pg_uow, pg_sessions)
    intent_id = world.buy().intent_id
    world.gateway.prepare_hook = lambda: time.sleep(0.4)  # proveedor lento: los dos workers coinciden
    results, errors = run_together(lambda: world.executor().advance(intent_id), 4)
    assert errors == [] and sorted(results) == ["done", "skipped", "skipped", "skipped"]
    assert len(world.gateway.prepared) == len(world.gateway.signed) == len(world.gateway.sent) == 1
    assert len(world.rows(orm.LivePosition)) == 1


def test_a_worker_that_lost_its_claim_never_sends_its_order(pg_uow, pg_sessions):
    world = World(pg_uow, pg_sessions)
    intent_id = world.buy().intent_id
    asked, resume = threading.Event(), threading.Event()
    outcome: list[str] = []

    def stall() -> None:
        world.gateway.prepare_hook = None  # solo el primer pedido queda colgado
        asked.set()
        assert resume.wait(10)

    world.gateway.prepare_hook = stall
    slow = threading.Thread(target=lambda: outcome.append(world.executor().advance(intent_id)))
    slow.start()
    assert asked.wait(10)
    world.h.clock.advance(CLAIM_TTL_S + 1)  # vence su reclamo mientras espera al proveedor
    assert world.executor().advance(intent_id) == "done"
    resume.set()
    slow.join(10)

    assert outcome == ["claim_lost"]
    first, second = world.rows(orm.ExecutionAttempt)
    assert (first.state, first.signature) == ("expired", None) and second.state == "confirmed"
    assert world.gateway.sent == [second.signature] and len(world.rows(orm.LivePosition)) == 1


def test_the_database_itself_refuses_a_second_open_attempt_or_a_second_sale(pg_uow, pg_sessions):
    world = World(pg_uow, pg_sessions)
    intent_id = world.buy().intent_id
    with pg_uow() as u:
        u.execution.create_attempt(intent_id, "fake", world.h.clock.now())
    with pytest.raises(IntegrityError), pg_uow() as u:
        u.execution.create_attempt(intent_id, "fake", world.h.clock.now())
    with pg_uow() as u:
        u.execution.move_attempt(u.execution.open_attempt(intent_id).id, AttemptState.EXPIRED, world.h.clock.now())

    position = world.bought()
    first = world.sell(position.id)
    results, errors = run_together(lambda: world.sell(position.id), 4)
    assert errors == [] and {r.intent_id for r in results} == {first.intent_id}
    with pg_sessions() as s, pytest.raises(IntegrityError):
        row = s.get(orm.ExecutionIntent, first.intent_id)
        table = orm.ExecutionIntent.__table__
        copy = {c.name: getattr(row, c.name) for c in table.columns if c.name != "id"}
        s.execute(table.insert().values(copy | {"idempotency_key": "otra"}))


def load_measurement_script():
    path = Path(__file__).resolve().parents[3] / "scripts" / "medir_live.py"
    spec = importlib.util.spec_from_file_location("medir_live", path)
    script = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(script)
    return script


def measure(script, pg_engine) -> tuple[dict, str]:
    with pg_engine.connect() as conn:
        data = conn.exec_driver_sql(script.SQL).scalar_one()
    return data, script.report({"hora": "x", "base": data, "log": defaultdict(int)}, None, [])


@pytest.mark.parametrize("orden_distinto", [True, False], ids=["eligen_distinto", "eligen_igual"])
def test_report_says_whether_the_selection_order_changed_the_decision(pg_uow, pg_sessions, pg_engine, orden_distinto):
    """Un solo lugar y dos señales a la vez: Ranking ordena por puntaje y Liquidez por liquidez."""
    h = Harness(pg_uow, only={"alpha_valid", "beta_valid"}, cfg=fixtures_strategy(max_positions=1))
    h.run(150)
    h.clock.advance(15)
    ingest_migrations(uow_factory=pg_uow, source=h.worker.migrations, clock=h.clock)
    evaluate_pools(
        uow_factory=pg_uow, market=h.worker.market, quotes=h.worker.quotes, clock=h.clock,
        version_id=h.worker.version_id, cfg=h.cfg,
    )  # fmt: skip
    with pg_sessions() as s:
        first, second = s.scalars(select(orm.Signal).order_by(orm.Signal.id)).all()
        first.score, second.score = 90, 10
        first.liquidity_lamports = 1_000 if orden_distinto else 2_000_000
        second.liquidity_lamports = 1_000_000 if orden_distinto else 1_000
        s.commit()
        chosen_by_ranking, other = first.pool[:6], second.pool[:6]
    with pg_uow() as u:
        sessions = u.sessions.list_for_version(h.worker.version_id)
    for session in sessions:
        admit_signals(uow_factory=pg_uow, clock=h.clock, session_id=session.id, cfg=h.cfg, at=h.clock.now())

    data, report = measure(load_measurement_script(), pg_engine)
    summary = data["seleccion"]
    assert summary["ciclos"] == 1
    if orden_distinto:
        assert summary["orden"] == 1 and summary["misma_eleccion_con_competencia"] == 0
        cases = {c["sesion"]: c["pool"] for c in data["casos_orden"]}
        assert cases == {"ranking": chosen_by_ranking, "liquidity": other}
        assert "El orden de selección cambió la decisión en 1 ciclo" in report and other in report
    else:
        assert summary["orden"] == 0 and summary["misma_eleccion_con_competencia"] == 1
        assert data["casos_orden"] is None
        assert "Todavía no hay casos para medir qué aporta el Ranking" in report


def test_measurement_script_reports_delays_and_shared_execution(pg_uow, pg_engine):
    """La consulta de ``scripts/medir_live.py`` corre sobre PostgreSQL: demoras y ejecución compartida."""
    script = load_measurement_script()
    h = Harness(pg_uow, only={"alpha_valid"})
    h.run_all()

    data, report = measure(script, pg_engine)
    delays = data["demoras_entrada"]
    assert delays["cotizaciones"] == 1 and delays["compartidas"] == 1
    assert delays["senal_a_turno"] == [h.cfg.entry_delay_s] * 2
    # el arnés avanza de a 15 s: el worker llega 10 s después del turno y las cotizaciones de fixtures no tardan
    assert delays["turno_a_pedido"] == [10, 10] and delays["pedido_a_respuesta"] == [0, 0]
    assert "## 5. Demora de las entradas" in report and "| Turno → pedido de la cotización | 10.0 s | 10.0 s |" in report

    assert data["parejas"] == {"misma_entrada": 1, "cerradas": 1, "mismo_pnl": 1}
    assert data["salidas"]["intentos"] == data["salidas"]["compartidos"] == 2 and data["salidas"]["solos"] is None
    assert data["seleccion"]["sin_competencia"] == data["seleccion"]["ciclos"] == 1
    assert "1 de 1 terminaron con el mismo PnL" in report


def test_job_claim_skips_locked_rows_across_connections(pg_uow, pg_sessions):
    with pg_uow() as u:
        for i in range(6):
            u.jobs.enqueue(type="noop", payload={"i": i}, key=f"k{i}", run_at=NOW, now=NOW)
    first = pg_sessions()
    second = pg_sessions()
    try:
        from app.infrastructure.repositories import JobRepo

        a = JobRepo(first).claim(worker_id="a", now=NOW, limit=3, claim_ttl_s=60)  # transacción abierta
        b = JobRepo(second).claim(worker_id="b", now=NOW, limit=3, claim_ttl_s=60)  # no debe esperar ni repetir
        assert len(a) == 3 and len(b) == 3
        assert {j.id for j in a}.isdisjoint({j.id for j in b})
        first.commit()
        second.commit()
    finally:
        first.close()
        second.close()


def test_idempotent_enqueue_and_expired_claims_are_retaken(pg_uow):
    with pg_uow() as u:
        assert u.jobs.enqueue(type="noop", payload={}, key="same", run_at=NOW, now=NOW) is True
        assert u.jobs.enqueue(type="noop", payload={}, key="same", run_at=NOW, now=NOW) is False
    with pg_uow() as u:
        (job,) = u.jobs.claim(worker_id="w1", now=NOW, limit=5, claim_ttl_s=30)
    with pg_uow() as u:
        assert u.jobs.claim(worker_id="w2", now=NOW + timedelta(seconds=10), limit=5, claim_ttl_s=30) == []
    with pg_uow() as u:  # el worker murió: vence el reclamo y otro lo retoma
        (again,) = u.jobs.claim(worker_id="w2", now=NOW + timedelta(seconds=31), limit=5, claim_ttl_s=30)
    assert again.id == job.id and again.attempts == 2


def test_downgrade_then_upgrade_is_repeatable(pg_engine, monkeypatch):
    from tests.integration.test_migrations import alembic_cfg

    cfg = alembic_cfg()
    command.downgrade(cfg, "base")
    assert set(inspect(pg_engine).get_table_names()) <= {"alembic_version"}
    command.upgrade(cfg, "head")
    assert "paper_ledger" in inspect(pg_engine).get_table_names()
