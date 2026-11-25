"use client";

import { useState } from "react";
import {
  formatClock,
  formatDateTime,
  formatNumber,
  formatPct,
  formatPrice,
  formatSol,
  formatUsd,
  pnlClass,
  shortKey,
  solscan,
} from "@/lib/format";
import {
  ATTEMPT_STATUS_LABEL,
  COMPONENT_LABEL,
  FILTER_LABEL,
  POLICY_LABEL,
  POSITION_LABEL,
  QUOTE_PURPOSE_LABEL,
  STATUS_LABEL,
  reasonLabel,
} from "@/lib/labels";
import type { Evaluation, PoolDetail, Position, TokenDetail } from "@/lib/types";
import { usePolling } from "@/lib/usePolling";
import { LiquidityChart, PriceChart } from "./charts/TokenCharts";
import { Icon, OUTCOME_ICON } from "./Icons";
import { ReasonChip, StatusBadge } from "./StatusBadge";

function CopyButton({ value }: { value: string }) {
  const [done, setDone] = useState(false);
  return (
    <button
      type="button"
      className="btn btn-sm"
      aria-label="Copiar"
      onClick={async () => {
        try {
          await navigator.clipboard.writeText(value);
          setDone(true);
          setTimeout(() => setDone(false), 1200);
        } catch {
          /* portapapeles no disponible */
        }
      }}
    >
      <Icon name={done ? "check" : "copy"} width={12} height={12} />
      {done ? "Copiado" : "Copiar"}
    </button>
  );
}

function ExternalLink({ href, children }: { href: string; children: React.ReactNode }) {
  return (
    <a href={href} target="_blank" rel="noopener noreferrer" className="btn btn-sm">
      {children}
      <Icon name="external" width={12} height={12} />
    </a>
  );
}

function Flag({ ok, yes, no, unknown }: { ok: boolean | null; yes: string; no: string; unknown?: string }) {
  const state = ok === null ? "neutral" : ok ? "eligible" : "rejected";
  return (
    <span className={`badge ${state}`}>
      <Icon name={ok === null ? "minus" : ok ? "check" : "x"} />
      {ok === null ? (unknown ?? "sin dato") : ok ? yes : no}
    </span>
  );
}

// ------------------------------------------------------------------ migración
function MigrationCard({ pool, mint }: { pool: PoolDetail; mint: string }) {
  const v = pool.verification as Record<string, unknown>;
  return (
    <section className="card" aria-labelledby={`mig-${pool.address}`}>
      <div className="card-head">
        <h2 id={`mig-${pool.address}`}>Migración y pool</h2>
        <p>Identificada por firma e índice de instrucción</p>
      </div>
      <dl className="kv">
        <dt>Transacción</dt>
        <dd>
          <span className="mono">{shortKey(pool.migration.signature, 10, 8)}</span>{" "}
          <ExternalLink href={solscan("tx", pool.migration.signature)}>Solscan</ExternalLink>{" "}
          <CopyButton value={pool.migration.signature} />
        </dd>
        <dt>Instrucción · slot</dt>
        <dd className="mono">
          #{pool.migration.instruction_index} · {pool.migration.slot.toLocaleString("es-AR")}
        </dd>
        <dt>Migrado</dt>
        <dd>{formatDateTime(pool.migrated_at)}</dd>
        <dt>Mint</dt>
        <dd>
          <span className="mono">{shortKey(mint, 8, 8)}</span> <ExternalLink href={solscan("token", mint)}>Solscan</ExternalLink> <CopyButton value={mint} />
        </dd>
        <dt>Pool (PumpSwap)</dt>
        <dd>
          <span className="mono">{shortKey(pool.address, 8, 8)}</span> <ExternalLink href={solscan("account", pool.address)}>Solscan</ExternalLink>{" "}
          <CopyButton value={pool.address} />
        </dd>
        <dt>Verificación</dt>
        <dd className="reasons">
          <Flag ok={pool.verified} yes="migración verificada" no="no verificada" />
          <Flag ok={pool.identity_ok} yes="mint y pool coinciden" no="no coinciden" />
          <Flag ok={pool.canonical} yes="pool canónico" no="no canónico" unknown="canónico sin dato" />
        </dd>
        {typeof v.reason === "string" && v.reason && (
          <>
            <dt>Motivo</dt>
            <dd>{v.reason}</dd>
          </>
        )}
      </dl>
      <details style={{ marginTop: 10 }}>
        <summary>Evidencia de la verificación</summary>
        <pre className="mono" style={{ whiteSpace: "pre-wrap", overflowWrap: "anywhere", margin: "8px 0 0" }}>
          {JSON.stringify(pool.verification, null, 2)}
        </pre>
      </details>
    </section>
  );
}

// ------------------------------------------------------------------ evaluación
function FiltersCard({ ev }: { ev: Evaluation }) {
  return (
    <section className="card" aria-labelledby="filters-title">
      <div className="card-head">
        <h2 id="filters-title">Por qué pasó o falló cada filtro</h2>
        <p>
          Ventana {ev.window_index} · evaluada {formatClock(ev.evaluated_at)}
        </p>
      </div>
      <ul className="filter-list">
        {ev.filters.map((f) => (
          <li className={`filter-item ${f.outcome}`} key={f.name}>
            <Icon name={OUTCOME_ICON[f.outcome] ?? "minus"} />
            <div>
              <div className="name">
                {FILTER_LABEL[f.name] ?? f.name} <span className="sr-only">({f.outcome})</span>
              </div>
              <div className="msg">{f.message}</div>
            </div>
            <div className="vals">
              {f.value !== null && <div>{f.value}</div>}
              {f.threshold !== null && <div className="muted">límite {f.threshold}</div>}
            </div>
          </li>
        ))}
      </ul>
    </section>
  );
}

function PatternCard({ ev }: { ev: Evaluation }) {
  const p = ev.pattern;
  return (
    <section className="card" aria-labelledby="pattern-title">
      <div className="card-head">
        <h2 id="pattern-title">Evidencia del patrón</h2>
        <p>Valores congelados al confirmar: ventanas futuras no los reescriben</p>
      </div>
      <dl className="kv">
        <dt>Estado</dt>
        <dd>{p.detail || p.state}</dd>
        <dt>Máximo inicial</dt>
        <dd>{p.peak ? `${formatPrice(p.peak.price)} SOL · ventana ${p.peak.window_index}` : "—"}</dd>
        <dt>Mínimo</dt>
        <dd>{p.low ? `${formatPrice(p.low.price)} SOL · ventana ${p.low.window_index}` : "—"}</dd>
        <dt>Caída</dt>
        <dd>{formatPct(p.drawdown_pct)}</dd>
        <dt>Recuperación desde el mínimo</dt>
        <dd>{formatPct(p.recovery_pct)}</dd>
        <dt>Compradores netos (cota inferior)</dt>
        <dd>{p.net_buyers ?? "—"}</dd>
        <dt>Liquidez vs. su máximo</dt>
        <dd>{formatPct(p.liquidity_retention_pct)}</dd>
        <dt>Nivel de invalidación</dt>
        <dd>{p.invalidation_level ? `${formatPrice(p.invalidation_level)} SOL` : "—"}</dd>
        <dt>Confirmada</dt>
        <dd>{p.confirmed_at ? `ventana ${p.confirmed_window_index} · ${formatClock(p.confirmed_at)}` : "todavía no"}</dd>
      </dl>
    </section>
  );
}

function ScoreCard({ ev }: { ev: Evaluation }) {
  if (!ev.score || ev.score_components.length === 0) {
    return (
      <section className="card">
        <div className="card-head">
          <h2>Puntaje</h2>
        </div>
        <p className="muted">Sin puntaje: requiere patrón confirmado, concentración conocida y cotizaciones de compra y venta.</p>
      </section>
    );
  }
  return (
    <section className="card" aria-labelledby="score-title">
      <div className="card-head">
        <h2 id="score-title">Puntaje {Number(ev.score).toFixed(2)} / 100</h2>
        <p>Pesos y rangos fijos por versión: no cambia por la aparición de otros candidatos</p>
      </div>
      <div className="score-rows">
        {ev.score_components.map((c) => (
          <div className="score-row" key={c.name}>
            <span>
              {COMPONENT_LABEL[c.name] ?? c.name}
              <span className="muted"> · {Number(c.weight).toFixed(0)}%</span>
            </span>
            <span className="bar" aria-hidden="true">
              <i style={{ width: `${Math.max(0, Math.min(100, Number(c.score)))}%` }} />
            </span>
            <span className="num" style={{ textAlign: "right", fontVariantNumeric: "tabular-nums" }} title={`valor ${Number(c.value).toFixed(2)} → ${Number(c.score).toFixed(1)}/100`}>
              {Number(c.points).toFixed(1)} pts
            </span>
          </div>
        ))}
        <div className="score-total">
          <span>Total</span>
          <span style={{ fontVariantNumeric: "tabular-nums" }}>{Number(ev.score).toFixed(2)}</span>
        </div>
      </div>
      <p className="muted" style={{ marginTop: 8 }}>Valor exacto sin redondear: {ev.score}</p>
    </section>
  );
}

function RoundTripCard({ ev }: { ev: Evaluation }) {
  const rt = ev.round_trip;
  return (
    <section className="card" aria-labelledby="rt-title">
      <div className="card-head">
        <h2 id="rt-title">Costo de ida y vuelta</h2>
        <p>(SOL debitado − SOL neto recuperable) / SOL debitado</p>
      </div>
      {!rt ? (
        <p className="muted">Sin cotizaciones válidas todavía.</p>
      ) : (
        <>
          <p style={{ fontSize: 22, fontWeight: 600, marginBottom: 8 }}>{formatPct(rt.cost_pct, 3)}</p>
          <dl className="kv">
            <dt>Compra (cotizada)</dt>
            <dd>{formatSol(rt.buy_in_lamports, 6)} SOL</dd>
            <dt>Cargos no incorporados (compra)</dt>
            <dd>{formatSol(rt.buy_extra_costs_lamports, 6)} SOL</dd>
            <dt>Total debitado</dt>
            <dd>{formatSol(rt.total_debited_lamports, 6)} SOL</dd>
            <dt>Venta inmediata (mín. de la compra)</dt>
            <dd>{formatSol(rt.sell_out_lamports, 6)} SOL</dd>
            <dt>Cargos no incorporados (venta)</dt>
            <dd>{formatSol(rt.sell_extra_costs_lamports, 6)} SOL</dd>
            <dt>Depósito reembolsable inmovilizado</dt>
            <dd>{formatSol(rt.immobilized_lamports, 6)} SOL</dd>
            <dt>Neto recuperable</dt>
            <dd>{formatSol(rt.net_recoverable_lamports, 6)} SOL</dd>
            <dt>Tokens (esperado / mínimo)</dt>
            <dd className="mono">
              {formatNumber(rt.tokens_expected, 0)} / {formatNumber(rt.tokens_min, 0)}
            </dd>
          </dl>
        </>
      )}
    </section>
  );
}

function QuotesCard({ pool }: { pool: PoolDetail }) {
  if (pool.quotes.length === 0) return null;
  return (
    <section className="card" aria-labelledby="quotes-title">
      <div className="card-head">
        <h2 id="quotes-title">Cotizaciones recientes</h2>
        <p>Ruta, montos, impacto, costos, momento y vencimiento</p>
      </div>
      <div className="table-wrap">
        <table>
          <thead>
            <tr>
              <th>Momento</th>
              <th>Uso</th>
              <th>Lado</th>
              <th>Resultado</th>
              <th>Ruta</th>
              <th className="num">Entrada</th>
              <th className="num">Salida</th>
              <th className="num">Impacto</th>
              <th>Cargos extra</th>
              <th>Vence</th>
            </tr>
          </thead>
          <tbody>
            {pool.quotes.map((q) => (
              <tr key={q.id}>
                <td>{formatClock(q.observed_at)}</td>
                <td>{QUOTE_PURPOSE_LABEL[q.purpose] ?? q.purpose}</td>
                <td>{q.side === "buy" ? "Compra" : "Venta"}</td>
                <td>
                  <span className={`badge ${q.status === "ok" ? "eligible" : q.status === "no_route" ? "pending_data" : "rejected"}`}>
                    <Icon name={q.status === "ok" ? "check" : q.status === "no_route" ? "minus" : "x"} />
                    {q.status === "ok" ? "ok" : q.status === "no_route" ? "sin ruta" : "error"}
                  </span>
                </td>
                <td>{q.route || q.error || "—"}</td>
                <td className="num">{q.side === "buy" ? `${formatSol(q.in_amount, 4)} SOL` : formatNumber(q.in_amount, 0)}</td>
                <td className="num">{q.side === "buy" ? formatNumber(q.out_amount, 0) : `${formatSol(q.out_amount, 4)} SOL`}</td>
                <td className="num">{Number(q.price_impact_bps).toFixed(1)} bps</td>
                <td>
                  {q.costs
                    .filter((c) => !c.included_in_amount)
                    .map((c) => `${c.kind}${c.refundable ? "*" : ""} ${formatSol(c.lamports, 5)}`)
                    .join(" · ") || "—"}
                </td>
                <td>{formatClock(q.expires_at)}</td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
      <p className="muted" style={{ marginTop: 6 }}>* depósito reembolsable que inmoviliza fondos hasta cerrar la cuenta de token.</p>
    </section>
  );
}

// ------------------------------------------------------------------ simulación
function PositionBlock({ p }: { p: Position }) {
  const usd =
    p.proceeds_lamports && p.sol_usd_open && p.sol_usd_close
      ? (Number(p.proceeds_lamports) / 1e9) * Number(p.sol_usd_close) - (Number(p.debited_lamports) / 1e9) * Number(p.sol_usd_open)
      : null;
  return (
    <div className="card" style={{ background: "var(--surface-2)", boxShadow: "none" }}>
      <div className="card-head">
        <h3>
          {POLICY_LABEL[p.policy ?? ""] ?? "Sesión"} ·{" "}
          <span className={`badge ${p.status === "closed" ? "eligible" : p.status === "open" ? "observing" : p.status === "rejected" ? "rejected" : "pending_data"}`}>
            <Icon name={p.status === "closed" ? "check" : p.status === "open" ? "eye" : p.status === "rejected" ? "x" : "clock"} />
            {POSITION_LABEL[p.status] ?? p.status}
          </span>
          {p.illiquid && (
            <span className="badge serious" style={{ marginLeft: 6 }} title="Sin ruta de venta o salidas fallidas: se calcula un escenario de recuperación cero">
              <Icon name="alert" />
              Ilíquida
            </span>
          )}
        </h3>
        {p.pnl_lamports !== null && (
          <strong className={pnlClass(p.pnl_lamports)}>
            {formatSol(p.pnl_lamports, 6, true)} SOL{usd !== null ? ` · ${formatUsd(usd, true)}` : ""}
          </strong>
        )}
      </div>
      {p.status === "rejected" && (
        <p>
          Sin entrada: <ReasonChip code={p.reject_reason ?? "UNKNOWN"} />
        </p>
      )}
      {(p.status === "open" || p.status === "closed") && (
        <dl className="kv" style={{ marginBottom: 8 }}>
          <dt>Debitado</dt>
          <dd>{formatSol(p.debited_lamports, 6)} SOL</dd>
          {p.status === "open" && (
            <>
              <dt>Valor recuperable (esperado)</dt>
              <dd>{formatSol(p.last_recoverable_lamports, 6)} SOL</dd>
              <dt>Valor conservador (mínimo garantizado)</dt>
              <dd>{formatSol(p.last_recoverable_adverse_lamports, 6)} SOL</dd>
              {p.illiquid && (
                <>
                  <dt>Escenario de recuperación cero</dt>
                  <dd className="neg">{formatSol(p.zero_recovery_pnl_lamports, 6, true)} SOL</dd>
                </>
              )}
            </>
          )}
          {p.status === "closed" && (
            <>
              <dt>Recuperado</dt>
              <dd>{formatSol(p.proceeds_lamports, 6)} SOL</dd>
              <dt>Escenario adverso</dt>
              <dd>{formatSol(BigInt(p.adverse_proceeds_lamports ?? "0") - BigInt(p.debited_lamports), 6, true)} SOL</dd>
            </>
          )}
          <dt>Disparador de salida</dt>
          <dd>{p.exit_trigger ? reasonLabel(p.exit_trigger) : "—"}</dd>
          <dt>Salidas fallidas</dt>
          <dd>{p.exit_failures}</dd>
        </dl>
      )}
      <ol className="timeline" aria-label="Intentos">
        {p.attempts.map((a) => (
          <li key={a.id}>
            <span className="when">{formatClock(a.executed_at ?? a.scheduled_at)}</span>
            <span>
              {a.kind === "entry" ? "Entrada" : `Salida #${a.number}`} · {ATTEMPT_STATUS_LABEL[a.status] ?? a.status}
              {a.trigger && <span className="muted"> · {reasonLabel(a.trigger)}</span>}
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
    </div>
  );
}

// ------------------------------------------------------------------ vista principal
function PoolSection({ pool, mint, symbol }: { pool: PoolDetail; mint: string; symbol: string }) {
  const ev = pool.evaluation;
  return (
    <div className="stack">
      {ev && (
        <section className="card" aria-label="Resumen">
          <div className="card-head" style={{ marginBottom: 0 }}>
            <div style={{ display: "flex", gap: 10, alignItems: "center", flexWrap: "wrap" }}>
              <StatusBadge status={ev.status} />
              {ev.reasons.map((r) => (
                <ReasonChip key={r.code} code={r.code} />
              ))}
            </div>
            <p>
              {symbol} · estado efectivo a las {formatClock(ev.as_of)}
              {ev.status !== ev.recorded_status ? ` (última evaluación: ${STATUS_LABEL[ev.recorded_status] ?? ev.recorded_status})` : ""}
            </p>
          </div>
        </section>
      )}

      <MigrationCard pool={pool} mint={mint} />

      <div className="grid-2">
        <section className="card" aria-labelledby="price-title">
          <div className="card-head">
            <h2 id="price-title">Precio y patrón</h2>
            <p>Cierre por ventana de {pool.snapshots.length ? "15 s" : "—"}</p>
          </div>
          <PriceChart pool={pool} />
        </section>
        <section className="card" aria-labelledby="liq-title">
          <div className="card-head">
            <h2 id="liq-title">Liquidez</h2>
            <p>Valor de ambos lados del pool</p>
          </div>
          <LiquidityChart pool={pool} />
        </section>
      </div>

      {ev && (
        <div className="grid-2">
          <FiltersCard ev={ev} />
          <div className="stack">
            <PatternCard ev={ev} />
            <ScoreCard ev={ev} />
          </div>
        </div>
      )}
      {ev && <RoundTripCard ev={ev} />}
      <QuotesCard pool={pool} />

      <section className="card" aria-labelledby="sim-title">
        <div className="card-head">
          <h2 id="sim-title">Operación simulada</h2>
          <p>Mismo universo y mismas reglas en las dos sesiones</p>
        </div>
        {pool.positions.length === 0 ? (
          <p className="muted">Este token no generó señal, así que ninguna sesión lo evaluó para entrar.</p>
        ) : (
          <div className="grid-2">
            {pool.positions.map((p) => (
              <PositionBlock key={p.id} p={p} />
            ))}
          </div>
        )}
      </section>

      {pool.history.length > 0 && (
        <details className="card">
          <summary>Historial de evaluaciones ({pool.history.length} ventanas)</summary>
          <div className="table-wrap" style={{ maxHeight: 260, overflowY: "auto", marginTop: 8 }}>
            <table>
              <thead>
                <tr>
                  <th>Ventana</th>
                  <th>Estado</th>
                  <th className="num">Puntaje</th>
                  <th>Momento</th>
                </tr>
              </thead>
              <tbody>
                {pool.history.map((h) => (
                  <tr key={h.window_index}>
                    <td>{h.window_index}</td>
                    <td>
                      <StatusBadge status={h.status} />
                    </td>
                    <td className="num">{h.score ? Number(h.score).toFixed(2) : "—"}</td>
                    <td>{formatClock(h.as_of)}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        </details>
      )}
    </div>
  );
}

export function TokenView({ mint, initial }: { mint: string; initial: TokenDetail | null }) {
  const { data, error, refreshing } = usePolling<TokenDetail>(`/api/v1/tokens/${encodeURIComponent(mint)}`, initial);
  if (!data) {
    return (
      <div className="banner" role="alert">
        <strong>No se pudo cargar el token.</strong> {error ?? "La API no respondió."}
      </div>
    );
  }
  const symbol = data.symbol ?? shortKey(data.mint);
  return (
    <div className={refreshing ? "refreshing" : undefined}>
      <div className="page-head">
        <div>
          <h1>
            {symbol} <span className="muted" style={{ fontWeight: 400 }}>{data.name}</span>
          </h1>
          <p className="mono">{data.mint}</p>
        </div>
        <span className="muted">Actualizado {formatClock(data.as_of)}</span>
      </div>
      {error && (
        <div className="banner warn" role="status">
          Sin conexión con la API: se muestra la última lectura.
        </div>
      )}
      {data.pools.length === 0 && <p className="empty">No hay pools registrados para este token.</p>}
      <div className="stack">
        {data.pools.map((pool) => (
          <PoolSection key={pool.address} pool={pool} mint={data.mint} symbol={symbol} />
        ))}
      </div>
    </div>
  );
}
