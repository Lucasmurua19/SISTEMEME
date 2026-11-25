from __future__ import annotations

import csv
import io
from datetime import UTC, datetime
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query, Response
from sqlalchemy.orm import Session

from app.api.deps import get_db, get_settings_dep
from app.api.schemas import PauseIn
from app.api.security import require_admin
from app.infrastructure import read_models as rm
from app.infrastructure.repositories import AuditRepo, SessionRepo

router = APIRouter(prefix="/api/v1", tags=["simulación"])
admin = require_admin(get_settings_dep)


def _resolve_session(db: Session, session_id: int | None) -> int:
    if session_id is not None:
        return session_id
    version = rm.latest_version(db)
    sess = rm.default_session(db, version.id if version else None)
    if sess is None:
        raise HTTPException(status_code=404, detail="todavía no hay sesiones de simulación")
    return sess.id


def csv_safe(value: Any) -> Any:
    """Evita la inyección de fórmulas al abrir el CSV en una planilla (nombres/símbolos son datos externos)."""
    if isinstance(value, str) and value and value[0] in "=+-@\t\r":
        return "'" + value
    return value


def _csv(rows: list[dict[str, Any]], columns: list[str]) -> Response:
    buf = io.StringIO()
    writer = csv.writer(buf)
    writer.writerow(columns)
    for row in rows:
        writer.writerow([csv_safe(row.get(c)) for c in columns])
    return Response(content=buf.getvalue(), media_type="text/csv; charset=utf-8")


@router.get("/sessions", summary="Sesiones de simulación (ranking y referencia por liquidez)")
def sessions(db: Session = Depends(get_db)) -> list[dict[str, Any]]:
    return rm.list_sessions(db)


@router.get("/paper-trades", summary="Posiciones, intentos, montos, costos, PnL y estado de salida")
def paper_trades(
    session_id: int | None = Query(None, ge=1),
    status: str | None = Query(None, pattern="^(pending_entry|open|closed|rejected)$"),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    out = rm.paper_trades(db, _resolve_session(db, session_id), status=status)
    if out is None:
        raise HTTPException(status_code=404, detail="sesión inexistente")
    return out


@router.get("/metrics", summary="PnL realizado, posiciones abiertas valoradas, expectativa, caída máxima del PnL realizado y comparación con la referencia por liquidez")
def metrics(
    session_id: int | None = Query(None, ge=1),
    since: datetime | None = Query(None, description="Inicio del bloque cronológico (UTC)"),
    until: datetime | None = Query(None, description="Fin exclusivo del bloque cronológico (UTC)"),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    for name, value in (("since", since), ("until", until)):
        if value is not None and value.tzinfo is None:
            raise HTTPException(status_code=422, detail=f"{name} requiere zona horaria (UTC)")
    out = rm.metrics(db, _resolve_session(db, session_id), since, until)
    if out is None:
        raise HTTPException(status_code=404, detail="sesión inexistente")
    return out


@router.get("/ops", summary="Indicadores operativos: retraso de ingesta, trabajos, cotizaciones, descartes")
def ops(db: Session = Depends(get_db)) -> dict[str, Any]:
    return rm.ops(db, rm.system_now(db))


@router.get("/export/paper-trades.csv", summary="Exporta posiciones simuladas a CSV")
def export_trades(session_id: int | None = Query(None, ge=1), db: Session = Depends(get_db)) -> Response:
    data = rm.paper_trades(db, _resolve_session(db, session_id))
    if data is None:
        raise HTTPException(status_code=404, detail="sesión inexistente")
    cols = [
        "id", "policy", "symbol", "mint", "pool", "status", "reject_reason", "opened_at", "closed_at",
        "debited_lamports", "proceeds_lamports", "pnl_lamports", "adverse_proceeds_lamports",
        "sol_usd_open", "sol_usd_close", "exit_trigger", "exit_failures", "illiquid",
        "last_recoverable_lamports",
    ]  # fmt: skip
    return _csv(data["positions"], cols)


@router.get("/export/candidates.csv", summary="Exporta candidatos y su último estado a CSV")
def export_candidates(db: Session = Depends(get_db)) -> Response:
    data = rm.list_candidates(db, status="all", limit=200, offset=0, as_of=rm.system_now(db))
    rows = [{**i, "reasons": ";".join(r["code"] for r in i["reasons"])} for i in data["items"]]
    cols = ["symbol", "mint", "pool", "status", "score", "liquidity_lamports", "round_trip_cost_pct",
            "reasons", "migrated_at", "evaluated_at", "migration_signature"]  # fmt: skip
    return _csv(rows, cols)


def _set_paused(db: Session, session_id: int, paused: bool, reason: str | None) -> dict[str, Any]:
    repo = SessionRepo(db, AuditRepo(db))
    result = repo.set_paused(session_id, paused, reason or ("pausa manual" if paused else None), datetime.now(UTC))
    if result is None:
        raise HTTPException(status_code=404, detail="sesión inexistente")
    db.commit()
    return {"id": result.id, "paused": result.paused, "pause_reason": result.pause_reason}


@router.post("/sessions/{session_id}/pause", dependencies=[Depends(admin)], summary="Pausa nuevas entradas (conserva el seguimiento)")
def pause(session_id: int, body: PauseIn | None = None, db: Session = Depends(get_db)) -> dict[str, Any]:
    return _set_paused(db, session_id, True, body.reason if body else None)


@router.post("/sessions/{session_id}/resume", dependencies=[Depends(admin)], summary="Reanuda nuevas entradas")
def resume(session_id: int, db: Session = Depends(get_db)) -> dict[str, Any]:
    return _set_paused(db, session_id, False, None)
