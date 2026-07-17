"""Take-profit pricing: an absolute ``tp_step`` above entry.

The TP is the higher of ``entry + tp_step`` and the break-even/min-profit
price, rounded up to ``tick_size`` and lifted so the sell clears
``min_order_amt``. The position is never sold below break-even.
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
    min_order_amt: Decimal = Decimal(0),
) -> Decimal:
    """Take-profit price for a lot: absolute target above the floors."""
    if qty <= 0:
        raise ValueError("qty must be positive")
    if maker_fee >= 1:
        raise ValueError("maker_fee must be < 1")

    target = entry_price + tp_step
    minimum = (min_profit_quote + entry_price * qty + fees_in) / (
        qty * (Decimal(1) - maker_fee)
    )
    chosen = max(target, minimum)
    if chosen * qty < min_order_amt:
        chosen = min_order_amt / qty
    return round_up_to_tick(chosen, tick_size)
