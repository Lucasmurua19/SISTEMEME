from __future__ import annotations

from fastapi import APIRouter, Depends, Response
from sqlalchemy import text
from sqlalchemy.orm import Session

from app.api.deps import get_db, get_settings_dep
from app.api.schemas import ReadyOut, StatusOut
from app.infrastructure import read_models as rm
from app.settings import Settings

router = APIRouter()


def _status(db: Session, settings: Settings) -> dict:
    out = rm.system_status(
        db,
        max_age_s=settings.source_max_age_s,
        max_failures=settings.source_max_failures,
        worker_max_silence_s=settings.worker_max_silence_s,
        data_mode=settings.data_mode,
    )
    out["database"] = "ok"
    return out


@router.get("/health/live", tags=["salud"], summary="El proceso está vivo")
def live() -> dict[str, str]:
    return {"status": "ok"}


@router.get("/health/ready", response_model=ReadyOut, tags=["salud"], summary="Preparación: base, último checkpoint y fuentes")
def ready(response: Response, db: Session = Depends(get_db), settings: Settings = Depends(get_settings_dep)):
    try:
        db.execute(text("SELECT 1"))
    except Exception:
        response.status_code = 503
        return {
            "ready": False, "overall": "down", "database": "error", "as_of": "", "data_mode": settings.data_mode,
            "worker": {"alive": False, "last_heartbeat_at": None, "silence_s": None},
            "ingest_lag_s": None, "last_slot": None, "strategy": None, "sources": [],
        }  # fmt: skip
    body = _status(db, settings)
    is_ready = body["worker"]["alive"]
    if not is_ready:
        response.status_code = 503
    body["ready"] = is_ready
    return body


@router.get("/api/v1/status", response_model=StatusOut, tags=["salud"], summary="Estado para el indicador del panel")
def status(db: Session = Depends(get_db), settings: Settings = Depends(get_settings_dep)):
    return _status(db, settings)
