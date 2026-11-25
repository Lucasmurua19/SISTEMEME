"""Entorno de Alembic. La URL sale de ``DATABASE_URL`` y nunca se registra."""

from __future__ import annotations

import os
from logging.config import fileConfig

from alembic import context
from sqlalchemy import engine_from_config, pool

from app.infrastructure import orm

config = context.config
if config.config_file_name is not None:
    fileConfig(config.config_file_name, disable_existing_loggers=False)

target_metadata = orm.Base.metadata


def database_url() -> str:
    url = os.environ.get("DATABASE_URL") or config.get_main_option("sqlalchemy.url")
    if not url:
        raise RuntimeError("DATABASE_URL no está definida")
    return url


def render_item(type_: str, obj, autogen_context):
    """Los tipos propios se escriben como tipos nativos: la migración no importa código de la app."""
    if type_ == "type":
        if isinstance(obj, orm.UTCDateTime):
            return "sa.DateTime(timezone=True)"
        if isinstance(obj, orm.BigInt):
            return "sa.Numeric(precision=40, scale=0)"
        if isinstance(obj, orm.Price):
            return "sa.Numeric(precision=38, scale=18)"
    return False


def compare_type(context, inspected_column, metadata_column, inspected_type, metadata_type):
    """BigInt/Price/UTCDateTime cambian de representación según el dialecto (texto en SQLite, NUMERIC en
    PostgreSQL); la migración los fija como tipos nativos, así que no se comparan entre dialectos."""
    if isinstance(metadata_type, (orm.BigInt, orm.Price, orm.UTCDateTime)):
        return False
    return None  # comparación estándar de Alembic


def run_migrations_offline() -> None:
    context.configure(
        url=database_url(),
        target_metadata=target_metadata,
        literal_binds=True,
        compare_type=compare_type,
        render_item=render_item,
    )
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    cfg = config.get_section(config.config_ini_section, {})
    cfg["sqlalchemy.url"] = database_url()
    connectable = engine_from_config(cfg, prefix="sqlalchemy.", poolclass=pool.NullPool)
    with connectable.connect() as connection:
        context.configure(
            connection=connection,
            target_metadata=target_metadata,
            compare_type=compare_type,
            render_item=render_item,
            render_as_batch=connection.dialect.name == "sqlite",
        )
        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
