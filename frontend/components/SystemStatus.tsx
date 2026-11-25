"use client";

import { formatClock, formatDuration, shortKey } from "@/lib/format";
import { usePolling } from "@/lib/usePolling";
import type { SystemStatus as Status } from "@/lib/types";

const LABEL: Record<string, string> = { ok: "Sistema OK", degraded: "Fuentes degradadas", down: "Worker detenido" };

export function SystemStatus({ initial }: { initial: Status | null }) {
  const { data, error } = usePolling<Status>("/api/v1/status", initial);
  if (!data) {
    return (
      <span className="status-pill down" role="status">
        <span className="status-dot" />
        {error ?? "Sin estado"}
      </span>
    );
  }
  const degraded = data.sources.filter((s) => s.degraded);
  return (
    <details className="status-details">
      <summary className={`status-pill ${error ? "down" : data.overall}`} role="status" aria-live="polite">
        <span className="status-dot" aria-hidden="true" />
        {error ? "API sin conexión" : LABEL[data.overall]}
        <span className="muted">· {formatClock(data.as_of)}</span>
      </summary>
      <div className="card status-popover">
        <dl className="kv">
          <dt>Modo de datos</dt>
          <dd>{data.data_mode === "fixtures" ? "Fixtures (reproducción sintética)" : "En vivo"}</dd>
          <dt>Último dato</dt>
          <dd>{formatClock(data.as_of)}</dd>
          <dt>Retraso de ingesta</dt>
          <dd>{formatDuration(data.ingest_lag_s)}</dd>
          <dt>Último slot</dt>
          <dd className="mono">{data.last_slot ?? "—"}</dd>
          <dt>Worker</dt>
          <dd>{data.worker.alive ? "activo" : `sin latido hace ${formatDuration(data.worker.silence_s)}`}</dd>
          <dt>Estrategia</dt>
          <dd>{data.strategy ? `${data.strategy.name} · ${shortKey(data.strategy.fingerprint, 8, 0)}` : "—"}</dd>
        </dl>
        <h3 style={{ margin: "12px 0 6px" }}>Fuentes</h3>
        <div className="source-list">
          {data.sources.length === 0 && <span className="muted">Sin lecturas todavía</span>}
          {data.sources.map((s) => (
            <div className="source-row" key={`${s.source}/${s.stream}`}>
              <span className={`badge ${s.degraded ? "pending_data" : "eligible"}`}>
                <svg viewBox="0 0 16 16" fill="none" stroke="currentColor" strokeWidth="1.6" strokeLinecap="round" aria-hidden="true">
                  <path d={s.degraded ? "M8 2.5L14 13H2L8 2.5zM8 6.5v3" : "M3 8.5l3.2 3.2L13 4.8"} />
                </svg>
                {s.source} · {s.stream}
              </span>
              <span className="muted">
                {s.degraded ? s.reason : `sana ${formatClock(s.last_healthy_at)}`}
                {s.last_error ? ` — ${s.last_error}` : ""}
              </span>
            </div>
          ))}
        </div>
        {degraded.length > 0 && (
          <p className="muted" style={{ marginTop: 10 }}>
            Con una fuente degradada se bloquean nuevas entradas simuladas; el seguimiento de posiciones continúa.
          </p>
        )}
      </div>
    </details>
  );
}
