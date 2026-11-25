"""Modelo relacional (SQLAlchemy 2.0).

Tipos:
- ``TIMESTAMPTZ`` para tiempos (``UTCDateTime`` devuelve siempre ``datetime`` con zona UTC).
- ``BigInt`` = ``NUMERIC(40, 0)`` para lamports y unidades base de tokens: nunca se pasa por float.
- ``Price`` = ``NUMERIC(38, 18)`` para precios, porcentajes y puntajes.
- ``JSON`` genérico, ``JSONB`` en PostgreSQL.
SQLite (solo pruebas) guarda enteros grandes y decimales como texto para conservar la exactitud.
"""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

from sqlalchemy import (
    JSON,
    BigInteger,
    Boolean,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    MetaData,
    Numeric,
    String,
    Text,
    TypeDecorator,
    UniqueConstraint,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

NAMING = {
    "ix": "ix_%(column_0_label)s",
    "uq": "uq_%(table_name)s_%(column_0_name)s",
    "ck": "ck_%(table_name)s_%(constraint_name)s",
    "fk": "fk_%(table_name)s_%(column_0_name)s_%(referred_table_name)s",
    "pk": "pk_%(table_name)s",
}


class UTCDateTime(TypeDecorator):
    impl = DateTime(timezone=True)
    cache_ok = True

    def process_bind_param(self, value: datetime | None, dialect: Any) -> datetime | None:
        if value is None:
            return None
        if value.tzinfo is None:
            raise ValueError("los instantes deben tener zona horaria (UTC)")
        value = value.astimezone(UTC)
        return value.replace(tzinfo=None) if dialect.name == "sqlite" else value

    def process_result_value(self, value: datetime | None, dialect: Any) -> datetime | None:
        if value is None:
            return None
        return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)


class BigInt(TypeDecorator):
    impl = Numeric(40, 0)
    cache_ok = True

    def load_dialect_impl(self, dialect: Any) -> Any:
        if dialect.name == "sqlite":
            return dialect.type_descriptor(String(48))
        return dialect.type_descriptor(Numeric(40, 0, asdecimal=True))

    def process_bind_param(self, value: Any, dialect: Any) -> Any:
        if value is None:
            return None
        as_int = int(value)
        return str(as_int) if dialect.name == "sqlite" else Decimal(as_int)

    def process_result_value(self, value: Any, dialect: Any) -> int | None:
        return None if value is None else int(value)


class Price(TypeDecorator):
    impl = Numeric(38, 18)
    cache_ok = True

    def load_dialect_impl(self, dialect: Any) -> Any:
        if dialect.name == "sqlite":
            return dialect.type_descriptor(String(80))
        return dialect.type_descriptor(Numeric(38, 18, asdecimal=True))

    def process_bind_param(self, value: Any, dialect: Any) -> Any:
        if value is None:
            return None
        dec = value if isinstance(value, Decimal) else Decimal(str(value))
        return format(dec, "f") if dialect.name == "sqlite" else dec

    def process_result_value(self, value: Any, dialect: Any) -> Decimal | None:
        return None if value is None else Decimal(str(value))


JSONType = JSON().with_variant(JSONB(), "postgresql")
PK = BigInteger().with_variant(Integer(), "sqlite")


class Base(DeclarativeBase):
    metadata = MetaData(naming_convention=NAMING)


class Token(Base):
    __tablename__ = "tokens"
    mint: Mapped[str] = mapped_column(String(64), primary_key=True)
    decimals: Mapped[int] = mapped_column(Integer, default=6)
    symbol: Mapped[str | None] = mapped_column(String(32))
    name: Mapped[str | None] = mapped_column(String(128))
    meta: Mapped[dict[str, Any]] = mapped_column("metadata", JSONType, default=dict)
    first_seen_at: Mapped[datetime] = mapped_column(UTCDateTime)


class Pool(Base):
    __tablename__ = "pools"
    address: Mapped[str] = mapped_column(String(64), primary_key=True)
    mint: Mapped[str] = mapped_column(ForeignKey("tokens.mint"), index=True)
    program: Mapped[str] = mapped_column(String(64))
    quote_mint: Mapped[str] = mapped_column(String(64))
    migration_slot: Mapped[int] = mapped_column(BigInteger)
    migration_signature: Mapped[str] = mapped_column(String(100))
    migration_instruction_index: Mapped[int] = mapped_column(Integer)
    migrated_at: Mapped[datetime] = mapped_column(UTCDateTime, index=True)
    observed_at: Mapped[datetime] = mapped_column(UTCDateTime)
    verified: Mapped[bool] = mapped_column(Boolean, default=False)
    identity_ok: Mapped[bool] = mapped_column(Boolean, default=False)
    canonical: Mapped[bool | None] = mapped_column(Boolean)
    verification: Mapped[dict[str, Any]] = mapped_column(JSONType, default=dict)
    status: Mapped[str] = mapped_column(String(16), default="active")
    created_at: Mapped[datetime] = mapped_column(UTCDateTime)


class ChainEvent(Base):
    __tablename__ = "chain_events"
    __table_args__ = (
        UniqueConstraint("signature", "instruction_index", "event_index", name="uq_chain_events_identity"),
    )
    id: Mapped[int] = mapped_column(PK, primary_key=True, autoincrement=True)
    kind: Mapped[str] = mapped_column(String(32), default="migration")
    signature: Mapped[str] = mapped_column(String(100))
    instruction_index: Mapped[int] = mapped_column(Integer)
    event_index: Mapped[int] = mapped_column(Integer, default=0)
    pool: Mapped[str] = mapped_column(ForeignKey("pools.address"), index=True)
    slot: Mapped[int] = mapped_column(BigInteger, index=True)
    confirmation: Mapped[str] = mapped_column(String(16))
    event_at: Mapped[datetime] = mapped_column(UTCDateTime)
    observed_at: Mapped[datetime] = mapped_column(UTCDateTime)
    evidence: Mapped[dict[str, Any]] = mapped_column(JSONType, default=dict)


class SourceCheckpoint(Base):
    __tablename__ = "source_checkpoints"
    source: Mapped[str] = mapped_column(String(48), primary_key=True)
    stream: Mapped[str] = mapped_column(String(48), primary_key=True)
    cursor: Mapped[str | None] = mapped_column(Text)
    last_slot: Mapped[int | None] = mapped_column(BigInteger)
    last_healthy_at: Mapped[datetime | None] = mapped_column(UTCDateTime)
    last_error: Mapped[str | None] = mapped_column(Text)
    consecutive_failures: Mapped[int] = mapped_column(Integer, default=0)
    updated_at: Mapped[datetime] = mapped_column(UTCDateTime)


class MarketSnapshotRow(Base):
    __tablename__ = "market_snapshots"
    # la restricción única (pool, ventana) también sirve de índice por pool y ventana
    __table_args__ = (UniqueConstraint("pool", "window_index", name="uq_market_snapshots_pool_window"),)
    id: Mapped[int] = mapped_column(PK, primary_key=True, autoincrement=True)
    pool: Mapped[str] = mapped_column(ForeignKey("pools.address"))
    window_index: Mapped[int] = mapped_column(Integer)
    window_start: Mapped[datetime] = mapped_column(UTCDateTime)
    window_end: Mapped[datetime] = mapped_column(UTCDateTime)
    observed_at: Mapped[datetime] = mapped_column(UTCDateTime)
    open: Mapped[Decimal] = mapped_column(Price)
    high: Mapped[Decimal] = mapped_column(Price)
    low: Mapped[Decimal] = mapped_column(Price)
    close: Mapped[Decimal] = mapped_column(Price)
    sol_reserve_lamports: Mapped[int] = mapped_column(BigInt)
    token_reserve_base: Mapped[int] = mapped_column(BigInt)
    swap_count: Mapped[int] = mapped_column(Integer)
    buy_count: Mapped[int] = mapped_column(Integer)
    sell_count: Mapped[int] = mapped_column(Integer)
    net_buyers: Mapped[int] = mapped_column(Integer)
    buy_volume_lamports: Mapped[int] = mapped_column(BigInt)
    sell_volume_lamports: Mapped[int] = mapped_column(BigInt)
    top10_concentration_pct: Mapped[Decimal | None] = mapped_column(Price)
    concentration_observed_at: Mapped[datetime | None] = mapped_column(UTCDateTime)
    coverage: Mapped[Decimal] = mapped_column(Price)
    source: Mapped[str] = mapped_column(String(48))


class QuoteRow(Base):
    __tablename__ = "quotes"
    __table_args__ = (Index("ix_quotes_pool_observed", "pool", "observed_at"),)
    id: Mapped[int] = mapped_column(PK, primary_key=True, autoincrement=True)
    pool: Mapped[str | None] = mapped_column(ForeignKey("pools.address"))
    purpose: Mapped[str] = mapped_column(String(16))  # evaluation | entry | valuation | exit
    side: Mapped[str] = mapped_column(String(8))
    input_mint: Mapped[str] = mapped_column(String(64))
    output_mint: Mapped[str] = mapped_column(String(64))
    in_amount: Mapped[int] = mapped_column(BigInt)
    out_amount: Mapped[int] = mapped_column(BigInt)
    min_out_amount: Mapped[int] = mapped_column(BigInt)
    route: Mapped[str] = mapped_column(String(200), default="")
    price_impact_bps: Mapped[Decimal] = mapped_column(Price)
    costs: Mapped[list[dict[str, Any]]] = mapped_column(JSONType, default=list)
    status: Mapped[str] = mapped_column(String(12))
    error: Mapped[str | None] = mapped_column(Text)
    request_id: Mapped[str | None] = mapped_column(String(100))
    observed_at: Mapped[datetime] = mapped_column(UTCDateTime)
    expires_at: Mapped[datetime] = mapped_column(UTCDateTime)


class FxRate(Base):
    __tablename__ = "fx_rates"
    id: Mapped[int] = mapped_column(PK, primary_key=True, autoincrement=True)
    pair: Mapped[str] = mapped_column(String(16), default="SOL/USD")
    rate: Mapped[Decimal] = mapped_column(Price)
    source: Mapped[str] = mapped_column(String(48))
    observed_at: Mapped[datetime] = mapped_column(UTCDateTime, index=True)


class StrategyVersion(Base):
    __tablename__ = "strategy_versions"
    id: Mapped[int] = mapped_column(PK, primary_key=True, autoincrement=True)
    name: Mapped[str] = mapped_column(String(64), unique=True)
    fingerprint: Mapped[str] = mapped_column(String(64))
    config: Mapped[dict[str, Any]] = mapped_column(JSONType)
    activated_at: Mapped[datetime] = mapped_column(UTCDateTime)


class Evaluation(Base):
    __tablename__ = "evaluations"
    __table_args__ = (
        UniqueConstraint("pool", "window_index", "strategy_version_id", name="uq_evaluations_pool_window_version"),
    )
    id: Mapped[int] = mapped_column(PK, primary_key=True, autoincrement=True)
    pool: Mapped[str] = mapped_column(ForeignKey("pools.address"), index=True)
    window_index: Mapped[int] = mapped_column(Integer)
    strategy_version_id: Mapped[int] = mapped_column(ForeignKey("strategy_versions.id"))
    snapshot_id: Mapped[int | None] = mapped_column(ForeignKey("market_snapshots.id"))
    status: Mapped[str] = mapped_column(String(16), index=True)
    reasons: Mapped[list[dict[str, Any]]] = mapped_column(JSONType, default=list)
    filters: Mapped[list[dict[str, Any]]] = mapped_column(JSONType, default=list)
    metrics: Mapped[dict[str, Any]] = mapped_column(JSONType, default=dict)
    pattern: Mapped[dict[str, Any]] = mapped_column(JSONType, default=dict)
    score: Mapped[Decimal | None] = mapped_column(Price)
    score_components: Mapped[list[dict[str, Any]]] = mapped_column(JSONType, default=list)
    round_trip: Mapped[dict[str, Any] | None] = mapped_column(JSONType)
    snapshot_refs: Mapped[list[int]] = mapped_column(JSONType, default=list)
    buy_quote_id: Mapped[int | None] = mapped_column(ForeignKey("quotes.id"))
    sell_quote_id: Mapped[int | None] = mapped_column(ForeignKey("quotes.id"))
    as_of: Mapped[datetime] = mapped_column(UTCDateTime)
    evaluated_at: Mapped[datetime] = mapped_column(UTCDateTime)


class Signal(Base):
    __tablename__ = "signals"
    __table_args__ = (
        UniqueConstraint("evaluation_id", name="uq_signals_evaluation"),
        UniqueConstraint("pool", "strategy_version_id", name="uq_signals_pool_version"),
        Index("ix_signals_status_expires", "status", "expires_at"),
    )
    id: Mapped[int] = mapped_column(PK, primary_key=True, autoincrement=True)
    evaluation_id: Mapped[int] = mapped_column(ForeignKey("evaluations.id"))
    pool: Mapped[str] = mapped_column(ForeignKey("pools.address"))
    strategy_version_id: Mapped[int] = mapped_column(ForeignKey("strategy_versions.id"))
    status: Mapped[str] = mapped_column(String(12), default="active")
    issued_at: Mapped[datetime] = mapped_column(UTCDateTime)
    expires_at: Mapped[datetime] = mapped_column(UTCDateTime)
    amount_lamports: Mapped[int] = mapped_column(BigInt)
    invalidation_level: Mapped[Decimal] = mapped_column(Price)
    entry_ref_price: Mapped[Decimal] = mapped_column(Price)
    score: Mapped[Decimal] = mapped_column(Price)
    liquidity_lamports: Mapped[int] = mapped_column(BigInt)
    round_trip_cost_pct: Mapped[Decimal] = mapped_column(Price)
    buy_quote_id: Mapped[int | None] = mapped_column(ForeignKey("quotes.id"))
    sell_quote_id: Mapped[int | None] = mapped_column(ForeignKey("quotes.id"))
    buy_price_lamports_per_unit: Mapped[Decimal | None] = mapped_column(Price)


class PaperSession(Base):
    __tablename__ = "paper_sessions"
    __table_args__ = (UniqueConstraint("strategy_version_id", "policy", name="uq_paper_sessions_version_policy"),)
    id: Mapped[int] = mapped_column(PK, primary_key=True, autoincrement=True)
    name: Mapped[str] = mapped_column(String(100), unique=True)
    policy: Mapped[str] = mapped_column(String(16))  # ranking | liquidity
    strategy_version_id: Mapped[int] = mapped_column(ForeignKey("strategy_versions.id"))
    capital_lamports: Mapped[int] = mapped_column(BigInt)
    trade_size_lamports: Mapped[int] = mapped_column(BigInt)
    max_exposure_lamports: Mapped[int] = mapped_column(BigInt)
    max_positions: Mapped[int] = mapped_column(Integer)
    max_session_loss_lamports: Mapped[int] = mapped_column(BigInt)
    paused: Mapped[bool] = mapped_column(Boolean, default=False)
    pause_reason: Mapped[str | None] = mapped_column(String(200))
    started_at: Mapped[datetime] = mapped_column(UTCDateTime)
    updated_at: Mapped[datetime] = mapped_column(UTCDateTime)


class PaperPosition(Base):
    __tablename__ = "paper_positions"
    __table_args__ = (
        UniqueConstraint("session_id", "signal_id", name="uq_paper_positions_session_signal"),
        Index("ix_paper_positions_session_status", "session_id", "status"),
    )
    id: Mapped[int] = mapped_column(PK, primary_key=True, autoincrement=True)
    session_id: Mapped[int] = mapped_column(ForeignKey("paper_sessions.id"))
    signal_id: Mapped[int] = mapped_column(ForeignKey("signals.id"))
    pool: Mapped[str] = mapped_column(ForeignKey("pools.address"))
    mint: Mapped[str] = mapped_column(ForeignKey("tokens.mint"))
    status: Mapped[str] = mapped_column(String(16))
    reject_reason: Mapped[str | None] = mapped_column(String(48))
    reject_detail: Mapped[dict[str, Any]] = mapped_column(JSONType, default=dict)
    tokens_expected: Mapped[int] = mapped_column(BigInt, default=0)
    tokens_min: Mapped[int] = mapped_column(BigInt, default=0)
    debited_lamports: Mapped[int] = mapped_column(BigInt, default=0)
    reserved_lamports: Mapped[int] = mapped_column(BigInt, default=0)
    immobilized_lamports: Mapped[int] = mapped_column(BigInt, default=0)
    entry_price: Mapped[Decimal | None] = mapped_column(Price)
    entry_liquidity_lamports: Mapped[int | None] = mapped_column(BigInt)
    invalidation_level: Mapped[Decimal | None] = mapped_column(Price)
    opened_at: Mapped[datetime | None] = mapped_column(UTCDateTime)
    closed_at: Mapped[datetime | None] = mapped_column(UTCDateTime)
    exit_trigger: Mapped[str | None] = mapped_column(String(32))
    exit_failures: Mapped[int] = mapped_column(Integer, default=0)
    illiquid: Mapped[bool] = mapped_column(Boolean, default=False)
    next_exit_at: Mapped[datetime | None] = mapped_column(UTCDateTime)
    last_recoverable_lamports: Mapped[int | None] = mapped_column(BigInt)
    last_recoverable_adverse_lamports: Mapped[int | None] = mapped_column(BigInt)
    last_valued_at: Mapped[datetime | None] = mapped_column(UTCDateTime)
    valuation_status: Mapped[str | None] = mapped_column(String(16))
    proceeds_lamports: Mapped[int | None] = mapped_column(BigInt)
    adverse_proceeds_lamports: Mapped[int | None] = mapped_column(BigInt)
    pnl_lamports: Mapped[int | None] = mapped_column(BigInt)
    sol_usd_open: Mapped[Decimal | None] = mapped_column(Price)
    sol_usd_close: Mapped[Decimal | None] = mapped_column(Price)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime)
    updated_at: Mapped[datetime] = mapped_column(UTCDateTime)


class PaperAttempt(Base):
    __tablename__ = "paper_attempts"
    __table_args__ = (
        UniqueConstraint("position_id", "kind", "number", name="uq_paper_attempts_position_kind_number"),
        Index("ix_paper_attempts_status_scheduled", "status", "scheduled_at"),
    )
    id: Mapped[int] = mapped_column(PK, primary_key=True, autoincrement=True)
    position_id: Mapped[int] = mapped_column(ForeignKey("paper_positions.id"), index=True)
    kind: Mapped[str] = mapped_column(String(8))
    number: Mapped[int] = mapped_column(Integer)
    status: Mapped[str] = mapped_column(String(12))
    scheduled_at: Mapped[datetime] = mapped_column(UTCDateTime)
    executed_at: Mapped[datetime | None] = mapped_column(UTCDateTime)
    trigger: Mapped[str | None] = mapped_column(String(32))
    reference_out_lamports: Mapped[int | None] = mapped_column(BigInt)
    quote_id: Mapped[int | None] = mapped_column(ForeignKey("quotes.id"))
    requested_amount: Mapped[int | None] = mapped_column(BigInt)
    result_amount: Mapped[int | None] = mapped_column(BigInt)
    failure_cause: Mapped[str | None] = mapped_column(String(48))
    detail: Mapped[dict[str, Any]] = mapped_column(JSONType, default=dict)


class PaperLedger(Base):
    __tablename__ = "paper_ledger"
    __table_args__ = (UniqueConstraint("attempt_id", "kind", name="uq_paper_ledger_attempt_kind"),)
    id: Mapped[int] = mapped_column(PK, primary_key=True, autoincrement=True)
    session_id: Mapped[int] = mapped_column(ForeignKey("paper_sessions.id"), index=True)
    position_id: Mapped[int] = mapped_column(ForeignKey("paper_positions.id"))
    attempt_id: Mapped[int] = mapped_column(ForeignKey("paper_attempts.id"))
    kind: Mapped[str] = mapped_column(String(8))  # reserve | release | debit | credit
    amount_lamports: Mapped[int] = mapped_column(BigInt)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime)


class Job(Base):
    __tablename__ = "jobs"
    __table_args__ = (
        UniqueConstraint("idempotency_key", name="uq_jobs_idempotency_key"),
        Index("ix_jobs_status_available", "status", "available_at"),
    )
    id: Mapped[int] = mapped_column(PK, primary_key=True, autoincrement=True)
    type: Mapped[str] = mapped_column(String(48))
    payload: Mapped[dict[str, Any]] = mapped_column(JSONType, default=dict)
    idempotency_key: Mapped[str] = mapped_column(String(200))
    status: Mapped[str] = mapped_column(String(12), default="queued")  # queued | running | done | dead
    available_at: Mapped[datetime] = mapped_column(UTCDateTime)
    claimed_by: Mapped[str | None] = mapped_column(String(64))
    claim_expires_at: Mapped[datetime | None] = mapped_column(UTCDateTime)
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    max_attempts: Mapped[int] = mapped_column(Integer, default=6)
    last_error: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime)
    updated_at: Mapped[datetime] = mapped_column(UTCDateTime)


class AuditLog(Base):
    """Cambios de configuración y transiciones de estado."""

    __tablename__ = "audit_log"
    __table_args__ = (Index("ix_audit_log_entity", "entity", "entity_id"),)
    id: Mapped[int] = mapped_column(PK, primary_key=True, autoincrement=True)
    at: Mapped[datetime] = mapped_column(UTCDateTime, index=True)
    entity: Mapped[str] = mapped_column(String(32))
    entity_id: Mapped[str] = mapped_column(String(100))
    action: Mapped[str] = mapped_column(String(48))
    from_state: Mapped[str | None] = mapped_column(String(32))
    to_state: Mapped[str | None] = mapped_column(String(32))
    detail: Mapped[dict[str, Any]] = mapped_column(JSONType, default=dict)
