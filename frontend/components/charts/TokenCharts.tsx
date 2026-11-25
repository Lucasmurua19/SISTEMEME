"use client";

import { formatDuration, formatPrice, formatSol, solNumber } from "@/lib/format";
import type { PoolDetail } from "@/lib/types";
import { LineChart, type HLine, type Marker } from "./LineChart";

function mmss(seconds: number): string {
  const s = Math.max(0, Math.round(seconds));
  return `${Math.floor(s / 60)}:${String(s % 60).padStart(2, "0")}`;
}

/** Precio de cierre por ventana, indexado a la apertura de la primera ventana (=100), con la evidencia del patrón. */
export function PriceChart({ pool }: { pool: PoolDetail }) {
  const snaps = pool.snapshots;
  if (snaps.length === 0) return <p className="empty">Todavía no hay ventanas cerradas.</p>;
  const t0 = Date.parse(pool.migrated_at);
  const base = Number(snaps[0].open);
  const idx = (p: string | number) => (Number(p) / base) * 100;
  const xOf = (iso: string) => (Date.parse(iso) - t0) / 1000;
  const points = snaps.map((s) => ({ x: xOf(s.window_end), y: idx(s.close) }));
  const byX = new Map(snaps.map((s) => [xOf(s.window_end), s]));
  const closeAt = (x: number) => {
    let last = points[0];
    for (const p of points) if (p.x <= x) last = p;
    return last.y;
  };

  const markers: Marker[] = [];
  const hlines: HLine[] = [];
  const pat = pool.evaluation?.pattern;
  if (pat?.peak) markers.push({ x: xOf(pat.peak.at), y: idx(pat.peak.price), label: "Máximo", color: "var(--ink-2)" });
  if (pat?.low) markers.push({ x: xOf(pat.low.at), y: idx(pat.low.price), label: "Mínimo", color: "var(--ink-2)", below: true });
  if (pat?.confirmed_window_index != null) {
    const s = snaps.find((x) => x.window_index === pat.confirmed_window_index);
    if (s) markers.push({ x: xOf(s.window_end), y: idx(s.close), label: "Confirmación", color: "var(--ink)", below: true });
  }
  if (pat?.invalidation_level) hlines.push({ y: idx(pat.invalidation_level), label: "Nivel de invalidación", color: "var(--critical)" });
  const pos = pool.positions.find((p) => p.policy === "ranking" && p.opened_at) ?? pool.positions.find((p) => p.opened_at);
  if (pos?.opened_at) markers.push({ x: xOf(pos.opened_at), y: closeAt(xOf(pos.opened_at)), label: "Entrada", color: "var(--series-2)" });
  if (pos?.closed_at) markers.push({ x: xOf(pos.closed_at), y: closeAt(xOf(pos.closed_at)), label: "Salida", color: "var(--series-2)", below: true });

  return (
    <LineChart
      ariaLabel="Precio de cierre por ventana, índice 100 = apertura de la primera ventana"
      series={[{ id: "close", name: "Precio (índice)", color: "var(--series-1)", points, format: (v) => v.toFixed(1) }]}
      xFormat={(x) => mmss(x)}
      yFormat={(y) => y.toFixed(0)}
      xTitle="Minutos desde la migración"
      markers={markers}
      hlines={hlines}
      rowsAt={(x) => {
        const s = byX.get(x);
        if (!s) return [];
        return [
          { label: "Precio", value: `${formatPrice(s.close)} SOL` },
          { label: "Compradores netos", value: String(s.net_buyers) },
          { label: "Swaps", value: String(s.swap_count) },
          { label: "Concentración top 10", value: s.top10_concentration_pct ? `${Number(s.top10_concentration_pct).toFixed(1)}%` : "desconocida" },
        ];
      }}
    />
  );
}

export function LiquidityChart({ pool }: { pool: PoolDetail }) {
  const snaps = pool.snapshots;
  if (snaps.length === 0) return <p className="empty">Todavía no hay ventanas cerradas.</p>;
  const t0 = Date.parse(pool.migrated_at);
  const points = snaps.map((s) => ({ x: (Date.parse(s.window_end) - t0) / 1000, y: solNumber(s.liquidity_lamports) }));
  const retention = pool.evaluation?.pattern.liquidity_retention_pct;
  return (
    <div>
      <LineChart
        ariaLabel="Liquidez del pool por ventana, en SOL"
        series={[{ id: "liq", name: "Liquidez (SOL)", color: "var(--series-1)", points, area: true, format: (v) => `${v.toFixed(1)} SOL` }]}
        xFormat={(x) => mmss(x)}
        yFormat={(y) => y.toFixed(0)}
        xTitle="Minutos desde la migración"
        height={200}
        rowsAt={(x) => {
          const s = snaps.find((q) => (Date.parse(q.window_end) - t0) / 1000 === x);
          return s ? [{ label: "Reserva de SOL", value: `${formatSol(BigInt(s.liquidity_lamports) / 2n, 1)} SOL` }] : [];
        }}
      />
      {retention && (
        <p className="muted" style={{ marginTop: 6 }}>
          Retención actual: {Number(retention).toFixed(1)}% del máximo observado. Última ventana hace{" "}
          {formatDuration(Math.max(0, (Date.parse(pool.evaluation!.as_of) - Date.parse(snaps[snaps.length - 1].window_end)) / 1000))}.
        </p>
      )}
    </div>
  );
}
