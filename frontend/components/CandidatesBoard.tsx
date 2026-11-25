"use client";

import Link from "next/link";
import { useMemo, useState } from "react";
import { formatDuration, formatPct, formatSol, formatClock, shortKey } from "@/lib/format";
import { POSITION_LABEL, STATUS_HELP, STATUS_LABEL, reasonLabel } from "@/lib/labels";
import type { Candidate, CandidateList } from "@/lib/types";
import { usePolling } from "@/lib/usePolling";
import { Icon } from "./Icons";
import { ReasonChips, StatusBadge } from "./StatusBadge";

const FILTERS = ["eligible", "observing", "pending_data", "rejected", "expired", "all"] as const;

function SimCell({ positions }: { positions: Record<string, string> }) {
  const entries = Object.entries(positions);
  if (entries.length === 0) return <span className="muted">—</span>;
  return (
    <span className="reasons">
      {entries.map(([policy, raw]) => {
        const [status, reason] = raw.split(":");
        const label = POSITION_LABEL[status] ?? status;
        return (
          <span className="reason-chip" key={policy} title={reason ? `${label}: ${reasonLabel(reason)}` : label}>
            {policy === "ranking" ? "Rank" : "Liq"} · {label}
          </span>
        );
      })}
    </span>
  );
}

export function NoEntryPanel({ data }: { data: NonNullable<CandidateList["no_entry"]> }) {
  return (
    <section className="card no-entry" aria-labelledby="no-entry-title">
      <div className="card-head">
        <h2 id="no-entry-title">Sin entrada</h2>
        <p>{data.tracked} tokens seguidos</p>
      </div>
      <p>{data.message} Estos son los motivos de los descartes y pendientes actuales:</p>
      {data.reasons.length > 0 ? (
        <ul>
          {data.reasons.slice(0, 12).map((r) => (
            <li key={r.code}>
              <span title={r.code}>{reasonLabel(r.code)}</span>
              <strong>{r.count}</strong>
            </li>
          ))}
        </ul>
      ) : (
        <p className="muted" style={{ marginTop: 8 }}>
          Todavía no se observó ninguna migración.
        </p>
      )}
    </section>
  );
}

export function CandidatesBoard({ initial }: { initial: CandidateList | null }) {
  const { data, error, refreshing } = usePolling<CandidateList>("/api/v1/candidates?status=all&limit=200", initial);
  // Si no hay elegibles al abrir, se muestran todos los seguidos (con el bloque «sin entrada» arriba).
  const [filter, setFilter] = useState<(typeof FILTERS)[number]>(() => ((initial?.counts?.eligible ?? 0) > 0 ? "eligible" : "all"));

  const items = useMemo(() => {
    const all = data?.items ?? [];
    return filter === "all" ? all : all.filter((i) => i.status === filter);
  }, [data, filter]);

  if (!data) {
    return (
      <div className="banner" role="alert">
        <strong>No se pudo cargar el ranking.</strong> {error ?? "La API no respondió."} Se reintentará automáticamente.
      </div>
    );
  }

  const counts = data.counts;
  const total = Object.values(counts).reduce((a, b) => a + b, 0);
  const eligible = counts.eligible ?? 0;

  return (
    <div className={refreshing ? "refreshing" : undefined}>
      {error && (
        <div className="banner warn" role="status">
          Sin conexión con la API: se muestra la última lectura ({formatClock(data.as_of)}).
        </div>
      )}
      <div className="filter-row" role="group" aria-label="Filtrar por estado">
        <div className="chips">
          {FILTERS.map((f) => (
            <button key={f} type="button" className="chip" aria-pressed={filter === f} onClick={() => setFilter(f)} title={f === "all" ? "Todos los tokens seguidos" : STATUS_HELP[f]}>
              {f === "all" ? "Todos" : STATUS_LABEL[f]}
              <span className="count">{f === "all" ? total : (counts[f] ?? 0)}</span>
            </button>
          ))}
        </div>
        <span className="spacer" />
        <span className="muted">
          Actualizado {formatClock(data.as_of)} · estrategia {data.strategy_version?.name ?? "—"}
        </span>
        <a className="btn btn-sm" href="/api/v1/export/candidates.csv" download>
          Exportar CSV
        </a>
      </div>

      {eligible === 0 && data.no_entry && <NoEntryPanel data={data.no_entry} />}

      <section className="card" style={{ marginTop: eligible === 0 && data.no_entry ? 16 : 0 }} aria-label="Candidatos">
        {items.length === 0 ? (
          <p className="empty">
            {filter === "eligible"
              ? "No hay candidatos elegibles ahora. Revisá los otros estados para ver cada descarte y su motivo."
              : "Ningún token en este estado."}
          </p>
        ) : (
          <div className="table-wrap">
            <table>
              <caption className="sr-only">Candidatos ordenados por puntaje</caption>
              <thead>
                <tr>
                  <th className="rank">#</th>
                  <th>Token</th>
                  <th>Estado</th>
                  <th className="num">Puntaje</th>
                  <th className="num hide-sm">Liquidez</th>
                  <th className="num hide-sm" title="(SOL debitado − SOL neto recuperable) / SOL debitado">
                    Costo ida y vuelta
                  </th>
                  <th className="num hide-sm">Edad</th>
                  <th>Motivos</th>
                  <th className="hide-sm">Simulación</th>
                </tr>
              </thead>
              <tbody>
                {items.map((c, i) => (
                  <Row key={c.pool} c={c} rank={c.status === "eligible" ? i + 1 : null} />
                ))}
              </tbody>
            </table>
          </div>
        )}
      </section>
    </div>
  );
}

function Row({ c, rank }: { c: Candidate; rank: number | null }) {
  const score = c.score !== null ? Number(c.score) : null;
  return (
    <tr>
      <td className="rank">{rank ?? ""}</td>
      <td>
        <div className="token-cell">
          <Link href={`/tokens/${encodeURIComponent(c.mint)}`}>{c.symbol ?? shortKey(c.mint)}</Link>
          <span className="muted mono" title={c.mint}>
            {c.name ? `${c.name} · ` : ""}
            {shortKey(c.mint)}
          </span>
        </div>
      </td>
      <td>
        <StatusBadge status={c.status} />
        {!c.verified && (
          <span className="badge rejected" style={{ marginLeft: 6 }} title="La migración o la relación mint–pool no se pudo verificar">
            <Icon name="alert" />
            sin verificar
          </span>
        )}
      </td>
      <td className="num">
        {score !== null ? (
          <span className="score-cell">
            <span className="score-bar" aria-hidden="true">
              <i style={{ width: `${Math.max(0, Math.min(100, score))}%` }} />
            </span>
            {score.toFixed(1)}
          </span>
        ) : (
          <span className="muted">—</span>
        )}
      </td>
      <td className="num hide-sm">{c.liquidity_lamports ? `${formatSol(c.liquidity_lamports, 1)} SOL` : "—"}</td>
      <td className="num hide-sm">{c.round_trip_cost_pct ? formatPct(c.round_trip_cost_pct, 2) : "—"}</td>
      <td className="num hide-sm" title={`Datos hace ${formatDuration(c.data_age_s)}`}>
        {formatDuration(c.age_s)}
      </td>
      <td>
        <ReasonChips reasons={c.reasons} />
      </td>
      <td className="hide-sm">
        <SimCell positions={c.positions} />
      </td>
    </tr>
  );
}
