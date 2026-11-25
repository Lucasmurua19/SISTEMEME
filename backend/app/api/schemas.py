"""Contratos de salida de la API.

Los enteros grandes (lamports, unidades base) viajan como cadenas para no perder precisión en JSON.
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field


class ReasonOut(BaseModel):
    code: str = Field(description="Código estable de motivo, p. ej. NO_SELL_ROUTE")
    message: str
    data: dict[str, Any] = Field(default_factory=dict)


class SignalBrief(BaseModel):
    id: int
    status: str
    issued_at: str | None
    expires_at: str | None
    invalidation_level: str | None


class CandidateOut(BaseModel):
    mint: str
    symbol: str | None
    name: str | None
    pool: str
    status: str = Field(description="observing | pending_data | eligible | rejected | expired")
    reasons: list[ReasonOut]
    score: str | None = Field(description="Puntaje 0-100 sin redondear (texto decimal exacto)")
    liquidity_lamports: str | None
    round_trip_cost_pct: str | None
    window_index: int
    evaluated_at: str | None
    migrated_at: str | None
    age_s: int
    data_age_s: int | None
    migration_signature: str
    verified: bool
    canonical: bool | None
    signal: SignalBrief | None
    positions: dict[str, str]


class StrategyBrief(BaseModel):
    id: int
    name: str


class NoEntryOut(BaseModel):
    message: str
    tracked: int
    reasons: list[dict[str, Any]]


class CandidateList(BaseModel):
    as_of: str
    strategy_version: StrategyBrief | None
    total: int
    limit: int = 25
    offset: int = 0
    counts: dict[str, int]
    items: list[CandidateOut]
    no_entry: NoEntryOut | None = Field(
        description="Presente cuando no hay candidatos elegibles: 'sin entrada' con sus motivos agregados"
    )


class WorkerOut(BaseModel):
    alive: bool
    last_heartbeat_at: str | None
    silence_s: float | None


class SourceOut(BaseModel):
    source: str
    stream: str
    degraded: bool
    reason: str | None
    last_healthy_at: str | None
    consecutive_failures: int
    last_error: str | None
    cursor: str | None = None
    last_slot: int | None = None


class StatusOut(BaseModel):
    overall: str = Field(description="ok | degraded | down")
    as_of: str
    data_mode: str
    worker: WorkerOut
    ingest_lag_s: float | None
    last_slot: int | None
    strategy: dict[str, Any] | None
    sources: list[SourceOut]
    database: str = "ok"


class ReadyOut(StatusOut):
    ready: bool


class PauseIn(BaseModel):
    reason: str | None = Field(default=None, max_length=200)
