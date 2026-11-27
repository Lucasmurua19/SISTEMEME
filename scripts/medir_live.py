"""Mide una instancia live (proyecto Compose ``radar-live``) y escribe un informe.

Solo lectura: consultas SQL en el contenedor de la base, el log del worker y la API local. No imprime variables de
entorno ni URLs. Usa únicamente la biblioteca estándar.

    python scripts/medir_live.py                          # informe del estado actual
    python scripts/medir_live.py --horas 5 --detener      # mide cada 10 min, informa a las 5 h y detiene la instancia

Los archivos quedan en ``reportes/`` (ignorada por Git). Con ``--detener`` se ejecuta ``docker compose stop``:
los datos se conservan y la instancia se reanuda con ``docker compose --env-file .env.live -p radar-live up -d``.
"""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
import time
import urllib.request
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "reportes"
WSOL = "So11111111111111111111111111111111111111112"
# Espejo de app.domain.strategy.TERMINAL_CODES (el script corre fuera del contenedor).
TERMINAL = (
    "MIGRATION_UNVERIFIED", "IDENTITY_MISMATCH", "UNSUPPORTED_QUOTE_MINT", "DRAWDOWN_TOO_DEEP",
    "PATTERN_EXPIRED", "PATTERN_INVALIDATED",
)  # fmt: skip

SQL = f"""
with ultima as (
  select distinct on (pool) pool, status, reasons::jsonb as reasons from evaluations order by pool, window_index desc
), descartados as (
  select pool from ultima where exists (
    select 1 from jsonb_array_elements(reasons) r where r->>'code' in ({",".join(f"'{c}'" for c in TERMINAL)}))
), lat as (
  select extract(epoch from observed_at - event_at) as s from chain_events
), mejor as (
  select distinct on (pool) pool, reasons::jsonb as reasons from evaluations
  order by pool, jsonb_array_length(reasons::jsonb), window_index desc
), porpool as (
  select p.address as pool, p.migrated_at, max(m.window_end) as fin, max(m.sol_reserve_lamports) as reserva_max,
         bool_or(m.top10_concentration_pct is not null) as con_conc, p.quote_mint = '{WSOL}' as en_sol
  from pools p left join market_snapshots m on m.pool = p.address group by p.address, p.migrated_at, p.quote_mint
), conc as (
  select distinct on (pool) pool, top10_concentration_pct as v from market_snapshots
  where top10_concentration_pct is not null order by pool, window_index desc
)
select json_build_object(
 'minutos', (select round((extract(epoch from now() - min(started_at))/60)::numeric, 1) from paper_sessions),
 'migraciones', (select count(*) from chain_events),
 'latencia', (select json_build_object('mediana', round(percentile_cont(0.5) within group (order by s)::numeric,1),
   'p90', round(percentile_cont(0.9) within group (order by s)::numeric,1), 'max', round(max(s)::numeric,1)) from lat),
 'pools', (select count(*) from pools),
 'pools_sol_ok', (select count(*) from pools where verified and identity_ok and quote_mint='{WSOL}'),
 'pools_otro_token', (select count(*) from pools where quote_mint<>'{WSOL}'),
 'pools_con_ventanas', (select count(*) from porpool where fin is not null),
 'pools_con_concentracion', (select count(*) from porpool where con_conc),
 'pools_20sol', (select count(*) from porpool where en_sol and reserva_max::numeric >= 20e9),
 'concentracion', (select json_build_object('mediana', round(percentile_cont(0.5) within group (order by v::double precision)::numeric,1),
   'sobre_50', count(*) filter (where v::numeric > 50), 'n', count(*)) from conc),
 'activos', (select json_build_object('pools', count(*), 'sin_ventanas', count(*) filter (where fin is null),
   'atraso_mediano_s', round(percentile_cont(0.5) within group (order by extract(epoch from now() - fin))::numeric,0))
   from porpool where migrated_at > now() - interval '15 minutes' and pool not in (select pool from descartados)),
 'descartados', (select count(*) from descartados),
 'evaluaciones', (select count(*) from evaluations),
 'pools_evaluados', (select count(*) from ultima),
 'estado_ultima', (select json_object_agg(status, n) from (select status, count(*) n from ultima group by status) t),
 'motivos_alguna_vez', (select json_object_agg(c, n) from (select r->>'code' c, count(distinct pool) n
   from evaluations, jsonb_array_elements(reasons::jsonb) r group by 1 order by 2 desc) t),
 'mas_cerca', (select json_agg(json_build_object('motivos', m, 'pools', n)) from (
   select (select string_agg(r->>'code', ' + ' order by r->>'code') from jsonb_array_elements(reasons) r) m, count(*) n,
          min(jsonb_array_length(reasons)) k from mejor group by 1 order by 3, 2 desc limit 8) t),
 'cotizaciones', (select json_object_agg(k, n) from (select purpose||'/'||side||'/'||status k, count(*) n from quotes group by 1) t),
 'pools_cotizados', (select count(distinct pool) from quotes where purpose='evaluation'),
 'senales', (select count(*) from signals),
 'posiciones', (select json_object_agg(k, n) from (select s.policy||'/'||p.status k, count(*) n
   from paper_positions p join paper_sessions s on s.id=p.session_id group by 1) t)
);
"""


def mask(text: str) -> str:
    return re.sub(r"(api[-_]?key=)[^&\s\"']+", r"\1***", text, flags=re.I)


class Instance:
    def __init__(self, project: str, env_file: str, api: str) -> None:
        self.base = ["docker", "compose", "--env-file", env_file, "-p", project]
        self.api = api

    def compose(self, args: list[str], stdin: str | None = None, timeout: int = 180) -> str:
        r = subprocess.run(
            self.base + args, cwd=ROOT, input=stdin, capture_output=True, text=True, encoding="utf-8",
            errors="replace", timeout=timeout,
        )  # fmt: skip
        if r.returncode != 0:
            raise RuntimeError(mask(r.stderr.strip())[-300:])
        return r.stdout

    def database(self) -> dict:
        raw = self.compose(
            ["exec", "-T", "db", "psql", "-U", "radar", "-d", "radar", "-At", "-v", "ON_ERROR_STOP=1"], stdin=SQL
        )
        return json.loads(raw.strip())

    def log(self) -> dict:
        text = self.compose(["logs", "worker", "--no-color", "--no-log-prefix"])
        n = lambda pat: len(re.findall(pat, text))  # noqa: E731
        times = sorted(x for x in (float(v) for v in re.findall(r"'evaluate': ([0-9.]+)", text)) if x > 0.05)
        bulk = re.findall(r"lectura en bloque acumulada: (\d+) llamadas, (\d+) transacciones, ~(\d+) créditos", text)
        return {
            "bloque_activa": n(r"lectura en bloque activa"),
            "bloque_no_admitida": n(r"no admite la lectura en bloque"),
            "bloque_llamadas": int(bulk[-1][0]) if bulk else 0,
            "bloque_transacciones": int(bulk[-1][1]) if bulk else 0,
            "bloque_creditos": int(bulk[-1][2]) if bulk else 0,
            "tardias": n(r"llegó tarde una transacción"),
            "arranques": n(r"worker iniciado"),
            "ws_conectado": n(r"WebSocket de migraciones conectado"),
            "ws_caidas": n(r"WebSocket caído"),
            "lineas_429": n(r"HTTP 429"),
            "fuente_transitoria": n(r"fuente transitoria"),
            "concentracion_no_disponible": n(r"concentración no disponible"),
            "historial_truncado": n(r"no se arman más ventanas"),
            "pasos_lentos": n(r"tardó"),
            "errores": n(r" ERROR "),
            "ciclos": n(r"ingesta="),
            "evaluate_mediana_s": times[len(times) // 2] if times else 0,
            "evaluate_max_s": times[-1] if times else 0,
        }

    def metrics(self) -> dict | None:
        try:
            with urllib.request.urlopen(f"{self.api}/api/v1/metrics", timeout=60) as resp:
                return json.load(resp)
        except Exception:
            return None

    def sample(self) -> dict:
        return {"hora": datetime.now().isoformat(timespec="seconds"), "base": self.database(), "log": self.log()}


def line(s: dict) -> str:
    b, g = s["base"], s["log"]
    lat, act = b.get("latencia") or {}, b.get("activos") or {}
    return (
        f"{s['hora']} | min {b.get('minutos')} | migraciones {b.get('migraciones')} lat med/p90 {lat.get('mediana')}/{lat.get('p90')}s | "
        f"con ventanas {b.get('pools_con_ventanas')}/{b.get('pools')} concentración {b.get('pools_con_concentracion')} "
        f"atraso activos {act.get('atraso_mediano_s')}s | descartados {b.get('descartados')} | cotizados {b.get('pools_cotizados')} "
        f"señales {b.get('senales')} posiciones {b.get('posiciones')} | 429 {g['lineas_429']} transitorias {g['fuente_transitoria']} "
        f"errores {g['errores']} ws_caídas {g['ws_caidas']} arranques {g['arranques']} | "
        f"bloque {g['bloque_transacciones']} tx ~{g['bloque_creditos']} créditos, tardías {g['tardias']}"
    )


def sol(value: str | None) -> str:
    return "—" if value is None else f"{int(value) / 1e9:+.4f} SOL"


def report(s: dict, metrics: dict | None, samples: list[dict]) -> str:
    b, g = s["base"], s["log"]
    lat, act, conc = b.get("latencia") or {}, b.get("activos") or {}, b.get("concentracion") or {}
    hours = float(b.get("minutos") or 0) / 60
    per_hour = f"{b['migraciones'] / hours:.0f}" if hours > 0.05 else "—"
    out = [
        f"# Informe live — {s['hora']}",
        "",
        f"Duración de la sesión: **{hours:.1f} h**. Estrategia congelada `live-v1`. Operaciones simuladas.",
        "",
        "## 1. Ingesta",
        f"- Migraciones detectadas: **{b.get('migraciones')}** ({per_hour} por hora).",
        f"- Latencia bloque → registro: mediana {lat.get('mediana')} s, p90 {lat.get('p90')} s, máxima {lat.get('max')} s.",
        f"- WebSocket: {g['ws_conectado']} conexión(es), {g['ws_caidas']} caída(s). Arranques del worker: {g['arranques']}.",
        "",
        "## 2. Datos",
        f"- Pools: {b.get('pools')} (en SOL y verificados {b.get('pools_sol_ok')}, cotizados en otro token {b.get('pools_otro_token')}).",
        f"- Con ventanas de mercado: {b.get('pools_con_ventanas')}. Con concentración conocida: {b.get('pools_con_concentracion')}.",
        f"- Concentración top-10 (último dato por pool): mediana {conc.get('mediana')} %, {conc.get('sobre_50')} de {conc.get('n')} sobre 50 %.",
        f"- Pools que alguna vez tuvieron 20 SOL o más de reserva: {b.get('pools_20sol')}.",
        f"- Pools activos ahora: {act.get('pools')} (sin ventanas {act.get('sin_ventanas')}); atraso mediano {act.get('atraso_mediano_s')} s "
        "(la estrategia considera vencidos los datos de más de 45 s).",
        f"- Líneas con HTTP 429: {g['lineas_429']}; evaluaciones con falla transitoria: {g['fuente_transitoria']}; "
        f"avisos de concentración no disponible: {g['concentracion_no_disponible']}; errores: {g['errores']}.",
        f"- Pools con historial demasiado grande para reconstruirlo (sin ventanas): {g['historial_truncado']}.",
        (
            f"- Lectura en bloque: {g['bloque_llamadas']} llamadas, {g['bloque_transacciones']} transacciones, "
            f"~{g['bloque_creditos']} créditos estimados (último registro del arranque actual; no incluye las demás llamadas). "
            f"Pools con transacciones llegadas tarde: {g['tardias']}."
            if g["bloque_activa"]
            else "- Lectura en bloque: no se usó (las transacciones se leyeron de a una)."
        ),
        f"- Paso de evaluación: mediana {g['evaluate_mediana_s']} s, máximo {g['evaluate_max_s']} s.",
        "",
        "## 3. Evaluación",
        f"- Evaluaciones guardadas: {b.get('evaluaciones')} sobre {b.get('pools_evaluados')} pools. "
        f"Descartados en forma definitiva: {b.get('descartados')}.",
        f"- Estado de la última evaluación por pool: {b.get('estado_ultima')}.",
        f"- Pools que llegaron a pedir cotización: **{b.get('pools_cotizados')}**. Cotizaciones: {b.get('cotizaciones')}.",
        f"- Señales: **{b.get('senales')}**. Posiciones: {b.get('posiciones')}.",
        "",
        "### Pools que alguna vez fallaron cada filtro",
        "",
        "| Motivo | Pools |",
        "|---|---:|",
    ]
    reasons = b.get("motivos_alguna_vez") or {}
    out += [f"| `{k}` | {v} |" for k, v in sorted(reasons.items(), key=lambda kv: -kv[1])]
    out += [
        "",
        "### Lo más cerca que estuvo cada pool de ser elegible",
        "",
        "Para cada pool, su evaluación con menos motivos en contra, agrupada.",
        "",
        "| Motivos en la mejor evaluación | Pools |",
        "|---|---:|",
    ]
    out += [f"| {x['motivos'] or '(ninguno: elegible)'} | {x['pools']} |" for x in (b.get("mas_cerca") or [])]
    out += ["", "## 4. Simulación", ""]
    if metrics:
        rows = [("Ranking", metrics.get("metrics"))]
        if metrics.get("comparison"):
            rows.append(("Liquidez", metrics["comparison"].get("reference")))
        out += ["| Sesión | Cerradas | Abiertas | PnL realizado | Abiertas valoradas | Total valorado |", "|---|---:|---:|---:|---:|---:|"]
        out += [
            f"| {name} | {m['trades_closed']} | {m['trades_open']} | {sol(m['realized_pnl_lamports'])} | "
            f"{sol(m.get('open_pnl_valued_lamports'))} | {sol(m.get('total_pnl_valued_lamports'))} |"
            for name, m in rows
            if m
        ]
    else:
        out.append("La API no respondió: sin métricas de las sesiones.")
    if samples:
        out += ["", "## Evolución", "", "```"] + [line(x) for x in samples] + ["```"]
    out += [
        "",
        "## Cómo leerlo",
        "- Cero señales no demuestra que la estrategia sea mala ni buena: mirá primero si los datos llegaron completos y a tiempo.",
        "- No cambies `live-v1` a partir de este informe sin crear una versión nueva y volver a medir.",
        "- Revisá el consumo de créditos en el panel de tu proveedor RPC.",
        "",
    ]
    return "\n".join(out)


def main() -> int:
    ap = argparse.ArgumentParser(description="Mide la instancia live y escribe un informe.")
    ap.add_argument("--horas", type=float, default=0.0, help="duración de la medición; 0 = informe único ahora")
    ap.add_argument("--cada", type=float, default=10.0, help="minutos entre registros")
    ap.add_argument("--detener", action="store_true", help="al terminar, detener la instancia (conserva los datos)")
    ap.add_argument("--proyecto", default="radar-live")
    ap.add_argument("--env-file", default=".env.live")
    ap.add_argument("--api", default="http://127.0.0.1:8001")
    args = ap.parse_args()
    if sys.stdout is not None:  # en una ventana oculta la consola de Windows no es UTF-8
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")

    OUT.mkdir(exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d-%H%M")
    log_path, report_path = OUT / f"live-{stamp}.log", OUT / f"live-{stamp}-informe.md"
    inst = Instance(args.proyecto, args.env_file, args.api)
    samples: list[dict] = []

    def record(text: str) -> None:
        if sys.stdout is not None:
            print(text, flush=True)
        with log_path.open("a", encoding="utf-8") as f:
            f.write(text + "\n")

    deadline = time.time() + args.horas * 3600
    try:
        while time.time() < deadline:
            time.sleep(max(1.0, min(args.cada * 60, deadline - time.time())))
            try:
                sample = inst.sample()
                samples.append(sample)
                record(line(sample))
                with (OUT / f"live-{stamp}.jsonl").open("a", encoding="utf-8") as f:
                    f.write(json.dumps(sample, ensure_ascii=False) + "\n")
            except Exception as exc:  # una medición fallida no corta la corrida
                record(f"{datetime.now().isoformat(timespec='seconds')} | no se pudo medir: {mask(str(exc))[:200]}")
        try:
            final = inst.sample()
            report_path.write_text(report(final, inst.metrics(), samples), encoding="utf-8")
            record(f"informe: {report_path}")
        except Exception as exc:
            record(f"no se pudo escribir el informe: {mask(str(exc))[:200]}")
            return 1
    finally:
        if args.detener and args.horas > 0:
            try:
                inst.compose(["stop"], timeout=300)
                record("instancia detenida (los datos se conservan)")
            except Exception as exc:
                record(f"NO se pudo detener la instancia: {mask(str(exc))[:200]}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
