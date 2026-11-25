"""Salud de las fuentes: una fuente está degradada si superó su antigüedad máxima o falla seguido.

Al degradarse una fuente se bloquean las entradas vinculadas a ella; el seguimiento de las posiciones
abiertas continúa.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from datetime import datetime

from app.application.ports import Checkpoint

WORKER_SOURCE = "worker"
HEARTBEAT_STREAM = "heartbeat"
INTERNAL_SOURCES = frozenset({WORKER_SOURCE, "replay"})  # estado interno, no son fuentes de datos
# streams cuya degradación impide abrir nuevas posiciones
ENTRY_CRITICAL_STREAMS = frozenset({"market", "quotes"})


@dataclass(frozen=True)
class SourceHealth:
    source: str
    stream: str
    degraded: bool
    reason: str | None
    last_healthy_at: datetime | None
    consecutive_failures: int
    last_error: str | None
    cursor: str | None
    last_slot: int | None


def assess(
    checkpoints: Iterable[Checkpoint], *, now: datetime, max_age_s: int, max_failures: int
) -> list[SourceHealth]:
    out: list[SourceHealth] = []
    for cp in checkpoints:
        if cp.source in INTERNAL_SOURCES:
            continue
        reason = None
        if cp.consecutive_failures >= max_failures:
            reason = f"{cp.consecutive_failures} fallas consecutivas"
        elif cp.last_healthy_at is None:
            reason = "sin lecturas sanas todavía"
        elif (now - cp.last_healthy_at).total_seconds() > max_age_s:
            reason = f"sin lecturas sanas hace {int((now - cp.last_healthy_at).total_seconds())}s"
        out.append(
            SourceHealth(
                cp.source, cp.stream, reason is not None, reason, cp.last_healthy_at,
                cp.consecutive_failures, cp.last_error, cp.cursor, cp.last_slot,
            )  # fmt: skip
        )
    return out


def blocked_entry_sources(health: Iterable[SourceHealth]) -> frozenset[str]:
    """Fuentes degradadas que bloquean nuevas entradas (mercado y cotizaciones)."""
    return frozenset(h.stream for h in health if h.degraded and h.stream in ENTRY_CRITICAL_STREAMS)
