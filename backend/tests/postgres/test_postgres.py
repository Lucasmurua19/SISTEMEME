from __future__ import annotations

import threading
from datetime import UTC, datetime, timedelta

from alembic import command
from sqlalchemy import func, inspect, select, text

from app.application.evaluate_pool import evaluate_pools
from app.application.process_position import admit_signals
from app.infrastructure import orm
from tests.harness import Harness
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
