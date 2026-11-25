"""Ejecutor de trabajos respaldado por PostgreSQL.

Un trabajo se reclama con ``FOR UPDATE SKIP LOCKED`` y un vencimiento de reclamo: si el worker muere,
otro lo retoma al vencer. Los errores transitorios se reintentan con espera creciente; los permanentes
o tras agotar intentos pasan a ``dead`` y quedan visibles para operación.
"""

from __future__ import annotations

import logging
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Any

from app.application.ports import Clock, PermanentSourceError, UnitOfWorkFactory

log = logging.getLogger(__name__)

Handler = Callable[[dict[str, Any]], Any]


@dataclass(frozen=True)
class JobRunResult:
    done: int = 0
    retried: int = 0
    dead: int = 0


class JobRunner:
    def __init__(
        self,
        *,
        uow_factory: UnitOfWorkFactory,
        clock: Clock,
        worker_id: str,
        handlers: Mapping[str, Handler],
        claim_ttl_s: int = 60,
    ) -> None:
        self._uow = uow_factory
        self._clock = clock
        self._worker_id = worker_id
        self._handlers = dict(handlers)
        self._ttl = claim_ttl_s

    def run_due(self, limit: int = 25) -> JobRunResult:
        with self._uow() as uow:
            jobs = uow.jobs.claim(worker_id=self._worker_id, now=self._clock.now(), limit=limit, claim_ttl_s=self._ttl)
        done = retried = dead = 0
        for job in jobs:
            handler = self._handlers.get(job.type)
            try:
                if handler is None:
                    raise PermanentSourceError(f"tipo de trabajo desconocido: {job.type}")
                handler(job.payload)
            except (PermanentSourceError, ValueError, KeyError, AssertionError) as exc:
                log.error("trabajo %s (%s) falló sin reintento: %s", job.id, job.type, exc)
                with self._uow() as uow:
                    uow.jobs.fail(job.id, self._clock.now(), f"{type(exc).__name__}: {exc}", permanent=True)
                dead += 1
            except Exception as exc:
                log.warning("trabajo %s (%s) falló, se reintentará: %s", job.id, job.type, exc)
                with self._uow() as uow:
                    status = uow.jobs.fail(job.id, self._clock.now(), f"{type(exc).__name__}: {exc}")
                if status == "dead":
                    dead += 1
                else:
                    retried += 1
            else:
                with self._uow() as uow:
                    uow.jobs.complete(job.id, self._clock.now())
                done += 1
        return JobRunResult(done, retried, dead)
