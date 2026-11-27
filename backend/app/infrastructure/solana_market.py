"""Snapshots de mercado de un pool de PumpSwap a partir de transacciones reales.

En lugar de decodificar eventos del programa (cuyo layout cambia entre versiones), se leen los saldos
de las bóvedas del pool antes y después de cada transacción (``pre/postTokenBalances``). De ahí salen
las reservas, el precio, la dirección del swap y las billeteras con flujo neto comprador.
"""

from __future__ import annotations

import logging
import time
from bisect import bisect_left
from collections import defaultdict, deque
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from itertools import islice
from typing import Any

from app.application.ports import Clock, PermanentSourceError
from app.domain.models import HUNDRED, LAMPORTS_PER_SOL, MarketSnapshot, PoolInfo
from app.infrastructure.solana_rpc import SolanaRpcClient, parse_pool_account, reserves_after_swap

log = logging.getLogger(__name__)

CONC_BACKOFF_MAX_S = 600
BULK_STATS_EVERY_S = 300
BURN_ADDRESSES = frozenset({"1nc1nerator11111111111111111111111111111111", "11111111111111111111111111111111"})


@dataclass(frozen=True)
class SwapRecord:
    at: datetime
    slot: int
    signature: str
    kind: str  # init | buy | sell | liquidity
    price: Decimal | None  # sobre la reserva efectiva de SOL (bóveda + virtual)
    base_reserve: int
    quote_reserve: int  # SOL real en la bóveda: base de los filtros de profundidad y retención
    quote_volume: int
    wallet_deltas: dict[str, int] = field(default_factory=dict)
    virtual_quote_reserves: int = 0  # vigente tras esta transacción; la usa la siguiente que no informe la suya


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
    tx: dict[str, Any],
    *,
    pool_address: str,
    mint: str,
    base_vault: str,
    quote_vault: str,
    decimals: int,
    virtual_quote_reserves: int = 0,
) -> SwapRecord | None:
    """Clasifica una transacción que toca las bóvedas del pool. ``None`` si no mueve las reservas.

    El precio usa la reserva efectiva de SOL. Un swap la informa en su evento; el resto de las transacciones
    (creación, aportes de liquidez) usan ``virtual_quote_reserves``, la última conocida del pool.
    """
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

    after = reserves_after_swap(tx, pool_address)
    if after is None:
        base, effective_quote = post_b, post_q + virtual_quote_reserves
    else:
        base, effective_quote = after.base, after.quote
        if after.base == post_b:  # nada movió el pool después del swap: la diferencia con la bóveda es la virtual
            virtual_quote_reserves = after.quote - post_q
    price = (
        (Decimal(effective_quote) / LAMPORTS_PER_SOL) / (Decimal(base) / Decimal(10**decimals))
        if base > 0 and effective_quote > 0
        else None
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
        virtual_quote_reserves=virtual_quote_reserves,
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
    virtual: int = 0  # reserva virtual de SOL: la de la cuenta al empezar y luego la que informe cada swap
    swaps: list[SwapRecord] = field(default_factory=list)
    unparsed_at: list[datetime] = field(default_factory=list)
    head_sig: str | None = None  # firma más nueva ya listada
    backlog: deque[dict[str, Any]] = field(default_factory=deque)  # listadas y sin procesar, de la más antigua a la más nueva
    complete_until: datetime | None = None
    truncated: bool = False  # el historial no se pudo leer entero: no se arman ventanas
    cursor: str | None = None  # lectura en bloque: última transacción leída
    read_count: int = 0  # lectura en bloque: transacciones leídas de este pool
    caught_up: bool = False  # lectura en bloque: la última consulta llegó hasta el presente
    late_warned: bool = False
    used_at: datetime | None = None
    conc: Decimal | None = None
    conc_at: datetime | None = None
    next_conc_at: datetime | None = None
    conc_failures: int = 0


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
        poll_budget_s: float | None = None,
        concurrency: int = 1,
        max_signature_pages: int = 30,  # 30.000 firmas: más de 15 minutos de un pool muy activo
        monotonic: Callable[[], float] = time.monotonic,
        bulk: bool = False,
        bulk_page: int = 500,  # unos 10 MB por respuesta
        bulk_max_per_poll: int = 2000,
        bulk_max_per_pool: int = 60_000,  # tope de gasto: 6.000 créditos de Helius por pool
        bulk_settle_s: int = 4,  # el índice en bloque puede ir alrededor de 1 s detrás del listado de firmas
        idle_evict_s: int = 1200,
    ) -> None:
        self._rpc = rpc
        self._clock = clock
        self._w = window_seconds
        self._lag = observation_lag_s
        self._settle = settle_s
        self._max_tx = max_tx_per_poll
        self._conc_every = concentration_refresh_s
        # Tope de tiempo real por sondeo de un pool: con un RPC lento, 80 transacciones pueden tardar minutos y
        # bloquear el ciclo. Lo que no alcanza queda para el próximo ciclo; las ventanas se arman hasta lo procesado.
        self._poll_budget_s = poll_budget_s
        self._mono = monotonic
        # Lectura concurrente de transacciones: con un proveedor de cupo alto multiplica el rendimiento de un pool
        # caliente, cuyo tráfico supera lo que una sola conexión secuencial (~5 tx/s) puede leer.
        self._max_pages = max(1, max_signature_pages)
        self._concurrency = max(1, concurrency)
        self._executor = ThreadPoolExecutor(max_workers=self._concurrency, thread_name_prefix="rpc-tx") if self._concurrency > 1 else None
        # Lectura en bloque (cientos de transacciones por solicitud). Si el proveedor no la admite se vuelve a la
        # lectura de a una, que funciona con cualquier RPC pero no alcanza a seguir pools calientes con poco cupo.
        self._bulk = bulk
        self._bulk_confirmed = False
        self._bulk_page = bulk_page
        self._bulk_max_poll = bulk_max_per_poll
        self._bulk_max_pool = bulk_max_per_pool
        self._bulk_settle = bulk_settle_s
        self._bulk_calls = self._bulk_txs = self._bulk_credits = 0
        self._bulk_logged_at: datetime | None = None
        self._idle_evict = idle_evict_s
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
            st = _PoolState(
                base_vault=fields["base_vault"],
                quote_vault=fields["quote_vault"],
                virtual=fields["virtual_quote_reserves"],
            )
            self._pools[pool.address] = st
        return st

    def _fetch_many(self, signatures: list[str]) -> list[dict[str, Any] | None]:
        """Transacciones de ``signatures`` en el mismo orden; en paralelo si ``concurrency`` > 1."""
        if len(signatures) <= 1 or self._executor is None:
            return [self._rpc.get_transaction(s) for s in signatures]
        return list(self._executor.map(self._rpc.get_transaction, signatures))

    def _poll(self, pool: PoolInfo, st: _PoolState) -> None:
        started = self._clock.now()
        # Solo se listan las firmas posteriores a la más nueva ya vista. Antes se volvía a pedir todo lo pendiente en
        # cada ciclo: en un pool caliente son páginas de 1000 firmas por ciclo y el listado dominaba el tiempo.
        new: list[dict[str, Any]] = []
        before: str | None = None
        for _ in range(self._max_pages):
            page = self._rpc.get_signatures_for_address(st.quote_vault, before=before, until=st.head_sig, limit=1000)
            new.extend(page)
            if len(page) < 1000:
                break
            before = page[-1]["signature"]
        else:
            # Quedan firmas más antiguas sin listar: habría un hueco que las ventanas rellenarían con precio plano y
            # cobertura completa, es decir, datos inventados. El pool deja de producir ventanas.
            st.truncated = True
            st.backlog.clear()
            log.warning(
                "historial del pool %s supera %d firmas sin leer: no se puede reconstruir y no se arman más ventanas",
                pool.address[:8], self._max_pages * 1000,
            )  # fmt: skip
            return
        if new:
            st.head_sig = new[0]["signature"]  # el RPC devuelve de la más nueva a la más vieja
            st.backlog.extend(reversed(new))  # el pendiente se procesa de la más antigua a la más reciente

        processed = 0
        deadline = None if self._poll_budget_s is None else self._mono() + self._poll_budget_s
        stalled = False
        while st.backlog and not stalled:
            if processed >= self._max_tx or (deadline is not None and self._mono() > deadline):
                break
            # Se leen varias transacciones a la vez (``concurrency``) pero se aplican en orden estricto: el estado del
            # pool solo avanza hasta la primera transacción que todavía no está disponible.
            batch = list(islice(st.backlog, max(1, min(self._concurrency, self._max_tx - processed))))
            wanted = [e["signature"] for e in batch if e.get("err") is None]
            fetched = dict(zip(wanted, self._fetch_many(wanted), strict=True))
            for entry in batch:
                if entry.get("err") is None:
                    tx = fetched[entry["signature"]]
                    if tx is None:
                        stalled = True  # queda al frente del pendiente y se reintenta en el próximo ciclo
                        break
                    self._apply(pool, st, tx)
                    processed += 1
                st.backlog.popleft()
        if not st.backlog:
            st.complete_until = started - timedelta(seconds=self._settle)
        elif st.swaps:
            st.complete_until = st.swaps[-1].at

    def _apply(self, pool: PoolInfo, st: _PoolState, tx: dict[str, Any]) -> None:
        """Incorpora una transacción al estado del pool; las que no se pueden interpretar bajan la cobertura."""
        try:
            rec = extract_swap(
                tx, pool_address=pool.address, mint=pool.mint, base_vault=st.base_vault,
                quote_vault=st.quote_vault, decimals=pool.decimals, virtual_quote_reserves=st.virtual,
            )  # fmt: skip
        except (KeyError, ValueError, TypeError):
            st.unparsed_at.append(datetime.fromtimestamp(tx.get("blockTime") or 0, UTC))
            return
        if rec is not None:
            st.swaps.append(rec)
            st.virtual = rec.virtual_quote_reserves

    # ------------------------------------------------------------------ lectura en bloque
    def _window_end(self, pool: PoolInfo, t: datetime) -> int:
        """Cantidad de ventanas del pool ya cerradas en el instante ``t``."""
        return max(0, int((t - pool.migrated_at).total_seconds() // self._w))

    def _poll_bulk(self, pool: PoolInfo, st: _PoolState) -> None:
        """Lee lo posterior al cursor, de la más antigua a la más nueva.

        El historial se da por completo hasta ``inicio - margen`` solo si una página llegó vacía o si ya se leyó una
        transacción de ese instante o posterior. Una página incompleta no alcanza como prueba de que no hay más.
        """
        started = self._clock.now()
        horizon = started - timedelta(seconds=self._bulk_settle)
        closed_until: datetime | None = None
        if st.complete_until is not None:
            closed = self._window_end(pool, st.complete_until)
            if st.caught_up and horizon < pool.migrated_at + timedelta(seconds=(closed + 1) * self._w):
                return  # todavía no puede cerrar otra ventana: consultar ahora solo gastaría cupo
            if closed:
                closed_until = pool.migrated_at + timedelta(seconds=closed * self._w)

        st.caught_up = False
        deadline = None if self._poll_budget_s is None else self._mono() + self._poll_budget_s
        read = 0
        last_at: datetime | None = None
        while not st.caught_up:
            if st.read_count >= self._bulk_max_pool:
                st.truncated = True
                st.swaps.clear()
                st.unparsed_at.clear()
                log.warning(
                    "historial del pool %s supera %d transacciones: se deja de leer y no se arman más ventanas",
                    pool.address[:8], self._bulk_max_pool,
                )  # fmt: skip
                return
            page = self._rpc.get_transactions_for_address(st.quote_vault, after=st.cursor, limit=self._bulk_page)
            self._count_bulk(len(page))
            if not page:
                st.caught_up = True
                break
            signature = ((page[-1].get("transaction") or {}).get("signatures") or [""])[0]
            if not signature or signature == st.cursor:
                raise PermanentSourceError("getTransactionsForAddress: la respuesta no permite avanzar el cursor")
            for tx in page:
                at = datetime.fromtimestamp(tx["blockTime"], UTC) if tx.get("blockTime") else None
                if at is not None:
                    last_at = at
                    if closed_until is not None and at < closed_until and not st.late_warned:
                        st.late_warned = True
                        log.warning(
                            "pool %s: llegó tarde una transacción de una ventana ya cerrada (índice del proveedor atrasado)",
                            pool.address[:8],
                        )  # fmt: skip
                self._apply(pool, st, tx)
            st.cursor = signature
            st.read_count += len(page)
            read += len(page)
            if last_at is not None and last_at >= horizon:
                st.caught_up = True
            elif read >= self._bulk_max_poll or (deadline is not None and self._mono() > deadline):
                break  # sigue en el próximo ciclo desde el cursor
        reached = horizon if st.caught_up else last_at
        if reached is not None and (st.complete_until is None or reached > st.complete_until):
            st.complete_until = reached

    def _count_bulk(self, returned: int) -> None:
        if not self._bulk_confirmed:
            self._bulk_confirmed = True
            log.info("lectura en bloque activa (getTransactionsForAddress)")
        self._bulk_calls += 1
        self._bulk_txs += returned
        # tarifa documentada por Helius: 10 créditos cada 100 transacciones devueltas, redondeado hacia arriba
        self._bulk_credits += 10 * max(1, -(-returned // 100))
        now = self._clock.now()
        if self._bulk_logged_at is None:
            self._bulk_logged_at = now
        elif (now - self._bulk_logged_at).total_seconds() >= BULK_STATS_EVERY_S:
            self._bulk_logged_at = now
            log.info(
                "lectura en bloque acumulada: %d llamadas, %d transacciones, ~%d créditos",
                self._bulk_calls, self._bulk_txs, self._bulk_credits,
            )  # fmt: skip

    def _sync(self, pool: PoolInfo, st: _PoolState) -> None:
        if not self._bulk:
            self._poll(pool, st)
            return
        try:
            self._poll_bulk(pool, st)
        except PermanentSourceError as exc:
            if self._bulk_confirmed:
                raise
            # nunca funcionó con este proveedor: no hay cursor que conservar y se usa la lectura estándar
            self._bulk = False
            log.warning("el proveedor no admite la lectura en bloque (%s): las transacciones se leen de a una", exc)
            self._poll(pool, st)

    def _refresh_concentration(self, pool: PoolInfo, st: _PoolState, now: datetime) -> None:
        if st.next_conc_at is not None and now < st.next_conc_at:
            return
        try:
            value = compute_concentration(self._rpc, pool.mint, pool.address, frozenset({st.base_vault, st.quote_vault}))
        except Exception as exc:  # el dato queda como desconocido en lugar de inventarse
            # Cada fallo duplica la espera hasta el próximo intento (tope CONC_BACKOFF_MAX_S): con un proveedor que
            # limita la tasa de esta llamada, insistir cada minuto solo agrega carga y retrasa el resto del ciclo.
            st.conc_failures += 1
            wait = min(CONC_BACKOFF_MAX_S, self._conc_every * 2 ** (st.conc_failures - 1))
            st.next_conc_at = now + timedelta(seconds=wait)
            if st.conc_failures == 1:  # una advertencia por pool; los reintentos siguientes no repiten el aviso
                log.warning("concentración no disponible para %s: %s (se reintenta con espera creciente)", pool.mint[:6], type(exc).__name__)
            return
        st.next_conc_at = now + timedelta(seconds=self._conc_every)
        st.conc_failures = 0
        if value is not None:
            st.conc, st.conc_at = value, now

    # ------------------------------------------------------------------ ventanas
    def get_snapshots(self, pool: PoolInfo, now: datetime) -> list[MarketSnapshot]:
        st = self._state(pool)
        st.used_at = now
        # los pools que dejaron de consultarse liberan su historial en memoria
        idle = [a for a, s in self._pools.items() if s.used_at and (now - s.used_at).total_seconds() > self._idle_evict]
        for address in idle:
            del self._pools[address]
        if not st.truncated:
            self._sync(pool, st)
        if st.truncated:
            return []  # lo ya guardado queda como está y envejece: el candidato pasa a «datos pendientes»
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
    times = [s.at for s in ordered]
    failed_at = sorted(unparsed_at)
    k = 0
    while True:
        ws = pool.migrated_at + timedelta(seconds=k * window_s)
        we = ws + timedelta(seconds=window_s)
        if we > horizon:
            break
        # búsqueda binaria: recorrer todos los swaps en cada ventana no escala con miles de transacciones por pool
        inside = ordered[bisect_left(times, ws) : bisect_left(times, we)]
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
        failed = bisect_left(failed_at, we) - bisect_left(failed_at, ws)
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
