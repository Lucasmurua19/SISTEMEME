"""Cliente JSON-RPC de Solana y fuente de migraciones de Pump.fun → PumpSwap.

Seguridad: la URL del RPC puede contener una clave. Nunca se escribe en logs ni en mensajes de error.
"""

from __future__ import annotations

import base64
import logging
import re
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

# Posiciones de cuentas de las instrucciones externas de Pump.fun, verificadas contra transacciones reales
# (`migrate`: 25 cuentas; `migrate_v2`: 29). Sirven de contraprueba: la identidad sale del `create_pool` interno.
MIGRATE_MINT_IX = 2
MIGRATE_ACCOUNT_MAP = {"migrate": {"pool": 9, "authority": 10}, "migrate_v2": {"pool": 10, "authority": 11}}
# `create_pool` de pump_amm: pool(0), global_config(1), creator(2), base_mint(3), quote_mint(4)
CREATE_POOL_POOL_IX = 0
CREATE_POOL_CREATOR_IX = 2
CREATE_POOL_BASE_MINT_IX = 3
CREATE_POOL_QUOTE_MINT_IX = 4

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

# Versión máxima de transacción que se pide al nodo. Con 0, el nodo rechaza (-32015) las transacciones versión 1,
# que ya circulan en la cuenta de migración. Si aparece una versión mayor, el nodo la informa y se reintenta con ella.
TX_VERSION_DEFAULT = 1
TX_VERSION_ERROR_CODE = -32015
_TX_VERSION_HINT = re.compile(r'maxSupportedTransactionVersion\\?"\s*:\s*(\d+)')


class RpcError(PermanentSourceError):
    """Error JSON-RPC no transitorio; conserva el código y el mensaje completo del nodo."""

    def __init__(self, method: str, code: int | None, message: str) -> None:
        super().__init__(f"{method}: RPC {code} {message[:120]}")
        self.method = method
        self.code = code
        self.node_message = message


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
        max_total_wait_s: float = 12.0,
    ) -> None:
        self._url = url
        self.commitment = commitment
        self._http = http or httpx.Client(timeout=timeout)
        self._tx_version = TX_VERSION_DEFAULT
        self._max_retries = max_retries
        self._backoff = backoff_s
        self._sleep = sleep
        # Tope de espera acumulada entre reintentos de una llamada. Un proveedor que limita la tasa responde 429 con
        # `Retry-After: 10`: sin tope, una sola llamada bloquea el ciclo del worker 30 s o más.
        self._max_total_wait = max_total_wait_s

    def call(self, method: str, params: list[Any], *, retries: int | None = None) -> Any:
        """Llamada JSON-RPC con reintentos acotados. ``retries=0`` para datos opcionales que no valen una espera."""
        payload = {"jsonrpc": "2.0", "id": 1, "method": method, "params": params}
        max_retries = self._max_retries if retries is None else retries
        last = "sin respuesta"
        waited = 0.0
        for attempt in range(max_retries + 1):
            wait = self._backoff * (2**attempt)
            try:
                resp = self._http.post(self._url, json=payload)
            except httpx.TransportError as exc:  # red caída, timeout, DNS…  (sin URL en el mensaje)
                last = type(exc).__name__
            else:
                if resp.status_code in _TRANSIENT_HTTP:
                    last = f"HTTP {resp.status_code}"
                    retry_after = resp.headers.get("Retry-After")
                    if retry_after and retry_after.isdigit():
                        wait = min(30.0, float(retry_after))
                elif resp.status_code >= 400:
                    raise PermanentSourceError(f"{method}: HTTP {resp.status_code}")
                else:
                    try:
                        body = resp.json()
                    except ValueError as exc:
                        raise PermanentSourceError(f"{method}: respuesta no JSON") from exc
                    if "error" not in body:
                        return body.get("result")
                    err = body["error"] or {}
                    code = err.get("code")
                    if code not in _TRANSIENT_RPC_CODES:
                        raise RpcError(method, code, str(err.get("message", "")))
                    last = f"RPC {code}"
            if attempt == max_retries:
                break
            if waited + wait > self._max_total_wait:
                raise TransientSourceError(f"{method}: límite de espera agotado ({last})")
            self._sleep(wait)
            waited += wait
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
        version = self._tx_version
        for _ in range(2):
            try:
                tx = self.call(
                    "getTransaction",
                    [signature, {"encoding": "jsonParsed", "maxSupportedTransactionVersion": version, "commitment": self.commitment}],
                )
                self._tx_version = version  # recuerda la versión aceptada para las llamadas siguientes
                return tx
            except RpcError as exc:
                hint = _TX_VERSION_HINT.search(exc.node_message) if exc.code == TX_VERSION_ERROR_CODE else None
                if hint is None or int(hint.group(1)) <= version or int(hint.group(1)) > 255:
                    raise
                version = int(hint.group(1))
        raise PermanentSourceError("getTransaction: versión de transacción no soportada")

    def get_account_info(self, pubkey: str) -> dict[str, Any] | None:
        res = self.call("getAccountInfo", [pubkey, {"encoding": "base64", "commitment": self.commitment}])
        return (res or {}).get("value")

    # Las tres siguientes alimentan solo la concentración de holders, un dato opcional (si falta, el filtro queda
    # pendiente). Sin reintentos: `getTokenLargestAccounts` es la llamada más limitada de los proveedores y una
    # espera de `Retry-After` bloquearía el ciclo; el reintento lo decide el retroceso del refresco, no esta llamada.
    def get_multiple_accounts_parsed(self, pubkeys: Sequence[str]) -> list[dict[str, Any] | None]:
        res = self.call(
            "getMultipleAccounts", [list(pubkeys), {"encoding": "jsonParsed", "commitment": self.commitment}], retries=0
        )
        return (res or {}).get("value") or []

    def get_token_supply(self, mint: str) -> dict[str, Any]:
        return (self.call("getTokenSupply", [mint, {"commitment": self.commitment}], retries=0) or {}).get("value") or {}

    def get_token_largest_accounts(self, mint: str) -> list[dict[str, Any]]:
        res = self.call("getTokenLargestAccounts", [mint, {"commitment": self.commitment}], retries=0)
        return (res or {}).get("value") or []

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


def _find_create_pool(group: list[dict[str, Any]]) -> tuple[str, str, str, str] | None:
    """(pool, base_mint, creator, quote_mint) del primer ``create_pool`` de PumpSwap dentro del grupo de internas."""
    for sub in group:
        if sub.get("programId") != PUMPSWAP_PROGRAM_ID or _data(sub)[:8] != AMM_CREATE_POOL_DISC:
            continue
        accounts = _account_list(sub)
        if len(accounts) > CREATE_POOL_QUOTE_MINT_IX:
            return (
                accounts[CREATE_POOL_POOL_IX],
                accounts[CREATE_POOL_BASE_MINT_IX],
                accounts[CREATE_POOL_CREATOR_IX],
                accounts[CREATE_POOL_QUOTE_MINT_IX],
            )
    return None


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

    # Una migración es real solo si el programa de Pump.fun hizo crear el pool en PumpSwap. Casi la mitad de las
    # transacciones sobre la cuenta de migración son reintentos de bots que terminan en «Bonding curve already
    # migrated» (éxito sin efecto): no crean pool y no deben contar como migración. Por eso, tanto para `migrate`
    # como para `migrate_v2`, la identidad (mint y pool) sale del `create_pool` interno y no de las cuentas externas.
    events: list[MigrationEvent] = []
    for ix, ev_ix, ins in candidates:
        if ins.get("programId") != PUMP_PROGRAM_ID:
            continue
        disc = _data(ins)[:8]
        if disc == MIGRATE_DISC:
            kind = "migrate"
        elif disc == MIGRATE_V2_DISC:
            kind = "migrate_v2"
        else:
            continue
        created = _find_create_pool(inner.get(ix, []))
        if created is None:
            continue
        pool, mint, authority, quote_mint = created
        outer_accounts = _account_list(ins)
        outer_map = MIGRATE_ACCOUNT_MAP[kind]
        outer_matches = (
            len(outer_accounts) > max(outer_map.values())
            and outer_accounts[MIGRATE_MINT_IX] == mint
            and outer_accounts[outer_map["pool"]] == pool
        )
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
                    "quote_mint": quote_mint,
                    "amm_program": PUMPSWAP_PROGRAM_ID,
                    "pool_authority": authority,
                    "outer_accounts_match": outer_matches,
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

    def __init__(
        self,
        rpc: SolanaRpcClient,
        *,
        clock: Clock,
        migration_account: str,
        batch_cap: int = 50,
        budget_s: float | None = None,
        monotonic: Callable[[], float] = time.monotonic,
    ) -> None:
        self._rpc = rpc
        self._clock = clock
        self._account = migration_account
        self._cap = batch_cap
        # Tope de tiempo real por lectura: con un RPC lento, procesar el tramo entero bloquearía el ciclo del worker
        # (sin latido ni seguimiento de posiciones). Lo no procesado queda detrás del cursor y sigue en el próximo ciclo.
        self._budget_s = budget_s
        self._mono = monotonic

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
        deadline = None if self._budget_s is None else self._mono() + self._budget_s
        for entry in list(reversed(collected))[: min(limit, self._cap)]:  # de la más antigua a la más reciente
            if deadline is not None and self._mono() > deadline:
                break  # se retoma en el próximo ciclo desde el cursor
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
        # La identidad es que el pool real sea el de la migración observada (mismo mint base y misma cotización que el
        # `create_pool` de la transacción). Que cotice en SOL es otra cuestión, resuelta al evaluar: hoy hay migraciones
        # legítimas cotizadas en otros tokens de Pump.fun, que no son un fallo de verificación sino algo fuera de alcance.
        expected_quote = event.evidence.get("quote_mint", WSOL_MINT)
        relation_ok = fields["base_mint"] == event.mint and fields["quote_mint"] == expected_quote
        canonical: bool | None
        try:
            expected_authority = solana_keys.pump_pool_authority(event.mint)
            expected_pool = solana_keys.canonical_pumpswap_pool(event.mint)
            canonical = expected_pool == event.pool and fields["creator"] == expected_authority
            evidence.update({"expected_pool": expected_pool, "expected_pool_authority": expected_authority})
        except ValueError:
            canonical = None
        reason = None if relation_ok else "el pool no corresponde al mint y a la cotización de la migración observada"
        return PoolVerification(True, relation_ok, canonical, reason, evidence)
