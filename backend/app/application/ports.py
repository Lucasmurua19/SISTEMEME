"""Contratos para fuentes de datos y persistencia.

La aplicación solo conoce estos protocolos. ``infrastructure`` aporta las implementaciones
(RPC de Solana, Jupiter, fixtures reproducibles, PostgreSQL).
"""

from __future__ import annotations

from collections.abc import Callable, Iterable, Sequence
from contextlib import AbstractContextManager
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from typing import Any, Protocol

from app.application.execution.ports import ExecutionRepository
from app.domain.config import StrategyConfig
from app.domain.models import (
    AttemptInfo,
    MarketSnapshot,
    MigrationBatch,
    MigrationEvent,
    PoolInfo,
    PoolVerification,
    PositionInfo,
    Quote,
    SessionInfo,
    SessionState,
    SignalInfo,
)
from app.domain.reasons import AttemptStatus, LedgerKind, PositionStatus
from app.domain.strategy import EvaluationResult


# --------------------------------------------------------------------------- errores
class ConfigConflictError(Exception):
    """Una versión de estrategia ya registrada no puede cambiar de contenido."""


class TransientSourceError(Exception):
    """Fallo reintentable de una fuente externa (red, límite de tasa, 5xx)."""


class PermanentSourceError(Exception):
    """Fallo no reintentable (respuesta inválida, esquema inesperado)."""


# --------------------------------------------------------------------------- DTOs
@dataclass(frozen=True)
class Checkpoint:
    source: str
    stream: str
    cursor: str | None
    last_slot: int | None
    last_healthy_at: datetime | None
    last_error: str | None
    consecutive_failures: int
    updated_at: datetime


@dataclass(frozen=True)
class JobInfo:
    id: int
    type: str
    payload: dict[str, Any]
    key: str
    attempts: int
    max_attempts: int


@dataclass(frozen=True)
class FxRate:
    rate: Decimal  # USD por SOL
    observed_at: datetime
    source: str


# --------------------------------------------------------------------------- fuentes
class Clock(Protocol):
    def now(self) -> datetime: ...


class MigrationSource(Protocol):
    name: str

    def fetch_migrations(self, cursor: str | None, limit: int) -> MigrationBatch:
        """Migraciones posteriores al cursor, de la más antigua a la más reciente."""

    def fetch_signature(self, signature: str) -> Sequence[MigrationEvent]:
        """Eventos de migración de una transacción puntual (ingesta por WebSocket)."""

    def verify(self, event: MigrationEvent) -> PoolVerification:
        """Valida programa, mint y pool contra la cadena."""


class MarketDataSource(Protocol):
    name: str

    def get_snapshots(self, pool: PoolInfo, now: datetime) -> list[MarketSnapshot]:
        """Ventanas cerradas y observadas hasta ``now``, de la primera a la última."""


class QuoteProvider(Protocol):
    name: str

    def quote_buy(self, pool: PoolInfo, lamports_in: int, slippage_bps: int, now: datetime) -> Quote:
        """SOL → token. Nunca lanza: los fallos se devuelven como ``Quote`` con estado ERROR/NO_ROUTE."""

    def quote_sell(self, pool: PoolInfo, tokens_in: int, slippage_bps: int, now: datetime) -> Quote:
        """token → SOL con la cantidad indicada en unidades base."""


class FxProvider(Protocol):
    def sol_usd(self, now: datetime) -> FxRate | None: ...


# --------------------------------------------------------------------------- persistencia
class CheckpointRepository(Protocol):
    def get(self, source: str, stream: str) -> Checkpoint | None: ...
    def save_cursor(self, source: str, stream: str, cursor: str | None, slot: int | None, now: datetime) -> None: ...
    def mark_ok(self, source: str, stream: str, now: datetime) -> None: ...
    def mark_failure(self, source: str, stream: str, now: datetime, error: str) -> None: ...
    def list_all(self) -> list[Checkpoint]: ...


class ChainRepository(Protocol):
    def record_migration(self, ev: MigrationEvent, ver: PoolVerification, now: datetime) -> bool: ...
    def get_pool(self, address: str) -> PoolInfo | None: ...
    def list_active_pools(self, now: datetime, max_age_s: int) -> list[PoolInfo]: ...


class SnapshotRepository(Protocol):
    def upsert_many(self, snaps: Sequence[MarketSnapshot]) -> list[MarketSnapshot]: ...
    def list_for_pool(self, pool: str, until: datetime | None = None) -> list[MarketSnapshot]: ...


class QuoteRepository(Protocol):
    def save(self, q: Quote, *, pool: str | None, purpose: str) -> int: ...
    def get(self, quote_id: int) -> Quote | None: ...
    def save_fx(self, rate: FxRate) -> None: ...


class StrategyRepository(Protocol):
    def register(self, cfg: StrategyConfig, now: datetime) -> int: ...
    def get(self, version_id: int) -> StrategyConfig: ...
    def latest_id(self) -> int | None: ...


class EvaluationRepository(Protocol):
    def exists(self, pool: str, window_index: int, version_id: int) -> bool: ...
    def terminal_pools(self, version_id: int, pools: Sequence[str]) -> set[str]: ...
    def save(
        self,
        *,
        pool: str,
        version_id: int,
        result: EvaluationResult,
        buy_quote_id: int | None,
        sell_quote_id: int | None,
        evaluated_at: datetime,
    ) -> int | None: ...


class SignalRepository(Protocol):
    def create(self, **kwargs: Any) -> int | None: ...
    def get(self, signal_id: int) -> SignalInfo | None: ...
    def list_unprocessed(self, session_id: int, version_id: int, now: datetime) -> list[SignalInfo]: ...
    def expire_due(self, now: datetime) -> int: ...


class SessionRepository(Protocol):
    def ensure(self, *, name: str, policy: str, version_id: int, cfg: StrategyConfig, now: datetime) -> SessionInfo: ...
    def get(self, session_id: int) -> SessionInfo | None: ...
    def lock(self, session_id: int) -> SessionInfo | None: ...
    def list_for_version(self, version_id: int) -> list[SessionInfo]: ...
    def set_paused(self, session_id: int, paused: bool, reason: str | None, now: datetime) -> SessionInfo | None: ...
    def state(self, session_id: int) -> SessionState: ...


class PositionRepository(Protocol):
    def create(self, **kwargs: Any) -> int | None: ...
    def get(self, position_id: int, *, lock: bool = False) -> PositionInfo | None: ...
    def update(self, position_id: int, now: datetime, **fields: Any) -> None: ...
    def list(self, statuses: Iterable[PositionStatus], session_id: int | None = None) -> list[PositionInfo]: ...
    def list_for_signal(self, signal_id: int) -> list[PositionInfo]: ...


class AttemptRepository(Protocol):
    def create(self, **kwargs: Any) -> int | None: ...
    def get(self, attempt_id: int, *, lock: bool = False) -> AttemptInfo | None: ...
    def entry_batch(self, signal_id: int, scheduled_at: datetime) -> list[tuple[int, int]]: ...
    def exit_batch(self, pool: str, scheduled_at: datetime) -> list[tuple[int, int]]: ...
    def finish(self, attempt_id: int, *, status: AttemptStatus, executed_at: datetime, **kwargs: Any) -> None: ...
    def last_number(self, position_id: int, kind: str) -> int: ...
    def pending(self, position_id: int, kind: str) -> AttemptInfo | None: ...


class LedgerRepository(Protocol):
    def add(
        self, *, session_id: int, position_id: int, attempt_id: int, kind: LedgerKind, amount: int, now: datetime
    ) -> bool: ...


class JobRepository(Protocol):
    def enqueue(
        self, *, type: str, payload: dict[str, Any], key: str, run_at: datetime, now: datetime, max_attempts: int = 6
    ) -> bool: ...
    def claim(self, *, worker_id: str, now: datetime, limit: int, claim_ttl_s: int) -> list[JobInfo]: ...
    def complete(self, job_id: int, now: datetime) -> None: ...
    def fail(self, job_id: int, now: datetime, error: str, *, permanent: bool = False, **kwargs: Any) -> str: ...


class AuditRepository(Protocol):
    def log(
        self,
        entity: str,
        entity_id: str,
        action: str,
        from_state: str | None,
        to_state: str | None,
        detail: dict[str, Any],
        now: datetime,
    ) -> None: ...


class UnitOfWork(Protocol):
    """Una transacción. Se confirma al salir sin error y se revierte si hay excepción."""

    checkpoints: CheckpointRepository
    chain: ChainRepository
    snapshots: SnapshotRepository
    quotes: QuoteRepository
    strategies: StrategyRepository
    evaluations: EvaluationRepository
    signals: SignalRepository
    sessions: SessionRepository
    positions: PositionRepository
    attempts: AttemptRepository
    ledger: LedgerRepository
    jobs: JobRepository
    audit: AuditRepository
    execution: ExecutionRepository


UnitOfWorkFactory = Callable[[], AbstractContextManager[UnitOfWork]]
