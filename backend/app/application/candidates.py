"""Estado efectivo de un candidato en el instante de lectura.

La evaluación guardada corresponde a la última ventana cerrada. Con el paso del tiempo (sin nuevas
ventanas) el candidato puede haber vencido por edad, por vigencia de la señal o por datos obsoletos;
esas condiciones dependen de ``as_of`` y se resuelven al leer, sin reescribir evaluaciones guardadas.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any

from app.domain.config import StrategyConfig
from app.domain.reasons import CandidateStatus, ReasonCode


@dataclass(frozen=True)
class EffectiveStatus:
    status: str
    reasons: list[dict[str, Any]]


def _reason(code: ReasonCode, message: str, **data: Any) -> dict[str, Any]:
    return {"code": code.value, "message": message, "data": data}


def effective_status(
    *,
    base_status: str,
    base_reasons: list[dict[str, Any]],
    migrated_at: datetime,
    last_window_end: datetime | None,
    signal_expires_at: datetime | None,
    signal_status: str | None,
    as_of: datetime,
    cfg: StrategyConfig,
) -> EffectiveStatus:
    status = base_status
    reasons = list(base_reasons)

    if status == CandidateStatus.ELIGIBLE.value and signal_expires_at is not None:
        if signal_status != "active" or signal_expires_at <= as_of:
            status = CandidateStatus.EXPIRED.value
            reasons = [_reason(ReasonCode.SIGNAL_EXPIRED, "la señal venció", expires_at=signal_expires_at.isoformat())]

    open_states = {
        CandidateStatus.OBSERVING.value,
        CandidateStatus.PENDING_DATA.value,
        CandidateStatus.ELIGIBLE.value,
    }
    age_s = (as_of - migrated_at).total_seconds()
    if status in open_states and age_s > cfg.max_age_after_migration_s:
        status = CandidateStatus.EXPIRED.value
        reasons = [_reason(ReasonCode.PATTERN_EXPIRED, "se superó la edad máxima tras la migración")] + reasons
    elif status in (CandidateStatus.OBSERVING.value, CandidateStatus.ELIGIBLE.value) and last_window_end is not None:
        data_age = (as_of - last_window_end).total_seconds()
        if data_age > cfg.data_max_age_s:
            status = CandidateStatus.PENDING_DATA.value
            reasons = [
                _reason(
                    ReasonCode.STALE_MARKET_DATA,
                    "la fuente de mercado dejó de entregar ventanas",
                    value=f"{data_age:.0f}s",
                    threshold=f"{cfg.data_max_age_s}s",
                )
            ] + reasons
    return EffectiveStatus(status, reasons)
