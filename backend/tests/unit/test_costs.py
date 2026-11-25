from decimal import Decimal

import pytest

from app.domain.costs import adverse_exit_proceeds, compute_round_trip, net_exit_proceeds
from app.domain.models import QuoteCost, QuoteStatus
from tests.helpers import T0, make_quote


def test_readme_example_cost_is_four_percent_and_pnl_is_plus_8_milli_sol():
    # Se debitan 0,100 SOL y la venta inmediata devolvería 0,096 SOL netos → costo 4 %.
    buy = make_quote("buy", in_amount=100_000_000, out_amount=1_000_000)
    sell = make_quote("sell", in_amount=1_000_000, out_amount=96_000_000)
    rt = compute_round_trip(buy, sell)
    assert rt.total_debited_lamports == 100_000_000
    assert rt.net_recoverable_lamports == 96_000_000
    assert rt.cost_pct == Decimal(4)
    # Si más tarde se cierra recuperando 0,108 SOL netos, el PnL realizado es +0,008 SOL.
    assert 108_000_000 - rt.total_debited_lamports == 8_000_000


def test_only_costs_not_already_in_quote_are_added():
    buy = make_quote(
        "buy",
        in_amount=100_000_000,
        out_amount=1_000_000,
        costs=(
            QuoteCost("protocol", 300_000, included_in_amount=True),  # ya descontado del monto
            QuoteCost("priority", 100_000),
            QuoteCost("tip", 50_000),
        ),
    )
    sell = make_quote(
        "sell",
        in_amount=1_000_000,
        out_amount=99_000_000,
        costs=(QuoteCost("priority", 100_000),),
    )
    rt = compute_round_trip(buy, sell)
    assert rt.buy_extra_costs_lamports == 150_000
    assert rt.total_debited_lamports == 100_150_000
    assert rt.sell_extra_costs_lamports == 100_000
    assert rt.net_recoverable_lamports == 98_900_000
    assert rt.cost_lamports == 100_150_000 - 98_900_000


def test_refundable_ata_deposit_is_immobilized_not_a_cost():
    deposit = 2_039_280
    buy = make_quote(
        "buy",
        in_amount=100_000_000,
        out_amount=1_000_000,
        costs=(QuoteCost("ata_deposit", deposit, refundable=True),),
    )
    sell = make_quote("sell", in_amount=1_000_000, out_amount=100_000_000)
    rt = compute_round_trip(buy, sell)
    assert rt.total_debited_lamports == 100_000_000 + deposit
    assert rt.immobilized_lamports == deposit
    assert rt.cost_lamports == 0


def test_requires_both_quotes():
    buy = make_quote("buy", in_amount=1, out_amount=1)
    sell = make_quote("sell", in_amount=1, out_amount=0, status=QuoteStatus.NO_ROUTE)
    with pytest.raises(ValueError):
        compute_round_trip(buy, sell)


def test_net_and_adverse_exit_proceeds():
    sell = make_quote(
        "sell",
        in_amount=1_000,
        out_amount=100_000,
        min_out=97_000,
        costs=(QuoteCost("priority", 5_000),),
        observed_at=T0,
    )
    assert net_exit_proceeds(sell) == 95_000
    assert adverse_exit_proceeds(sell) == 92_000
    dead = make_quote("sell", in_amount=1_000, out_amount=0, status=QuoteStatus.NO_ROUTE)
    assert net_exit_proceeds(dead) == 0
