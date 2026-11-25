// Formato de montos, porcentajes y tiempos. Los lamports se manejan con BigInt: nunca pasan por float.

const LAMPORTS = 1_000_000_000n;

export function toBig(v: string | number | bigint | null | undefined): bigint | null {
  if (v === null || v === undefined || v === "") return null;
  try {
    return typeof v === "bigint" ? v : BigInt(typeof v === "number" ? Math.trunc(v) : v.split(".")[0]);
  } catch {
    return null;
  }
}

/** Lamports → SOL con ``digits`` decimales (trunca hacia cero, exacto con BigInt). */
export function formatSol(v: string | number | bigint | null | undefined, digits = 4, signed = false): string {
  const n = toBig(v);
  if (n === null) return "—";
  const neg = n < 0n;
  const abs = neg ? -n : n;
  const whole = abs / LAMPORTS;
  const frac = (abs % LAMPORTS).toString().padStart(9, "0").slice(0, digits);
  const body = digits > 0 ? `${whole.toString()}.${frac}` : whole.toString();
  const sign = neg ? "−" : signed && abs > 0n ? "+" : "";
  return `${sign}${body}`;
}

export function solNumber(v: string | null | undefined): number {
  const n = toBig(v);
  return n === null ? 0 : Number(n) / 1e9;
}

export function formatPct(v: string | number | null | undefined, digits = 2, signed = false): string {
  if (v === null || v === undefined || v === "") return "—";
  const n = Number(v);
  if (!Number.isFinite(n)) return "—";
  const s = n.toFixed(digits);
  return `${signed && n > 0 ? "+" : n < 0 ? "−" : ""}${s.replace("-", "")}%`;
}

export function formatRatioPct(v: string | null | undefined, digits = 1): string {
  return v === null || v === undefined ? "—" : formatPct(Number(v) * 100, digits);
}

export function formatNumber(v: string | number | null | undefined, digits = 2): string {
  if (v === null || v === undefined || v === "") return "—";
  const n = Number(v);
  return Number.isFinite(n) ? n.toLocaleString("es-AR", { maximumFractionDigits: digits, minimumFractionDigits: 0 }) : "—";
}

export function formatUsd(v: string | number | null | undefined, signed = false): string {
  if (v === null || v === undefined || v === "") return "—";
  const n = Number(v);
  if (!Number.isFinite(n)) return "—";
  const body = Math.abs(n).toLocaleString("es-AR", { minimumFractionDigits: 2, maximumFractionDigits: 2 });
  return `${n < 0 ? "−" : signed && n > 0 ? "+" : ""}US$ ${body}`;
}

/** Precio en SOL por token con cifras significativas (los precios de memecoins son muy pequeños). */
export function formatPrice(v: string | number | null | undefined): string {
  if (v === null || v === undefined || v === "") return "—";
  const n = Number(v);
  if (!Number.isFinite(n) || n === 0) return "0";
  return n.toPrecision(4).replace(/\.?0+$/, (m) => (m.includes("e") ? m : ""));
}

export function shortKey(k: string | null | undefined, head = 4, tail = 4): string {
  if (!k) return "—";
  return k.length <= head + tail + 1 ? k : `${k.slice(0, head)}…${k.slice(-tail)}`;
}

export function formatDuration(totalSeconds: number | null | undefined): string {
  if (totalSeconds === null || totalSeconds === undefined || !Number.isFinite(totalSeconds)) return "—";
  const s = Math.max(0, Math.round(totalSeconds));
  if (s < 60) return `${s}s`;
  const m = Math.floor(s / 60);
  if (m < 60) return `${m}m ${String(s % 60).padStart(2, "0")}s`;
  const h = Math.floor(m / 60);
  return `${h}h ${String(m % 60).padStart(2, "0")}m`;
}

/** Hora UTC determinista (igual en servidor y cliente: sin diferencias de hidratación). */
export function formatClock(iso: string | null | undefined): string {
  if (!iso) return "—";
  const d = new Date(iso);
  if (Number.isNaN(d.getTime())) return "—";
  return `${d.toISOString().slice(11, 19)} UTC`;
}

export function formatDateTime(iso: string | null | undefined): string {
  if (!iso) return "—";
  const d = new Date(iso);
  if (Number.isNaN(d.getTime())) return "—";
  return `${d.toISOString().slice(0, 10)} ${d.toISOString().slice(11, 19)} UTC`;
}

export function secondsBetween(fromIso: string | null | undefined, toIso: string | null | undefined): number | null {
  if (!fromIso || !toIso) return null;
  const a = Date.parse(fromIso);
  const b = Date.parse(toIso);
  return Number.isNaN(a) || Number.isNaN(b) ? null : (b - a) / 1000;
}

export function pnlClass(v: string | null | undefined): string {
  const n = toBig(v);
  if (n === null || n === 0n) return "";
  return n > 0n ? "pos" : "neg";
}

export function solscan(kind: "tx" | "account" | "token", id: string): string {
  return `https://solscan.io/${kind}/${encodeURIComponent(id)}`;
}
