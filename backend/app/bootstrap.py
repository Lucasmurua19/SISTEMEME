"""Ensamblado de dependencias según ``DATA_MODE``."""

from __future__ import annotations

import json
import logging
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

from app.application.ports import Clock, UnitOfWorkFactory
from app.domain.config import StrategyConfig
from app.infrastructure.clock import ReplayClock, SystemClock
from app.infrastructure.db import make_engine, make_session_factory, uow_factory
from app.infrastructure.fixtures import (
    FixtureFxProvider,
    FixtureLibrary,
    FixtureMarketSource,
    FixtureMigrationSource,
    FixtureQuoteProvider,
)
from app.infrastructure.tracking import TrackedMarketSource, TrackedQuoteProvider
from app.settings import Settings
from app.worker import Worker

log = logging.getLogger(__name__)

BACKEND_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_FIXTURE_STRATEGY = "config/strategy-fixtures-v1.json"
# Reglas congeladas del modo live: un archivo explícito y versionado, no valores implícitos del código.
DEFAULT_LIVE_STRATEGY = "config/strategy-live-v1.json"


def resolve_path(raw: str) -> Path:
    p = Path(raw)
    if p.is_absolute() or p.exists():
        return p
    return BACKEND_ROOT / raw


def load_strategy(settings: Settings) -> StrategyConfig:
    """Versión de estrategia: el archivo indicado o, por defecto, el archivo congelado del modo de datos."""
    raw = settings.strategy_config_path or (DEFAULT_FIXTURE_STRATEGY if settings.data_mode == "fixtures" else DEFAULT_LIVE_STRATEGY)
    return StrategyConfig.from_dict(json.loads(resolve_path(raw).read_text(encoding="utf-8")))


def align(ts: datetime, step_s: int) -> datetime:
    epoch = int(ts.timestamp())
    return datetime.fromtimestamp(epoch - epoch % step_s, tz=ts.tzinfo)


def fixture_clock_and_library(settings: Settings, uow: UnitOfWorkFactory) -> tuple[ReplayClock, FixtureLibrary]:
    """Reloj de reproducción persistido: tras reiniciar continúa donde quedó.

    ``t0`` se fija una sola vez, lo bastante atrás como para que la reproducción termine alcanzando el
    reloj real: nunca hay instantes en el futuro.
    """
    scenarios = FixtureLibrary.load_scenarios(resolve_path(settings.fixtures_dir))
    if not scenarios:
        raise RuntimeError(f"no hay escenarios en {settings.fixtures_dir}")
    horizon = FixtureLibrary.horizon_s(scenarios)
    real = SystemClock()
    step = settings.fixture_step_seconds
    with uow() as u:
        t0_cp = u.checkpoints.get("replay", "t0")
        if t0_cp is None or not t0_cp.cursor:
            t0 = align(real.now() - timedelta(seconds=horizon + 60), step)
            u.checkpoints.save_cursor("replay", "t0", t0.isoformat(), None, real.now())
        else:
            t0 = datetime.fromisoformat(t0_cp.cursor)
        clock_cp = u.checkpoints.get("replay", "clock")
    start = datetime.fromisoformat(clock_cp.cursor) if clock_cp and clock_cp.cursor else t0
    return ReplayClock(start, step, real), FixtureLibrary(scenarios, t0)


def build_fixture_sources(library: FixtureLibrary, clock: Clock):
    market = FixtureMarketSource(library)
    return (
        FixtureMigrationSource(library, clock),
        market,
        FixtureQuoteProvider(market, library),
        FixtureFxProvider(),
    )


def build_worker(settings: Settings) -> tuple[Worker, dict[str, Any]]:
    engine = make_engine(settings.database_url)
    uow = uow_factory(make_session_factory(engine))
    cfg = load_strategy(settings)
    extras: dict[str, Any] = {"background": [], "engine": engine}

    if settings.data_mode == "fixtures":
        clock, library = fixture_clock_and_library(settings, uow)
        migrations, market, quotes, fx = build_fixture_sources(library, clock)
        log.info("modo fixtures: %d escenarios, reloj de reproducción en %s", len(library.scenarios), clock.now())
    else:
        from app.infrastructure.live import build_live_sources  # import diferido: requiere red y credenciales

        clock = SystemClock()
        migrations, market, quotes, fx, background = build_live_sources(settings, uow, clock)
        extras["background"] = background

    worker = Worker(
        uow_factory=uow,
        clock=clock,
        migrations=migrations,
        market=TrackedMarketSource(market),
        quotes=TrackedQuoteProvider(quotes),
        fx=fx,
        cfg=cfg,
        worker_id=settings.worker_id,
        source_max_age_s=settings.source_max_age_s,
        source_max_failures=settings.source_max_failures,
        job_claim_ttl_s=settings.job_claim_ttl_s,
        evaluation_budget_s=settings.evaluation_budget_s if settings.data_mode == "live" else None,
        skip_terminal_pools=settings.data_mode == "live",
    )
    return worker, extras
