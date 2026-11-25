import { NextRequest, NextResponse } from "next/server";

// Proxy de la API: el navegador solo habla con este origen.
// - GET: lectura (candidatos, detalle, simulación, métricas, CSV).
// - POST: únicamente pausar/reanudar una sesión, y solo si PANEL_CONTROLS=true. El token de
//   administración se agrega aquí, del lado del servidor, y nunca llega al navegador.

export const dynamic = "force-dynamic";

const API = (process.env.API_INTERNAL_URL ?? "http://localhost:8000").replace(/\/$/, "");
const SEGMENT = /^[A-Za-z0-9._-]+$/;
const CONTROL_PATH = /^sessions\/\d+\/(pause|resume)$/;

async function forward(req: NextRequest, path: string[], method: "GET" | "POST"): Promise<NextResponse> {
  if (path.length === 0 || !path.every((s) => SEGMENT.test(s) && s !== "." && s !== "..")) {
    return NextResponse.json({ detail: "ruta inválida" }, { status: 400 });
  }
  const joined = path.join("/");
  const headers: Record<string, string> = { Accept: req.headers.get("accept") ?? "application/json" };
  let body: string | undefined;

  if (method === "POST") {
    if (process.env.PANEL_CONTROLS !== "true" || !CONTROL_PATH.test(joined)) {
      return NextResponse.json({ detail: "controles deshabilitados en el panel" }, { status: 403 });
    }
    headers["Content-Type"] = "application/json";
    headers["X-Admin-Token"] = process.env.ADMIN_TOKEN ?? "";
    body = await req.text();
  }

  try {
    const upstream = await fetch(`${API}/api/v1/${path.map(encodeURIComponent).join("/")}${req.nextUrl.search}`, {
      method,
      headers,
      body,
      cache: "no-store",
      signal: AbortSignal.timeout(15_000),
    });
    const out = new NextResponse(upstream.body, { status: upstream.status });
    const type = upstream.headers.get("content-type");
    if (type) out.headers.set("content-type", type);
    out.headers.set("cache-control", "no-store");
    return out;
  } catch {
    return NextResponse.json({ detail: "API no disponible" }, { status: 502 });
  }
}

type Ctx = { params: Promise<{ path: string[] }> };

export async function GET(req: NextRequest, { params }: Ctx) {
  return forward(req, (await params).path, "GET");
}

export async function POST(req: NextRequest, { params }: Ctx) {
  return forward(req, (await params).path, "POST");
}
