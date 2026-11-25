// Acceso a la API.
// - En el servidor se usa API_INTERNAL_URL (red interna de Compose).
// - En el navegador solo se llama a /api/v1/*, que el proxy de Next reenvía: ni la URL interna ni el
//   token de administración llegan al cliente.

import type { CandidateList, MetricsResponse, OpsResponse, PaperTrades, SessionInfo, SystemStatus, TokenDetail } from "./types";

const INTERNAL = process.env.API_INTERNAL_URL ?? "http://localhost:8000";

export class ApiError extends Error {
  constructor(
    public status: number,
    message: string,
  ) {
    super(message);
  }
}

async function request<T>(path: string, init?: RequestInit): Promise<T> {
  const base = typeof window === "undefined" ? INTERNAL : "";
  const res = await fetch(`${base}${path}`, { cache: "no-store", ...init });
  if (!res.ok) {
    let detail = res.statusText;
    try {
      const body = await res.json();
      detail = typeof body.detail === "string" ? body.detail : detail;
    } catch {
      /* cuerpo no JSON */
    }
    throw new ApiError(res.status, detail);
  }
  return (await res.json()) as T;
}

export const api = {
  status: () => request<SystemStatus>("/api/v1/status"),
  candidates: (status = "all", limit = 100) => request<CandidateList>(`/api/v1/candidates?status=${status}&limit=${limit}`),
  token: (mint: string) => request<TokenDetail>(`/api/v1/tokens/${encodeURIComponent(mint)}`),
  sessions: () => request<SessionInfo[]>("/api/v1/sessions"),
  paperTrades: (sessionId?: number) =>
    request<PaperTrades>(`/api/v1/paper-trades${sessionId ? `?session_id=${sessionId}` : ""}`),
  metrics: (sessionId?: number, since?: string, until?: string) => {
    const q = new URLSearchParams();
    if (sessionId) q.set("session_id", String(sessionId));
    if (since) q.set("since", since);
    if (until) q.set("until", until);
    const qs = q.toString();
    return request<MetricsResponse>(`/api/v1/metrics${qs ? `?${qs}` : ""}`);
  },
  ops: () => request<OpsResponse>("/api/v1/ops"),
  pause: (sessionId: number, reason?: string) =>
    request<{ id: number; paused: boolean }>(`/api/v1/sessions/${sessionId}/pause`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ reason: reason ?? null }),
    }),
  resume: (sessionId: number) => request<{ id: number; paused: boolean }>(`/api/v1/sessions/${sessionId}/resume`, { method: "POST" }),
};

/** Carga para páginas de servidor: si la API no responde, devuelve ``null`` y la página muestra el estado. */
export async function safe<T>(fn: () => Promise<T>): Promise<{ data: T | null; error: string | null }> {
  try {
    return { data: await fn(), error: null };
  } catch (e) {
    return { data: null, error: e instanceof ApiError ? `${e.status}: ${e.message}` : "API no disponible" };
  }
}
