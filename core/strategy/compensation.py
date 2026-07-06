"""Pairwise compensation: redirect realized profit from closed winners into
walking the *tail* of the bag (the orders parked highest above the market) down
toward the market so they eventually fill.

Profit is **accumulated per position** in ``compensation_credit``. Given total
credit C already applied plus a fresh profit X, the target's TP is re-priced so
its realized loss equals C+X:

    new_tp = (entry * qty + fees_in - (C + X)) / (qty * (1 - fee))

As credit grows the TP steps down; once it would drop to/through the market it
is capped one tick above the market so the order rests and fills. Selection is
"sticky": we keep funnelling profit into the position already in progress until
it can go no lower, then start the next-highest tail order.

The real per-pair outcome is ``(entry_estimate - true_cost) * qty`` regardless of
how far the TP is walked — so an over-estimated entry keeps every close safe.
"""

from __future__ import annotations

from decimal import Decimal

from core.strategy.rounding import round_up_to_tick
from core.strategy.types import CompensationDecision, OpenPosition


def _market_floor(current_price: Decimal, tick_size: Decimal) -> Decimal:
    """Lowest price we will rest a compensated sell at (one tick above market)."""
    return round_up_to_tick(current_price + tick_size, tick_size)


def select_compensation_target(
    positions: list[OpenPosition], current_price: Decimal, tick_size: Decimal
) -> OpenPosition | None:
    """Pick which bag position to work down next.

    Only positions whose TP still sits above the market floor are workable. Among
    those we stick to the one already in progress (largest accumulated credit);
    with none in progress we start the highest-TP tail order.
    """
    floor = _market_floor(current_price, tick_size)
    workable = [p for p in positions if p.current_tp_price > floor]
    if not workable:
        return None
    in_progress = [p for p in workable if p.compensation_credit > 0]
    if in_progress:
        return max(in_progress, key=lambda p: p.compensation_credit)
    return max(workable, key=lambda p: p.current_tp_price)


def compute_compensation(
    *,
    target: OpenPosition,
    profit_from_other: Decimal,
    maker_fee: Decimal,
    current_price: Decimal,
    tick_size: Decimal,
) -> CompensationDecision | None:
    if profit_from_other <= 0:
        return None
    new_credit = target.compensation_credit + profit_from_other
    raw = (target.entry_price * target.qty + target.fees_in - new_credit) / (
        target.qty * (Decimal(1) - maker_fee)
    )
    new_tp = round_up_to_tick(raw, tick_size)
    floor = _market_floor(current_price, tick_size)
    if new_tp < floor:
        new_tp = floor  # rest just above market so it fills; never cross into a taker
    if new_tp >= target.current_tp_price:
        return None  # already at/below this price — no improvement
    return CompensationDecision(
        target_position_id=target.id, new_tp_price=new_tp, new_credit=new_credit
    )


def plan_compensation(
    *,
    open_positions: list[OpenPosition],
    profit_from_other: Decimal,
    maker_fee: Decimal,
    current_price: Decimal,
    tick_size: Decimal,
) -> CompensationDecision | None:
    """Convenience: pick victim and compute decision in one call."""
    victim = select_compensation_target(open_positions, current_price, tick_size)
    if victim is None:
        return None
    return compute_compensation(
        target=victim,
        profit_from_other=profit_from_other,
        maker_fee=maker_fee,
        current_price=current_price,
        tick_size=tick_size,
    )
