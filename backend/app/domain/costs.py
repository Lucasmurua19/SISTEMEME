"""Costo de ida y vuelta.

``costo = (SOL total debitado - SOL neto recuperable) / SOL total debitado``

- Solo se suman los cargos que la cotización todavía no incorporó en sus montos.
- Los depósitos de cuentas de token (``refundable``) inmovilizan fondos: forman parte de lo debitado y
  vuelven en el cierre, por eso no se cuentan como costo pero se registran por separado.
"""

from __future__ import annotations

from decimal import Decimal

from .models import HUNDRED, Quote, RoundTrip


def compute_round_trip(buy: Quote, sell: Quote) -> RoundTrip:
    """Calcula el costo de ida y vuelta de una compra SOL→token y su venta token→SOL.

    La venta debe cotizarse con la cantidad mínima que la compra podría acreditar
    (``buy.min_out_amount``), de modo que el resultado sea conservador.
    """
    if not buy.ok or not sell.ok:
        raise ValueError("ambas cotizaciones deben estar disponibles para calcular el costo")

    buy_extra = buy.additional_cost_lamports()
    total_debited = buy.in_amount + buy_extra
    if total_debited <= 0:
        raise ValueError("el monto debitado debe ser positivo")

    sell_extra = sum(c.lamports for c in sell.costs if not c.included_in_amount and not c.refundable)
    refunds = sum(c.lamports for c in buy.costs if c.refundable and not c.included_in_amount)
    net_recoverable = sell.out_amount - sell_extra + refunds
    cost = total_debited - net_recoverable
    cost_pct = Decimal(cost) * HUNDRED / Decimal(total_debited)

    return RoundTrip(
        total_debited_lamports=total_debited,
        net_recoverable_lamports=net_recoverable,
        cost_lamports=cost,
        cost_pct=cost_pct,
        buy_in_lamports=buy.in_amount,
        buy_extra_costs_lamports=buy_extra,
        sell_out_lamports=sell.out_amount,
        sell_extra_costs_lamports=sell_extra,
        refunds_lamports=refunds,
        immobilized_lamports=refunds,
        tokens_expected=buy.out_amount,
        tokens_min=buy.min_out_amount,
    )


def net_exit_proceeds(sell: Quote) -> int:
    """SOL neto que una venta acreditaría: salida cotizada menos cargos no incorporados."""
    if not sell.ok:
        return 0
    extra = sum(c.lamports for c in sell.costs if not c.included_in_amount and not c.refundable)
    return max(0, sell.out_amount - extra)


def adverse_exit_proceeds(sell: Quote) -> int:
    """Escenario adverso: usa el mínimo garantizado por la tolerancia de slippage."""
    if not sell.ok:
        return 0
    extra = sum(c.lamports for c in sell.costs if not c.included_in_amount and not c.refundable)
    return max(0, sell.min_out_amount - extra)
