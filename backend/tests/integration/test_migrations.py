"""La migración de Alembic y los modelos ORM no pueden desalinearse."""

from __future__ import annotations

from pathlib import Path

from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine, inspect

from app.infrastructure.orm import Base

BACKEND = Path(__file__).resolve().parents[2]


def alembic_cfg() -> Config:
    cfg = Config(str(BACKEND / "alembic.ini"))
    cfg.set_main_option("script_location", str(BACKEND / "migrations"))
    return cfg


def test_upgrade_matches_models_and_downgrade_is_clean(tmp_path, monkeypatch):
    url = f"sqlite:///{tmp_path / 'migrated.db'}"
    monkeypatch.setenv("DATABASE_URL", url)
    cfg = alembic_cfg()

    command.upgrade(cfg, "head")
    insp = inspect(create_engine(url))
    tables = set(insp.get_table_names()) - {"alembic_version"}
    assert tables == set(Base.metadata.tables)
    for name, table in Base.metadata.tables.items():
        assert {c["name"] for c in insp.get_columns(name)} == {c.name for c in table.columns}, name

    command.check(cfg)  # falla si autogenerate detectaría diferencias pendientes

    command.downgrade(cfg, "base")
    assert set(inspect(create_engine(url)).get_table_names()) <= {"alembic_version"}


def test_unique_constraints_that_guarantee_idempotency_exist(tmp_path, monkeypatch):
    url = f"sqlite:///{tmp_path / 'u.db'}"
    monkeypatch.setenv("DATABASE_URL", url)
    command.upgrade(alembic_cfg(), "head")
    insp = inspect(create_engine(url))

    def uniques(table: str) -> set[tuple[str, ...]]:
        return {tuple(u["column_names"]) for u in insp.get_unique_constraints(table)}

    assert ("signature", "instruction_index", "event_index") in uniques("chain_events")
    assert ("pool", "window_index", "strategy_version_id") in uniques("evaluations")
    assert ("session_id", "signal_id") in uniques("paper_positions")
    assert ("attempt_id", "kind") in uniques("paper_ledger")
    assert ("idempotency_key",) in uniques("jobs")
    assert ("pool", "window_index") in uniques("market_snapshots")
