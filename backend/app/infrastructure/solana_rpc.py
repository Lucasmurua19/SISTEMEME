"""Cliente JSON-RPC de Solana y fuente de migraciones de Pump.fun → PumpSwap.

Seguridad: la URL del RPC puede contener una clave. Nunca se escribe en logs ni en mensajes de error.
"""

from __future__ import annotations

import base64
import logging
import time
from collections.abc import Callable, Sequence
from datetime import UTC, datetime
from typing import Any

import base58
import httpx

from app.application.ports import Clock, PermanentSourceError, TransientSourceError
from app.domain.models import (
    PUMP_PROGRAM_ID,
    PUMPSWAP_PROGRAM_ID,
    WSOL_MINT,
    MigrationBatch,
    MigrationEvent,
    PoolVerification,
)
from app.infrastructure import solana_keys

log = logging.getLogger(__name__)

# Instrucciones del programa Pump.fun (discriminadores del IDL público)
MIGRATE_DISC = bytes([155, 234, 231, 146, 236, 158, 162, 30])
MIGRATE_V2_DISC = bytes([187, 203, 18, 31, 206, 237, 254, 41])
AMM_CREATE_POOL_DISC = bytes([233, 146, 209, 142, 207, 104, 64, 188])

# Índices de cuentas de `migrate` según el IDL público de Pump.fun
MIGRATE_MINT_IX = 2
MIGRATE_POOL_IX = 9
MIGRATE_POOL_AUTHORITY_IX = 10
# `create_pool` de pump_amm: pool(0), global_config(1), creator(2), base_mint(3), quote_mint(4)
CREATE_POOL_POOL_IX = 0
CREATE_POOL_BASE_MINT_IX = 3

# Layout de la cuenta Pool de pump_amm tras el discriminador de 8 bytes
POOL_MIN_LEN = 203
_OFF_CREATOR = 11
_OFF_BASE_MINT = 43
_OFF_QUOTE_MINT = 75
_OFF_LP_MINT = 107
_OFF_BASE_VAULT = 139
_OFF_QUOTE_VAULT = 171

_TRANSIENT_HTTP = {408, 425, 429, 500, 502, 503, 504}
_TRANSIENT_RPC_CODES = {-32005, -32004, -32009, -32014, -32016}  # nodo no sano, bloque no disponible, etc.


class SolanaRpcClient:
    def __init__(
        self,
        url: str,
        *,
        commitment: str = "confirmed",
        timeout: float = 15.0,
        max_retries: int = 3,
        backoff_s: float = 0.5,
        http: httpx.Client | None = None,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self._url = url
        self.commitment = commitment
        self._http = http or httpx.Client(timeout=timeout)
        self._max_retries = max_retries
        self._backoff = backoff_s
        self._sleep = sleep

    def call(self, method: str, params: list[Any]) -> Any:
        payload = {"jsonrpc": "2.0", "id": 1, "method": method, "params": params}
        last = "sin respuesta"
        for attempt in range(self._max_retries + 1):
            delay = self._backoff * (2**attempt)
            try:
                resp = self._http.post(self._url, json=payload)
            except httpx.TransportError as exc:  # red caída, timeout, DNS…  (sin URL en el mensaje)
                last = type(exc).__name__
                self._sleep(delay)
                continue
            if resp.status_code in _TRANSIENT_HTTP:
                last = f"HTTP {resp.status_code}"
                retry_after = resp.headers.get("Retry-After")
                self._sleep(min(30.0, float(retry_after)) if retry_after and retry_after.isdigit() else delay)
                continue
            if resp.status_code >= 400:
                raise PermanentSourceError(f"{method}: HTTP {resp.status_code}")
            try:
                body = resp.json()
            except ValueError as exc:
                raise PermanentSourceError(f"{method}: respuesta no JSON") from exc
            if "error" in body:
                err = body["error"] or {}
                code = err.get("code")
                if code in _TRANSIENT_RPC_CODES:
                    last = f"RPC {code}"
                    self._sleep(delay)
                    continue
                raise PermanentSourceError(f"{method}: RPC {code} {str(err.get('message', ''))[:120]}")
            return body.get("result")
        raise TransientSourceError(f"{method}: reintentos agotados ({last})")

    # --- métodos usados por el radar
    def get_signatures_for_address(
        self, address: str, *, before: str | None = None, until: str | None = None, limit: int = 1000
    ) -> list[dict[str, Any]]:
        cfg: dict[str, Any] = {"limit": limit, "commitment": self.commitment}
        if before:
            cfg["before"] = before
        if until:
            cfg["until"] = until
        return self.call("getSignaturesForAddress", [address, cfg]) or []

    def get_transaction(self, signature: str) -> dict[str, Any] | None:
        return self.call(
            "getTransaction",
            [signature, {"encoding": "jsonParsed", "maxSupportedTransactionVersion": 0, "commitment": self.commitment}],
        )

    def get_account_info(self, pubkey: str) -> dict[str, Any] | None:
        res = self.call("getAccountInfo", [pubkey, {"encoding": "base64", "commitment": self.commitment}])
        return (res or {}).get("value")

    def get_multiple_accounts_parsed(self, pubkeys: Sequence[str]) -> list[dict[str, Any] | None]:
        res = self.call("getMultipleAccounts", [list(pubkeys), {"encoding": "jsonParsed", "commitment": self.commitment}])
        return (res or {}).get("value") or []

    def get_token_supply(self, mint: str) -> dict[str, Any]:
        return (self.call("getTokenSupply", [mint, {"commitment": self.commitment}]) or {}).get("value") or {}

    def get_token_largest_accounts(self, mint: str) -> list[dict[str, Any]]:
        return (self.call("getTokenLargestAccounts", [mint, {"commitment": self.commitment}]) or {}).get("value") or []

    def get_slot(self) -> int:
        return int(self.call("getSlot", [{"commitment": self.commitment}]))


# --------------------------------------------------------------------------- parseo de transacciones
def _data(ins: dict[str, Any]) -> bytes:
    raw = ins.get("data")
    if not isinstance(raw, str):
        return b""
    try:
        return base58.b58decode(raw)
    except Exception:
        return b""


def _account_list(ins: dict[str, Any]) -> list[str]:
    return [a if isinstance(a, str) else a.get("pubkey", "") for a in ins.get("accounts", [])]


def _token_decimals(tx: dict[str, Any], mint: str, default: int = 6) -> int:
    meta = tx.get("meta") or {}
    for bal in (meta.get("postTokenBalances") or []) + (meta.get("preTokenBalances") or []):
        if bal.get("mint") == mint:
            return int(bal.get("uiTokenAmount", {}).get("decimals", default))
    return default


def parse_migrations(
    tx: dict[str, Any], signature: str, observed_at: datetime, confirmation: str = "confirmed"
) -> list[MigrationEvent]:
    """Extrae las migraciones de Pump.fun de una transacción ``jsonParsed``.

    Identidad del evento: (firma, índice de instrucción de nivel superior, índice de evento) con
    índice de evento 0 para la instrucción externa y 1+n para la n-ésima instrucción interna.
    """
    meta = tx.get("meta") or {}
    if meta.get("err") is not None:
        return []
    message = (tx.get("transaction") or {}).get("message") or {}
    outer: list[dict[str, Any]] = message.get("instructions") or []
    inner: dict[int, list[dict[str, Any]]] = {
        int(i["index"]): i.get("instructions") or [] for i in (meta.get("innerInstructions") or [])
    }
    block_time = tx.get("blockTime")
    event_at = datetime.fromtimestamp(block_time, UTC) if block_time else observed_at
    slot = int(tx.get("slot", 0))

    candidates: list[tuple[int, int, dict[str, Any]]] = []
    for i, ins in enumerate(outer):
        candidates.append((i, 0, ins))
        for j, sub in enumerate(inner.get(i, [])):
            candidates.append((i, j + 1, sub))

    events: list[MigrationEvent] = []
    for ix, ev_ix, ins in candidates:
        if ins.get("programId") != PUMP_PROGRAM_ID:
            continue
        disc = _data(ins)[:8]
        accounts = _account_list(ins)
        mint = pool = authority = None
        kind = None
        if disc == MIGRATE_DISC and len(accounts) > MIGRATE_POOL_AUTHORITY_IX:
            kind = "migrate"
            mint, pool, authority = accounts[MIGRATE_MINT_IX], accounts[MIGRATE_POOL_IX], accounts[MIGRATE_POOL_AUTHORITY_IX]
        elif disc == MIGRATE_V2_DISC:
            kind = "migrate_v2"  # el mapa de cuentas de v2 no está documentado: se toma del create_pool interno
            for sub in inner.get(ix, []):
                sub_accounts = _account_list(sub)
                if sub.get("programId") == PUMPSWAP_PROGRAM_ID and _data(sub)[:8] == AMM_CREATE_POOL_DISC and len(sub_accounts) > 3:
                    pool, mint = sub_accounts[CREATE_POOL_POOL_IX], sub_accounts[CREATE_POOL_BASE_MINT_IX]
                    break
        if not (kind and mint and pool):
            continue
        events.append(
            MigrationEvent(
                signature=signature,
                instruction_index=ix,
                event_index=ev_ix,
                slot=slot,
                mint=mint,
                pool=pool,
                program=PUMP_PROGRAM_ID,
                event_at=event_at,
                observed_at=observed_at,
                confirmation=confirmation,
                decimals=_token_decimals(tx, mint),
                evidence={
                    "instruction": kind,
                    "quote_mint": WSOL_MINT,
                    "amm_program": PUMPSWAP_PROGRAM_ID,
                    "pool_authority": authority,
                    "block_time": block_time,
                },
            )
        )
    return events


def parse_pool_account(data_b64: str) -> dict[str, str] | None:
    """Campos del Pool de pump_amm (offsets tras el discriminador); ``None`` si la cuenta es más corta."""
    raw = base64.b64decode(data_b64)
    if len(raw) < POOL_MIN_LEN:
        return None

    def pk(off: int) -> str:
        return base58.b58encode(raw[off : off + 32]).decode()

    return {
        "creator": pk(_OFF_CREATOR),
        "base_mint": pk(_OFF_BASE_MINT),
        "quote_mint": pk(_OFF_QUOTE_MINT),
        "lp_mint": pk(_OFF_LP_MINT),
        "base_vault": pk(_OFF_BASE_VAULT),
        "quote_vault": pk(_OFF_QUOTE_VAULT),
    }


class PumpMigrationSource:
    """Detecta migraciones por la cuenta de migración de Pump.fun (poco tráfico) y las verifica on-chain."""

    name = "solana_rpc"

    def __init__(self, rpc: SolanaRpcClient, *, clock: Clock, migration_account: str, batch_cap: int = 50) -> None:
        self._rpc = rpc
        self._clock = clock
        self._account = migration_account
        self._cap = batch_cap

    def fetch_migrations(self, cursor: str | None, limit: int) -> MigrationBatch:
        if cursor is None:
            # primer arranque: se empieza desde el estado actual (sin reprocesar historia)
            head = self._rpc.get_signatures_for_address(self._account, limit=1)
            if not head:
                return MigrationBatch((), None, None)
            return MigrationBatch((), head[0]["signature"], head[0].get("slot"))

        collected: list[dict[str, Any]] = []
        before: str | None = None
        for _ in range(10):  # tope de páginas por ciclo
            page = self._rpc.get_signatures_for_address(self._account, before=before, until=cursor, limit=1000)
            collected.extend(page)
            if len(page) < 1000:
                break
            before = page[-1]["signature"]
        if not collected:
            return MigrationBatch((), cursor, None)

        events: list[MigrationEvent] = []
        last_ok = cursor
        head_slot: int | None = None
        for entry in list(reversed(collected))[: min(limit, self._cap)]:  # de la más antigua a la más reciente
            sig = entry["signature"]
            if entry.get("err") is None:
                tx = self._rpc.get_transaction(sig)
                if tx is None:
                    break  # todavía no disponible: no se avanza el cursor más allá, se reintenta luego
                events.extend(parse_migrations(tx, sig, self._clock.now(), self._rpc.commitment))
            last_ok = sig
            head_slot = entry.get("slot", head_slot)
        return MigrationBatch(tuple(events), last_ok, head_slot)

    def fetch_signature(self, signature: str) -> Sequence[MigrationEvent]:
        tx = self._rpc.get_transaction(signature)
        if tx is None:
            raise TransientSourceError("transacción todavía no disponible")
        return parse_migrations(tx, signature, self._clock.now(), self._rpc.commitment)

    def verify(self, event: MigrationEvent) -> PoolVerification:
        """Valida programa, mint y pool contra la cuenta real del pool en PumpSwap."""
        info = self._rpc.get_account_info(event.pool)
        if info is None:
            raise TransientSourceError("la cuenta del pool todavía no está disponible")
        evidence: dict[str, Any] = {"owner": info.get("owner"), "checked_at": self._clock.now().isoformat()}
        if info.get("owner") != PUMPSWAP_PROGRAM_ID:
            return PoolVerification(False, False, None, "la cuenta no pertenece al programa PumpSwap", evidence)
        data = info.get("data") or []
        fields = parse_pool_account(data[0]) if data and isinstance(data[0], str) else None
        if fields is None:
            return PoolVerification(False, False, None, "datos de pool con formato inesperado", evidence)
        evidence.update({k: fields[k] for k in ("creator", "base_mint", "quote_mint", "base_vault", "quote_vault")})
        relation_ok = fields["base_mint"] == event.mint and fields["quote_mint"] == WSOL_MINT
        canonical: bool | None
        try:
            expected_authority = solana_keys.pump_pool_authority(event.mint)
            expected_pool = solana_keys.canonical_pumpswap_pool(event.mint)
            canonical = expected_pool == event.pool and fields["creator"] == expected_authority
            evidence.update({"expected_pool": expected_pool, "expected_pool_authority": expected_authority})
        except ValueError:
            canonical = None
        reason = None if relation_ok else "el pool no corresponde al mint y a WSOL de la migración observada"
        return PoolVerification(True, relation_ok, canonical, reason, evidence)
