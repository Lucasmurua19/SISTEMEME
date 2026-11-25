"""Genera los escenarios sintéticos de ``fixtures/scenarios`` de forma determinista.

Uso (desde ``backend/``):  ``python scripts/generate_fixtures.py``

Los datos son SINTÉTICOS: sirven para reproducir el comportamiento del sistema, no son evidencia de
rentabilidad. Mints, pools y firmas son cadenas base58 derivadas de hashes (formato válido, sin
existencia on-chain). Las reservas siguen producto constante para que liquidez, precio e impacto
sean coherentes entre sí.
"""

from __future__ import annotations

import hashlib
import json
import math
from pathlib import Path

import base58

OUT = Path(__file__).resolve().parents[1] / "fixtures" / "scenarios"
WINDOW = 15
T0_TOKENS_UI = 150_000_000  # tokens en el pool al migrar (unidades enteras)
DECIMALS = 6
WICK = 0.004
LAMPORTS = 1_000_000_000


def b58(seed: str, kind: str, size: int) -> str:
    digest = hashlib.sha512(f"{seed}:{kind}".encode()).digest()[:size]
    return base58.b58encode(digest).decode()


def ramp(a: float, b: float, n: int) -> list[float]:
    """n cierres geométricos que terminan exactamente en b."""
    return [a * (b / a) ** (i / n) for i in range(1, n + 1)]


def flat(v: float, n: int) -> list[float]:
    return [v] * n


# Tramo común: máximo ~1.30 en la ventana 2 y mínimo ~0.80 en la ventana 6 (caída ≈ 39 %).
PRE = [1.10, 1.20, 1.30, 1.15, 1.02, 0.90, 0.80]

VALID_BUYERS = lambda n: [12, 11, 10, 6, 4, 3, 4] + [9] * (n - 7)  # noqa: E731


def fmt(x: float) -> str:
    return f"{x:.18f}"


def build(spec: dict) -> dict:
    sid = spec["id"]
    sol0 = spec.get("sol0", 60)
    p0 = sol0 / T0_TOKENS_UI
    closes: list[float] = spec["closes"]
    n = len(closes)
    buyers = spec.get("buyers") or VALID_BUYERS(n)
    conc = spec.get("conc", 30)
    snaps = []
    prev = 1.0
    for i, c in enumerate(closes):
        hi = max(prev, c) * (1 + WICK)
        lo = min(prev, c) * (1 - WICK)
        sol = sol0 * math.sqrt(c)
        tok = T0_TOKENS_UI / math.sqrt(c)
        nb = buyers[i]
        buys = nb + 3
        sells = 4 + (i % 3)
        snaps.append(
            {
                "open": fmt(p0 * prev),
                "high": fmt(p0 * hi),
                "low": fmt(p0 * lo),
                "close": fmt(p0 * c),
                "sol_reserve_lamports": str(int(sol * LAMPORTS)),
                "token_reserve_base": str(int(tok * 10**DECIMALS)),
                "swap_count": buys + sells,
                "buy_count": buys,
                "sell_count": sells,
                "net_buyers": nb,
                "buy_volume_lamports": str(int(buys * 0.4 * LAMPORTS)),
                "sell_volume_lamports": str(int(sells * 0.3 * LAMPORTS)),
                "top10_concentration_pct": None if conc is None else str(conc),
                "coverage": "1",
            }
        )
        prev = c
    doc = {
        "id": sid,
        "description": spec["description"],
        "synthetic": True,
        "window_seconds": WINDOW,
        "token": {
            "mint": b58(sid, "mint", 32),
            "symbol": spec["symbol"],
            "name": spec["name"],
            "decimals": DECIMALS,
        },
        "pool": {"address": b58(sid, "pool", 32)},
        "migration": {
            "signature": b58(sid, "sig", 64),
            "instruction_index": 2,
            "slot": 330_000_000 + spec["offset"],
            "offset_s": spec["offset"],
        },
        "snapshots": snaps,
        "quotes": spec.get("quotes", {}),
    }
    if "verification" in spec:
        doc["verification"] = spec["verification"]
    return doc


SCENARIOS: list[dict] = [
    # --- Grupo 1: mismo instante de confirmación (ventana 9) para comparar ranking vs. liquidez
    dict(
        id="alpha_valid", symbol="ALFA", name="Alfa Coin", offset=0, sol0=60, conc=22,
        description="Rebote válido con la mejor recuperación y liquidez media. Entra y cierra por objetivo.",
        closes=PRE + [0.83, 0.88, 1.02] + [1.04, 1.12, 1.22, 1.32, 1.40] + flat(1.40, 9),
    ),
    dict(
        id="beta_valid", symbol="BETA", name="Beta Token", offset=0, sol0=90, conc=32,
        description="Rebote válido con recuperación media y mayor liquidez. Entra y cierra por objetivo.",
        closes=PRE + [0.83, 0.87, 0.97] + [0.985, 1.04, 1.12, 1.20, 1.30] + flat(1.30, 9),
    ),
    dict(
        id="gamma_liquid_loser", symbol="GAMA", name="Gamma Inu", offset=0, sol0=140, conc=44,
        description="La mayor liquidez pero el rebote más débil y mayor costo: luego pierde el nivel de invalidación.",
        quotes={"fee_bps": 60},
        closes=PRE + [0.82, 0.86, 0.93] + [0.90, 0.84, 0.76, 0.70, 0.66] + flat(0.66, 9),
    ),
    # --- Descartes y pendientes
    dict(
        id="lambda_high_cost", symbol="LAMB", name="Lambda Cash", offset=30, sol0=70, conc=30,
        description="Patrón válido pero la comisión de ruta hace que el costo de ida y vuelta supere el límite.",
        quotes={"fee_bps": 300},
        closes=PRE + [0.83, 0.88, 0.98] + [1.0, 1.05, 1.10, 1.15, 1.20] + flat(1.20, 9),
    ),
    dict(
        id="delta_low_volume", symbol="DELT", name="Delta Dog", offset=45, sol0=80, conc=30,
        description="El rebote ocurre sin compradores netos suficientes (rebote sin volumen).",
        buyers=[12, 11, 10, 6, 4, 3, 4] + [2] * 17,
        closes=PRE + [0.83, 0.88, 0.99] + [1.0, 1.04, 1.08, 1.12, 1.15] + flat(1.15, 9),
    ),
    dict(
        id="epsilon_unknown_holders", symbol="EPSI", name="Epsilon Cat", offset=60, sol0=75, conc=None,
        description="No se pudo evaluar la concentración de holders: dato crítico incompleto, queda pendiente.",
        closes=PRE + [0.83, 0.88, 0.99] + [1.0, 1.04, 1.08, 1.12, 1.15] + flat(1.15, 9),
    ),
    dict(
        id="zeta_no_sell_route", symbol="ZETA", name="Zeta Pepe", offset=75, sol0=80, conc=28,
        description="Patrón confirmado pero no existe ruta de venta cotizable: pendiente hasta que venza.",
        quotes={"sell_unavailable_s": [[0, 100000]]},
        closes=PRE + [0.83, 0.88, 0.99] + [1.0, 1.04, 1.08, 1.12, 1.15] + flat(1.15, 9),
    ),
    dict(
        id="eta_rug_drop", symbol="ETA", name="Eta Rug", offset=90, sol0=65, conc=30,
        description="Caída del 80 %: supera el máximo admisible antes de buscar recuperación.",
        closes=[1.10, 1.20, 1.30, 0.90, 0.60, 0.35, 0.25, 0.30, 0.40, 0.50, 0.55, 0.60] + flat(0.6, 12),
    ),
    dict(
        id="mu_concentrated", symbol="MU", name="Mu Whale", offset=105, sol0=85, conc=72,
        description="Patrón válido pero los 10 mayores propietarios concentran el 72 % del suministro.",
        closes=PRE + [0.83, 0.88, 0.99] + [1.0, 1.04, 1.08, 1.12, 1.15] + flat(1.15, 9),
    ),
    dict(
        id="theta_invalidated", symbol="THET", name="Theta Fall", offset=120, sol0=88, conc=26,
        description="Señal válida; tras la entrada el precio rompe el nivel de invalidación y se sale con pérdida.",
        closes=PRE + [0.83, 0.87, 0.97] + [0.95, 0.88, 0.74, 0.70, 0.68] + flat(0.68, 9),
    ),
    dict(
        id="nu_stale_data", symbol="NU", name="Nu Stale", offset=135, sol0=70, conc=30,
        description="La fuente de mercado deja de entregar ventanas: los datos vencen y el candidato queda pendiente.",
        closes=PRE[:6],
    ),
    dict(
        id="xi_unverified", symbol="XI", name="Xi Fake", offset=150, sol0=70, conc=30,
        description="El pool no pudo verificarse contra el programa publicado: descartado por identidad.",
        verification={"verified": False, "identity_ok": False, "canonical": False, "reason": "pool no derivado del mint"},
        closes=PRE + [0.83, 0.88, 0.99] + [1.0, 1.04, 1.08, 1.12, 1.15] + flat(1.15, 9),
    ),
    # --- Ejecución simulada
    dict(
        id="iota_price_jump", symbol="IOTA", name="Iota Jump", offset=180, sol0=82, conc=29,
        description="Señal válida; tras el retraso de entrada el precio cotizado salta 12 % y la entrada se rechaza.",
        quotes={"price_multiplier": [{"from_s": 175, "to_s": 195, "multiplier": "1.12"}]},
        closes=PRE + [0.83, 0.87, 0.97] + [0.985, 1.04, 1.12, 1.20, 1.30] + flat(1.30, 9),
    ),
    dict(
        id="omicron_retry_exit", symbol="OMIC", name="Omicron Retry", offset=225, sol0=78, conc=27,
        description="Entra y dispara salida por invalidación; la ruta de venta desaparece y vuelve: reintentos hasta cerrar.",
        quotes={"sell_unavailable_s": [[195, 300]]},
        closes=PRE + [0.83, 0.87, 0.97] + [0.95, 0.88, 0.74, 0.70, 0.68] + flat(0.68, 11),
    ),
    dict(
        id="kappa_stuck_exit", symbol="KAPA", name="Kappa Stuck", offset=270, sol0=80, conc=25,
        description="Entra y luego se queda sin ruta de venta: la posición sigue abierta, ilíquida, con recuperación cero.",
        quotes={"sell_unavailable_s": [[190, 100000]]},
        closes=PRE + [0.83, 0.87, 0.97] + [0.95, 0.88, 0.74, 0.70, 0.68] + flat(0.68, 11),
    ),
]


def main() -> None:
    OUT.mkdir(parents=True, exist_ok=True)
    for old in OUT.glob("*.json"):
        old.unlink()
    for spec in SCENARIOS:
        doc = build(spec)
        (OUT / f"{spec['id']}.json").write_text(json.dumps(doc, indent=1, ensure_ascii=False) + "\n", encoding="utf-8")
    print(f"{len(SCENARIOS)} escenarios en {OUT}")


if __name__ == "__main__":
    main()
