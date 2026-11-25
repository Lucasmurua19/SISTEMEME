"""Envoltorios que registran éxitos y fallas de las fuentes para marcar su salud."""

from __future__ import annotations

from datetime import datetime

from app.application.ports import MarketDataSource, QuoteProvider
from app.domain.models import MarketSnapshot, PoolInfo, Quote, QuoteStatus


class SourceTally:
    def __init__(self) -> None:
        self.ok = 0
        self.failed = 0
        self.last_error: str | None = None

    def success(self) -> None:
        self.ok += 1

    def failure(self, error: str) -> None:
        self.failed += 1
        self.last_error = error

    def reset(self) -> None:
        self.ok = self.failed = 0
        self.last_error = None


class TrackedMarketSource:
    def __init__(self, inner: MarketDataSource) -> None:
        self._inner = inner
        self.name = inner.name
        self.tally = SourceTally()

    def get_snapshots(self, pool: PoolInfo, now: datetime) -> list[MarketSnapshot]:
        try:
            out = self._inner.get_snapshots(pool, now)
        except Exception as exc:
            self.tally.failure(f"{type(exc).__name__}: {exc}")
            raise
        self.tally.success()
        return out


class TrackedQuoteProvider:
    """Cuenta como falla de la fuente los errores del proveedor; que no haya ruta no es una falla."""

    def __init__(self, inner: QuoteProvider) -> None:
        self._inner = inner
        self.name = inner.name
        self.tally = SourceTally()

    def _track(self, q: Quote) -> Quote:
        if q.status is QuoteStatus.ERROR:
            self.tally.failure(q.error or "error")
        else:
            self.tally.success()
        return q

    def quote_buy(self, pool: PoolInfo, lamports_in: int, slippage_bps: int, now: datetime) -> Quote:
        return self._track(self._inner.quote_buy(pool, lamports_in, slippage_bps, now))

    def quote_sell(self, pool: PoolInfo, tokens_in: int, slippage_bps: int, now: datetime) -> Quote:
        return self._track(self._inner.quote_sell(pool, tokens_in, slippage_bps, now))
