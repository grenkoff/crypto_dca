"""Take-profit pricing.

The take-profit is an **absolute** offset above entry (``tp_step``, a plain price
delta in quote units), independent of the grid spacing. Net PnL for a long
position closed by a limit sell is:

    pnl = sell_price * qty * (1 - sell_fee) - entry_price * qty - fees_in

The chosen TP is the higher of ``entry + tp_step`` and the price that still nets
``min_profit_quote`` (a break-even floor when ``min_profit_quote == 0``), rounded
up to ``tick_size``. That way the absolute target is used whenever it clears the
floor, and the position is never sold below break-even.
"""

from __future__ import annotations

from decimal import Decimal

from core.strategy.rounding import round_up_to_tick


def compute_tp_price(
    *,
    entry_price: Decimal,
    qty: Decimal,
    fees_in: Decimal,
    tp_step: Decimal,
    min_profit_quote: Decimal,
    maker_fee: Decimal,
    tick_size: Decimal,
) -> Decimal:
    if qty <= 0:
        raise ValueError("qty must be positive")
    if maker_fee >= 1:
        raise ValueError("maker_fee must be < 1")

    target = entry_price + tp_step
    minimum = (min_profit_quote + entry_price * qty + fees_in) / (qty * (Decimal(1) - maker_fee))
    chosen = target if target >= minimum else minimum
    return round_up_to_tick(chosen, tick_size)
