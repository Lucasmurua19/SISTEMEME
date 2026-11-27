"""Repositorios sobre SQLAlchemy.

Reglas de escritura:
- Los inserts idempotentes usan ``INSERT ... ON CONFLICT DO NOTHING`` y devuelven si insertaron.
- Las llamadas de red nunca ocurren dentro de estos métodos ni de las transacciones que los usan.
"""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Iterable, Sequence
from datetime import datetime, timedelta
from decimal import Decimal
from typing import Any

from sqlalchemy import func, select, update
from sqlalchemy.dialects import postgresql, sqlite
from sqlalchemy.orm import Session

from app.application.ports import Checkpoint, ConfigConflictError, FxRate, JobInfo
from app.domain.config import StrategyConfig
from app.domain.models import (
    AttemptInfo,
    MarketSnapshot,
    MigrationEvent,
    PoolInfo,
    PoolVerification,
    PositionInfo,
    Quote,
    QuoteCost,
    QuoteStatus,
    SessionInfo,
    SessionState,
    SignalInfo,
)
from app.domain.reasons import AttemptKind, AttemptStatus, LedgerKind, PositionStatus, SignalStatus
from app.domain.strategy import TERMINAL_CODES, EvaluationResult
from app.infrastructure import orm
from app.logging_config import redact


def insert_ignore(session: Session, model: Any, values: dict[str, Any], pk: Any) -> Any | None:
    """Inserta y devuelve la clave primaria; ``None`` si ya existía (restricción única)."""
    dialect = session.get_bind().dialect.name
    ins = postgresql.insert if dialect == "postgresql" else sqlite.insert
    stmt = ins(model).values(**values).on_conflict_do_nothing().returning(pk)
    return session.execute(stmt).scalar_one_or_none()


# ------------------------------------------------------------------ mapeos a registros de dominio
def _snap(r: orm.MarketSnapshotRow) -> MarketSnapshot:
    return MarketSnapshot(
        pool=r.pool,
        window_index=r.window_index,
        window_start=r.window_start,
        window_end=r.window_end,
        observed_at=r.observed_at,
        open=r.open,
        high=r.high,
        low=r.low,
        close=r.close,
        sol_reserve_lamports=r.sol_reserve_lamports,
        token_reserve_base=r.token_reserve_base,
        swap_count=r.swap_count,
        buy_count=r.buy_count,
        sell_count=r.sell_count,
        net_buyers=r.net_buyers,
        buy_volume_lamports=r.buy_volume_lamports,
        sell_volume_lamports=r.sell_volume_lamports,
        top10_concentration_pct=r.top10_concentration_pct,
        concentration_observed_at=r.concentration_observed_at,
        coverage=r.coverage,
        source=r.source,
        id=r.id,
    )


def _pool(p: orm.Pool, t: orm.Token) -> PoolInfo:
    return PoolInfo(
        address=p.address,
        mint=p.mint,
        symbol=t.symbol,
        name=t.name,
        decimals=t.decimals,
        program=p.program,
        quote_mint=p.quote_mint,
        migrated_at=p.migrated_at,
        migration_signature=p.migration_signature,
        migration_instruction_index=p.migration_instruction_index,
        migration_slot=p.migration_slot,
        verified=p.verified,
        identity_ok=p.identity_ok,
        canonical=p.canonical,
    )


def _signal(s: orm.Signal, mint: str) -> SignalInfo:
    return SignalInfo(
        id=s.id,
        evaluation_id=s.evaluation_id,
        pool=s.pool,
        mint=mint,
        strategy_version_id=s.strategy_version_id,
        status=s.status,
        issued_at=s.issued_at,
        expires_at=s.expires_at,
        amount_lamports=s.amount_lamports,
        invalidation_level=s.invalidation_level,
        entry_ref_price=s.entry_ref_price,
        score=s.score,
        liquidity_lamports=s.liquidity_lamports,
        round_trip_cost_pct=s.round_trip_cost_pct,
        buy_quote_id=s.buy_quote_id,
        sell_quote_id=s.sell_quote_id,
        buy_price_lamports_per_unit=s.buy_price_lamports_per_unit,
    )


def _session(r: orm.PaperSession) -> SessionInfo:
    return SessionInfo(
        id=r.id,
        name=r.name,
        policy=r.policy,
        strategy_version_id=r.strategy_version_id,
        capital_lamports=r.capital_lamports,
        trade_size_lamports=r.trade_size_lamports,
        max_exposure_lamports=r.max_exposure_lamports,
        max_positions=r.max_positions,
        max_session_loss_lamports=r.max_session_loss_lamports,
        paused=r.paused,
        pause_reason=r.pause_reason,
        started_at=r.started_at,
    )


def _position(r: orm.PaperPosition) -> PositionInfo:
    return PositionInfo(
        id=r.id,
        session_id=r.session_id,
        signal_id=r.signal_id,
        pool=r.pool,
        mint=r.mint,
        status=r.status,
        tokens_expected=r.tokens_expected,
        tokens_min=r.tokens_min,
        debited_lamports=r.debited_lamports,
        reserved_lamports=r.reserved_lamports,
        entry_price=r.entry_price,
        entry_liquidity_lamports=r.entry_liquidity_lamports,
        invalidation_level=r.invalidation_level,
        opened_at=r.opened_at,
        exit_trigger=r.exit_trigger,
        exit_failures=r.exit_failures,
        illiquid=r.illiquid,
        next_exit_at=r.next_exit_at,
        last_recoverable_lamports=r.last_recoverable_lamports,
        last_valued_at=r.last_valued_at,
        immobilized_lamports=r.immobilized_lamports,
        last_recoverable_adverse_lamports=r.last_recoverable_adverse_lamports,
    )


def _attempt(r: orm.PaperAttempt) -> AttemptInfo:
    return AttemptInfo(
        id=r.id,
        position_id=r.position_id,
        kind=r.kind,
        number=r.number,
        status=r.status,
        scheduled_at=r.scheduled_at,
        trigger=r.trigger,
        reference_out_lamports=r.reference_out_lamports,
        detail=r.detail or {},
    )


def quote_from_row(r: orm.QuoteRow) -> Quote:
    return Quote(
        side=r.side,
        input_mint=r.input_mint,
        output_mint=r.output_mint,
        in_amount=r.in_amount,
        out_amount=r.out_amount,
        min_out_amount=r.min_out_amount,
        observed_at=r.observed_at,
        expires_at=r.expires_at,
        status=QuoteStatus(r.status),
        route=r.route,
        price_impact_bps=r.price_impact_bps,
        costs=tuple(
            QuoteCost(c["kind"], int(c["lamports"]), c["included_in_amount"], c.get("refundable", False))
            for c in (r.costs or [])
        ),
        error=r.error,
        id=r.id,
    )


# ------------------------------------------------------------------ repositorios
class CheckpointRepo:
    def __init__(self, s: Session) -> None:
        self.s = s

    def _ensure(self, source: str, stream: str, now: datetime) -> orm.SourceCheckpoint:
        row = self.s.get(orm.SourceCheckpoint, (source, stream))
        if row is None:
            insert_ignore(
                self.s,
                orm.SourceCheckpoint,
                {"source": source, "stream": stream, "consecutive_failures": 0, "updated_at": now},
                orm.SourceCheckpoint.source,
            )
            row = self.s.get(orm.SourceCheckpoint, (source, stream))
            assert row is not None
        return row

    @staticmethod
    def _dto(r: orm.SourceCheckpoint) -> Checkpoint:
        return Checkpoint(
            r.source, r.stream, r.cursor, r.last_slot, r.last_healthy_at, r.last_error,
            r.consecutive_failures, r.updated_at,
        )  # fmt: skip

    def get(self, source: str, stream: str) -> Checkpoint | None:
        row = self.s.get(orm.SourceCheckpoint, (source, stream))
        return self._dto(row) if row else None

    def save_cursor(self, source: str, stream: str, cursor: str | None, slot: int | None, now: datetime) -> None:
        row = self._ensure(source, stream, now)
        row.cursor = cursor
        if slot is not None:
            row.last_slot = slot
        row.last_healthy_at = now
        row.last_error = None
        row.consecutive_failures = 0
        row.updated_at = now

    def mark_ok(self, source: str, stream: str, now: datetime) -> None:
        row = self._ensure(source, stream, now)
        row.last_healthy_at = now
        row.last_error = None
        row.consecutive_failures = 0
        row.updated_at = now

    def mark_failure(self, source: str, stream: str, now: datetime, error: str) -> None:
        row = self._ensure(source, stream, now)
        row.consecutive_failures += 1
        row.last_error = redact(error)[:500]  # se muestra en /api/v1/status: nunca con claves
        row.updated_at = now

    def list_all(self) -> list[Checkpoint]:
        return [self._dto(r) for r in self.s.scalars(select(orm.SourceCheckpoint).order_by(orm.SourceCheckpoint.source))]


class ChainRepo:
    def __init__(self, s: Session) -> None:
        self.s = s

    def record_migration(self, ev: MigrationEvent, ver: PoolVerification, now: datetime) -> bool:
        """Registra token, pool y evento. Devuelve ``True`` si el evento es nuevo."""
        insert_ignore(
            self.s,
            orm.Token,
            {"mint": ev.mint, "decimals": ev.decimals, "symbol": ev.symbol, "name": ev.name, "meta": {}, "first_seen_at": now},
            orm.Token.mint,
        )
        insert_ignore(
            self.s,
            orm.Pool,
            {
                "address": ev.pool,
                "mint": ev.mint,
                "program": ev.program,
                "quote_mint": ev.evidence.get("quote_mint", "So11111111111111111111111111111111111111112"),
                "migration_slot": ev.slot,
                "migration_signature": ev.signature,
                "migration_instruction_index": ev.instruction_index,
                "migrated_at": ev.event_at,
                "observed_at": ev.observed_at,
                "verified": ver.verified,
                "identity_ok": ver.identity_ok,
                "canonical": ver.canonical,
                "verification": {"reason": ver.reason, **ver.evidence},
                "status": "active",
                "created_at": now,
            },
            orm.Pool.address,
        )
        event_id = insert_ignore(
            self.s,
            orm.ChainEvent,
            {
                "kind": "migration",
                "signature": ev.signature,
                "instruction_index": ev.instruction_index,
                "event_index": ev.event_index,
                "pool": ev.pool,
                "slot": ev.slot,
                "confirmation": ev.confirmation,
                "event_at": ev.event_at,
                "observed_at": ev.observed_at,
                "evidence": ev.evidence,
            },
            orm.ChainEvent.id,
        )
        return event_id is not None

    def get_pool(self, address: str) -> PoolInfo | None:
        row = self.s.execute(
            select(orm.Pool, orm.Token).join(orm.Token, orm.Token.mint == orm.Pool.mint).where(orm.Pool.address == address)
        ).first()
        return _pool(*row) if row else None

    def list_active_pools(self, now: datetime, max_age_s: int) -> list[PoolInfo]:
        """Pools dentro de la ventana de estrategia, más los que tienen posiciones por observar."""
        cutoff = now - timedelta(seconds=max_age_s)
        watched = select(orm.PaperPosition.pool).where(
            orm.PaperPosition.status.in_([PositionStatus.PENDING_ENTRY.value, PositionStatus.OPEN.value])
        )
        rows = self.s.execute(
            select(orm.Pool, orm.Token)
            .join(orm.Token, orm.Token.mint == orm.Pool.mint)
            .where(orm.Pool.status == "active", orm.Pool.migrated_at <= now)
            .where((orm.Pool.migrated_at >= cutoff) | (orm.Pool.address.in_(watched)))
            .order_by(orm.Pool.migrated_at, orm.Pool.address)
        ).all()
        return [_pool(p, t) for p, t in rows]


class SnapshotRepo:
    def __init__(self, s: Session) -> None:
        self.s = s

    def upsert_many(self, snaps: Sequence[MarketSnapshot]) -> list[MarketSnapshot]:
        """Persiste las ventanas que faltan de un pool. Las ventanas cerradas son inmutables."""
        if not snaps:
            return []
        pool = snaps[0].pool
        existing = set(self.s.scalars(select(orm.MarketSnapshotRow.window_index).where(orm.MarketSnapshotRow.pool == pool)))
        for sn in snaps:
            if sn.window_index in existing:
                continue
            insert_ignore(
                self.s,
                orm.MarketSnapshotRow,
                {
                    "pool": sn.pool,
                    "window_index": sn.window_index,
                    "window_start": sn.window_start,
                    "window_end": sn.window_end,
                    "observed_at": sn.observed_at,
                    "open": sn.open,
                    "high": sn.high,
                    "low": sn.low,
                    "close": sn.close,
                    "sol_reserve_lamports": sn.sol_reserve_lamports,
                    "token_reserve_base": sn.token_reserve_base,
                    "swap_count": sn.swap_count,
                    "buy_count": sn.buy_count,
                    "sell_count": sn.sell_count,
                    "net_buyers": sn.net_buyers,
                    "buy_volume_lamports": sn.buy_volume_lamports,
                    "sell_volume_lamports": sn.sell_volume_lamports,
                    "top10_concentration_pct": sn.top10_concentration_pct,
                    "concentration_observed_at": sn.concentration_observed_at,
                    "coverage": sn.coverage,
                    "source": sn.source,
                },
                orm.MarketSnapshotRow.id,
            )
        return self.list_for_pool(pool)

    def list_for_pool(self, pool: str, until: datetime | None = None) -> list[MarketSnapshot]:
        stmt = select(orm.MarketSnapshotRow).where(orm.MarketSnapshotRow.pool == pool)
        if until is not None:
            stmt = stmt.where(orm.MarketSnapshotRow.observed_at <= until)
        return [_snap(r) for r in self.s.scalars(stmt.order_by(orm.MarketSnapshotRow.window_index))]


class QuoteRepo:
    def __init__(self, s: Session) -> None:
        self.s = s

    def save(self, q: Quote, *, pool: str | None, purpose: str) -> int:
        row = orm.QuoteRow(
            pool=pool,
            purpose=purpose,
            side=q.side,
            input_mint=q.input_mint,
            output_mint=q.output_mint,
            in_amount=q.in_amount,
            out_amount=q.out_amount,
            min_out_amount=q.min_out_amount,
            route=q.route[:200],
            price_impact_bps=q.price_impact_bps,
            costs=[c.to_dict() for c in q.costs],
            status=q.status.value,
            error=(q.error or None) and q.error[:500],
            request_id=q.request_id,
            observed_at=q.observed_at,
            expires_at=q.expires_at,
        )
        self.s.add(row)
        self.s.flush()
        return row.id

    def get(self, quote_id: int) -> Quote | None:
        row = self.s.get(orm.QuoteRow, quote_id)
        return quote_from_row(row) if row else None

    def save_fx(self, rate: FxRate) -> None:
        self.s.add(orm.FxRate(pair="SOL/USD", rate=rate.rate, source=rate.source, observed_at=rate.observed_at))


class StrategyRepo:
    def __init__(self, s: Session, audit: AuditRepo) -> None:
        self.s = s
        self.audit = audit

    def register(self, cfg: StrategyConfig, now: datetime) -> int:
        """Registra una versión inmutable. Mismo nombre con distinta huella es un error."""
        existing = self.s.scalar(select(orm.StrategyVersion).where(orm.StrategyVersion.name == cfg.name))
        if existing is not None:
            if existing.fingerprint != cfg.fingerprint():
                raise ConfigConflictError(
                    f"la versión '{cfg.name}' ya existe con otra configuración; crear una versión nueva"
                )
            return existing.id
        row = orm.StrategyVersion(name=cfg.name, fingerprint=cfg.fingerprint(), config=cfg.to_dict(), activated_at=now)
        self.s.add(row)
        self.s.flush()
        self.audit.log("strategy_version", str(row.id), "registered", None, cfg.name, {"fingerprint": row.fingerprint}, now)
        return row.id

    def get(self, version_id: int) -> StrategyConfig:
        row = self.s.get(orm.StrategyVersion, version_id)
        if row is None:
            raise KeyError(f"versión de estrategia {version_id} inexistente")
        return StrategyConfig.from_dict(row.config)

    def latest_id(self) -> int | None:
        return self.s.scalar(select(orm.StrategyVersion.id).order_by(orm.StrategyVersion.activated_at.desc(), orm.StrategyVersion.id.desc()).limit(1))


class EvaluationRepo:
    def __init__(self, s: Session) -> None:
        self.s = s

    def terminal_pools(self, version_id: int, pools: Sequence[str]) -> set[str]:
        """Pools cuya última evaluación en esta versión tiene un motivo definitivo (ver ``TERMINAL_CODES``)."""
        if not pools:
            return set()
        latest = (
            select(orm.Evaluation.pool, func.max(orm.Evaluation.window_index).label("last_window"))
            .where(orm.Evaluation.strategy_version_id == version_id, orm.Evaluation.pool.in_(list(pools)))
            .group_by(orm.Evaluation.pool)
            .subquery()
        )
        rows = self.s.execute(
            select(orm.Evaluation.pool, orm.Evaluation.reasons)
            .join(latest, (orm.Evaluation.pool == latest.c.pool) & (orm.Evaluation.window_index == latest.c.last_window))
            .where(orm.Evaluation.strategy_version_id == version_id)
        )
        terminal = {c.value for c in TERMINAL_CODES}
        return {pool for pool, reasons in rows if terminal & {r.get("code") for r in reasons or []}}

    def exists(self, pool: str, window_index: int, version_id: int) -> bool:
        return (
            self.s.scalar(
                select(orm.Evaluation.id).where(
                    orm.Evaluation.pool == pool,
                    orm.Evaluation.window_index == window_index,
                    orm.Evaluation.strategy_version_id == version_id,
                )
            )
            is not None
        )

    def save(
        self,
        *,
        pool: str,
        version_id: int,
        result: EvaluationResult,
        buy_quote_id: int | None,
        sell_quote_id: int | None,
        evaluated_at: datetime,
    ) -> int | None:
        """Guarda la evaluación de una ventana. Devuelve ``None`` si ya existía (idempotente)."""
        if result.window_index is None:
            return None
        snap = result.latest_snapshot
        return insert_ignore(
            self.s,
            orm.Evaluation,
            {
                "pool": pool,
                "window_index": result.window_index,
                "strategy_version_id": version_id,
                "snapshot_id": snap.id if snap else None,
                "status": result.status.value,
                "reasons": [r.to_dict() for r in result.reasons],
                "filters": [f.to_dict() for f in result.filters],
                "metrics": result.metrics,
                "pattern": result.pattern.to_dict(),
                "score": result.score,
                "score_components": [c.to_dict() for c in result.score_components],
                "round_trip": result.round_trip.to_dict() if result.round_trip else None,
                "snapshot_refs": list(result.snapshot_ids),
                "buy_quote_id": buy_quote_id,
                "sell_quote_id": sell_quote_id,
                "as_of": result.as_of,
                "evaluated_at": evaluated_at,
            },
            orm.Evaluation.id,
        )


class SignalRepo:
    def __init__(self, s: Session) -> None:
        self.s = s

    def create(
        self,
        *,
        evaluation_id: int,
        pool: str,
        version_id: int,
        issued_at: datetime,
        expires_at: datetime,
        amount_lamports: int,
        invalidation_level: Decimal,
        entry_ref_price: Decimal,
        score: Decimal,
        liquidity_lamports: int,
        round_trip_cost_pct: Decimal,
        buy_quote_id: int | None,
        sell_quote_id: int | None,
        buy_price_lamports_per_unit: Decimal | None,
    ) -> int | None:
        return insert_ignore(
            self.s,
            orm.Signal,
            {
                "evaluation_id": evaluation_id,
                "pool": pool,
                "strategy_version_id": version_id,
                "status": SignalStatus.ACTIVE.value,
                "issued_at": issued_at,
                "expires_at": expires_at,
                "amount_lamports": amount_lamports,
                "invalidation_level": invalidation_level,
                "entry_ref_price": entry_ref_price,
                "score": score,
                "liquidity_lamports": liquidity_lamports,
                "round_trip_cost_pct": round_trip_cost_pct,
                "buy_quote_id": buy_quote_id,
                "sell_quote_id": sell_quote_id,
                "buy_price_lamports_per_unit": buy_price_lamports_per_unit,
            },
            orm.Signal.id,
        )

    def get(self, signal_id: int) -> SignalInfo | None:
        row = self.s.execute(
            select(orm.Signal, orm.Pool.mint).join(orm.Pool, orm.Pool.address == orm.Signal.pool).where(orm.Signal.id == signal_id)
        ).first()
        return _signal(*row) if row else None

    def list_unprocessed(self, session_id: int, version_id: int, now: datetime) -> list[SignalInfo]:
        """Señales vigentes de la versión que esta sesión todavía no procesó."""
        taken = select(orm.PaperPosition.signal_id).where(orm.PaperPosition.session_id == session_id)
        rows = self.s.execute(
            select(orm.Signal, orm.Pool.mint)
            .join(orm.Pool, orm.Pool.address == orm.Signal.pool)
            .where(
                orm.Signal.strategy_version_id == version_id,
                orm.Signal.status == SignalStatus.ACTIVE.value,
                orm.Signal.expires_at > now,
                orm.Signal.id.not_in(taken),
            )
            .order_by(orm.Signal.id)
        ).all()
        return [_signal(s, m) for s, m in rows]

    def expire_due(self, now: datetime) -> int:
        res = self.s.execute(
            update(orm.Signal)
            .where(orm.Signal.status == SignalStatus.ACTIVE.value, orm.Signal.expires_at <= now)
            .values(status=SignalStatus.EXPIRED.value)
        )
        return res.rowcount or 0


class SessionRepo:
    def __init__(self, s: Session, audit: AuditRepo) -> None:
        self.s = s
        self.audit = audit

    def ensure(self, *, name: str, policy: str, version_id: int, cfg: StrategyConfig, now: datetime) -> SessionInfo:
        row = self.s.scalar(
            select(orm.PaperSession).where(orm.PaperSession.strategy_version_id == version_id, orm.PaperSession.policy == policy)
        )
        if row is None:
            row = orm.PaperSession(
                name=name,
                policy=policy,
                strategy_version_id=version_id,
                capital_lamports=cfg.session_capital_lamports,
                trade_size_lamports=cfg.trade_size_lamports,
                max_exposure_lamports=cfg.max_exposure_lamports,
                max_positions=cfg.max_positions,
                max_session_loss_lamports=cfg.max_session_loss_lamports,
                paused=False,
                started_at=now,
                updated_at=now,
            )
            self.s.add(row)
            self.s.flush()
            self.audit.log("paper_session", str(row.id), "created", None, policy, {"name": name}, now)
        return _session(row)

    def get(self, session_id: int) -> SessionInfo | None:
        row = self.s.get(orm.PaperSession, session_id)
        return _session(row) if row else None

    def lock(self, session_id: int) -> SessionInfo | None:
        """Bloquea la fila de la sesión (FOR UPDATE) para serializar reservas de capital."""
        row = self.s.scalar(select(orm.PaperSession).where(orm.PaperSession.id == session_id).with_for_update())
        return _session(row) if row else None

    def list_for_version(self, version_id: int) -> list[SessionInfo]:
        rows = self.s.scalars(
            select(orm.PaperSession).where(orm.PaperSession.strategy_version_id == version_id).order_by(orm.PaperSession.id)
        )
        return [_session(r) for r in rows]

    def set_paused(self, session_id: int, paused: bool, reason: str | None, now: datetime) -> SessionInfo | None:
        row = self.s.scalar(select(orm.PaperSession).where(orm.PaperSession.id == session_id).with_for_update())
        if row is None:
            return None
        if row.paused != paused:
            self.audit.log(
                "paper_session", str(row.id), "paused" if paused else "resumed",
                "paused" if row.paused else "running", "paused" if paused else "running",
                {"reason": reason}, now,
            )  # fmt: skip
        row.paused = paused
        row.pause_reason = reason if paused else None
        row.updated_at = now
        return _session(row)

    def state(self, session_id: int) -> SessionState:
        sess = self.s.get(orm.PaperSession, session_id)
        assert sess is not None
        sums: dict[str, int] = defaultdict(int)
        for kind, amount in self.s.execute(
            select(orm.PaperLedger.kind, orm.PaperLedger.amount_lamports).where(orm.PaperLedger.session_id == session_id)
        ):
            sums[kind] += amount
        cash = sess.capital_lamports + sums[LedgerKind.CREDIT.value] - sums[LedgerKind.DEBIT.value]
        reserved = sums[LedgerKind.RESERVE.value] - sums[LedgerKind.RELEASE.value]

        active = 0
        cost_basis = 0
        deficit = 0
        realized = 0
        for status, debited, proceeds, adverse in self.s.execute(
            select(
                orm.PaperPosition.status,
                orm.PaperPosition.debited_lamports,
                orm.PaperPosition.proceeds_lamports,
                orm.PaperPosition.last_recoverable_adverse_lamports,
            ).where(orm.PaperPosition.session_id == session_id)
        ):
            if status == PositionStatus.PENDING_ENTRY.value:
                active += 1
            elif status == PositionStatus.OPEN.value:
                active += 1
                cost_basis += debited
                deficit += max(0, debited - (adverse or 0))
            elif status == PositionStatus.CLOSED.value:
                realized += (proceeds or 0) - debited
        return SessionState(
            capital_lamports=sess.capital_lamports,
            cash_lamports=cash,
            reserved_lamports=reserved,
            active_positions=active,
            open_cost_basis_lamports=cost_basis,
            realized_pnl_lamports=realized,
            open_deficit_lamports=deficit,
            paused=sess.paused,
        )


class PositionRepo:
    def __init__(self, s: Session) -> None:
        self.s = s

    def create(
        self,
        *,
        session_id: int,
        signal: SignalInfo,
        status: PositionStatus,
        reserved_lamports: int,
        now: datetime,
        reject_reason: str | None = None,
        reject_detail: dict[str, Any] | None = None,
    ) -> int | None:
        """Una posición por (sesión, señal): impide que dos workers abran la misma señal."""
        return insert_ignore(
            self.s,
            orm.PaperPosition,
            {
                "session_id": session_id,
                "signal_id": signal.id,
                "pool": signal.pool,
                "mint": signal.mint,
                "status": status.value,
                "reject_reason": reject_reason,
                "reject_detail": reject_detail or {},
                "reserved_lamports": reserved_lamports,
                "invalidation_level": signal.invalidation_level,
                "created_at": now,
                "updated_at": now,
            },
            orm.PaperPosition.id,
        )

    def get(self, position_id: int, *, lock: bool = False) -> PositionInfo | None:
        stmt = select(orm.PaperPosition).where(orm.PaperPosition.id == position_id)
        if lock:
            stmt = stmt.with_for_update()
        row = self.s.scalar(stmt)
        return _position(row) if row else None

    def update(self, position_id: int, now: datetime, **fields: Any) -> None:
        row = self.s.get(orm.PaperPosition, position_id)
        assert row is not None, position_id
        for key, value in fields.items():
            setattr(row, key, value)
        row.updated_at = now

    def list(self, statuses: Iterable[PositionStatus], session_id: int | None = None) -> list[PositionInfo]:
        stmt = select(orm.PaperPosition).where(orm.PaperPosition.status.in_([s.value for s in statuses]))
        if session_id is not None:
            stmt = stmt.where(orm.PaperPosition.session_id == session_id)
        return [_position(r) for r in self.s.scalars(stmt.order_by(orm.PaperPosition.id))]


class AttemptRepo:
    def __init__(self, s: Session) -> None:
        self.s = s

    def create(
        self,
        *,
        position_id: int,
        kind: str,
        number: int,
        scheduled_at: datetime,
        trigger: str | None = None,
        reference_out_lamports: int | None = None,
        detail: dict[str, Any] | None = None,
    ) -> int | None:
        return insert_ignore(
            self.s,
            orm.PaperAttempt,
            {
                "position_id": position_id,
                "kind": kind,
                "number": number,
                "status": AttemptStatus.PENDING.value,
                "scheduled_at": scheduled_at,
                "trigger": trigger,
                "reference_out_lamports": reference_out_lamports,
                "detail": detail or {},
            },
            orm.PaperAttempt.id,
        )

    def get(self, attempt_id: int, *, lock: bool = False) -> AttemptInfo | None:
        stmt = select(orm.PaperAttempt).where(orm.PaperAttempt.id == attempt_id)
        if lock:
            stmt = stmt.with_for_update()
        row = self.s.scalar(stmt)
        return _attempt(row) if row else None

    def entry_batch(self, signal_id: int, scheduled_at: datetime) -> list[tuple[int, int]]:
        """``(sesión, intento)`` de las entradas pendientes de una señal programadas para ese instante, por sesión."""
        rows = self.s.execute(
            select(orm.PaperPosition.session_id, orm.PaperAttempt.id, orm.PaperAttempt.scheduled_at)
            .join(orm.PaperPosition, orm.PaperPosition.id == orm.PaperAttempt.position_id)
            .where(
                orm.PaperPosition.signal_id == signal_id,
                orm.PaperAttempt.kind == AttemptKind.ENTRY.value,
                orm.PaperAttempt.status == AttemptStatus.PENDING.value,
            )
            .order_by(orm.PaperPosition.session_id)
        )
        return [(session_id, attempt_id) for session_id, attempt_id, due in rows if due == scheduled_at]

    def finish(
        self,
        attempt_id: int,
        *,
        status: AttemptStatus,
        executed_at: datetime,
        quote_id: int | None = None,
        requested_amount: int | None = None,
        result_amount: int | None = None,
        failure_cause: str | None = None,
        detail: dict[str, Any] | None = None,
    ) -> None:
        row = self.s.get(orm.PaperAttempt, attempt_id)
        assert row is not None
        row.status = status.value
        row.executed_at = executed_at
        row.quote_id = quote_id
        row.requested_amount = requested_amount
        row.result_amount = result_amount
        row.failure_cause = failure_cause
        row.detail = {**(row.detail or {}), **(detail or {})}

    def last_number(self, position_id: int, kind: str) -> int:
        return (
            self.s.scalar(
                select(func.max(orm.PaperAttempt.number)).where(
                    orm.PaperAttempt.position_id == position_id, orm.PaperAttempt.kind == kind
                )
            )
            or 0
        )

    def pending(self, position_id: int, kind: str) -> AttemptInfo | None:
        row = self.s.scalar(
            select(orm.PaperAttempt).where(
                orm.PaperAttempt.position_id == position_id,
                orm.PaperAttempt.kind == kind,
                orm.PaperAttempt.status == AttemptStatus.PENDING.value,
            )
        )
        return _attempt(row) if row else None


class LedgerRepo:
    def __init__(self, s: Session) -> None:
        self.s = s

    def add(
        self, *, session_id: int, position_id: int, attempt_id: int, kind: LedgerKind, amount: int, now: datetime
    ) -> bool:
        """Asiento único por (intento, tipo): aplicar dos veces el mismo intento no duplica capital."""
        return (
            insert_ignore(
                self.s,
                orm.PaperLedger,
                {
                    "session_id": session_id,
                    "position_id": position_id,
                    "attempt_id": attempt_id,
                    "kind": kind.value,
                    "amount_lamports": amount,
                    "created_at": now,
                },
                orm.PaperLedger.id,
            )
            is not None
        )


class JobRepo:
    def __init__(self, s: Session) -> None:
        self.s = s

    def enqueue(
        self, *, type: str, payload: dict[str, Any], key: str, run_at: datetime, now: datetime, max_attempts: int = 6
    ) -> bool:
        return (
            insert_ignore(
                self.s,
                orm.Job,
                {
                    "type": type,
                    "payload": payload,
                    "idempotency_key": key,
                    "status": "queued",
                    "available_at": run_at,
                    "attempts": 0,
                    "max_attempts": max_attempts,
                    "created_at": now,
                    "updated_at": now,
                },
                orm.Job.id,
            )
            is not None
        )

    def claim(self, *, worker_id: str, now: datetime, limit: int, claim_ttl_s: int) -> list[JobInfo]:
        """Reclama trabajos vencidos o sin tomar. ``SKIP LOCKED`` permite varios workers sin pisarse."""
        stmt = (
            select(orm.Job)
            .where(
                ((orm.Job.status == "queued") & (orm.Job.available_at <= now))
                | ((orm.Job.status == "running") & (orm.Job.claim_expires_at <= now))
            )
            .order_by(orm.Job.available_at, orm.Job.id)
            .limit(limit)
            .with_for_update(skip_locked=True)
        )
        out: list[JobInfo] = []
        for row in self.s.scalars(stmt):
            row.status = "running"
            row.claimed_by = worker_id
            row.claim_expires_at = now + timedelta(seconds=claim_ttl_s)
            row.attempts += 1
            row.updated_at = now
            out.append(JobInfo(row.id, row.type, row.payload or {}, row.idempotency_key, row.attempts, row.max_attempts))
        return out

    def complete(self, job_id: int, now: datetime) -> None:
        row = self.s.get(orm.Job, job_id)
        assert row is not None
        row.status = "done"
        row.claim_expires_at = None
        row.updated_at = now

    def fail(
        self,
        job_id: int,
        now: datetime,
        error: str,
        *,
        permanent: bool = False,
        base_backoff_s: int = 5,
        max_backoff_s: int = 300,
    ) -> str:
        """Reintenta con espera creciente; tras agotar intentos (o error permanente) queda ``dead``."""
        row = self.s.get(orm.Job, job_id)
        assert row is not None
        row.last_error = redact(error)[:1000]
        row.claim_expires_at = None
        row.updated_at = now
        if permanent or row.attempts >= row.max_attempts:
            row.status = "dead"
        else:
            row.status = "queued"
            row.available_at = now + timedelta(seconds=min(max_backoff_s, base_backoff_s * 2 ** (row.attempts - 1)))
        return row.status


class AuditRepo:
    def __init__(self, s: Session) -> None:
        self.s = s

    def log(
        self,
        entity: str,
        entity_id: str,
        action: str,
        from_state: str | None,
        to_state: str | None,
        detail: dict[str, Any],
        now: datetime,
    ) -> None:
        self.s.add(
            orm.AuditLog(
                at=now,
                entity=entity,
                entity_id=entity_id,
                action=action,
                from_state=from_state,
                to_state=to_state,
                detail=detail,
            )
        )
