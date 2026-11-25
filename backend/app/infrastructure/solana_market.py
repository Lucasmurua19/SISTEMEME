"""Snapshots de mercado de un pool de PumpSwap a partir de transacciones reales.

En lugar de decodificar eventos del programa (cuyo layout cambia entre versiones), se leen los saldos
de las bóvedas del pool antes y después de cada transacción (``pre/postTokenBalances``). De ahí salen
las reservas, el precio, la dirección del swap y las billeteras con flujo neto comprador.
"""

from __future__ import annotations

import logging
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any

from app.application.ports import Clock
from app.domain.models import HUNDRED, LAMPORTS_PER_SOL, MarketSnapshot, PoolInfo
from app.infrastructure.solana_rpc import SolanaRpcClient, parse_pool_account

log = logging.getLogger(__name__)

BURN_ADDRESSES = frozenset({"1nc1nerator11111111111111111111111111111111", "11111111111111111111111111111111"})


@dataclass(frozen=True)
class SwapRecord:
    at: datetime
    slot: int
    signature: str
    kind: str  # init | buy | sell | liquidity
    price: Decimal | None
    base_reserve: int
    quote_reserve: int
    quote_volume: int
    wallet_deltas: dict[str, int] = field(default_factory=dict)


def _keys(tx: dict[str, Any]) -> list[str]:
    raw = ((tx.get("transaction") or {}).get("message") or {}).get("accountKeys") or []
    return [k["pubkey"] if isinstance(k, dict) else k for k in raw]


def _amount_at(entries: list[dict[str, Any]], keys: list[str], address: str) -> int | None:
    for e in entries:
        idx = e.get("accountIndex")
        if idx is not None and idx < len(keys) and keys[idx] == address:
            return int(e["uiTokenAmount"]["amount"])
    return None


def extract_swap(
    tx: dict[str, Any], *, pool_address: str, mint: str, base_vault: str, quote_vault: str, decimals: int
) -> SwapRecord | None:
    """Clasifica una transacción que toca las bóvedas del pool. ``None`` si no mueve las reservas."""
    meta = tx.get("meta") or {}
    if meta.get("err") is not None:
        return None
    keys = _keys(tx)
    pre = meta.get("preTokenBalances") or []
    post = meta.get("postTokenBalances") or []
    post_b = _amount_at(post, keys, base_vault)
    post_q = _amount_at(post, keys, quote_vault)
    if post_b is None or post_q is None:
        return None
    pre_b = _amount_at(pre, keys, base_vault) or 0
    pre_q = _amount_at(pre, keys, quote_vault) or 0
    db, dq = post_b - pre_b, post_q - pre_q

    if pre_b == 0 and pre_q == 0:
        kind = "init"
    elif db < 0 < dq:
        kind = "buy"  # salen tokens del pool, entra SOL
    elif dq < 0 < db:
        kind = "sell"
    elif db * dq > 0:
        kind = "liquidity"  # ambas reservas se mueven en el mismo sentido
    else:
        return None

    price = (
        (Decimal(post_q) / LAMPORTS_PER_SOL) / (Decimal(post_b) / Decimal(10**decimals)) if post_b > 0 else None
    )
    pre_w: dict[str, int] = defaultdict(int)
    post_w: dict[str, int] = defaultdict(int)
    for entries, bucket in ((pre, pre_w), (post, post_w)):
        for e in entries:
            owner = e.get("owner")
            if e.get("mint") == mint and owner and owner != pool_address:
                bucket[owner] += int(e["uiTokenAmount"]["amount"])
    deltas = {o: post_w.get(o, 0) - pre_w.get(o, 0) for o in set(pre_w) | set(post_w)}
    block_time = tx.get("blockTime")
    return SwapRecord(
        at=datetime.fromtimestamp(block_time, UTC) if block_time else datetime.now(UTC),
        slot=int(tx.get("slot", 0)),
        signature=(tx.get("transaction") or {}).get("signatures", [""])[0],
        kind=kind,
        price=price,
        base_reserve=post_b,
        quote_reserve=post_q,
        quote_volume=abs(dq),
        wallet_deltas={o: d for o, d in deltas.items() if d != 0},
    )


def compute_concentration(
    rpc: SolanaRpcClient, mint: str, pool_address: str, extra_excluded: frozenset[str] = frozenset()
) -> Decimal | None:
    """Concentración de los 10 mayores propietarios sobre el suministro analizado.

    Suma balances por propietario (no por cuenta) y separa el pool y las cuentas de quema antes de
    calcular. Devuelve ``None`` si el dato es incompleto (queda como pendiente, no como cero).
    """
    supply = rpc.get_token_supply(mint)
    total = int(supply.get("amount", 0))
    largest = rpc.get_token_largest_accounts(mint)
    if total <= 0 or not largest:
        return None
    addresses = [a["address"] for a in largest]
    parsed = rpc.get_multiple_accounts_parsed(addresses)
    if len(parsed) != len(addresses):
        return None
    by_owner: dict[str, int] = defaultdict(int)
    for acc, info in zip(largest, parsed, strict=True):
        try:
            owner = info["data"]["parsed"]["info"]["owner"]  # type: ignore[index]
        except (TypeError, KeyError):
            return None  # no se pudo resolver el propietario de una cuenta grande: dato incompleto
        by_owner[owner] += int(acc["amount"])
    excluded = {pool_address, *BURN_ADDRESSES, *extra_excluded}
    excluded_amount = sum(v for o, v in by_owner.items() if o in excluded)
    analyzed = total - excluded_amount
    if analyzed <= 0:
        return None
    top10 = sorted((v for o, v in by_owner.items() if o not in excluded), reverse=True)[:10]
    return Decimal(sum(top10)) * HUNDRED / Decimal(analyzed)


@dataclass
class _PoolState:
    base_vault: str
    quote_vault: str
    swaps: list[SwapRecord] = field(default_factory=list)
    unparsed_at: list[datetime] = field(default_factory=list)
    last_sig: str | None = None
    complete_until: datetime | None = None
    conc: Decimal | None = None
    conc_at: datetime | None = None
    next_conc_at: datetime | None = None


class SolanaMarketSource:
    name = "solana_rpc"

    def __init__(
        self,
        rpc: SolanaRpcClient,
        *,
        clock: Clock,
        window_seconds: int = 15,
        observation_lag_s: int = 2,
        settle_s: int = 2,
        max_tx_per_poll: int = 80,
        concentration_refresh_s: int = 60,
    ) -> None:
        self._rpc = rpc
        self._clock = clock
        self._w = window_seconds
        self._lag = observation_lag_s
        self._settle = settle_s
        self._max_tx = max_tx_per_poll
        self._conc_every = concentration_refresh_s
        self._pools: dict[str, _PoolState] = {}

    # ------------------------------------------------------------------ estado por pool
    def _state(self, pool: PoolInfo) -> _PoolState:
        st = self._pools.get(pool.address)
        if st is None:
            info = self._rpc.get_account_info(pool.address)
            fields = None
            if info and info.get("data"):
                fields = parse_pool_account(info["data"][0])
            if not fields:
                raise RuntimeError("no se pudieron leer las bóvedas del pool")
            st = _PoolState(base_vault=fields["base_vault"], quote_vault=fields["quote_vault"])
            self._pools[pool.address] = st
        return st

    def _poll(self, pool: PoolInfo, st: _PoolState) -> None:
        started = self._clock.now()
        collected: list[dict[str, Any]] = []
        before: str | None = None
        for _ in range(10):
            page = self._rpc.get_signatures_for_address(st.quote_vault, before=before, until=st.last_sig, limit=1000)
            collected.extend(page)
            if len(page) < 1000:
                break
            before = page[-1]["signature"]

        drained = True
        processed = 0
        for entry in reversed(collected):  # de la más antigua a la más reciente
            if processed >= self._max_tx:
                drained = False
                break
            sig = entry["signature"]
            if entry.get("err") is None:
                tx = self._rpc.get_transaction(sig)
                if tx is None:
                    drained = False
                    break
                try:
                    rec = extract_swap(
                        tx, pool_address=pool.address, mint=pool.mint, base_vault=st.base_vault,
                        quote_vault=st.quote_vault, decimals=pool.decimals,
                    )  # fmt: skip
                except (KeyError, ValueError, TypeError):
                    st.unparsed_at.append(datetime.fromtimestamp(tx.get("blockTime") or 0, UTC))
                    rec = None
                if rec is not None:
                    st.swaps.append(rec)
                processed += 1
            st.last_sig = sig
        if drained:
            st.complete_until = started - timedelta(seconds=self._settle)
        elif st.swaps:
            st.complete_until = st.swaps[-1].at

    def _refresh_concentration(self, pool: PoolInfo, st: _PoolState, now: datetime) -> None:
        if st.next_conc_at is not None and now < st.next_conc_at:
            return
        st.next_conc_at = now + timedelta(seconds=self._conc_every)
        try:
            value = compute_concentration(self._rpc, pool.mint, pool.address, frozenset({st.base_vault, st.quote_vault}))
        except Exception as exc:  # el dato queda como desconocido en lugar de inventarse
            log.warning("concentración no disponible para %s: %s", pool.mint[:6], type(exc).__name__)
            return
        if value is not None:
            st.conc, st.conc_at = value, now

    # ------------------------------------------------------------------ ventanas
    def get_snapshots(self, pool: PoolInfo, now: datetime) -> list[MarketSnapshot]:
        st = self._state(pool)
        self._poll(pool, st)
        self._refresh_concentration(pool, st, now)
        return build_windows(
            pool, st.swaps, st.unparsed_at, window_s=self._w, horizon=min(st.complete_until or now, now - timedelta(seconds=self._lag)),
            lag_s=self._lag, conc=st.conc, conc_at=st.conc_at, source=self.name,
        )  # fmt: skip


def build_windows(
    pool: PoolInfo,
    swaps: list[SwapRecord],
    unparsed_at: list[datetime],
    *,
    window_s: int,
    horizon: datetime,
    lag_s: int,
    conc: Decimal | None,
    conc_at: datetime | None,
    source: str,
) -> list[MarketSnapshot]:
    """Ventanas cerradas desde la migración hasta ``horizon``; las vacías arrastran precio y reservas."""
    ordered = sorted(swaps, key=lambda s: (s.at, s.slot))
    first = next((s for s in ordered if s.price is not None), None)
    if first is None:
        return []
    out: list[MarketSnapshot] = []
    prev_close = first.price
    reserves = (first.base_reserve, first.quote_reserve)
    k = 0
    while True:
        ws = pool.migrated_at + timedelta(seconds=k * window_s)
        we = ws + timedelta(seconds=window_s)
        if we > horizon:
            break
        inside = [s for s in ordered if ws <= s.at < we]
        trades = [s for s in inside if s.kind in ("buy", "sell") and s.price is not None]
        reserved = [s for s in inside if s.price is not None]
        if reserved:
            reserves = (reserved[-1].base_reserve, reserved[-1].quote_reserve)
        open_ = prev_close if k > 0 else first.price
        prices = [open_] + [t.price for t in trades]
        close = trades[-1].price if trades else (reserved[-1].price if reserved else open_)
        prices.append(close)
        net: dict[str, int] = defaultdict(int)
        for t in trades:
            for owner, delta in t.wallet_deltas.items():
                net[owner] += delta
        buys = [t for t in trades if t.kind == "buy"]
        sells = [t for t in trades if t.kind == "sell"]
        failed = sum(1 for u in unparsed_at if ws <= u < we)
        coverage = Decimal(len(inside)) / Decimal(len(inside) + failed) if inside or failed else Decimal(1)
        out.append(
            MarketSnapshot(
                pool=pool.address,
                window_index=k,
                window_start=ws,
                window_end=we,
                observed_at=we + timedelta(seconds=lag_s),
                open=open_,
                high=max(prices),
                low=min(prices),
                close=close,
                sol_reserve_lamports=reserves[1],
                token_reserve_base=reserves[0],
                swap_count=len(trades),
                buy_count=len(buys),
                sell_count=len(sells),
                net_buyers=sum(1 for d in net.values() if d > 0),
                buy_volume_lamports=sum(t.quote_volume for t in buys),
                sell_volume_lamports=sum(t.quote_volume for t in sells),
                top10_concentration_pct=conc,
                concentration_observed_at=conc_at,
                coverage=coverage,
                source=source,
            )
        )
        prev_close = close
        k += 1
    return out
