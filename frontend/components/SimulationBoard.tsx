"use client";

import Link from "next/link";
import { useMemo, useState } from "react";
import {
  formatClock,
  formatNumber,
  formatPct,
  formatRatioPct,
  formatSol,
  formatUsd,
  pnlClass,
  shortKey,
} from "@/lib/format";
import { POLICY_LABEL, POSITION_LABEL, reasonLabel } from "@/lib/labels";
import type { Metrics, MetricsResponse, OpsResponse, PaperTrades, Position, SessionInfo } from "@/lib/types";
import { api } from "@/lib/api";
import { usePolling } from "@/lib/usePolling";
import { LineChart, type Series } from "./charts/LineChart";
import { Icon } from "./Icons";
import { ReasonChip } from "./StatusBadge";

function Tile({
  label,
  value,
  sub,
  tone,
  big,
}: {
  label: string;
  value: React.ReactNode;
  sub?: React.ReactNode;
  tone?: string;
  big?: boolean;
}) {
  return (
    <div className={big ? "tile big" : "tile"}>
      <div className="label">{label}</div>
      <div className={`value ${tone ?? ""}`}>{value}</div>
      {sub && <div className="sub">{sub}</div>}
    </div>
  );
}

function Line({ children }: { children: React.ReactNode }) {
  return <span className="line">{children}</span>;
}

function usdFor(m: Metrics) {
  return m.realized_pnl_usd !== null ? formatUsd(m.realized_pnl_usd, true) : null;
}

/** Lo realizado y lo abierto se leen juntos: en estos activos una posición abierta puede dar vuelta el resultado. */
function Headline({ m }: { m: Metrics }) {
  const withRoute = m.trades_open - m.trades_illiquid;
  return (
    <div className="headline" role="group" aria-label="Resultado de la sesión: realizado, abiertas valoradas y total">
      <Tile
        big
        label="PnL realizado"
        value={`${formatSol(m.realized_pnl_lamports, 4, true)} SOL`}
        tone={pnlClass(m.realized_pnl_lamports)}
        sub={
          <>
            <Line>{m.trades_closed} cerradas · solo ventas ya ejecutadas</Line>
            <Line>{usdFor(m) ?? "USD: sin referencia"}</Line>
          </>
        }
      />
      <Tile
        big
        label="Abiertas valoradas"
        value={`${formatSol(m.open_pnl_valued_lamports, 4, true)} SOL`}
        tone={pnlClass(m.open_pnl_valued_lamports)}
        sub={
          m.trades_open === 0 ? (
            <Line>sin posiciones abiertas</Line>
          ) : (
            <>
              <Line>
                {m.trades_open} abierta(s): {withRoute} con ruta de salida · {m.trades_illiquid} sin ruta
              </Line>
              <Line>Venta al mínimo garantizado, después de costos</Line>
              {m.trades_illiquid > 0 && (
                <Line>
                  Sin ruta = recuperación cero: −{formatSol(m.illiquid_cost_basis_lamports, 4)} SOL
                </Line>
              )}
              {withRoute > 0 && <Line>A precio cotizado: {formatSol(m.open_pnl_quoted_lamports, 4, true)} SOL</Line>}
              {m.open_oldest_valuation_at && <Line>Valoración más vieja: {formatClock(m.open_oldest_valuation_at)}</Line>}
            </>
          )
        }
      />
      <Tile
        big
        label="Total valorado"
        value={`${formatSol(m.total_pnl_valued_lamports, 4, true)} SOL`}
        tone={pnlClass(m.total_pnl_valued_lamports)}
        sub={<Line>realizado + abiertas valoradas</Line>}
      />
    </div>
  );
}

function MetricsGrid({ m, title, policy }: { m: Metrics; title: string; policy: string }) {
  return (
    <section className="card" aria-label={title}>
      <div className="card-head">
        <h2>{title}</h2>
        <p>{POLICY_LABEL[policy]}</p>
      </div>
      <Headline m={m} />
      <div className="grid-tiles">
        <Tile label="Expectativa por operación" value={`${formatSol(m.expectancy_lamports, 5, true)} SOL`} sub={`${m.trades_closed} cerradas`} />
        <Tile label="Tasa de acierto" value={formatRatioPct(m.hit_rate, 0)} sub={`ganancia prom. ${formatSol(m.avg_win_lamports, 4)} · pérdida prom. ${formatSol(m.avg_loss_lamports, 4)}`} />
        <Tile
          label="Caída máxima del PnL realizado"
          value={`${formatSol(m.max_realized_drawdown_lamports, 4)} SOL`}
          sub={
            <>
              {m.max_realized_drawdown_pct_of_capital && <Line>{formatPct(m.max_realized_drawdown_pct_of_capital, 2)} del capital</Line>}
              <Line>solo cerradas: no incluye la pérdida latente de las abiertas</Line>
            </>
          }
        />
        <Tile
          label="PnL sin la mejor operación"
          value={`${formatSol(m.pnl_without_best_lamports, 4, true)} SOL`}
          tone={pnlClass(m.pnl_without_best_lamports)}
          sub={m.best_trade_lamports ? `mejor: ${formatSol(m.best_trade_lamports, 4, true)} SOL` : undefined}
        />
        <Tile
          label="Posiciones con salida fallida"
          value={`${m.positions_with_failed_exit} / ${m.positions_with_exit_trigger}`}
          sub={
            m.exit_attempts_total
              ? `${m.failed_exit_attempts} de ${m.exit_attempts_total} intentos fallaron (${formatRatioPct(m.failed_exit_rate, 0)}); una posición ilíquida reintenta sin parar`
              : "sin intentos de salida"
          }
        />
        <Tile
          label="Escenario adverso (cerradas)"
          value={`${formatSol(m.adverse_pnl_lamports, 4, true)} SOL`}
          tone={pnlClass(m.adverse_pnl_lamports)}
          sub="cerradas vendidas al mínimo garantizado"
        />
        <Tile label="Señales sin entrada" value={m.entries_rejected} sub="admitidas y rechazadas al ejecutar" />
      </div>
    </section>
  );
}

function equitySeries(trades: PaperTrades | null, name: string, color: string, id: string): Series | null {
  if (!trades) return null;
  const closed = trades.positions
    .filter((p) => p.status === "closed" && p.closed_at && p.pnl_lamports !== null)
    .sort((a, b) => Date.parse(a.closed_at!) - Date.parse(b.closed_at!));
  const start = Date.parse(trades.session.started_at) / 1000;
  let acc = 0n;
  const points = [{ x: start, y: 0 }];
  for (const p of closed) {
    acc += BigInt(p.pnl_lamports!);
    points.push({ x: Date.parse(p.closed_at!) / 1000, y: Number(acc) / 1e9 });
  }
  return { id, name, color, points, step: true, format: (v) => `${v >= 0 ? "+" : "−"}${Math.abs(v).toFixed(4)} SOL` };
}

function clockOf(x: number): string {
  return new Date(x * 1000).toISOString().slice(11, 19);
}

function EquityCard({ ranking, reference }: { ranking: PaperTrades | null; reference: PaperTrades | null }) {
  const series = [
    equitySeries(ranking, "Ranking por puntaje", "var(--series-1)", "ranking"),
    equitySeries(reference, "Referencia por liquidez", "var(--series-2)", "liquidity"),
  ].filter((s): s is Series => s !== null);
  // los instantes de cierre se reproducen en tiempo simulado: el eje usa la hora del sistema
  const lone = series.flatMap((s) => s.points);
  const hasTrades = lone.length > series.length;
  return (
    <section className="card" aria-labelledby="equity-title">
      <div className="card-head">
        <h2 id="equity-title">PnL neto realizado acumulado</h2>
        <p>Solo operaciones cerradas; las ilíquidas no suman hasta salir</p>
      </div>
      {!hasTrades ? (
        <p className="empty">Todavía no hay operaciones cerradas.</p>
      ) : (
        <LineChart
          ariaLabel="PnL neto realizado acumulado por sesión, en SOL"
          series={series}
          xFormat={clockOf}
          yFormat={(y) => y.toFixed(2)}
          xTitle="Hora UTC"
          readout="last"
          endLabels
          zeroLine
        />
      )}
    </section>
  );
}

function PositionRow({ p }: { p: Position }) {
  const usd =
    p.proceeds_lamports && p.sol_usd_open && p.sol_usd_close
      ? (Number(p.proceeds_lamports) / 1e9) * Number(p.sol_usd_close) - (Number(p.debited_lamports) / 1e9) * Number(p.sol_usd_open)
      : null;
  const failed = p.attempts.filter((a) => a.kind === "exit" && a.status === "failed");
  return (
    <>
      <tr>
        <td>
          <Link href={`/tokens/${encodeURIComponent(p.mint)}`}>{p.symbol ?? shortKey(p.mint)}</Link>
        </td>
        <td>
          <span className={`badge ${p.status === "closed" ? "eligible" : p.status === "open" ? "observing" : p.status === "rejected" ? "rejected" : "pending_data"}`}>
            <Icon name={p.status === "closed" ? "check" : p.status === "open" ? "eye" : p.status === "rejected" ? "x" : "clock"} />
            {POSITION_LABEL[p.status] ?? p.status}
          </span>
          {p.illiquid && (
            <span className="badge serious" style={{ marginLeft: 6 }} title="Recuperación cero en el escenario conservador">
              <Icon name="alert" />
              Ilíquida
            </span>
          )}
        </td>
        <td>{p.status === "rejected" ? <ReasonChip code={p.reject_reason ?? "UNKNOWN"} /> : p.exit_trigger ? reasonLabel(p.exit_trigger) : "—"}</td>
        <td className="num">{p.status === "rejected" ? "—" : formatSol(p.debited_lamports, 5)}</td>
        <td className="num">
          {p.status === "closed" ? formatSol(p.proceeds_lamports, 5) : p.status === "open" ? <span className="muted">{formatSol(p.last_recoverable_lamports, 5)} (est.)</span> : "—"}
        </td>
        <td className={`num ${pnlClass(p.pnl_lamports)}`}>{p.pnl_lamports !== null ? formatSol(p.pnl_lamports, 5, true) : p.zero_recovery_pnl_lamports ? <span className="neg">{formatSol(p.zero_recovery_pnl_lamports, 5, true)} (rec. cero)</span> : "—"}</td>
        <td className={`num hide-sm ${usd !== null ? (usd >= 0 ? "pos" : "neg") : ""}`}>{usd !== null ? formatUsd(usd, true) : "—"}</td>
        <td className="num hide-sm">{p.exit_failures > 0 ? `${p.exit_failures}` : "—"}</td>
        <td className="hide-sm">{formatClock(p.opened_at)}</td>
      </tr>
      {failed.length > 0 && (
        <tr>
          <td colSpan={9} style={{ paddingTop: 0 }}>
            <details>
              <summary className="muted">
                {failed.length} intento(s) de salida fallido(s) · {Array.from(new Set(failed.map((a) => reasonLabel(a.failure_cause ?? "EXIT_FAILED")))).join(", ")}
              </summary>
              <ol className="timeline" style={{ margin: "6px 0" }}>
                {p.attempts
                  .filter((a) => a.kind === "exit")
                  .map((a) => (
                    <li key={a.id}>
                      <span className="when">{formatClock(a.executed_at ?? a.scheduled_at)}</span>
                      <span>
                        Intento #{a.number} · {a.status === "applied" ? "ejecutado" : a.status === "failed" ? "fallido" : "pendiente"}
                        {a.failure_cause && (
                          <>
                            {" "}
                            <ReasonChip code={a.failure_cause} />
                          </>
                        )}
                      </span>
                    </li>
                  ))}
              </ol>
            </details>
          </td>
        </tr>
      )}
    </>
  );
}

function Controls({ session, enabled, onChange }: { session: SessionInfo; enabled: boolean; onChange: () => void }) {
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState<string | null>(null);
  if (!enabled) return null;
  const toggle = async () => {
    setBusy(true);
    setErr(null);
    try {
      if (session.paused) await api.resume(session.id);
      else await api.pause(session.id, "pausa manual desde el panel");
      onChange();
    } catch (e) {
      setErr(e instanceof Error ? e.message : "No se pudo cambiar el estado");
    } finally {
      setBusy(false);
    }
  };
  return (
    <span style={{ display: "inline-flex", gap: 8, alignItems: "center" }}>
      <button type="button" className="btn btn-sm" onClick={toggle} disabled={busy} aria-pressed={session.paused}>
        <Icon name={session.paused ? "play" : "pause"} width={12} height={12} />
        {session.paused ? "Reanudar entradas" : "Pausar entradas"}
      </button>
      {err && <span className="neg">{err}</span>}
    </span>
  );
}

export function SimulationBoard({
  sessions,
  controls,
  initialMetrics,
}: {
  sessions: SessionInfo[];
  controls: boolean;
  initialMetrics: MetricsResponse | null;
}) {
  const ranking = sessions.find((s) => s.policy === "ranking") ?? sessions[0];
  const reference = sessions.find((s) => s.policy === "liquidity");
  const [since, setSince] = useState("");
  const [until, setUntil] = useState("");
  const [view, setView] = useState<"ranking" | "liquidity">("ranking");

  const qs = useMemo(() => {
    const q = new URLSearchParams();
    if (ranking) q.set("session_id", String(ranking.id));
    if (since) q.set("since", `${since}:00Z`);
    if (until) q.set("until", `${until}:00Z`);
    return q.toString();
  }, [ranking, since, until]);

  const metrics = usePolling<MetricsResponse>(ranking ? `/api/v1/metrics?${qs}` : null, !since && !until ? initialMetrics : null);
  const rankTrades = usePolling<PaperTrades>(ranking ? `/api/v1/paper-trades?session_id=${ranking.id}` : null);
  const refTrades = usePolling<PaperTrades>(reference ? `/api/v1/paper-trades?session_id=${reference.id}` : null);
  const ops = usePolling<OpsResponse>("/api/v1/ops");

  if (!ranking) {
    return <p className="empty">Todavía no hay sesiones de simulación: se crean cuando arranca el worker.</p>;
  }
  const m = metrics.data;
  const live = view === "ranking" ? rankTrades.data : refTrades.data;
  const session = live?.session ?? (view === "ranking" ? ranking : reference) ?? ranking;
  const positions = live?.positions ?? [];
  const delta = m?.comparison?.delta;

  return (
    <div className={metrics.refreshing ? "refreshing" : undefined}>
      <div className="banner warn" role="note">
        <strong>Resultados simulados.</strong> Cotizaciones de solo lectura con retraso y costos estimados; no hay órdenes reales. En el modo
        fixtures los datos son sintéticos y no demuestran ventaja alguna.
      </div>

      <div className="filter-row">
        <label className="field">
          Bloque desde (UTC)
          <input type="datetime-local" value={since} onChange={(e) => setSince(e.target.value)} />
        </label>
        <label className="field">
          hasta
          <input type="datetime-local" value={until} onChange={(e) => setUntil(e.target.value)} />
        </label>
        {(since || until) && (
          <button type="button" className="btn btn-sm" onClick={() => (setSince(""), setUntil(""))}>
            Limpiar bloque
          </button>
        )}
        <span className="spacer" />
        {m?.fx && (
          <span className="muted" title={`Referencia fechada ${m.fx.observed_at}`}>
            SOL/USD {Number(m.fx.rate).toFixed(2)} · {formatClock(m.fx.observed_at)}
          </span>
        )}
        <a className="btn btn-sm" href={`/api/v1/export/paper-trades.csv?session_id=${session.id}`} download>
          Exportar CSV
        </a>
      </div>

      {metrics.error && !m && (
        <div className="banner" role="alert">
          No se pudieron cargar las métricas: {metrics.error}
        </div>
      )}

      {m && (
        <div className="stack">
          <div className="grid-2">
            <MetricsGrid m={m.metrics} title="Ranking por puntaje" policy="ranking" />
            {m.comparison ? (
              <MetricsGrid m={m.comparison.reference} title="Referencia por liquidez" policy="liquidity" />
            ) : (
              <section className="card">
                <p className="muted">Sin sesión de referencia.</p>
              </section>
            )}
          </div>
          {delta && (
            <section className="card" aria-label="Diferencia contra la referencia">
              <div className="card-head">
                <h2>Ranking menos referencia</h2>
                <p>{m.comparison?.note}</p>
              </div>
              <div className="grid-tiles">
                <Tile label="PnL realizado" value={`${formatSol(delta.realized_pnl_lamports, 4, true)} SOL`} tone={pnlClass(delta.realized_pnl_lamports)} />
                <Tile label="Abiertas valoradas" value={`${formatSol(delta.open_pnl_valued_lamports, 4, true)} SOL`} tone={pnlClass(delta.open_pnl_valued_lamports)} />
                <Tile label="Total valorado" value={`${formatSol(delta.total_pnl_valued_lamports, 4, true)} SOL`} tone={pnlClass(delta.total_pnl_valued_lamports)} />
                <Tile label="Escenario adverso (cerradas)" value={`${formatSol(delta.adverse_pnl_lamports, 4, true)} SOL`} tone={pnlClass(delta.adverse_pnl_lamports)} />
              </div>
            </section>
          )}
        </div>
      )}

      <div className="stack" style={{ marginTop: 16 }}>
        <EquityCard ranking={rankTrades.data} reference={refTrades.data} />

        <section className="card" aria-labelledby="pos-title">
          <div className="card-head">
            <h2 id="pos-title">Operaciones</h2>
            <div className="chips" role="group" aria-label="Sesión">
              {[ranking, reference].filter((s): s is SessionInfo => !!s).map((s) => (
                <button key={s.id} type="button" className="chip" aria-pressed={view === s.policy} onClick={() => setView(s.policy)}>
                  {POLICY_LABEL[s.policy]}
                </button>
              ))}
            </div>
          </div>
          <div className="filter-row" style={{ marginBottom: 8 }}>
            <span className="muted">
              Capital {formatSol(session.capital.capital_lamports, 2)} SOL · disponible {formatSol(session.capital.available_lamports, 4)} · reservado{" "}
              {formatSol(session.capital.reserved_lamports, 4)} · {session.capital.active_positions}/{session.limits.max_positions} posiciones
            </span>
            {session.paused && (
              <span className="badge pending_data">
                <Icon name="pause" />
                Entradas en pausa
              </span>
            )}
            <span className="spacer" />
            <Controls session={session} enabled={controls} onChange={() => (rankTrades.reload(), refTrades.reload())} />
          </div>
          {positions.length === 0 ? (
            <p className="empty">Todavía no hay señales procesadas en esta sesión.</p>
          ) : (
            <div className="table-wrap">
              <table>
                <thead>
                  <tr>
                    <th>Token</th>
                    <th>Estado</th>
                    <th>Salida / motivo</th>
                    <th className="num">Debitado (SOL)</th>
                    <th className="num">Recuperado (SOL)</th>
                    <th className="num">PnL (SOL)</th>
                    <th className="num hide-sm">PnL (USD)</th>
                    <th className="num hide-sm">Salidas fallidas</th>
                    <th className="hide-sm">Apertura</th>
                  </tr>
                </thead>
                <tbody>
                  {[...positions]
                    .sort((a, b) => b.id - a.id)
                    .map((p) => (
                      <PositionRow key={p.id} p={p} />
                    ))}
                </tbody>
              </table>
            </div>
          )}
        </section>

        {m && m.metrics.no_entry_reasons.length > 0 && (
          <section className="card" aria-labelledby="noentry-title">
            <div className="card-head">
              <h2 id="noentry-title">Señales sin entrada y por qué</h2>
              <p>Sesión de ranking</p>
            </div>
            <ul className="reasons" style={{ listStyle: "none", padding: 0, margin: 0 }}>
              {m.metrics.no_entry_reasons.map((r) => (
                <li key={r.code}>
                  <span className="reason-chip" title={r.code}>
                    {reasonLabel(r.code)} · <strong>{r.count}</strong>
                  </span>
                </li>
              ))}
            </ul>
          </section>
        )}

        {ops.data && <OpsCard ops={ops.data} />}
      </div>
    </div>
  );
}

function OpsCard({ ops }: { ops: OpsResponse }) {
  const jobs = ops.jobs.by_status;
  return (
    <section className="card" aria-labelledby="ops-title">
      <div className="card-head">
        <h2 id="ops-title">Operación del sistema</h2>
        <p>Actualizado {formatClock(ops.as_of)}</p>
      </div>
      <div className="grid-tiles">
        <Tile label="Retraso de ingesta" value={ops.ingestion_lag_s === null ? "—" : `${ops.ingestion_lag_s.toFixed(0)} s`} />
        <Tile label="Pools con datos vencidos" value={ops.stale_snapshot_pools} />
        <Tile label="Trabajos reintentados" value={ops.jobs.retried} sub={`hechos ${jobs.done ?? 0} · muertos ${jobs.dead ?? 0} · cola ${(jobs.queued ?? 0) + (jobs.running ?? 0)}`} />
        <Tile label="Fallas de cotización" value={ops.quotes.failures} sub={`${formatNumber(ops.quotes.by_status.ok ?? 0, 0)} ok · ${ops.quotes.by_status.no_route ?? 0} sin ruta`} />
        <Tile label="Intentos de salida fallidos" value={`${ops.exit_attempts.failed} / ${ops.exit_attempts.total}`} />
        <Tile label="Capital reservado" value={`${formatSol(ops.reserved_capital_lamports, 4)} SOL`} />
      </div>
      {ops.candidates_discarded_by_reason.length > 0 && (
        <>
          <h3 style={{ margin: "12px 0 6px" }}>Descartes y pendientes por motivo</h3>
          <p className="reasons">
            {ops.candidates_discarded_by_reason.map((r) => (
              <span className="reason-chip" key={r.code} title={r.code}>
                {reasonLabel(r.code)} · {r.count}
              </span>
            ))}
          </p>
        </>
      )}
    </section>
  );
}
