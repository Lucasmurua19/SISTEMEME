"""Precio de PumpSwap sobre la reserva efectiva de SOL, contrastado con transacciones reales de mainnet.

PumpSwap cotiza sobre «bóveda de SOL + reserva virtual». Los casos cubren un pool con aporte virtual de 17,58 SOL,
otro con 18,93 y uno «mayhem», sin aporte y con la virtual apenas negativa por comisiones retenidas.
"""

import base64
import json
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path

import base58
import pytest

from app.domain.models import PUMPSWAP_PROGRAM_ID, WSOL_MINT, PoolInfo
from app.infrastructure.clock import ManualClock
from app.infrastructure.solana_market import SolanaMarketSource, extract_swap
from app.infrastructure.solana_rpc import (
    BUY_EVENT_DISC,
    EVENT_CPI_DISC,
    parse_pool_account,
    reserves_after_swap,
)

FIXTURE = Path(__file__).resolve().parents[1] / "fixtures" / "solana_real" / "pumpswap_swaps.json"
POOLS = json.loads(FIXTURE.read_text(encoding="utf-8"))["pools"]
BY_CASE = {p["caso"]: p for p in POOLS}
LAMPORTS = 1_000_000_000


def events(tx):
    """Campos crudos de cada evento de swap, leídos de forma independiente del código que se prueba."""
    out = []
    for group in tx["meta"]["innerInstructions"]:
        for ins in group["instructions"]:
            raw = base58.b58decode(ins["data"])
            assert raw[:8] == EVENT_CPI_DISC
            raw = raw[8:]
            buy = raw[:8] == BUY_EVENT_DISC

            def u(off, n=8, signed=False, data=raw):
                return int.from_bytes(data[off : off + n], "little", signed=signed)

            if buy:
                name = raw[405 : 405 + u(401, 4)].decode()
                virtual = u(405 + len(name) + 32, 16, True)
            else:
                name, virtual = "sell", u(392, 16, True)
            out.append({
                "buy": buy, "name": name, "base_amount": u(16), "base": u(48), "vault_quote": u(56), "virtual": virtual,
                "quote_amount": u(64), "lp_fee": u(80), "protocol_fee": u(96), "creator_fee": u(352),
            })  # fmt: skip
    return out


def quote_error(e, virtual) -> int:
    """Diferencia entre el SOL real del swap y el que predice x·y=k con ``bóveda + virtual``."""
    base, quote, amount = e["base"], e["vault_quote"] + virtual, e["base_amount"]
    if not e["buy"]:
        return abs(quote * amount // (base + amount) - e["quote_amount"])
    real = e["quote_amount"]
    if "exact_quote_in" in e["name"]:  # ahí el monto incluye las comisiones
        real -= e["lp_fee"] + e["protocol_fee"] + e["creator_fee"]
    return abs(-(-quote * amount // (base - amount)) - real)


def balance(tx, name, address):
    keys = [k["pubkey"] for k in tx["transaction"]["message"]["accountKeys"]]
    return next((int(b["uiTokenAmount"]["amount"]) for b in tx["meta"][name] if keys[b["accountIndex"]] == address), None)


def record(pool, tx, virtual=0):
    return extract_swap(
        tx, pool_address=pool["pool"], mint=pool["mint"], base_vault=pool["base_vault"],
        quote_vault=pool["quote_vault"], decimals=pool["decimals"], virtual_quote_reserves=virtual,
    )  # fmt: skip


def price_of(base, quote, decimals) -> Decimal:
    return (Decimal(quote) / LAMPORTS) / (Decimal(base) / Decimal(10**decimals))


@pytest.mark.parametrize("pool", POOLS, ids=[p["caso"] for p in POOLS])
class TestRealSwaps:
    def test_constant_product_on_effective_reserves_reproduces_every_swap(self, pool):
        checked = 0
        for tx in pool["txs"]:
            for e in events(tx):
                assert quote_error(e, e["virtual"]) <= 3, e
                checked += 1
        assert checked >= len(pool["txs"])

    def test_state_after_a_swap_is_the_state_before_the_next_one(self, pool):
        for previous, following in zip(pool["txs"], pool["txs"][1:], strict=False):
            after = reserves_after_swap(previous, pool["pool"])
            nxt = events(following)[0]
            assert after.base == nxt["base"]
            assert abs(after.quote - (nxt["vault_quote"] + nxt["virtual"])) <= 5

    def test_window_price_uses_the_effective_reserve_and_liquidity_stays_in_real_sol(self, pool):
        for tx in pool["txs"]:
            after = reserves_after_swap(tx, pool["pool"])
            rec = record(pool, tx)
            assert rec.price == price_of(after.base, after.quote, pool["decimals"])
            assert rec.quote_reserve == balance(tx, "postTokenBalances", pool["quote_vault"])
            assert rec.virtual_quote_reserves == after.quote - rec.quote_reserve

    def test_events_of_another_pool_are_ignored(self, pool):
        other = next(p for p in POOLS if p is not pool)
        assert reserves_after_swap(pool["txs"][0], other["pool"]) is None


class TestVirtualReserve:
    @pytest.mark.parametrize(
        "case, low, high",
        [("aporte_17_58", 17.4, 17.6), ("aporte_18_93", 18.7, 19.0), ("mayhem_virtual_negativa", -0.1, -0.000001)],
    )
    def test_is_read_signed_from_the_pool_account(self, case, low, high):
        fields = parse_pool_account(BY_CASE[case]["account"])
        assert fields["quote_vault"] == BY_CASE[case]["quote_vault"]
        assert low <= fields["virtual_quote_reserves"] / LAMPORTS <= high

    def test_accounts_older_than_the_field_count_as_zero(self):
        raw = base64.b64decode(BY_CASE["aporte_17_58"]["account"])[:245]
        assert parse_pool_account(base64.b64encode(raw).decode())["virtual_quote_reserves"] == 0

    def test_ignoring_it_understates_the_price(self):
        pool = BY_CASE["aporte_17_58"]
        errors, ratios = [], []
        for tx in pool["txs"]:
            rec = record(pool, tx)
            ratios.append(rec.price / price_of(rec.base_reserve, rec.quote_reserve, pool["decimals"]))
            errors += [quote_error(e, 0) / e["quote_amount"] for e in events(tx)]
        assert min(errors) > 0.1  # sin la virtual la fórmula falla por más del 10 %
        assert min(ratios) > Decimal("1.15")

    def test_pool_creation_has_no_event_and_uses_the_account_value(self):
        checked = 0
        for pool in POOLS:
            if pool["creacion"] is None:
                continue
            virtual = parse_pool_account(pool["account"])["virtual_quote_reserves"]
            created = record(pool, pool["creacion"], virtual)
            first = events(pool["primer_swap"])[0]
            before_first = price_of(first["base"], first["vault_quote"] + first["virtual"], pool["decimals"])
            assert created.kind == "init"
            # el pool mayhem tiene tan poco SOL que las comisiones retenidas desde la creación pesan ~1 %
            assert abs(created.price / before_first - 1) < Decimal("0.02")
            checked += 1
        assert checked >= 2

    def test_the_market_source_carries_the_latest_value_to_the_next_transaction(self):
        pool = BY_CASE["aporte_17_58"]
        migrated = datetime.fromtimestamp(pool["txs"][0]["blockTime"], UTC) - timedelta(seconds=5)
        info = PoolInfo(pool["pool"], pool["mint"], "T", "Token", 6, PUMPSWAP_PROGRAM_ID, WSOL_MINT, migrated, "s", 0, 1, True, True, True)

        class Rpc:
            def get_account_info(self, _):
                return {"data": [pool["account"], "base64"]}

        source = SolanaMarketSource(Rpc(), clock=ManualClock(migrated))
        state = source._state(info)
        assert state.virtual == parse_pool_account(pool["account"])["virtual_quote_reserves"]
        source._apply(info, state, pool["txs"][0])
        expected = reserves_after_swap(pool["txs"][0], pool["pool"])
        assert state.virtual == expected.quote - state.swaps[-1].quote_reserve
        assert state.virtual != parse_pool_account(pool["account"])["virtual_quote_reserves"]
