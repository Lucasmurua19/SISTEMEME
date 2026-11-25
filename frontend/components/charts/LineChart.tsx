"use client";

import { useCallback, useEffect, useMemo, useRef, useState } from "react";

export interface Point {
  x: number;
  y: number;
}
export interface Series {
  id: string;
  name: string;
  color: string; // var(--series-N)
  points: Point[];
  area?: boolean;
  /** Serie escalonada (el valor se mantiene hasta el próximo cambio): p. ej. PnL realizado acumulado. */
  step?: boolean;
  /** Formato del valor en el tooltip y la tabla (por defecto, el del eje). */
  format?: (y: number) => string;
}
export interface Marker {
  x: number;
  y: number;
  label?: string;
  color?: string;
  below?: boolean;
}
export interface HLine {
  y: number;
  label: string;
  color: string;
}

interface Props {
  series: Series[];
  height?: number;
  ariaLabel: string;
  xFormat: (x: number) => string;
  yFormat: (y: number) => string;
  xTitle?: string;
  markers?: Marker[];
  hlines?: HLine[];
  /** Filas extra del tooltip para el x seleccionado (valores secundarios, siempre en tinta de texto). */
  rowsAt?: (x: number) => { label: string; value: string }[];
  /** Interpolación de la lectura en el crosshair: "last" repite el último valor conocido (series escalonadas). */
  readout?: "last" | "linear";
  endLabels?: boolean;
  yDomain?: [number, number];
  zeroLine?: boolean;
}

const M = { top: 12, right: 16, bottom: 30, left: 56 };

function niceTicks(min: number, max: number, count = 5): number[] {
  if (!(max > min)) return [min];
  const span = max - min;
  const raw = span / count;
  const pow = Math.pow(10, Math.floor(Math.log10(raw)));
  const step = [1, 2, 2.5, 5, 10].map((m) => m * pow).find((s) => s >= raw) ?? raw;
  const start = Math.ceil(min / step) * step;
  const out: number[] = [];
  for (let v = start; v <= max + step * 1e-9; v += step) out.push(Number(v.toPrecision(12)));
  return out;
}

function valueAt(points: Point[], x: number, mode: "last" | "linear"): number | null {
  if (points.length === 0 || x < points[0].x) return null;
  let lo = 0;
  let hi = points.length - 1;
  while (lo < hi) {
    const mid = (lo + hi + 1) >> 1;
    if (points[mid].x <= x) lo = mid;
    else hi = mid - 1;
  }
  const a = points[lo];
  if (mode === "last" || lo === points.length - 1 || a.x === x) return a.y;
  const b = points[lo + 1];
  return a.y + ((b.y - a.y) * (x - a.x)) / (b.x - a.x);
}

export function LineChart({
  series,
  height = 260,
  ariaLabel,
  xFormat,
  yFormat,
  xTitle,
  markers = [],
  hlines = [],
  rowsAt,
  readout = "linear",
  endLabels = false,
  yDomain,
  zeroLine = false,
}: Props) {
  const wrapRef = useRef<HTMLDivElement>(null);
  const [width, setWidth] = useState(640);
  const [hover, setHover] = useState<number | null>(null); // índice en xs
  const [table, setTable] = useState(false);

  useEffect(() => {
    const el = wrapRef.current;
    if (!el) return;
    const ro = new ResizeObserver(() => setWidth(Math.max(280, el.clientWidth)));
    ro.observe(el);
    setWidth(Math.max(280, el.clientWidth));
    return () => ro.disconnect();
  }, []);

  const right = endLabels ? 96 : M.right;
  const bottom = xTitle ? 48 : M.bottom; // el título del eje X va en su propia línea, sin pisar las marcas
  const innerW = Math.max(10, width - M.left - right);
  const innerH = height - M.top - bottom;

  const xs = useMemo(() => Array.from(new Set(series.flatMap((s) => s.points.map((p) => p.x)))).sort((a, b) => a - b), [series]);

  const { x0, x1, y0, y1, yTicks } = useMemo(() => {
    const allX = xs.length ? xs : [0, 1];
    const lo = Math.min(...allX);
    const hi = Math.max(...allX);
    const ys = [
      ...series.flatMap((s) => s.points.map((p) => p.y)),
      ...hlines.map((h) => h.y),
      ...markers.map((m) => m.y),
      ...(zeroLine ? [0] : []),
    ];
    let ymin = yDomain ? yDomain[0] : Math.min(...ys);
    let ymax = yDomain ? yDomain[1] : Math.max(...ys);
    if (!yDomain) {
      const pad = (ymax - ymin || Math.abs(ymax) || 1) * 0.08;
      ymin -= pad;
      ymax += pad;
    }
    const ticks = niceTicks(ymin, ymax, 5);
    return { x0: lo, x1: hi === lo ? lo + 1 : hi, y0: ymin, y1: ymax, yTicks: ticks };
  }, [xs, series, hlines, markers, yDomain, zeroLine]);

  const sx = useCallback((x: number) => M.left + ((x - x0) / (x1 - x0)) * innerW, [x0, x1, innerW]);
  const sy = useCallback((y: number) => M.top + innerH - ((y - y0) / (y1 - y0 || 1)) * innerH, [y0, y1, innerH]);
  const xTicks = useMemo(() => niceTicks(x0, x1, Math.max(2, Math.floor(innerW / 110))), [x0, x1, innerW]);

  const pathFor = (pts: Point[], step = false) => {
    if (!step) return pts.map((p, i) => `${i ? "L" : "M"}${sx(p.x).toFixed(1)} ${sy(p.y).toFixed(1)}`).join(" ");
    const head = pts.map((p, i) => (i ? `H${sx(p.x).toFixed(1)} V${sy(p.y).toFixed(1)}` : `M${sx(p.x).toFixed(1)} ${sy(p.y).toFixed(1)}`)).join(" ");
    return `${head} H${sx(x1).toFixed(1)}`; // el último valor se sostiene hasta el final del eje
  };

  const onMove = (clientX: number) => {
    const rect = wrapRef.current?.getBoundingClientRect();
    if (!rect || xs.length === 0) return;
    const px = clientX - rect.left;
    const xv = x0 + ((px - M.left) / innerW) * (x1 - x0);
    let best = 0;
    for (let i = 1; i < xs.length; i++) if (Math.abs(xs[i] - xv) < Math.abs(xs[best] - xv)) best = i;
    setHover(best);
  };

  const onKey = (e: React.KeyboardEvent) => {
    if (xs.length === 0) return;
    if (e.key === "ArrowRight") setHover((h) => Math.min(xs.length - 1, (h ?? -1) + 1));
    else if (e.key === "ArrowLeft") setHover((h) => Math.max(0, (h ?? xs.length) - 1));
    else if (e.key === "Home") setHover(0);
    else if (e.key === "End") setHover(xs.length - 1);
    else if (e.key === "Escape") setHover(null);
    else return;
    e.preventDefault();
  };

  if (xs.length === 0) return <p className="empty">Todavía no hay datos para graficar.</p>;

  const hx = hover !== null ? xs[hover] : null;
  const tipLeft = hx !== null ? sx(hx) : 0;
  const flip = tipLeft > width * 0.6;

  return (
    <div>
      {series.length >= 2 && (
        <div className="legend" aria-hidden={false}>
          {series.map((s) => (
            <span key={s.id}>
              <i style={{ background: s.color }} />
              {s.name}
            </span>
          ))}
        </div>
      )}
      <div
        className="chart"
        ref={wrapRef}
        tabIndex={0}
        role="group"
        aria-label={`${ariaLabel}. Use las flechas para recorrer los valores; también hay una vista de tabla.`}
        onKeyDown={onKey}
        onBlur={() => setHover(null)}
      >
        <svg width={width} height={height} role="img" aria-label={ariaLabel}>
          <g className="grid">
            {yTicks.map((t) => (
              <line key={t} x1={M.left} x2={width - right} y1={sy(t)} y2={sy(t)} />
            ))}
          </g>
          <g className="axis">
            <line x1={M.left} x2={width - right} y1={M.top + innerH} y2={M.top + innerH} />
          </g>
          {zeroLine && y0 < 0 && y1 > 0 && <line x1={M.left} x2={width - right} y1={sy(0)} y2={sy(0)} stroke="var(--axis)" strokeWidth="1" />}
          {yTicks.map((t) => (
            <text key={`y${t}`} x={M.left - 8} y={sy(t) + 4} textAnchor="end">
              {yFormat(t)}
            </text>
          ))}
          {xTicks.map((t) => (
            <text key={`x${t}`} x={sx(t)} y={M.top + innerH + 16} textAnchor="middle">
              {xFormat(t)}
            </text>
          ))}
          {xTitle && (
            <text x={M.left + innerW / 2} y={height - 6} textAnchor="middle">
              {xTitle}
            </text>
          )}

          {hlines.map((h) => (
            <g key={h.label}>
              <line x1={M.left} x2={width - right} y1={sy(h.y)} y2={sy(h.y)} stroke={h.color} strokeWidth="1" opacity="0.8" />
              <text x={width - right - 4} y={sy(h.y) - 5} textAnchor="end" className="anno-label">
                {h.label}
              </text>
            </g>
          ))}

          {series.map((s) =>
            s.area && s.points.length > 1 ? (
              <path
                key={`a-${s.id}`}
                d={`${pathFor(s.points, s.step)} L${sx(s.step ? x1 : s.points[s.points.length - 1].x).toFixed(1)} ${M.top + innerH} L${sx(s.points[0].x).toFixed(1)} ${M.top + innerH} Z`}
                fill={s.color}
                opacity="0.1"
              />
            ) : null,
          )}
          {series.map((s) => (
            <path key={s.id} d={pathFor(s.points, s.step)} fill="none" stroke={s.color} strokeWidth="2" strokeLinejoin="round" strokeLinecap="round" />
          ))}
          {/* en series escalonadas cada cambio se marca con un punto (con anillo de superficie) */}
          {series.map((s) =>
            s.step
              ? s.points.slice(1).map((p, i) => (
                  <circle key={`d-${s.id}-${i}`} cx={sx(p.x)} cy={sy(p.y)} r="4" fill={s.color} stroke="var(--surface)" strokeWidth="2" />
                ))
              : null,
          )}

          {endLabels &&
            series.map((s) => {
              const last = s.points[s.points.length - 1];
              if (!last) return null;
              const ex = s.step ? x1 : last.x;
              return (
                <g key={`e-${s.id}`}>
                  <circle cx={sx(ex)} cy={sy(last.y)} r="4.5" fill={s.color} stroke="var(--surface)" strokeWidth="2" />
                  <text x={sx(ex) + 9} y={sy(last.y) + 4} className="anno-label" style={{ fontWeight: 600 }}>
                    {(s.format ?? yFormat)(last.y)}
                  </text>
                </g>
              );
            })}

          {markers.map((m, i) => (
            <g key={`m${i}`}>
              <circle cx={sx(m.x)} cy={sy(m.y)} r="4.5" fill={m.color ?? "var(--ink)"} stroke="var(--surface)" strokeWidth="2" />
              {m.label && (
                <text x={sx(m.x)} y={sy(m.y) + (m.below ? 18 : -9)} textAnchor="middle" className="anno-label">
                  {m.label}
                </text>
              )}
            </g>
          ))}

          {hx !== null && (
            <g>
              <line x1={sx(hx)} x2={sx(hx)} y1={M.top} y2={M.top + innerH} stroke="var(--axis)" strokeWidth="1" />
              {series.map((s) => {
                const v = valueAt(s.points, hx, readout);
                return v === null ? null : <circle key={s.id} cx={sx(hx)} cy={sy(v)} r="4.5" fill={s.color} stroke="var(--surface)" strokeWidth="2" />;
              })}
            </g>
          )}
          {/* capa de captura: más grande que cualquier marca */}
          <rect
            x={M.left}
            y={M.top}
            width={innerW}
            height={innerH}
            fill="transparent"
            onPointerMove={(e) => onMove(e.clientX)}
            onPointerLeave={() => setHover(null)}
          />
        </svg>
        {hx !== null && (
          <div className="chart-tooltip" style={{ top: 8, left: flip ? undefined : tipLeft + 12, right: flip ? width - tipLeft + 12 : undefined }} role="status">
            <div className="t-head">{xFormat(hx)}</div>
            {series.map((s) => {
              const v = valueAt(s.points, hx, readout);
              return (
                <div className="t-row" key={s.id}>
                  <span className="t-name">
                    <span className="t-key" style={{ background: s.color }} />
                    {s.name}
                  </span>
                  <span className="t-val">{v === null ? "—" : (s.format ?? yFormat)(v)}</span>
                </div>
              );
            })}
            {rowsAt?.(hx).map((r) => (
              <div className="t-row" key={r.label}>
                <span className="t-name">{r.label}</span>
                <span className="t-val">{r.value}</span>
              </div>
            ))}
          </div>
        )}
      </div>
      <div className="chart-actions">
        <button type="button" className="btn btn-sm" onClick={() => setTable((t) => !t)} aria-expanded={table}>
          {table ? "Ocultar tabla" : "Ver como tabla"}
        </button>
      </div>
      {table && (
        <div className="table-wrap" style={{ maxHeight: 280, overflowY: "auto", marginTop: 8 }}>
          <table>
            <caption className="sr-only">{ariaLabel}</caption>
            <thead>
              <tr>
                <th>{xTitle ?? "x"}</th>
                {series.map((s) => (
                  <th key={s.id} className="num">
                    {s.name}
                  </th>
                ))}
              </tr>
            </thead>
            <tbody>
              {xs.map((x) => (
                <tr key={x}>
                  <td>{xFormat(x)}</td>
                  {series.map((s) => {
                    const v = valueAt(s.points, x, readout);
                    return (
                      <td key={s.id} className="num">
                        {v === null ? "—" : (s.format ?? yFormat)(v)}
                      </td>
                    );
                  })}
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
    </div>
  );
}
