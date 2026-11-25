from __future__ import annotations

from typing import Any, Literal

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.orm import Session

from app.api.deps import get_db
from app.api.schemas import CandidateList
from app.api.security import validate_mint
from app.infrastructure import read_models as rm

router = APIRouter(prefix="/api/v1", tags=["radar"])

StatusFilter = Literal["all", "eligible", "observing", "pending_data", "rejected", "expired"]


@router.get("/candidates", response_model=CandidateList, summary="Ranking y candidatos con su evidencia")
def candidates(
    status: StatusFilter = "eligible",
    limit: int = Query(25, ge=1, le=200),
    offset: int = Query(0, ge=0, le=10_000),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    """Los elegibles van primero por puntaje; sin elegibles se devuelve ``no_entry`` con los motivos."""
    return rm.list_candidates(db, status=status, limit=limit, offset=offset, as_of=rm.system_now(db))


@router.get("/tokens/{mint}", summary="Detalle: pools, serie de snapshots, cotizaciones y evidencia")
def token(mint: str, db: Session = Depends(get_db)) -> dict[str, Any]:
    validate_mint(mint)
    detail = rm.token_detail(db, mint, rm.system_now(db))
    if detail is None:
        raise HTTPException(status_code=404, detail="token no observado")
    return detail


@router.get("/signals", summary="Señales emitidas (primeras N para depuración)")
def signals(limit: int = Query(30, ge=1, le=200), db: Session = Depends(get_db)) -> list[dict[str, Any]]:
    return rm.list_signals(db, limit)
