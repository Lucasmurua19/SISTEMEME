"""Cotizaciones de solo lectura con Jupiter Swap API v2 (``GET /order`` sin ``taker``).

El MVP únicamente consume la cotización: no se pide ni se firma ninguna transacción. La API key va en
la cabecera ``x-api-key`` y nunca se registra. Las llamadas se espacian para respetar el límite de tasa
(sin clave ≈ 0,5 req/s) y los errores transitorios se reintentan con espera creciente.
"""

from __future__ import annotations

import logging
import threading
import time
from collections.abc import Callable
from datetime import datetime, timedelta
from decimal import Decimal
from typing import Any

import httpx

from app.application.ports import FxRate
from app.domain.models import USDC_MINT, WSOL_MINT, PoolInfo, Quote, QuoteCost, QuoteStatus

log = logging.getLogger(__name__)

# «failed to get quotes» es lo que responde hoy Jupiter v2 (HTTP 400) cuando no hay ruta para un mint válido,
# comprobado contra la API real. Un mint inválido responde otro texto y sigue siendo un error.
_NO_ROUTE_MARKERS = (
    "could_not_find_any_route", "no route", "no routes", "route not found", "token_not_tradable", "failed to get quotes",
)  # fmt: skip


class JupiterClient:
    def __init__(
        self,
        base_url: str,
        api_key: str = "",
        *,
        http: httpx.Client | None = None,
        max_rps: float = 0.0,
        max_retries: int = 2,
        backoff_s: float = 1.0,
        sleep: Callable[[float], None] = time.sleep,
        monotonic: Callable[[], float] = time.monotonic,
    ) -> None:
        self._base = base_url.rstrip("/")
        self._headers = {"x-api-key": api_key} if api_key else {}
        self._http = http or httpx.Client(timeout=10.0)
        # sin clave el límite público es de ~0,5 req/s; con clave, ~1 req/s por defecto
        self._min_interval = 1.0 / (max_rps or (1.0 if api_key else 0.5))
        self._retries = max_retries
        self._backoff = backoff_s
        self._sleep = sleep
        self._mono = monotonic
        self._last = -1e9
        self._lock = threading.Lock()

    def _throttle(self) -> None:
        with self._lock:
            wait = self._min_interval - (self._mono() - self._last)
            if wait > 0:
                self._sleep(wait)
            self._last = self._mono()

    def order(self, input_mint: str, output_mint: str, amount: int, slippage_bps: int) -> tuple[str, dict[str, Any] | str]:
        """Devuelve ``("ok", cuerpo)``, ``("no_route", motivo)`` o ``("error", motivo)``."""
        params = {"inputMint": input_mint, "outputMint": output_mint, "amount": str(amount), "slippageBps": str(slippage_bps)}
        last = "sin respuesta"
        for attempt in range(self._retries + 1):
            self._throttle()
            try:
                resp = self._http.get(f"{self._base}/order", params=params, headers=self._headers)
            except httpx.TransportError as exc:
                last = type(exc).__name__
                self._sleep(self._backoff * (2**attempt))
                continue
            if resp.status_code == 429 or resp.status_code >= 500:
                last = f"HTTP {resp.status_code}"
                self._sleep(self._backoff * (2**attempt))
                continue
            try:
                body = resp.json()
            except ValueError:
                return "error", f"HTTP {resp.status_code}: respuesta no JSON"
            if resp.status_code >= 400:
                text = str(body.get("error") or body.get("errorMessage") or body).lower() if isinstance(body, dict) else ""
                if any(m in text for m in _NO_ROUTE_MARKERS):
                    return "no_route", text[:200]
                return "error", f"HTTP {resp.status_code}: {text[:160]}"
            return "ok", body
        return "error", f"reintentos agotados ({last})"


def _int(value: Any) -> int:
    try:
        return int(str(value))
    except (TypeError, ValueError):
        return 0


class JupiterQuoteProvider:
    name = "jupiter"

    def __init__(
        self,
        client: JupiterClient,
        *,
        quote_ttl_s: int = 20,
        priority_fee_lamports: int = 100_000,
        ata_deposit_lamports: int = 2_039_280,
    ) -> None:
        self._client = client
        self._ttl = quote_ttl_s
        self._priority = priority_fee_lamports
        self._ata = ata_deposit_lamports

    def _quote(self, side: str, pool: PoolInfo, amount: int, slippage_bps: int, now: datetime) -> Quote:
        in_mint, out_mint = (WSOL_MINT, pool.mint) if side == "buy" else (pool.mint, WSOL_MINT)
        base = dict(side=side, input_mint=in_mint, output_mint=out_mint, in_amount=amount, observed_at=now,
                    expires_at=now + timedelta(seconds=self._ttl))  # fmt: skip
        kind, body = self._client.order(in_mint, out_mint, amount, slippage_bps)
        if kind == "no_route":
            return Quote(out_amount=0, min_out_amount=0, status=QuoteStatus.NO_ROUTE, error=str(body), **base)
        if kind == "error":
            return Quote(out_amount=0, min_out_amount=0, status=QuoteStatus.ERROR, error=str(body), **base)
        assert isinstance(body, dict)
        out = _int(body.get("outAmount"))
        if out <= 0:
            return Quote(out_amount=0, min_out_amount=0, status=QuoteStatus.NO_ROUTE, error="outAmount vacío", **base)
        min_out = _int(body.get("otherAmountThreshold")) or out * (10_000 - slippage_bps) // 10_000
        impact_pct = body.get("priceImpact")
        try:
            impact_bps = abs(Decimal(str(impact_pct))) * 100 if impact_pct is not None else Decimal(0)
        except Exception:
            impact_bps = Decimal(0)
        labels = [
            (leg.get("swapInfo") or {}).get("label") for leg in (body.get("routePlan") or []) if isinstance(leg, dict)
        ]
        route = " → ".join(label for label in labels if label) or str(body.get("router") or "jupiter")
        fee_bps = _int(body.get("feeBps"))
        # El monto de salida ya descuenta comisiones de protocolo/ruta; prioridad, firma y depósitos no.
        costs = [QuoteCost("protocol", (out if side == "sell" else amount) * fee_bps // 10_000, included_in_amount=True)]
        costs.append(QuoteCost("priority", _int(body.get("prioritizationFeeLamports")) or self._priority))
        if _int(body.get("signatureFeeLamports")):
            costs.append(QuoteCost("signature", _int(body["signatureFeeLamports"])))
        if side == "buy":
            costs.append(QuoteCost("ata_deposit", _int(body.get("rentFeeLamports")) or self._ata, refundable=True))
        return Quote(
            out_amount=out,
            min_out_amount=min_out,
            status=QuoteStatus.OK,
            route=route,
            price_impact_bps=impact_bps,
            costs=tuple(costs),
            request_id=body.get("requestId"),
            **{**base, "in_amount": _int(body.get("inAmount")) or amount},
        )

    def quote_buy(self, pool: PoolInfo, lamports_in: int, slippage_bps: int, now: datetime) -> Quote:
        return self._quote("buy", pool, lamports_in, slippage_bps, now)

    def quote_sell(self, pool: PoolInfo, tokens_in: int, slippage_bps: int, now: datetime) -> Quote:
        return self._quote("sell", pool, tokens_in, slippage_bps, now)


class JupiterFxProvider:
    """SOL/USD de referencia fechada: se obtiene cotizando 1 SOL → USDC (sin otra dependencia)."""

    def __init__(self, client: JupiterClient, *, max_age_s: int = 60) -> None:
        self._client = client
        self._max_age = max_age_s
        self._cached: FxRate | None = None

    def sol_usd(self, now: datetime) -> FxRate | None:
        if self._cached and (now - self._cached.observed_at).total_seconds() < self._max_age:
            return self._cached
        kind, body = self._client.order(WSOL_MINT, USDC_MINT, 1_000_000_000, 50)
        if kind != "ok" or not isinstance(body, dict) or _int(body.get("outAmount")) <= 0:
            return self._cached  # la última referencia conocida conserva su fecha
        self._cached = FxRate(Decimal(_int(body["outAmount"])) / Decimal(1_000_000), now, "jupiter")
        return self._cached
