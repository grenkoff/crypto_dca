"""Take-profit grid compaction: pull TPs down onto empty grid slots.

On each profitable close the profit is banked into a credit pool. One TP —
the nearest-to-market one that has an empty ``grid_step`` slot directly
below it — descends into that slot, funded so the compensated pair stays
strictly in profit; otherwise the profit stays banked until it can. This
compacts the TP wall toward market with no gaps and no off-lattice orders,
its bottom resting one ``tp_step`` above the nearest buy.
"""

from __future__ import annotations

from decimal import Decimal

from core.strategy.rounding import (
    min_notional_price,
    next_tick_above,
    round_down_to_tick,
)
from core.strategy.types import (
    CompensationContext,
    CompensationDecision,
    OpenPosition,
)

_PROFIT_EPS = Decimal("1E-10")


def slot_below(tp_price: Decimal, grid_step: Decimal) -> Decimal:
    """The nearest ``grid_step`` level strictly below ``tp_price``.

    An on-grid price steps down a full ``grid_step``; an off-grid price
    snaps down to its grid level, pulling a stray TP back onto the lattice.
    """
    snapped = round_down_to_tick(tp_price, grid_step)
    if snapped < tp_price:
        return snapped
    return tp_price - grid_step


def plan_compensation(
    open_positions: list[OpenPosition], ctx: CompensationContext
) -> CompensationDecision | None:
    """Plan the next TP compaction move, or None to keep banking the pool.

    Picks the nearest-to-market TP whose grid slot directly below is empty
    and at or above the wall floor (``nearest_buy + tp_step``, market, min
    notional), then moves it there if the pool funds a strictly-positive
    pair. If that nearest gap can't be funded yet, returns None so the
    profit keeps accumulating.
    """
    if ctx.pool <= 0 or ctx.grid_step <= 0 or not open_positions:
        return None

    market_floor = next_tick_above(ctx.current_price, ctx.tick_size)
    wall_floor = (
        ctx.nearest_buy_price + ctx.tp_step
        if ctx.nearest_buy_price > 0
        else market_floor
    )
    floor = max(market_floor, wall_floor)

    occupied = {p.current_tp_price for p in open_positions}
    for victim in sorted(open_positions, key=lambda p: p.current_tp_price):
        if victim.current_tp_price <= floor:
            continue
        target = slot_below(victim.current_tp_price, ctx.grid_step)
        if target in occupied:
            continue
        victim_floor = floor
        if ctx.min_order_amt > 0:
            victim_floor = max(
                victim_floor,
                min_notional_price(
                    ctx.min_order_amt, victim.qty, ctx.tick_size
                ),
            )
        if target < victim_floor:
            continue
        realized = (
            target * victim.qty * (Decimal(1) - ctx.maker_fee)
            - victim.entry_price * victim.qty
            - victim.fees_in
        )
        pair = realized + victim.compensation_credit
        draw = Decimal(0) if pair > 0 else (-pair + _PROFIT_EPS)
        if draw > ctx.pool:
            return None
        return CompensationDecision(
            target_position_id=victim.id,
            new_tp_price=target,
            new_credit=victim.compensation_credit + draw,
            credit_drawn=draw,
        )
    return None
