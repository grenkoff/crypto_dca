"""Decimal rounding helpers for snapping prices to the exchange tick."""

from __future__ import annotations

from decimal import ROUND_CEILING, ROUND_FLOOR, Decimal


def round_down_to_tick(price: Decimal, tick: Decimal) -> Decimal:
    """Round ``price`` down to the nearest multiple of ``tick``."""
    return (price / tick).to_integral_value(ROUND_FLOOR) * tick


def round_up_to_tick(price: Decimal, tick: Decimal) -> Decimal:
    """Round ``price`` up to the nearest multiple of ``tick``."""
    return (price / tick).to_integral_value(ROUND_CEILING) * tick


def next_tick_above(price: Decimal, tick: Decimal) -> Decimal:
    """One tick above ``price``, snapped up to the tick grid."""
    return round_up_to_tick(price + tick, tick)


def min_notional_price(
    min_order_amt: Decimal, qty: Decimal, tick: Decimal
) -> Decimal:
    """Lowest tick-aligned price whose ``price * qty`` clears the minimum."""
    return round_up_to_tick(min_order_amt / qty, tick)
