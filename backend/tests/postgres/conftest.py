"""Pruebas contra PostgreSQL real.

Usa ``TEST_DATABASE_URL`` si está definida (p. ej. el servicio ``db`` de Compose o un servicio de CI);
si no, levanta un PostgreSQL embebido con ``pgserver``. Si ninguna opción está disponible, se omiten.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import text

from app.infrastructure.db import make_engine, make_session_factory, uow_factory

BACKEND = Path(__file__).resolve().parents[2]


@pytest.fixture(scope="session")
def pg_url(tmp_path_factory):
    url = os.environ.get("TEST_DATABASE_URL")
    if url:
        yield url
        return
    pgserver = pytest.importorskip("pgserver", reason="instalar pgserver o definir TEST_DATABASE_URL")
    server = pgserver.get_server(tmp_path_factory.mktemp("pg"), cleanup_mode="stop")
    try:
        yield server.get_uri().replace("postgresql://", "postgresql+psycopg://", 1)
    finally:
        server.cleanup()


@pytest.fixture()
def pg_engine(pg_url, monkeypatch):
    """Esquema limpio por prueba, creado con las migraciones reales de Alembic."""
    engine = make_engine(pg_url)
    with engine.begin() as conn:
        conn.execute(text("DROP SCHEMA public CASCADE"))
        conn.execute(text("CREATE SCHEMA public"))
    monkeypatch.setenv("DATABASE_URL", pg_url)
    cfg = Config(str(BACKEND / "alembic.ini"))
    cfg.set_main_option("script_location", str(BACKEND / "migrations"))
    command.upgrade(cfg, "head")
    yield engine
    engine.dispose()


@pytest.fixture()
def pg_uow(pg_engine):
    return uow_factory(make_session_factory(pg_engine))


@pytest.fixture()
def pg_sessions(pg_engine):
    return make_session_factory(pg_engine)
