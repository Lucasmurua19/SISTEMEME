"""Adaptadores en vivo con payloads sintéticos (estructura según la documentación pública de RPC y el IDL)."""

from __future__ import annotations

import base64
import hashlib
import json
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import base58
import httpx
import pytest

from app.application.ports import PermanentSourceError, TransientSourceError
from app.domain.models import (
    PUMP_PROGRAM_ID,
    PUMPSWAP_PROGRAM_ID,
    USDC_MINT,
    WSOL_MINT,
    PoolInfo,
    QuoteStatus,
)
from app.infrastructure import solana_keys
from app.infrastructure.clock import ManualClock
from app.infrastructure.jupiter_quotes import JupiterClient, JupiterFxProvider, JupiterQuoteProvider
from app.infrastructure.solana_market import (
    SwapRecord,
    build_windows,
    compute_concentration,
    extract_swap,
)
from app.infrastructure.solana_rpc import (
    AMM_CREATE_POOL_DISC,
    MIGRATE_DISC,
    MIGRATE_V2_DISC,
    PumpMigrationSource,
    SolanaRpcClient,
    parse_migrations,
    parse_pool_account,
)
from app.infrastructure.solana_ws import extract_signature
from app.logging_config import redact

NOW = datetime(2026, 3, 1, tzinfo=UTC)


def key(name: str) -> str:
    return base58.b58encode(hashlib.sha256(name.encode()).digest()).decode()


MINT, POOL, VAULT_B, VAULT_Q, USER_A, USER_B = (key(n) for n in ("mint", "pool", "vb", "vq", "ua", "ub"))
SIG = base58.b58encode(hashlib.sha512(b"sig").digest()).decode()


# ----------------------------------------------------------------------------- claves y PDA
class TestKeys:
    def test_curve_check_known_points(self):
        base_point = bytes.fromhex("5866666666666666666666666666666666666666666666666666666666666666")
        assert solana_keys.is_on_curve(base_point)  # punto base de ed25519
        assert solana_keys.is_on_curve(bytes(32))  # y = 0 (punto de orden 4)
        assert not solana_keys.is_on_curve(b"\x01" * 31)  # longitud inválida
        on = sum(solana_keys.is_on_curve(hashlib.sha256(bytes([i])).digest()) for i in range(200))
        assert 60 < on < 140  # ~50 % de los hashes aleatorios son puntos de la curva

    def test_pda_is_deterministic_and_off_curve(self):
        addr, bump = solana_keys.find_program_address([b"pool-authority", solana_keys.b58decode32(MINT)], PUMP_PROGRAM_ID)
        assert (addr, bump) == solana_keys.find_program_address([b"pool-authority", solana_keys.b58decode32(MINT)], PUMP_PROGRAM_ID)
        assert 0 <= bump <= 255 and not solana_keys.is_on_curve(solana_keys.b58decode32(addr))
        other, _ = solana_keys.find_program_address([b"pool-authority", solana_keys.b58decode32(key("otro"))], PUMP_PROGRAM_ID)
        assert other != addr

    def test_canonical_pool_derivation_chain(self):
        authority = solana_keys.pump_pool_authority(MINT)
        pool = solana_keys.canonical_pumpswap_pool(MINT)
        assert authority != pool and len(solana_keys.b58decode32(pool)) == 32
        assert pool == solana_keys.canonical_pumpswap_pool(MINT)

    def test_seed_longer_than_32_bytes_is_rejected(self):
        with pytest.raises(ValueError):
            solana_keys.find_program_address([b"x" * 33], PUMP_PROGRAM_ID)


# ----------------------------------------------------------------------------- transacciones de migración
def migrate_ix(mint=MINT, pool=POOL, disc=MIGRATE_DISC, program=PUMP_PROGRAM_ID):
    accounts = [key(f"acc{i}") for i in range(25)]
    accounts[2], accounts[9], accounts[10] = mint, pool, key("authority")
    return {"programId": program, "accounts": accounts, "data": base58.b58encode(disc).decode()}


def tx_with(outer, inner=None, err=None, block_time=1_772_323_200):
    return {
        "slot": 330_000_123,
        "blockTime": block_time,
        "transaction": {"signatures": [SIG], "message": {"accountKeys": [], "instructions": outer}},
        "meta": {
            "err": err,
            "innerInstructions": inner or [],
            "postTokenBalances": [{"accountIndex": 0, "mint": MINT, "uiTokenAmount": {"amount": "1", "decimals": 6}}],
        },
    }


class TestParseMigrations:
    def test_migrate_instruction_yields_mint_pool_and_identity(self):
        (ev,) = parse_migrations(tx_with([{"programId": "Other", "accounts": [], "data": ""}, migrate_ix()]), SIG, NOW)
        assert (ev.mint, ev.pool, ev.program) == (MINT, POOL, PUMP_PROGRAM_ID)
        assert (ev.signature, ev.instruction_index, ev.event_index, ev.slot) == (SIG, 1, 0, 330_000_123)
        assert ev.decimals == 6 and ev.event_at == datetime.fromtimestamp(1_772_323_200, UTC)
        assert ev.evidence["instruction"] == "migrate" and ev.evidence["quote_mint"] == WSOL_MINT

    def test_migrate_via_cpi_gets_a_distinct_event_index(self):
        tx = tx_with([{"programId": "Router", "accounts": [], "data": ""}], inner=[{"index": 0, "instructions": [migrate_ix()]}])
        (ev,) = parse_migrations(tx, SIG, NOW)
        assert (ev.instruction_index, ev.event_index) == (0, 1)

    def test_v2_uses_the_inner_create_pool_accounts(self):
        create_pool = {
            "programId": PUMPSWAP_PROGRAM_ID,
            "accounts": [POOL, key("cfg"), key("creator"), MINT, WSOL_MINT],
            "data": base58.b58encode(AMM_CREATE_POOL_DISC + b"\x00" * 4).decode(),
        }
        v2 = {"programId": PUMP_PROGRAM_ID, "accounts": [key("x")] * 5, "data": base58.b58encode(MIGRATE_V2_DISC).decode()}
        (ev,) = parse_migrations(tx_with([v2], inner=[{"index": 0, "instructions": [create_pool]}]), SIG, NOW)
        assert (ev.mint, ev.pool, ev.evidence["instruction"]) == (MINT, POOL, "migrate_v2")

    def test_ignores_failed_transactions_other_programs_and_other_instructions(self):
        assert parse_migrations(tx_with([migrate_ix()], err={"InstructionError": [0, "Custom"]}), SIG, NOW) == []
        assert parse_migrations(tx_with([migrate_ix(program=key("falso"))]), SIG, NOW) == []
        assert parse_migrations(tx_with([migrate_ix(disc=bytes(8))]), SIG, NOW) == []


def pool_account_b64(*, mint=MINT, quote=WSOL_MINT, creator=None):
    raw = bytes(8) + bytes([254]) + (0).to_bytes(2, "little")
    for k in (creator or key("creator"), mint, quote, key("lp"), VAULT_B, VAULT_Q):
        raw += base58.b58decode(k)
    return base64.b64encode(raw + bytes(60)).decode()


class FakeRpc:
    commitment = "confirmed"

    def __init__(self):
        self.signatures: list[dict] = []
        self.txs: dict[str, dict | None] = {}
        self.accounts: dict[str, dict | None] = {}
        self.calls: list[tuple] = []

    def get_signatures_for_address(self, address, *, before=None, until=None, limit=1000):
        self.calls.append(("sigs", address, before, until))
        out = []
        for e in self.signatures:  # newest-first, como el RPC real
            if until and e["signature"] == until:
                break
            out.append(e)
        return out

    def get_transaction(self, sig):
        return self.txs.get(sig)

    def get_account_info(self, pubkey):
        return self.accounts.get(pubkey)


class TestMigrationSource:
    def src(self, rpc):
        return PumpMigrationSource(rpc, clock=ManualClock(NOW), migration_account=key("migr"))

    def test_pool_account_layout(self):
        f = parse_pool_account(pool_account_b64())
        assert f["base_mint"] == MINT and f["quote_mint"] == WSOL_MINT and f["quote_vault"] == VAULT_Q and f["base_vault"] == VAULT_B
        assert parse_pool_account(base64.b64encode(bytes(50)).decode()) is None

    def test_first_run_starts_from_head_without_reprocessing_history(self):
        rpc = FakeRpc()
        rpc.signatures = [{"signature": "s3", "slot": 3, "err": None}, {"signature": "s2", "slot": 2, "err": None}]
        batch = self.src(rpc).fetch_migrations(None, 50)
        assert batch.events == () and batch.next_cursor == "s3" and batch.head_slot == 3

    def test_processes_oldest_first_and_advances_the_cursor(self):
        rpc = FakeRpc()
        rpc.signatures = [{"signature": "s3", "slot": 3, "err": None}, {"signature": "s2", "slot": 2, "err": {"x": 1}}, {"signature": "s1", "slot": 1, "err": None}, {"signature": "s0", "slot": 0, "err": None}]
        rpc.txs = {"s1": tx_with([migrate_ix()]), "s3": tx_with([migrate_ix(mint=key("m2"), pool=key("p2"))])}
        batch = self.src(rpc).fetch_migrations("s0", 50)
        assert [e.mint for e in batch.events] == [MINT, key("m2")]  # s2 falló on-chain: se salta pero no frena
        assert batch.next_cursor == "s3" and batch.head_slot == 3

    def test_unavailable_transaction_stops_before_it_without_losing_the_gap(self):
        rpc = FakeRpc()
        rpc.signatures = [{"signature": "s2", "slot": 2, "err": None}, {"signature": "s1", "slot": 1, "err": None}, {"signature": "s0", "slot": 0, "err": None}]
        rpc.txs = {"s1": tx_with([migrate_ix()]), "s2": None}
        batch = self.src(rpc).fetch_migrations("s0", 50)
        assert len(batch.events) == 1 and batch.next_cursor == "s1"  # s2 se reintenta en el próximo ciclo

    def test_no_new_signatures_keeps_the_cursor(self):
        batch = self.src(FakeRpc()).fetch_migrations("s9", 50)
        assert batch.events == () and batch.next_cursor == "s9"

    def event(self):
        return parse_migrations(tx_with([migrate_ix()]), SIG, NOW)[0]

    def test_verify_accepts_matching_pool(self):
        rpc = FakeRpc()
        rpc.accounts[POOL] = {"owner": PUMPSWAP_PROGRAM_ID, "data": [pool_account_b64(), "base64"]}
        v = self.src(rpc).verify(self.event())
        assert v.verified and v.identity_ok and v.reason is None
        assert v.evidence["base_vault"] == VAULT_B and "expected_pool" in v.evidence
        assert v.canonical is False  # el pool sintético no es la PDA canónica de ese mint

    def test_verify_rejects_wrong_owner_wrong_mint_and_garbage(self):
        rpc = FakeRpc()
        rpc.accounts[POOL] = {"owner": key("otro"), "data": [pool_account_b64(), "base64"]}
        assert not self.src(rpc).verify(self.event()).verified
        rpc.accounts[POOL] = {"owner": PUMPSWAP_PROGRAM_ID, "data": [pool_account_b64(mint=key("distinto")), "base64"]}
        v = self.src(rpc).verify(self.event())
        assert v.verified and not v.identity_ok and "no corresponde" in v.reason
        rpc.accounts[POOL] = {"owner": PUMPSWAP_PROGRAM_ID, "data": [base64.b64encode(bytes(20)).decode(), "base64"]}
        assert not self.src(rpc).verify(self.event()).verified

    def test_verify_missing_account_is_transient(self):
        with pytest.raises(TransientSourceError):
            self.src(FakeRpc()).verify(self.event())


# ----------------------------------------------------------------------------- cliente RPC
def rpc_with(handler, **kw):
    sleeps: list[float] = []
    client = SolanaRpcClient(
        "https://rpc.example/SECRETKEYSECRETKEYSECRETKEY",
        http=httpx.Client(transport=httpx.MockTransport(handler)),
        sleep=sleeps.append,
        **kw,
    )
    return client, sleeps


class TestRpcClient:
    def test_retries_transient_http_with_growing_backoff_then_succeeds(self):
        calls = {"n": 0}

        def handler(request):
            calls["n"] += 1
            return httpx.Response(429) if calls["n"] < 3 else httpx.Response(200, json={"result": {"value": 7}})

        client, sleeps = rpc_with(handler)
        assert client.call("getSlot", []) == {"value": 7}
        assert calls["n"] == 3 and sleeps[1] > sleeps[0]

    def test_permanent_rpc_error_is_not_retried(self):
        calls = {"n": 0}

        def handler(request):
            calls["n"] += 1
            return httpx.Response(200, json={"error": {"code": -32602, "message": "Invalid param"}})

        client, _ = rpc_with(handler)
        with pytest.raises(PermanentSourceError):
            client.call("getTransaction", ["x"])
        assert calls["n"] == 1

    def test_node_unhealthy_is_transient_and_exhaustion_is_reported_without_leaking_the_url(self):
        client, _ = rpc_with(lambda r: httpx.Response(200, json={"error": {"code": -32005, "message": "Node is behind"}}), max_retries=2)
        with pytest.raises(TransientSourceError) as exc:
            client.call("getSlot", [])
        assert "SECRETKEY" not in str(exc.value) and "rpc.example" not in str(exc.value)

    def test_transport_errors_are_transient_and_do_not_leak_the_url(self):
        def boom(request):
            raise httpx.ConnectError("fallo contra https://rpc.example/SECRETKEYSECRETKEYSECRETKEY")

        client, _ = rpc_with(boom, max_retries=1)
        with pytest.raises(TransientSourceError) as exc:
            client.call("getSlot", [])
        assert "SECRETKEY" not in str(exc.value)

    def test_http_client_error_is_permanent(self):
        client, _ = rpc_with(lambda r: httpx.Response(401))
        with pytest.raises(PermanentSourceError):
            client.call("getSlot", [])


# ----------------------------------------------------------------------------- mercado
def swap_tx(*, t, pre_b, post_b, pre_q, post_q, wallets=(), sig="s", err=None):
    """Transacción con saldos de las bóvedas y de las billeteras (pre → post)."""
    keys = [{"pubkey": VAULT_B}, {"pubkey": VAULT_Q}] + [{"pubkey": w} for w, *_ in wallets]
    def bal(idx, mint, owner, amount):
        return {"accountIndex": idx, "mint": mint, "owner": owner, "uiTokenAmount": {"amount": str(int(amount)), "decimals": 6 if mint == MINT else 9}}
    pre = [bal(0, MINT, POOL, pre_b), bal(1, WSOL_MINT, POOL, pre_q)] if pre_b or pre_q else []
    post = [bal(0, MINT, POOL, post_b), bal(1, WSOL_MINT, POOL, post_q)]
    for i, (w, w_pre, w_post) in enumerate(wallets, start=2):
        pre.append(bal(i, MINT, w, w_pre))
        post.append(bal(i, MINT, w, w_post))
    return {
        "slot": 1, "blockTime": int(t.timestamp()),
        "transaction": {"signatures": [sig], "message": {"accountKeys": keys}},
        "meta": {"err": err, "preTokenBalances": pre, "postTokenBalances": post},
    }  # fmt: skip


def swap_of(tx):
    return extract_swap(tx, pool_address=POOL, mint=MINT, base_vault=VAULT_B, quote_vault=VAULT_Q, decimals=6)


class TestMarket:
    def test_classifies_init_buy_sell_and_liquidity(self):
        init = swap_of(swap_tx(t=NOW, pre_b=0, post_b=200_000_000_000_000, pre_q=0, post_q=80_000_000_000))
        assert init.kind == "init" and init.price == Decimal(80) / Decimal(200_000_000)
        buy = swap_of(swap_tx(t=NOW, pre_b=200e12, post_b=199e12, pre_q=80e9, post_q=80.5e9, wallets=[(USER_A, 0, 1_000_000_000_000)]))
        assert buy.kind == "buy" and buy.quote_volume == 500_000_000 and buy.wallet_deltas == {USER_A: 1_000_000_000_000}
        sell = swap_of(swap_tx(t=NOW, pre_b=199e12, post_b=199.5e12, pre_q=80.5e9, post_q=80.3e9))
        assert sell.kind == "sell"
        add = swap_of(swap_tx(t=NOW, pre_b=199e12, post_b=209e12, pre_q=80e9, post_q=84e9))
        assert add.kind == "liquidity"

    def test_ignores_failed_and_unrelated_transactions(self):
        assert swap_of(swap_tx(t=NOW, pre_b=1, post_b=1, pre_q=1, post_q=1)) is None
        assert swap_of(swap_tx(t=NOW, pre_b=10, post_b=9, pre_q=5, post_q=6, err={"x": 1})) is None

    def rec(self, t, kind, price, base, quote, vol=0, wallets=None):
        return SwapRecord(at=t, slot=1, signature="s", kind=kind, price=Decimal(price), base_reserve=base, quote_reserve=quote, quote_volume=vol, wallet_deltas=wallets or {})

    def pool(self):
        return PoolInfo(POOL, MINT, "T", "Token", 6, PUMPSWAP_PROGRAM_ID, WSOL_MINT, NOW, SIG, 0, 1, True, True, True)

    def test_windows_aggregate_trades_net_buyers_and_carry_empty_windows(self):
        swaps = [
            self.rec(NOW, "init", "1.0", 1000, 1000),
            self.rec(NOW + timedelta(seconds=2), "buy", "1.2", 900, 1100, 100, {USER_A: 50, USER_B: 30}),
            self.rec(NOW + timedelta(seconds=5), "sell", "1.1", 950, 1050, 50, {USER_B: -40}),
            self.rec(NOW + timedelta(seconds=7), "buy", "1.3", 850, 1150, 100, {USER_A: 10}),
        ]
        out = build_windows(self.pool(), swaps, [], window_s=15, horizon=NOW + timedelta(seconds=45), lag_s=2, conc=Decimal(30), conc_at=NOW, source="t")
        assert [w.window_index for w in out] == [0, 1, 2]
        w0, w1, w2 = out
        assert (w0.open, w0.high, w0.low, w0.close) == (Decimal(1), Decimal("1.3"), Decimal(1), Decimal("1.3"))
        assert w0.net_buyers == 1  # A neto +60; B neto −10 → solo A compra en términos netos
        assert (w0.buy_count, w0.sell_count, w0.buy_volume_lamports, w0.sell_volume_lamports) == (2, 1, 200, 50)
        assert w0.observed_at == w0.window_end + timedelta(seconds=2)
        assert (w1.open, w1.close, w1.swap_count, w1.sol_reserve_lamports) == (Decimal("1.3"), Decimal("1.3"), 0, 1150)  # ventana vacía: arrastra
        assert w2.top10_concentration_pct == Decimal(30)

    def test_no_windows_before_the_first_priced_trade_and_none_past_the_horizon(self):
        assert build_windows(self.pool(), [], [], window_s=15, horizon=NOW + timedelta(hours=1), lag_s=2, conc=None, conc_at=None, source="t") == []
        swaps = [self.rec(NOW, "init", "1", 10, 10)]
        out = build_windows(self.pool(), swaps, [], window_s=15, horizon=NOW + timedelta(seconds=29), lag_s=2, conc=None, conc_at=None, source="t")
        assert len(out) == 1  # la ventana 1 aún no cerró

    def test_unparsed_transactions_reduce_coverage(self):
        swaps = [self.rec(NOW, "init", "1", 10, 10), self.rec(NOW + timedelta(seconds=1), "buy", "1.1", 9, 11, 1, {USER_A: 1})]
        out = build_windows(self.pool(), swaps, [NOW + timedelta(seconds=3)] * 2, window_s=15, horizon=NOW + timedelta(seconds=15), lag_s=0, conc=None, conc_at=None, source="t")
        assert out[0].coverage == Decimal(2) / Decimal(4)

    def test_concentration_sums_by_owner_and_excludes_pool_and_burn(self):
        class Rpc:
            def get_token_supply(self, mint):
                return {"amount": "1000"}

            def get_token_largest_accounts(self, mint):
                return [{"address": f"acc{i}", "amount": str(a)} for i, a in enumerate([500, 100, 80, 70, 60, 50, 40, 30, 20, 15, 10, 5])]

            def get_multiple_accounts_parsed(self, keys):
                owners = [POOL, "whale", "whale"] + [f"w{i}" for i in range(9)]
                return [{"data": {"parsed": {"info": {"owner": o}}}} for o in owners]

        # excluye el pool (500) → suministro analizado = 500. 'whale' suma 180 (dos cuentas, un propietario);
        # los 10 mayores propietarios son 180,70,60,50,40,30,20,15,10,5 = 480 → 96 %
        assert compute_concentration(Rpc(), MINT, POOL) == Decimal(480) * 100 / Decimal(500)

    def test_concentration_is_unknown_when_an_owner_cannot_be_resolved(self):
        class Rpc:
            def get_token_supply(self, mint):
                return {"amount": "1000"}

            def get_token_largest_accounts(self, mint):
                return [{"address": "a", "amount": "10"}]

            def get_multiple_accounts_parsed(self, keys):
                return [None]

        assert compute_concentration(Rpc(), MINT, POOL) is None


# ----------------------------------------------------------------------------- Jupiter
def jup(handler, **kw):
    sleeps: list[float] = []
    client = JupiterClient(
        "https://api.jup.ag/swap/v2", kw.pop("api_key", ""), http=httpx.Client(transport=httpx.MockTransport(handler)),
        sleep=sleeps.append, monotonic=lambda: 1e6, **kw,
    )  # fmt: skip
    return client, sleeps


POOL_INFO = PoolInfo(POOL, MINT, "T", "Token", 6, PUMPSWAP_PROGRAM_ID, WSOL_MINT, NOW, SIG, 0, 1, True, True, True)
ORDER_BODY = {
    "inAmount": "100000000", "outAmount": "52000000", "otherAmountThreshold": "50440000", "priceImpact": -0.35,
    "feeBps": 30, "requestId": "req-1", "router": "metis", "prioritizationFeeLamports": 50_000, "signatureFeeLamports": 5000,
    "routePlan": [{"swapInfo": {"label": "Pump.fun Amm"}, "percent": 100}],
}  # fmt: skip


class TestJupiter:
    def test_maps_a_quote_with_costs_and_sends_the_api_key_header(self):
        seen = {}

        def handler(request):
            seen.update(dict(request.url.params), key=request.headers.get("x-api-key"), path=request.url.path)
            return httpx.Response(200, json=ORDER_BODY)

        client, _ = jup(handler, api_key="k123")
        q = JupiterQuoteProvider(client).quote_buy(POOL_INFO, 100_000_000, 300, NOW)
        assert seen["path"].endswith("/order") and seen["key"] == "k123" and "taker" not in seen
        assert seen["inputMint"] == WSOL_MINT and seen["outputMint"] == MINT and seen["slippageBps"] == "300"
        assert q.status is QuoteStatus.OK and (q.in_amount, q.out_amount, q.min_out_amount) == (100_000_000, 52_000_000, 50_440_000)
        assert q.price_impact_bps == Decimal("35.00") and q.route == "Pump.fun Amm" and q.request_id == "req-1"
        kinds = {c.kind: c for c in q.costs}
        assert kinds["protocol"].included_in_amount and not kinds["priority"].included_in_amount
        assert kinds["priority"].lamports == 50_000 and kinds["ata_deposit"].refundable and kinds["signature"].lamports == 5000
        assert q.expires_at == NOW + timedelta(seconds=20)

    def test_sell_has_no_ata_deposit(self):
        client, _ = jup(lambda r: httpx.Response(200, json=ORDER_BODY))
        q = JupiterQuoteProvider(client).quote_sell(POOL_INFO, 50_000_000, 300, NOW)
        assert q.input_mint == MINT and q.output_mint == WSOL_MINT and "ata_deposit" not in {c.kind for c in q.costs}

    def test_no_route_error_body_is_not_an_error(self):
        client, _ = jup(lambda r: httpx.Response(400, json={"error": "COULD_NOT_FIND_ANY_ROUTE"}))
        q = JupiterQuoteProvider(client).quote_sell(POOL_INFO, 1, 300, NOW)
        assert q.status is QuoteStatus.NO_ROUTE and not q.ok

    def test_empty_out_amount_is_no_route(self):
        client, _ = jup(lambda r: httpx.Response(200, json={**ORDER_BODY, "outAmount": "0"}))
        assert JupiterQuoteProvider(client).quote_sell(POOL_INFO, 1, 300, NOW).status is QuoteStatus.NO_ROUTE

    def test_rate_limit_is_retried_then_reported_as_error(self):
        calls = {"n": 0}

        def handler(request):
            calls["n"] += 1
            return httpx.Response(429)

        client, sleeps = jup(handler, max_retries=2)
        q = JupiterQuoteProvider(client).quote_buy(POOL_INFO, 1, 300, NOW)
        assert q.status is QuoteStatus.ERROR and calls["n"] == 3 and "429" in q.error

    def test_unauthorised_is_an_error_not_no_route(self):
        client, _ = jup(lambda r: httpx.Response(401, json={"error": "invalid api key"}))
        q = JupiterQuoteProvider(client).quote_buy(POOL_INFO, 1, 300, NOW)
        assert q.status is QuoteStatus.ERROR

    def test_calls_are_spaced_to_respect_the_rate_limit(self):
        ticks = iter(range(10_000, 20_000))
        sleeps: list[float] = []
        client = JupiterClient("https://x", http=httpx.Client(transport=httpx.MockTransport(lambda r: httpx.Response(200, json=ORDER_BODY))),
                               sleep=sleeps.append, monotonic=lambda: next(ticks) * 0.0 + 100.0)  # fmt: skip
        client.order(WSOL_MINT, MINT, 1, 300)
        client.order(WSOL_MINT, MINT, 1, 300)
        assert sleeps and sleeps[-1] == pytest.approx(2.0)  # sin clave: 0,5 req/s → 2 s entre llamadas

    def test_fx_rate_from_one_sol_to_usdc_is_cached(self):
        calls = {"n": 0}

        def handler(request):
            calls["n"] += 1
            assert request.url.params["outputMint"] == USDC_MINT
            return httpx.Response(200, json={"outAmount": "151250000"})

        client, _ = jup(handler)
        fx = JupiterFxProvider(client)
        assert fx.sol_usd(NOW).rate == Decimal("151.25")
        fx.sol_usd(NOW + timedelta(seconds=10))
        assert calls["n"] == 1
        assert fx.sol_usd(NOW + timedelta(seconds=120)).observed_at == NOW + timedelta(seconds=120)


# ----------------------------------------------------------------------------- varios
def test_websocket_notification_filtering():
    def msg(err=None, logs=("Program log: Instruction: Migrate",), sig="abc"):
        return json.dumps({"jsonrpc": "2.0", "method": "logsNotification", "params": {"result": {"value": {"signature": sig, "err": err, "logs": list(logs)}}}})

    assert extract_signature(msg()) == "abc"
    assert extract_signature(msg(err={"x": 1})) is None
    assert extract_signature(msg(logs=("Program log: Instruction: Buy",))) is None
    assert extract_signature(msg(logs=())) == "abc"  # logs truncados: se deja pasar y el trabajo verifica
    assert extract_signature('{"id":1,"result":42}') is None and extract_signature("no json") is None


def test_log_redaction_hides_keys_and_credentials():
    assert "SECRET" not in redact("GET https://mainnet.helius-rpc.com/?api-key=SECRET123 falló")
    assert "pass" not in redact("https://user:pass@rpc.example/path")
    assert "AAAAAAAAAAAAAAAAAAAAAAAAAAAA" not in redact("https://rpc.example/AAAAAAAAAAAAAAAAAAAAAAAAAAAA")
    assert "x-api-key=***" in redact("x-api-key=abcd1234")
    assert redact("migración verificada") == "migración verificada"
