"""Modelos de lectura para la API: consultas sobre datos persistidos (sin llamadas externas).

Los enteros grandes se serializan como cadenas y los decimales como texto exacto.
"""

from __future__ import annotations

from collections import Counter, defaultdict
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.application import health as health_mod
from app.application.candidates import effective_status
from app.domain.config import StrategyConfig
from app.domain.metrics import ClosedTrade, OpenTrade, compute_metrics
from app.domain.reasons import CandidateStatus, PositionStatus
from app.infrastructure import orm
from app.infrastructure.repositories import AuditRepo, CheckpointRepo, EvaluationRepo, SessionRepo


# ------------------------------------------------------------------ utilidades
def iso(dt: datetime | None) -> str | None:
    return dt.astimezone(UTC).isoformat() if dt else None


def dstr(v: Decimal | int | None) -> str | None:
    return None if v is None else str(v)


def sanitize(text: str | None, limit: int = 64) -> str | None:
    """Nombres y símbolos son datos externos: se quitan caracteres de control y se acota el largo."""
    if text is None:
        return None
    cleaned = "".join(ch for ch in text if ch.isprintable())
    return cleaned[:limit]


def system_now(s: Session) -> datetime:
    """Reloj del sistema según el último latido del worker (en fixtures es el reloj de reproducción)."""
    cp = CheckpointRepo(s).get(health_mod.WORKER_SOURCE, health_mod.HEARTBEAT_STREAM)
    if cp and cp.cursor:
        try:
            return datetime.fromisoformat(cp.cursor)
        except ValueError:
            pass
    return datetime.now(UTC)


def latest_version(s: Session) -> orm.StrategyVersion | None:
    return s.scalar(select(orm.StrategyVersion).order_by(orm.StrategyVersion.activated_at.desc(), orm.StrategyVersion.id.desc()).limit(1))


def version_cfg(row: orm.StrategyVersion) -> StrategyConfig:
    return StrategyConfig.from_dict(row.config)


def default_session(s: Session, version_id: int | None = None, policy: str = "ranking") -> orm.PaperSession | None:
    stmt = select(orm.PaperSession).where(orm.PaperSession.policy == policy).order_by(orm.PaperSession.id.desc()).limit(1)
    if version_id is not None:
        stmt = select(orm.PaperSession).where(orm.PaperSession.policy == policy, orm.PaperSession.strategy_version_id == version_id).limit(1)
    return s.scalar(stmt)


# ------------------------------------------------------------------ estado general y salud
def system_status(s: Session, *, max_age_s: int, max_failures: int, worker_max_silence_s: int, data_mode: str) -> dict[str, Any]:
    cps = CheckpointRepo(s).list_all()
    as_of = system_now(s)
    heartbeat = next((c for c in cps if c.source == health_mod.WORKER_SOURCE), None)
    real_now = datetime.now(UTC)
    silence = (real_now - heartbeat.updated_at).total_seconds() if heartbeat else None
    alive = heartbeat is not None and silence is not None and silence <= worker_max_silence_s
    sources = health_mod.assess(cps, now=as_of, max_age_s=max_age_s, max_failures=max_failures)
    migrations = next((c for c in cps if c.stream == "migrations"), None)
    ingest_lag = (as_of - migrations.last_healthy_at).total_seconds() if migrations and migrations.last_healthy_at else None
    version = latest_version(s)
    degraded = [x for x in sources if x.degraded]
    if not alive:
        overall = "down"
    elif degraded:
        overall = "degraded"
    else:
        overall = "ok"
    return {
        "overall": overall,
        "as_of": iso(as_of),
        "data_mode": data_mode,
        "worker": {
            "alive": alive,
            "last_heartbeat_at": iso(heartbeat.updated_at) if heartbeat else None,
            "silence_s": None if silence is None else round(silence, 1),
        },
        "ingest_lag_s": None if ingest_lag is None else round(ingest_lag, 1),
        "last_slot": max((c.last_slot for c in cps if c.last_slot is not None), default=None),
        "strategy": {"id": version.id, "name": version.name, "fingerprint": version.fingerprint} if version else None,
        "sources": [
            {
                "source": x.source,
                "stream": x.stream,
                "degraded": x.degraded,
                "reason": x.reason,
                "last_healthy_at": iso(x.last_healthy_at),
                "consecutive_failures": x.consecutive_failures,
                "last_error": x.last_error,
                "cursor": x.cursor if x.stream == "migrations" else None,
                "last_slot": x.last_slot,
            }
            for x in sources
        ],
    }


# ------------------------------------------------------------------ candidatos
def _latest_evaluations(s: Session, version_id: int) -> list[tuple[orm.Evaluation, orm.Pool, orm.Token]]:
    latest = (
        select(orm.Evaluation.pool.label("pool"), func.max(orm.Evaluation.window_index).label("w"))
        .where(orm.Evaluation.strategy_version_id == version_id)
        .group_by(orm.Evaluation.pool)
        .subquery()
    )
    rows = s.execute(
        select(orm.Evaluation, orm.Pool, orm.Token)
        .join(latest, (orm.Evaluation.pool == latest.c.pool) & (orm.Evaluation.window_index == latest.c.w))
        .join(orm.Pool, orm.Pool.address == orm.Evaluation.pool)
        .join(orm.Token, orm.Token.mint == orm.Pool.mint)
        .where(orm.Evaluation.strategy_version_id == version_id)
    ).all()
    return [(e, p, t) for e, p, t in rows]


def candidate_rows(s: Session, version: orm.StrategyVersion, as_of: datetime) -> list[dict[str, Any]]:
    cfg = version_cfg(version)
    evals = _latest_evaluations(s, version.id)
    signals = {x.pool: x for x in s.scalars(select(orm.Signal).where(orm.Signal.strategy_version_id == version.id))}
    last_end = dict(s.execute(select(orm.MarketSnapshotRow.pool, func.max(orm.MarketSnapshotRow.window_end)).group_by(orm.MarketSnapshotRow.pool)).all())
    policies = {x.id: x.policy for x in s.scalars(select(orm.PaperSession).where(orm.PaperSession.strategy_version_id == version.id))}
    positions: dict[str, dict[str, str]] = defaultdict(dict)
    for p in s.scalars(select(orm.PaperPosition).where(orm.PaperPosition.session_id.in_(list(policies) or [0]))):
        label = p.status if not p.reject_reason else f"{p.status}:{p.reject_reason}"
        positions[p.pool][policies[p.session_id]] = label

    rows: list[dict[str, Any]] = []
    for ev, pool, token in evals:
        sig = signals.get(pool.address)
        eff = effective_status(
            base_status=ev.status,
            base_reasons=ev.reasons or [],
            migrated_at=pool.migrated_at,
            last_window_end=last_end.get(pool.address),
            signal_expires_at=sig.expires_at if sig else None,
            signal_status=sig.status if sig else None,
            as_of=as_of,
            cfg=cfg,
        )
        data_end = last_end.get(pool.address)
        rows.append(
            {
                "mint": token.mint,
                "symbol": sanitize(token.symbol, 32),
                "name": sanitize(token.name, 128),
                "pool": pool.address,
                "status": eff.status,
                "reasons": eff.reasons,
                "score": dstr(ev.score) if eff.status == "eligible" or ev.score is not None else None,
                "liquidity_lamports": (ev.metrics or {}).get("liquidity_lamports"),
                "round_trip_cost_pct": (ev.metrics or {}).get("round_trip_cost_pct"),
                "window_index": ev.window_index,
                "evaluated_at": iso(ev.evaluated_at),
                "migrated_at": iso(pool.migrated_at),
                "age_s": int((as_of - pool.migrated_at).total_seconds()),
                "data_age_s": None if data_end is None else int((as_of - data_end).total_seconds()),
                "migration_signature": pool.migration_signature,
                "verified": pool.verified and pool.identity_ok,
                "canonical": pool.canonical,
                "signal": None
                if sig is None
                else {
                    "id": sig.id,
                    "status": sig.status,
                    "issued_at": iso(sig.issued_at),
                    "expires_at": iso(sig.expires_at),
                    "invalidation_level": dstr(sig.invalidation_level),
                },
                "positions": positions.get(pool.address, {}),
                "_sort_score": ev.score,
                "_sort_cost": Decimal((ev.metrics or {}).get("round_trip_cost_pct", "999")),
                "_evaluated_at": ev.evaluated_at,
            }
        )
    return rows


def list_candidates(
    s: Session, *, status: str | None, limit: int, offset: int, as_of: datetime
) -> dict[str, Any]:
    version = latest_version(s)
    if version is None:
        return {"as_of": iso(as_of), "strategy_version": None, "total": 0, "items": [], "no_entry": None, "counts": {}}
    rows = candidate_rows(s, version, as_of)
    counts = Counter(r["status"] for r in rows)

    def order(r: dict[str, Any]):
        eligible = r["status"] == CandidateStatus.ELIGIBLE.value
        score = r["_sort_score"] if r["_sort_score"] is not None else Decimal(-1)
        return (0 if eligible else 1, -score if eligible else Decimal(0), r["_sort_cost"] if eligible else Decimal(0), -r["_evaluated_at"].timestamp())

    rows.sort(key=order)
    filtered = rows if status in (None, "", "all") else [r for r in rows if r["status"] == status]

    no_entry = None
    if counts.get(CandidateStatus.ELIGIBLE.value, 0) == 0:
        reason_counts: Counter[str] = Counter()
        for r in rows:
            for code in {x["code"] for x in r["reasons"]}:
                reason_counts[code] += 1
        no_entry = {
            "message": "Sin entrada: ningún token pasa todos los filtros en este momento.",
            "tracked": len(rows),
            "reasons": [{"code": c, "count": n} for c, n in reason_counts.most_common()],
        }
    page = filtered[offset : offset + limit]
    for r in page:
        r.pop("_sort_score"), r.pop("_sort_cost"), r.pop("_evaluated_at")
    return {
        "as_of": iso(as_of),
        "strategy_version": {"id": version.id, "name": version.name},
        "total": len(filtered),
        "limit": limit,
        "offset": offset,
        "counts": dict(counts),
        "items": page,
        "no_entry": no_entry,
    }


def list_signals(s: Session, limit: int) -> list[dict[str, Any]]:
    """Primeras señales para depuración: tiempos, cotizaciones y evidencia del patrón."""
    rows = s.execute(
        select(orm.Signal, orm.Token, orm.Evaluation)
        .join(orm.Pool, orm.Pool.address == orm.Signal.pool)
        .join(orm.Token, orm.Token.mint == orm.Pool.mint)
        .join(orm.Evaluation, orm.Evaluation.id == orm.Signal.evaluation_id)
        .order_by(orm.Signal.id)
        .limit(limit)
    ).all()
    return [
        {
            "id": sig.id,
            "mint": tok.mint,
            "symbol": sanitize(tok.symbol, 32),
            "pool": sig.pool,
            "issued_at": iso(sig.issued_at),
            "expires_at": iso(sig.expires_at),
            "status": sig.status,
            "score": dstr(sig.score),
            "round_trip_cost_pct": dstr(sig.round_trip_cost_pct),
            "invalidation_level": dstr(sig.invalidation_level),
            "window_index": ev.window_index,
            "pattern": ev.pattern,
            "evaluation_id": ev.id,
        }
        for sig, tok, ev in rows
    ]


# ------------------------------------------------------------------ detalle de token
def _attempt_dict(a: orm.PaperAttempt) -> dict[str, Any]:
    return {
        "id": a.id,
        "kind": a.kind,
        "number": a.number,
        "status": a.status,
        "scheduled_at": iso(a.scheduled_at),
        "executed_at": iso(a.executed_at),
        "trigger": a.trigger,
        "failure_cause": a.failure_cause,
        "requested_amount": dstr(a.requested_amount),
        "result_amount": dstr(a.result_amount),
        "detail": a.detail,
    }


def position_dict(p: orm.PaperPosition, attempts: list[orm.PaperAttempt], *, symbol: str | None = None, policy: str | None = None) -> dict[str, Any]:
    return {
        "id": p.id,
        "session_id": p.session_id,
        "policy": policy,
        "signal_id": p.signal_id,
        "mint": p.mint,
        "symbol": sanitize(symbol, 32),
        "pool": p.pool,
        "status": p.status,
        "reject_reason": p.reject_reason,
        "reject_detail": p.reject_detail if p.status == PositionStatus.REJECTED.value else None,
        "tokens_expected": dstr(p.tokens_expected),
        "tokens_min": dstr(p.tokens_min),
        "debited_lamports": dstr(p.debited_lamports),
        "reserved_lamports": dstr(p.reserved_lamports),
        "immobilized_lamports": dstr(p.immobilized_lamports),
        "entry_price": dstr(p.entry_price),
        "opened_at": iso(p.opened_at),
        "closed_at": iso(p.closed_at),
        "exit_trigger": p.exit_trigger,
        "exit_failures": p.exit_failures,
        "illiquid": p.illiquid,
        "next_exit_at": iso(p.next_exit_at),
        "last_recoverable_lamports": dstr(p.last_recoverable_lamports),
        "last_recoverable_adverse_lamports": dstr(p.last_recoverable_adverse_lamports),
        "last_valued_at": iso(p.last_valued_at),
        "valuation_status": p.valuation_status,
        "proceeds_lamports": dstr(p.proceeds_lamports),
        "adverse_proceeds_lamports": dstr(p.adverse_proceeds_lamports),
        "pnl_lamports": dstr(p.pnl_lamports),
        "zero_recovery_pnl_lamports": dstr(-p.debited_lamports) if p.status == PositionStatus.OPEN.value and p.illiquid else None,
        "sol_usd_open": dstr(p.sol_usd_open),
        "sol_usd_close": dstr(p.sol_usd_close),
        "attempts": [_attempt_dict(a) for a in attempts],
    }


def token_detail(s: Session, mint: str, as_of: datetime) -> dict[str, Any] | None:
    token = s.get(orm.Token, mint)
    if token is None:
        return None
    version = latest_version(s)
    pools = list(s.scalars(select(orm.Pool).where(orm.Pool.mint == mint).order_by(orm.Pool.migrated_at)))
    policies = {x.id: x.policy for x in s.scalars(select(orm.PaperSession))}
    out_pools = []
    for pool in pools:
        snaps = list(
            s.scalars(
                select(orm.MarketSnapshotRow).where(orm.MarketSnapshotRow.pool == pool.address).order_by(orm.MarketSnapshotRow.window_index)
            )
        )
        evs = list(
            s.scalars(
                select(orm.Evaluation)
                .where(orm.Evaluation.pool == pool.address, *( [orm.Evaluation.strategy_version_id == version.id] if version else []))
                .order_by(orm.Evaluation.window_index)
            )
        )
        quotes = list(
            s.scalars(select(orm.QuoteRow).where(orm.QuoteRow.pool == pool.address).order_by(orm.QuoteRow.id.desc()).limit(12))
        )
        sig = s.scalar(select(orm.Signal).where(orm.Signal.pool == pool.address, *([orm.Signal.strategy_version_id == version.id] if version else [])))
        positions = list(s.scalars(select(orm.PaperPosition).where(orm.PaperPosition.pool == pool.address).order_by(orm.PaperPosition.id)))
        attempts: dict[int, list[orm.PaperAttempt]] = defaultdict(list)
        if positions:
            for a in s.scalars(select(orm.PaperAttempt).where(orm.PaperAttempt.position_id.in_([p.id for p in positions])).order_by(orm.PaperAttempt.id)):
                attempts[a.position_id].append(a)
        last = evs[-1] if evs else None
        last_eval = None
        if last is not None and version is not None:
            eff = effective_status(
                base_status=last.status,
                base_reasons=last.reasons or [],
                migrated_at=pool.migrated_at,
                last_window_end=snaps[-1].window_end if snaps else None,
                signal_expires_at=sig.expires_at if sig else None,
                signal_status=sig.status if sig else None,
                as_of=as_of,
                cfg=version_cfg(version),
            )
            last_eval = {
                "id": last.id,
                "window_index": last.window_index,
                "status": eff.status,
                "recorded_status": last.status,
                "reasons": eff.reasons,
                "filters": last.filters,
                "metrics": last.metrics,
                "pattern": last.pattern,
                "score": dstr(last.score),
                "score_components": last.score_components,
                "round_trip": last.round_trip,
                "as_of": iso(last.as_of),
                "evaluated_at": iso(last.evaluated_at),
                "snapshot_refs": last.snapshot_refs,
            }
        out_pools.append(
            {
                "address": pool.address,
                "program": pool.program,
                "quote_mint": pool.quote_mint,
                "migrated_at": iso(pool.migrated_at),
                "migration": {
                    "signature": pool.migration_signature,
                    "instruction_index": pool.migration_instruction_index,
                    "slot": pool.migration_slot,
                },
                "verified": pool.verified,
                "identity_ok": pool.identity_ok,
                "canonical": pool.canonical,
                "verification": pool.verification,
                "snapshots": [
                    {
                        "window_index": x.window_index,
                        "window_end": iso(x.window_end),
                        "observed_at": iso(x.observed_at),
                        "open": dstr(x.open),
                        "high": dstr(x.high),
                        "low": dstr(x.low),
                        "close": dstr(x.close),
                        "liquidity_lamports": dstr(2 * x.sol_reserve_lamports),
                        "swap_count": x.swap_count,
                        "net_buyers": x.net_buyers,
                        "buy_volume_lamports": dstr(x.buy_volume_lamports),
                        "sell_volume_lamports": dstr(x.sell_volume_lamports),
                        "top10_concentration_pct": dstr(x.top10_concentration_pct),
                        "coverage": dstr(x.coverage),
                    }
                    for x in snaps
                ],
                "evaluation": last_eval,
                "history": [
                    {"window_index": e.window_index, "status": e.status, "score": dstr(e.score), "as_of": iso(e.as_of)} for e in evs
                ],
                "quotes": [
                    {
                        "id": q.id,
                        "purpose": q.purpose,
                        "side": q.side,
                        "status": q.status,
                        "in_amount": dstr(q.in_amount),
                        "out_amount": dstr(q.out_amount),
                        "min_out_amount": dstr(q.min_out_amount),
                        "route": q.route,
                        "price_impact_bps": dstr(q.price_impact_bps),
                        "costs": q.costs,
                        "error": q.error,
                        "observed_at": iso(q.observed_at),
                        "expires_at": iso(q.expires_at),
                    }
                    for q in quotes
                ],
                "signal": None
                if sig is None
                else {
                    "id": sig.id,
                    "status": sig.status,
                    "issued_at": iso(sig.issued_at),
                    "expires_at": iso(sig.expires_at),
                    "score": dstr(sig.score),
                    "round_trip_cost_pct": dstr(sig.round_trip_cost_pct),
                    "invalidation_level": dstr(sig.invalidation_level),
                    "entry_ref_price": dstr(sig.entry_ref_price),
                },
                "positions": [
                    position_dict(p, attempts.get(p.id, []), symbol=token.symbol, policy=policies.get(p.session_id)) for p in positions
                ],
            }
        )
    return {
        "mint": token.mint,
        "symbol": sanitize(token.symbol, 32),
        "name": sanitize(token.name, 128),
        "decimals": token.decimals,
        "as_of": iso(as_of),
        "pools": out_pools,
    }


# ------------------------------------------------------------------ simulación
def _session_info(s: Session, sess: orm.PaperSession) -> dict[str, Any]:
    state = SessionRepo(s, AuditRepo(s)).state(sess.id)
    return {
        "id": sess.id,
        "name": sess.name,
        "policy": sess.policy,
        "strategy_version_id": sess.strategy_version_id,
        "paused": sess.paused,
        "pause_reason": sess.pause_reason,
        "started_at": iso(sess.started_at),
        "limits": {
            "trade_size_lamports": str(sess.trade_size_lamports),
            "max_exposure_lamports": str(sess.max_exposure_lamports),
            "max_positions": sess.max_positions,
            "max_session_loss_lamports": str(sess.max_session_loss_lamports),
        },
        "capital": {
            "capital_lamports": str(state.capital_lamports),
            "cash_lamports": str(state.cash_lamports),
            "reserved_lamports": str(state.reserved_lamports),
            "available_lamports": str(state.available_lamports),
            "open_cost_basis_lamports": str(state.open_cost_basis_lamports),
            "realized_pnl_lamports": str(state.realized_pnl_lamports),
            "open_deficit_lamports": str(state.open_deficit_lamports),
            "net_session_pnl_lamports": str(state.net_session_pnl_lamports),
            "active_positions": state.active_positions,
        },
    }


def list_sessions(s: Session) -> list[dict[str, Any]]:
    return [_session_info(s, x) for x in s.scalars(select(orm.PaperSession).order_by(orm.PaperSession.id))]


def paper_trades(s: Session, session_id: int, *, status: str | None = None) -> dict[str, Any] | None:
    sess = s.get(orm.PaperSession, session_id)
    if sess is None:
        return None
    stmt = (
        select(orm.PaperPosition, orm.Token.symbol)
        .join(orm.Token, orm.Token.mint == orm.PaperPosition.mint)
        .where(orm.PaperPosition.session_id == session_id)
        .order_by(orm.PaperPosition.id)
    )
    if status:
        stmt = stmt.where(orm.PaperPosition.status == status)
    rows = s.execute(stmt).all()
    attempts: dict[int, list[orm.PaperAttempt]] = defaultdict(list)
    ids = [p.id for p, _ in rows]
    if ids:
        for a in s.scalars(select(orm.PaperAttempt).where(orm.PaperAttempt.position_id.in_(ids)).order_by(orm.PaperAttempt.id)):
            attempts[a.position_id].append(a)
    return {
        "session": _session_info(s, sess),
        "positions": [position_dict(p, attempts.get(p.id, []), symbol=sym, policy=sess.policy) for p, sym in rows],
    }


def _trades_for(s: Session, session_id: int, since: datetime | None, until: datetime | None):
    closed: list[ClosedTrade] = []
    open_: list[OpenTrade] = []
    rejected = 0
    reasons: Counter[str] = Counter()
    for p in s.scalars(select(orm.PaperPosition).where(orm.PaperPosition.session_id == session_id)):
        if p.status == PositionStatus.CLOSED.value and p.closed_at is not None:
            if (since and p.closed_at < since) or (until and p.closed_at >= until):
                continue
            closed.append(
                ClosedTrade(
                    p.id, p.closed_at, p.debited_lamports, p.proceeds_lamports or 0,
                    p.adverse_proceeds_lamports if p.adverse_proceeds_lamports is not None else (p.proceeds_lamports or 0),
                    p.sol_usd_open, p.sol_usd_close,
                )  # fmt: skip
            )
        elif p.status == PositionStatus.OPEN.value:
            open_.append(
                OpenTrade(
                    p.id, p.debited_lamports, p.last_recoverable_adverse_lamports, p.illiquid,
                    quoted_recoverable_lamports=p.last_recoverable_lamports, valued_at=p.last_valued_at,
                )  # fmt: skip
            )
        elif p.status == PositionStatus.REJECTED.value:
            rejected += 1
            reasons[p.reject_reason or "UNKNOWN"] += 1
    return closed, open_, rejected, reasons


def session_metrics(s: Session, sess: orm.PaperSession, since: datetime | None, until: datetime | None) -> dict[str, Any]:
    closed, open_, rejected, reasons = _trades_for(s, sess.id, since, until)
    total = s.scalar(
        select(func.count()).select_from(orm.PaperAttempt).join(orm.PaperPosition, orm.PaperPosition.id == orm.PaperAttempt.position_id)
        .where(orm.PaperPosition.session_id == sess.id, orm.PaperAttempt.kind == "exit", orm.PaperAttempt.status.in_(["applied", "failed"]))
    ) or 0
    failed = s.scalar(
        select(func.count()).select_from(orm.PaperAttempt).join(orm.PaperPosition, orm.PaperPosition.id == orm.PaperAttempt.position_id)
        .where(orm.PaperPosition.session_id == sess.id, orm.PaperAttempt.kind == "exit", orm.PaperAttempt.status == "failed")
    ) or 0
    m = compute_metrics(
        closed, open_, capital_lamports=sess.capital_lamports, exit_attempts_total=total, exit_attempts_failed=failed, rejected_entries=rejected
    )
    m["no_entry_reasons"] = [{"code": c, "count": n} for c, n in reasons.most_common()]
    # Frecuencia de salidas fallidas por posición: una posición ilíquida reintenta sin parar y, contando solo
    # intentos, dominaría la métrica; por posición se ve cuántas tuvieron al menos un fallo.
    m["positions_with_exit_trigger"] = (
        s.scalar(
            select(func.count()).select_from(orm.PaperPosition).where(
                orm.PaperPosition.session_id == sess.id, orm.PaperPosition.exit_trigger.is_not(None)
            )
        )
        or 0
    )
    m["positions_with_failed_exit"] = (
        s.scalar(
            select(func.count(func.distinct(orm.PaperAttempt.position_id)))
            .select_from(orm.PaperAttempt)
            .join(orm.PaperPosition, orm.PaperPosition.id == orm.PaperAttempt.position_id)
            .where(
                orm.PaperPosition.session_id == sess.id,
                orm.PaperAttempt.kind == "exit",
                orm.PaperAttempt.status == "failed",
            )
        )
        or 0
    )
    return m


def metrics(s: Session, session_id: int, since: datetime | None = None, until: datetime | None = None) -> dict[str, Any] | None:
    sess = s.get(orm.PaperSession, session_id)
    if sess is None:
        return None
    mine = session_metrics(s, sess, since, until)
    other = s.scalar(
        select(orm.PaperSession).where(
            orm.PaperSession.strategy_version_id == sess.strategy_version_id, orm.PaperSession.id != sess.id
        )
    )
    comparison = None
    if other is not None:
        theirs = session_metrics(s, other, since, until)
        pairs = {
            "realized_pnl_lamports": (mine["realized_pnl_lamports"], theirs["realized_pnl_lamports"]),
            "open_pnl_valued_lamports": (mine["open_pnl_valued_lamports"], theirs["open_pnl_valued_lamports"]),
            "total_pnl_valued_lamports": (mine["total_pnl_valued_lamports"], theirs["total_pnl_valued_lamports"]),
            "zero_recovery_scenario_pnl_lamports": (mine["zero_recovery_scenario_pnl_lamports"], theirs["zero_recovery_scenario_pnl_lamports"]),
            "adverse_pnl_lamports": (mine["adverse_pnl_lamports"], theirs["adverse_pnl_lamports"]),
        }
        comparison = {
            "reference_session": {"id": other.id, "name": other.name, "policy": other.policy},
            "reference": theirs,
            "delta": {k: str(int(a) - int(b)) for k, (a, b) in pairs.items()},
            "note": "Mismo universo elegible, mismos tamaños, presupuesto, retrasos y reglas de salida; solo cambia el orden de admisión.",
        }
    fx = s.scalar(select(orm.FxRate).order_by(orm.FxRate.observed_at.desc()).limit(1))
    return {
        "session": _session_info(s, sess),
        "window": {"since": iso(since), "until": iso(until)},
        "metrics": mine,
        "comparison": comparison,
        "fx": None if fx is None else {"pair": fx.pair, "rate": dstr(fx.rate), "observed_at": iso(fx.observed_at), "source": fx.source},
        "disclaimer": "Resultados simulados con cotizaciones de solo lectura. No constituyen evidencia de rentabilidad real.",
    }


# ------------------------------------------------------------------ operación
def ops(s: Session, as_of: datetime) -> dict[str, Any]:
    cps = CheckpointRepo(s).list_all()
    migrations = next((c for c in cps if c.stream == "migrations"), None)
    stale_cut = as_of.timestamp()
    version = latest_version(s)
    cfg = version_cfg(version) if version else None
    latest_end = s.execute(select(orm.MarketSnapshotRow.pool, func.max(orm.MarketSnapshotRow.window_end)).group_by(orm.MarketSnapshotRow.pool)).all()
    stale_pools = 0
    if cfg:
        active_cut = as_of.timestamp() - cfg.max_age_after_migration_s
        # los descartados en forma definitiva dejan de consultarse a propósito: no cuentan como datos vencidos
        dropped = EvaluationRepo(s).terminal_pools(version.id, [pool for pool, _ in latest_end])
        for pool, end in latest_end:
            migrated = s.get(orm.Pool, pool).migrated_at.timestamp()
            if pool not in dropped and migrated >= active_cut and stale_cut - end.timestamp() > cfg.data_max_age_s:
                stale_pools += 1
    jobs_by_status = dict(s.execute(select(orm.Job.status, func.count()).group_by(orm.Job.status)).all())
    retried = s.scalar(select(func.count()).select_from(orm.Job).where(orm.Job.attempts > 1)) or 0
    quote_status = dict(s.execute(select(orm.QuoteRow.status, func.count()).group_by(orm.QuoteRow.status)).all())
    discarded: Counter[str] = Counter()
    if version:
        for r in candidate_rows(s, version, as_of):
            if r["status"] in ("rejected", "expired", "pending_data"):
                for code in {x["code"] for x in r["reasons"]}:
                    discarded[code] += 1
    exit_total = s.scalar(select(func.count()).select_from(orm.PaperAttempt).where(orm.PaperAttempt.kind == "exit", orm.PaperAttempt.status.in_(["applied", "failed"]))) or 0
    exit_failed = s.scalar(select(func.count()).select_from(orm.PaperAttempt).where(orm.PaperAttempt.kind == "exit", orm.PaperAttempt.status == "failed")) or 0
    reserved = 0
    for sess in s.scalars(select(orm.PaperSession)):
        reserved += SessionRepo(s, AuditRepo(s)).state(sess.id).reserved_lamports
    return {
        "as_of": iso(as_of),
        "ingestion_lag_s": None if not (migrations and migrations.last_healthy_at) else round((as_of - migrations.last_healthy_at).total_seconds(), 1),
        "stale_snapshot_pools": stale_pools,
        "jobs": {"by_status": jobs_by_status, "retried": retried},
        "quotes": {"by_status": quote_status, "failures": quote_status.get("error", 0)},
        "candidates_discarded_by_reason": [{"code": c, "count": n} for c, n in discarded.most_common()],
        "exit_attempts": {"total": exit_total, "failed": exit_failed},
        "reserved_capital_lamports": str(reserved),
    }
