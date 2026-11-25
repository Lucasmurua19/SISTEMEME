"""Fuentes reproducibles a partir de escenarios grabados (``DATA_MODE=fixtures``).

Los escenarios son JSON bajo ``fixtures/scenarios``. Cada uno define un token, su migración, las
ventanas de mercado y reglas de cotización (comisión, ausencia de ruta, saltos de precio). Las
cotizaciones se derivan de las reservas del último snapshot visible con producto constante, de modo
que costo e impacto son coherentes con la liquidez del escenario.
"""

from __future__ import annotations

import json
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any

from app.application.ports import Clock, FxRate
from app.domain.models import (
    PUMP_PROGRAM_ID,
    PUMPSWAP_PROGRAM_ID,
    WSOL_MINT,
    MarketSnapshot,
    MigrationBatch,
    MigrationEvent,
    PoolInfo,
    PoolVerification,
    Quote,
    QuoteCost,
    QuoteStatus,
)

ATA_DEPOSIT_LAMPORTS = 2_039_280
PRIORITY_FEE_LAMPORTS = 100_000
QUOTE_TTL_S = 20
OBSERVATION_LAG_S = 1


@dataclass(frozen=True)
class Scenario:
    id: str
    description: str
    symbol: str
    name: str
    mint: str
    decimals: int
    pool: str
    signature: str
    instruction_index: int
    slot: int
    offset_s: int
    window_seconds: int
    snapshots: tuple[dict[str, Any], ...]
    quotes: dict[str, Any] = field(default_factory=dict)
    verification: dict[str, Any] = field(default_factory=dict)

    @property
    def end_s(self) -> int:
        return self.offset_s + (len(self.snapshots) + 1) * self.window_seconds


class FixtureLibrary:
    def __init__(self, scenarios: Sequence[Scenario], t0: datetime) -> None:
        self.scenarios = sorted(scenarios, key=lambda s: (s.offset_s, s.id))
        self.t0 = t0
        self._by_pool = {s.pool: s for s in self.scenarios}

    @classmethod
    def horizon_s(cls, scenarios: Sequence[Scenario]) -> int:
        return max((s.end_s for s in scenarios), default=0)

    @classmethod
    def load_scenarios(cls, directory: str | Path) -> list[Scenario]:
        out: list[Scenario] = []
        for path in sorted(Path(directory).glob("*.json")):
            d = json.loads(path.read_text(encoding="utf-8"))
            out.append(
                Scenario(
                    id=d["id"],
                    description=d.get("description", ""),
                    symbol=d["token"]["symbol"],
                    name=d["token"]["name"],
                    mint=d["token"]["mint"],
                    decimals=d["token"].get("decimals", 6),
                    pool=d["pool"]["address"],
                    signature=d["migration"]["signature"],
                    instruction_index=d["migration"].get("instruction_index", 0),
                    slot=d["migration"]["slot"],
                    offset_s=d["migration"]["offset_s"],
                    window_seconds=d.get("window_seconds", 15),
                    snapshots=tuple(d["snapshots"]),
                    quotes=d.get("quotes", {}),
                    verification=d.get("verification", {}),
                )
            )
        return out

    def scenario_for_pool(self, pool: str) -> Scenario | None:
        return self._by_pool.get(pool)

    def migration_at(self, sc: Scenario) -> datetime:
        return self.t0 + timedelta(seconds=sc.offset_s)


class FixtureMigrationSource:
    name = "fixtures"

    def __init__(self, library: FixtureLibrary, clock: Clock) -> None:
        self._lib = library
        self._clock = clock

    def _events(self) -> list[MigrationEvent]:
        events = []
        for sc in self._lib.scenarios:
            at = self._lib.migration_at(sc)
            events.append(
                MigrationEvent(
                    signature=sc.signature,
                    instruction_index=sc.instruction_index,
                    event_index=0,
                    slot=sc.slot,
                    mint=sc.mint,
                    pool=sc.pool,
                    program=PUMP_PROGRAM_ID,
                    event_at=at,
                    observed_at=at + timedelta(seconds=OBSERVATION_LAG_S),
                    confirmation="confirmed",
                    symbol=sc.symbol,
                    name=sc.name,
                    decimals=sc.decimals,
                    evidence={"fixture": sc.id, "quote_mint": WSOL_MINT, "amm_program": PUMPSWAP_PROGRAM_ID},
                )
            )
        return sorted(events, key=lambda e: (e.observed_at, e.signature))

    def fetch_migrations(self, cursor: str | None, limit: int) -> MigrationBatch:
        now = self._clock.now()
        due = [e for e in self._events() if e.observed_at <= now]
        start = int(cursor) if cursor else 0
        batch = due[start : start + limit]
        return MigrationBatch(tuple(batch), str(start + len(batch)), head_slot=batch[-1].slot if batch else None)

    def fetch_signature(self, signature: str) -> Sequence[MigrationEvent]:
        return [e for e in self._events() if e.signature == signature and e.observed_at <= self._clock.now()]

    def verify(self, event: MigrationEvent) -> PoolVerification:
        sc = self._lib.scenario_for_pool(event.pool)
        v = (sc.verification if sc else {}) or {}
        verified = v.get("verified", True)
        identity = v.get("identity_ok", True)
        return PoolVerification(
            verified=verified,
            identity_ok=identity,
            canonical=v.get("canonical", True),
            reason=None if verified and identity else v.get("reason", "fixture"),
            evidence={"source": "fixture", "program": PUMP_PROGRAM_ID, "pool_authority_match": identity},
        )


class FixtureMarketSource:
    name = "fixtures"

    def __init__(self, library: FixtureLibrary) -> None:
        self._lib = library

    def get_snapshots(self, pool: PoolInfo, now: datetime) -> list[MarketSnapshot]:
        sc = self._lib.scenario_for_pool(pool.address)
        if sc is None:
            return []
        migrated = self._lib.migration_at(sc)
        out: list[MarketSnapshot] = []
        for i, raw in enumerate(sc.snapshots):
            start = migrated + timedelta(seconds=i * sc.window_seconds)
            end = start + timedelta(seconds=sc.window_seconds)
            observed = end + timedelta(seconds=raw.get("observed_lag_s", OBSERVATION_LAG_S))
            if observed > now:
                break
            conc = raw.get("top10_concentration_pct")
            out.append(
                MarketSnapshot(
                    pool=pool.address,
                    window_index=i,
                    window_start=start,
                    window_end=end,
                    observed_at=observed,
                    open=Decimal(raw["open"]),
                    high=Decimal(raw["high"]),
                    low=Decimal(raw["low"]),
                    close=Decimal(raw["close"]),
                    sol_reserve_lamports=int(raw["sol_reserve_lamports"]),
                    token_reserve_base=int(raw["token_reserve_base"]),
                    swap_count=raw["swap_count"],
                    buy_count=raw["buy_count"],
                    sell_count=raw["sell_count"],
                    net_buyers=raw["net_buyers"],
                    buy_volume_lamports=int(raw["buy_volume_lamports"]),
                    sell_volume_lamports=int(raw["sell_volume_lamports"]),
                    top10_concentration_pct=Decimal(conc) if conc is not None else None,
                    concentration_observed_at=end if conc is not None else None,
                    coverage=Decimal(raw.get("coverage", "1")),
                    source="fixtures",
                )
            )
        return out

    def latest_before(self, pool: str, now: datetime) -> tuple[Scenario, MarketSnapshot] | None:
        sc = self._lib.scenario_for_pool(pool)
        if sc is None:
            return None
        info = PoolInfo(pool, sc.mint, sc.symbol, sc.name, sc.decimals, PUMPSWAP_PROGRAM_ID, WSOL_MINT,
                        self._lib.migration_at(sc), sc.signature, sc.instruction_index, sc.slot, True, True, True)  # fmt: skip
        snaps = self.get_snapshots(info, now)
        return (sc, snaps[-1]) if snaps else None


class FixtureQuoteProvider:
    """Cotizaciones por producto constante a partir del último snapshot visible del escenario."""

    name = "fixtures"

    def __init__(self, market: FixtureMarketSource, library: FixtureLibrary) -> None:
        self._market = market
        self._lib = library

    # -- reglas del escenario
    @staticmethod
    def _in_ranges(ranges: Sequence[Sequence[int]], seconds: float) -> bool:
        return any(lo <= seconds < hi for lo, hi in ranges)

    def _multiplier(self, rules: dict[str, Any], seconds: float) -> Decimal:
        for rule in rules.get("price_multiplier", []):
            if rule["from_s"] <= seconds < rule["to_s"]:
                return Decimal(str(rule["multiplier"]))
        return Decimal(1)

    def _unavailable(self, side: str, pool: PoolInfo, now: datetime, error: str, status: QuoteStatus) -> Quote:
        return Quote(
            side=side,
            input_mint=WSOL_MINT if side == "buy" else pool.mint,
            output_mint=pool.mint if side == "buy" else WSOL_MINT,
            in_amount=0,
            out_amount=0,
            min_out_amount=0,
            observed_at=now,
            expires_at=now + timedelta(seconds=QUOTE_TTL_S),
            status=status,
            error=error,
        )

    def _context(self, pool: PoolInfo, now: datetime):
        found = self._market.latest_before(pool.address, now)
        if found is None:
            return None
        sc, snap = found
        seconds = (now - self._lib.migration_at(sc)).total_seconds()
        return sc, snap, seconds

    def quote_buy(self, pool: PoolInfo, lamports_in: int, slippage_bps: int, now: datetime) -> Quote:
        ctx = self._context(pool, now)
        if ctx is None:
            return self._unavailable("buy", pool, now, "sin datos de mercado", QuoteStatus.NO_ROUTE)
        sc, snap, seconds = ctx
        rules = sc.quotes
        if self._in_ranges(rules.get("error_s", []), seconds):
            return self._unavailable("buy", pool, now, "HTTP 503 simulado", QuoteStatus.ERROR)
        if self._in_ranges(rules.get("buy_unavailable_s", []), seconds):
            return self._unavailable("buy", pool, now, "sin ruta de compra", QuoteStatus.NO_ROUTE)
        fee_bps = int(rules.get("fee_bps", 30))
        fee = lamports_in * fee_bps // 10_000
        eff = lamports_in - fee
        s, t = snap.sol_reserve_lamports, snap.token_reserve_base
        out = t * eff // (s + eff)
        mult = self._multiplier(rules, seconds)
        if mult != 1:
            out = int(Decimal(out) / mult)  # precio más caro → menos tokens
        impact = Decimal(eff) * 10_000 / Decimal(s + eff)
        return Quote(
            side="buy",
            input_mint=WSOL_MINT,
            output_mint=pool.mint,
            in_amount=lamports_in,
            out_amount=out,
            min_out_amount=out * (10_000 - slippage_bps) // 10_000,
            observed_at=now,
            expires_at=now + timedelta(seconds=QUOTE_TTL_S),
            status=QuoteStatus.OK if out > 0 else QuoteStatus.NO_ROUTE,
            route="PumpSwap (fixture)",
            price_impact_bps=impact,
            costs=(
                QuoteCost("protocol", fee, included_in_amount=True),
                QuoteCost("priority", PRIORITY_FEE_LAMPORTS),
                QuoteCost("ata_deposit", ATA_DEPOSIT_LAMPORTS, refundable=True),
            ),
            request_id=f"fx-{sc.id}-{int(now.timestamp())}-b",
        )

    def quote_sell(self, pool: PoolInfo, tokens_in: int, slippage_bps: int, now: datetime) -> Quote:
        ctx = self._context(pool, now)
        if ctx is None:
            return self._unavailable("sell", pool, now, "sin datos de mercado", QuoteStatus.NO_ROUTE)
        sc, snap, seconds = ctx
        rules = sc.quotes
        if self._in_ranges(rules.get("error_s", []), seconds):
            return self._unavailable("sell", pool, now, "HTTP 503 simulado", QuoteStatus.ERROR)
        if self._in_ranges(rules.get("sell_unavailable_s", []), seconds):
            return self._unavailable("sell", pool, now, "sin ruta de venta", QuoteStatus.NO_ROUTE)
        fee_bps = int(rules.get("fee_bps", 30))
        s, t = snap.sol_reserve_lamports, snap.token_reserve_base
        gross = s * tokens_in // (t + tokens_in)
        fee = gross * fee_bps // 10_000
        out = gross - fee
        mult = self._multiplier(rules, seconds)
        if mult != 1:
            out = int(Decimal(out) * mult)
        impact = Decimal(tokens_in) * 10_000 / Decimal(t + tokens_in)
        return Quote(
            side="sell",
            input_mint=pool.mint,
            output_mint=WSOL_MINT,
            in_amount=tokens_in,
            out_amount=out,
            min_out_amount=out * (10_000 - slippage_bps) // 10_000,
            observed_at=now,
            expires_at=now + timedelta(seconds=QUOTE_TTL_S),
            status=QuoteStatus.OK if out > 0 else QuoteStatus.NO_ROUTE,
            route="PumpSwap (fixture)",
            price_impact_bps=impact,
            costs=(
                QuoteCost("protocol", fee, included_in_amount=True),
                QuoteCost("priority", PRIORITY_FEE_LAMPORTS),
            ),
            request_id=f"fx-{sc.id}-{int(now.timestamp())}-s",
        )


class FixtureFxProvider:
    """SOL/USD de referencia fechada y determinista (varía por minuto de reproducción)."""

    def sol_usd(self, now: datetime) -> FxRate | None:
        drift = Decimal(int(now.timestamp() // 60) % 20) / Decimal(10)
        return FxRate(Decimal(150) + drift, now, "fixtures")
