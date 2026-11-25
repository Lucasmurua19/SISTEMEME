"""Ingesta de migraciones: validación, deduplicación y reconciliación.

Una migración se identifica por ``(firma, índice de instrucción, índice de evento)``. El checkpoint
avanza en la *misma transacción* que los eventos recuperados: si algo falla, ninguno de los dos
cambia y el tramo se reintenta sin huecos ni duplicados.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

from app.application.ports import Clock, MigrationSource, UnitOfWorkFactory

log = logging.getLogger(__name__)

STREAM = "migrations"


@dataclass(frozen=True)
class IngestResult:
    fetched: int
    inserted: int
    duplicates: int
    unverified: int
    cursor: str | None


def ingest_migrations(
    *, uow_factory: UnitOfWorkFactory, source: MigrationSource, clock: Clock, limit: int = 100
) -> IngestResult:
    """Recupera el tramo posterior al cursor persistido y lo registra de forma atómica."""
    with uow_factory() as uow:
        cp = uow.checkpoints.get(source.name, STREAM)
    cursor = cp.cursor if cp else None

    batch = source.fetch_migrations(cursor, limit)  # red: fuera de la transacción
    verified = [(ev, source.verify(ev)) for ev in batch.events]  # red: fuera de la transacción

    now = clock.now()
    inserted = unverified = 0
    with uow_factory() as uow:
        for ev, ver in verified:
            if uow.chain.record_migration(ev, ver, now):
                inserted += 1
                if not (ver.verified and ver.identity_ok):
                    unverified += 1
                    log.warning("migración no verificada sig=%s pool=%s motivo=%s", ev.signature, ev.pool, ver.reason)
        uow.checkpoints.save_cursor(source.name, STREAM, batch.next_cursor or cursor, batch.head_slot, now)
    return IngestResult(
        fetched=len(batch.events),
        inserted=inserted,
        duplicates=len(batch.events) - inserted,
        unverified=unverified,
        cursor=batch.next_cursor or cursor,
    )


def ingest_signature(*, uow_factory: UnitOfWorkFactory, source: MigrationSource, clock: Clock, signature: str) -> int:
    """Ingesta puntual disparada por WebSocket. No mueve el checkpoint: lo hace la reconciliación."""
    events = source.fetch_signature(signature)
    verified = [(ev, source.verify(ev)) for ev in events]
    now = clock.now()
    inserted = 0
    with uow_factory() as uow:
        for ev, ver in verified:
            inserted += int(uow.chain.record_migration(ev, ver, now))
    return inserted
