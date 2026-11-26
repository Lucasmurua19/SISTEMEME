"""Worker: ingesta, snapshots, evaluación, simulación y conciliación.

Cada ciclo ejecuta, en orden: reloj de reproducción → ingesta → evaluación → admisión de señales →
vencimiento de señales → trabajos pendientes → monitoreo de posiciones → latido. Un fallo en un paso
se registra y marca la fuente como degradada, pero no impide los pasos siguientes: el seguimiento de
posiciones abiertas nunca se corta por una falla de ingesta.
"""

from __future__ import annotations

import logging
import signal
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from app.application import health as health_mod
from app.application.evaluate_pool import evaluate_pools
from app.application.ingest_migrations import ingest_migrations, ingest_signature
from app.application.ports import Clock, FxProvider, MigrationSource, UnitOfWorkFactory
from app.application.process_position import admit_signals, execute_entry, execute_exit, monitor_positions
from app.domain.config import StrategyConfig
from app.infrastructure.clock import ReplayClock, SystemClock
from app.infrastructure.jobs import JobRunner, JobRunResult
from app.infrastructure.tracking import TrackedMarketSource, TrackedQuoteProvider

log = logging.getLogger("radar.worker")


@dataclass
class CycleReport:
    now: datetime
    ingested: int = 0
    evaluations: int = 0
    signals: int = 0
    admitted: int = 0
    rejected: int = 0
    jobs: JobRunResult = field(default_factory=JobRunResult)
    monitored: int = 0
    exits_triggered: int = 0
    errors: list[str] = field(default_factory=list)
    timings: dict[str, float] = field(default_factory=dict)  # segundos por paso


class Worker:
    def __init__(
        self,
        *,
        uow_factory: UnitOfWorkFactory,
        clock: Clock,
        migrations: MigrationSource,
        market: TrackedMarketSource,
        quotes: TrackedQuoteProvider,
        fx: FxProvider | None,
        cfg: StrategyConfig,
        worker_id: str = "worker-1",
        source_max_age_s: int = 120,
        source_max_failures: int = 3,
        job_claim_ttl_s: int = 60,
        real_clock: Clock | None = None,
        evaluation_budget_s: float | None = None,
        slow_step_s: float = 15.0,
    ) -> None:
        self.uow = uow_factory
        self.clock = clock
        self.real_clock = real_clock or SystemClock()
        self.evaluation_budget_s = evaluation_budget_s  # None = evaluar todos los pools (fixtures)
        self.slow_step_s = slow_step_s
        self.migrations = migrations
        self.market = market
        self.quotes = quotes
        self.fx = fx
        self.cfg = cfg
        self.worker_id = worker_id
        self.source_max_age_s = source_max_age_s
        self.source_max_failures = source_max_failures
        self.version_id: int = 0
        self._cfg_cache: dict[int, StrategyConfig] = {}
        self.jobs = JobRunner(
            uow_factory=uow_factory,
            clock=clock,
            worker_id=worker_id,
            claim_ttl_s=job_claim_ttl_s,
            handlers={
                "entry_attempt": self._handle_entry,
                "exit_attempt": self._handle_exit,
                "ingest_signature": self._handle_ingest_signature,
            },
        )

    # ------------------------------------------------------------------ arranque
    def bootstrap(self) -> None:
        """Registra la versión de estrategia (inmutable) y crea las dos sesiones de comparación."""
        now = self.clock.now()
        with self.uow() as uow:
            self.version_id = uow.strategies.register(self.cfg, now)
            for policy in ("ranking", "liquidity"):
                uow.sessions.ensure(
                    name=f"{policy}-{self.cfg.name}", policy=policy, version_id=self.version_id, cfg=self.cfg, now=now
                )
        self._cfg_cache[self.version_id] = self.cfg
        log.info("estrategia %s registrada (versión %s)", self.cfg.name, self.version_id)

    def cfg_for(self, version_id: int) -> StrategyConfig:
        if version_id not in self._cfg_cache:
            with self.uow() as uow:
                self._cfg_cache[version_id] = uow.strategies.get(version_id)
        return self._cfg_cache[version_id]

    # ------------------------------------------------------------------ manejadores de trabajos
    def _handle_entry(self, payload: dict[str, Any]) -> None:
        execute_entry(
            uow_factory=self.uow, quotes=self.quotes, fx=self.fx, clock=self.clock,
            cfg_loader=self.cfg_for, attempt_id=int(payload["attempt_id"]),
        )  # fmt: skip

    def _handle_exit(self, payload: dict[str, Any]) -> None:
        execute_exit(
            uow_factory=self.uow, quotes=self.quotes, fx=self.fx, clock=self.clock,
            cfg_loader=self.cfg_for, attempt_id=int(payload["attempt_id"]),
        )  # fmt: skip

    def _handle_ingest_signature(self, payload: dict[str, Any]) -> None:
        ingest_signature(
            uow_factory=self.uow, source=self.migrations, clock=self.clock, signature=str(payload["signature"])
        )

    # ------------------------------------------------------------------ ciclo
    def _guard(self, rep: CycleReport, step: str, fn: Callable[[], Any]) -> Any:
        started = time.monotonic()
        try:
            return fn()
        except Exception as exc:
            log.exception("falló el paso %s", step)
            rep.errors.append(f"{step}: {type(exc).__name__}: {exc}")
            return None
        finally:
            took = time.monotonic() - started
            rep.timings[step] = round(took, 2)
            if took > self.slow_step_s:
                log.warning("el paso %s tardó %.1f s (fuente lenta o límite de tasa del proveedor)", step, took)

    def _heartbeat(self) -> None:
        """Latido: el panel usa ``cursor`` como reloj del sistema y ``updated_at`` para saber si el worker vive.

        Se emite también durante los pasos largos (entre pools), para que un RPC lento no parezca un worker caído.
        """
        with self.uow() as uow:
            uow.checkpoints.save_cursor(
                health_mod.WORKER_SOURCE, health_mod.HEARTBEAT_STREAM,
                self.clock.now().isoformat(), None, self.real_clock.now(),
            )  # fmt: skip

    def run_cycle(self) -> CycleReport:
        if isinstance(self.clock, ReplayClock):
            self.clock.tick()
            with self.uow() as uow:
                uow.checkpoints.save_cursor("replay", "clock", self.clock.now().isoformat(), None, self.clock.now())
        now = self.clock.now()
        rep = CycleReport(now=now)
        self.market.tally.reset()
        self.quotes.tally.reset()

        # 1. ingesta (el checkpoint avanza en la misma transacción que los eventos)
        res = self._guard(
            rep, "ingest",
            lambda: ingest_migrations(uow_factory=self.uow, source=self.migrations, clock=self.clock),
        )  # fmt: skip
        if res is not None:
            rep.ingested = res.inserted
        else:
            self._mark_failure(self.migrations.name, "migrations", rep.errors[-1])
        self._heartbeat()

        # 2. snapshots, filtros, cotizaciones y ranking
        outcomes = self._guard(
            rep, "evaluate",
            lambda: evaluate_pools(
                uow_factory=self.uow, market=self.market, quotes=self.quotes, clock=self.clock,
                version_id=self.version_id, cfg=self.cfg,
                budget_s=self.evaluation_budget_s, progress=self._heartbeat,
            ),
        )  # fmt: skip
        for o in outcomes or []:
            if o.error:
                rep.errors.append(f"evaluate[{o.pool[:8]}]: {o.error}")
            elif o.evaluation_id is not None:
                rep.evaluations += 1
                rep.signals += int(o.signal_id is not None)
        self._record_tallies()

        # 3. admisión de señales en cada sesión (política de ranking y referencia por liquidez)
        blocked = self._blocked_sources(now)
        with self.uow() as uow:
            sessions = uow.sessions.list_for_version(self.version_id)
        for sess in sessions:
            admitted = self._guard(
                rep, f"admit[{sess.policy}]",
                lambda sess=sess: admit_signals(
                    uow_factory=self.uow, clock=self.clock, session_id=sess.id, cfg=self.cfg, blocked_sources=blocked
                ),
            )  # fmt: skip
            for a in admitted or []:
                rep.admitted += int(a.admitted)
                rep.rejected += int(not a.admitted)

        # 4. vencimiento de señales
        def _expire() -> None:
            with self.uow() as uow:
                uow.signals.expire_due(now)

        self._guard(rep, "expire_signals", _expire)

        # 5. trabajos pendientes: entradas y salidas simuladas, ingesta por WebSocket
        jobs = self._guard(rep, "jobs", lambda: self.jobs.run_due())
        if jobs is not None:
            rep.jobs = jobs
        self._heartbeat()

        # 6. seguimiento de posiciones abiertas (continúa aunque la sesión esté pausada o sin presupuesto)
        monitored = self._guard(
            rep, "monitor",
            lambda: monitor_positions(
                uow_factory=self.uow, quotes=self.quotes, clock=self.clock, cfg_loader=self.cfg_for
            ),
        )  # fmt: skip
        for m in monitored or []:
            rep.monitored += 1
            rep.exits_triggered += int(m.trigger is not None and m.attempt_id is not None)
        self._record_tallies()

        # 7. latido final del ciclo
        self._heartbeat()
        return rep

    # ------------------------------------------------------------------ salud
    def _mark_failure(self, source: str, stream: str, error: str) -> None:
        with self.uow() as uow:
            uow.checkpoints.mark_failure(source, stream, self.clock.now(), error)

    def _record_tallies(self) -> None:
        """Marca sana o fallida cada fuente según las llamadas del ciclo."""
        now = self.clock.now()
        with self.uow() as uow:
            m, q = self.market.tally, self.quotes.tally
            if m.failed and not m.ok:
                uow.checkpoints.mark_failure(self.market.name, "market", now, m.last_error or "error")
            else:
                uow.checkpoints.mark_ok(self.market.name, "market", now)
            cp = uow.checkpoints.get(self.quotes.name, "quotes")
            if q.failed and not q.ok:
                uow.checkpoints.mark_failure(self.quotes.name, "quotes", now, q.last_error or "error")
            elif q.ok or cp is None or cp.consecutive_failures == 0:
                # sin llamadas en el ciclo = fuente inactiva, no degradada; si venía fallando, solo
                # una cotización exitosa la recupera
                uow.checkpoints.mark_ok(self.quotes.name, "quotes", now)

    def _blocked_sources(self, now: datetime) -> frozenset[str]:
        with self.uow() as uow:
            cps = uow.checkpoints.list_all()
        sources = health_mod.assess(
            cps, now=now, max_age_s=self.source_max_age_s, max_failures=self.source_max_failures
        )
        return health_mod.blocked_entry_sources(sources)


# --------------------------------------------------------------------------- proceso
def run_forever(worker: Worker, tick_seconds: float, stop: threading.Event) -> None:
    log.info("worker iniciado (tick=%.1fs)", tick_seconds)
    while not stop.is_set():
        started = time.monotonic()
        try:
            rep = worker.run_cycle()
            if rep.errors:
                log.warning("ciclo con errores: %s", "; ".join(rep.errors))
            log.info(
                "t=%s ingesta=%d evals=%d señales=%d admitidas=%d rechazadas=%d trabajos=%s monitoreadas=%d pasos=%s",
                rep.now.strftime("%H:%M:%S"), rep.ingested, rep.evaluations, rep.signals,
                rep.admitted, rep.rejected, rep.jobs, rep.monitored, rep.timings,
            )  # fmt: skip
        except Exception:
            log.exception("ciclo abortado")
        stop.wait(max(0.0, tick_seconds - (time.monotonic() - started)))
    log.info("worker detenido")


def main() -> None:
    from app.bootstrap import build_worker
    from app.logging_config import configure_logging
    from app.settings import get_settings

    settings = get_settings()
    configure_logging(settings.log_level)
    worker, extras = build_worker(settings)
    worker.bootstrap()

    stop = threading.Event()
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            signal.signal(sig, lambda *_: stop.set())
        except ValueError:  # fuera del hilo principal
            pass
    for starter in extras.get("background", []):
        starter(stop)
    run_forever(worker, settings.tick_seconds, stop)


if __name__ == "__main__":
    main()
