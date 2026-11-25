"""Arnés de integración: reproduce escenarios de fixtures con un reloj manual sobre la base de pruebas."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

from app.application.ports import UnitOfWorkFactory
from app.bootstrap import build_fixture_sources
from app.domain.config import StrategyConfig
from app.infrastructure.clock import ManualClock
from app.infrastructure.fixtures import FixtureLibrary
from app.infrastructure.tracking import TrackedMarketSource, TrackedQuoteProvider
from app.worker import CycleReport, Worker

BACKEND = Path(__file__).resolve().parents[1]
SCENARIOS_DIR = BACKEND / "fixtures" / "scenarios"
STRATEGY_PATH = BACKEND / "config" / "strategy-fixtures-v1.json"
T0 = datetime(2026, 3, 1, 0, 0, 0, tzinfo=UTC)  # múltiplo de 15 s: ticks alineados con las ventanas


def fixtures_strategy(**overrides) -> StrategyConfig:
    data = json.loads(STRATEGY_PATH.read_text(encoding="utf-8"))
    data.update(overrides)
    return StrategyConfig.from_dict(data)


class Harness:
    def __init__(
        self,
        uow: UnitOfWorkFactory,
        *,
        only: set[str] | None = None,
        cfg: StrategyConfig | None = None,
        scenarios_dir: Path = SCENARIOS_DIR,
        t0: datetime = T0,
        scenarios=None,
    ) -> None:
        loaded = scenarios if scenarios is not None else FixtureLibrary.load_scenarios(scenarios_dir)
        if only is not None:
            loaded = [s for s in loaded if s.id in only]
        self.scenarios = loaded
        self.library = FixtureLibrary(loaded, t0)
        self.clock = ManualClock(t0)
        migrations, market, quotes, fx = build_fixture_sources(self.library, self.clock)
        self.market = TrackedMarketSource(market)
        self.quotes = TrackedQuoteProvider(quotes)
        self.cfg = cfg or fixtures_strategy()
        self.worker = Worker(
            uow_factory=uow,
            clock=self.clock,
            migrations=migrations,
            market=self.market,
            quotes=self.quotes,
            fx=fx,
            cfg=self.cfg,
        )
        self.worker.bootstrap()
        self.reports: list[CycleReport] = []

    @property
    def horizon_s(self) -> int:
        return FixtureLibrary.horizon_s(self.scenarios)

    def run(self, seconds: int, step: int = 15) -> list[CycleReport]:
        out = []
        for _ in range(seconds // step):
            self.clock.advance(step)
            out.append(self.worker.run_cycle())
        self.reports.extend(out)
        return out

    def run_all(self, extra_s: int = 240) -> list[CycleReport]:
        return self.run(self.horizon_s + extra_s)
