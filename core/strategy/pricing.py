"""Take-profit pricing.

Net PnL for a long position closed by a limit sell:

    pnl = sell_price * qty * (1 - sell_fee) - entry_price * qty - fees_in

We need pnl >= min_profit_quote, which solves to:

    sell_price >= (min_profit_quote + entry_price * qty + fees_in) / (qty * (1 - sell_fee))

The chosen TP is the higher of the grid-step target and that minimum, rounded up
to ``tick_size``.
"""

from __future__ import annotations

from decimal import Decimal

from core.strategy.rounding import round_up_to_tick
from core.strategy.types import GridMode


def compute_tp_price(
    *,
    entry_price: Decimal,
    qty: Decimal,
    fees_in: Decimal,
    mode: GridMode,
    step: Decimal,
    min_profit_quote: Decimal,
    maker_fee: Decimal,
    tick_size: Decimal,
) -> Decimal:
    if qty <= 0:
        raise ValueError("qty must be positive")
    if maker_fee >= 1:
        raise ValueError("maker_fee must be < 1")

    target = entry_price * (Decimal(1) + step) if mode == "percent" else entry_price + step
    minimum = (min_profit_quote + entry_price * qty + fees_in) / (qty * (Decimal(1) - maker_fee))
    chosen = target if target >= minimum else minimum
    return round_up_to_tick(chosen, tick_size)
