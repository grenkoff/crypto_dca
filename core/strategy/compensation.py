"""Pairwise compensation: each realized profit pulls a near-market take-profit
one ``tp_step`` closer to the market so it fills sooner and frees capital.

Target selection: the **second-nearest** TP above the market. The nearest one is
likely to fill on its own; helping the next one down releases locked capital
soonest. (With a single candidate, that one is taken.) Several TPs may end up
stacked on the same price level — that is intended; holes left higher up are not
re-seeded.

Each application lowers the target's TP by exactly one ``tp_step`` and books the
triggering profit into ``compensation_credit`` (the fund that covers the loss
when the pulled-down TP eventually fills below break-even).

The re-priced TP always rests as a maker limit: it is floored one tick above the
market (never crosses into a taker fill) and at ``min_order_amt / qty`` so the
exchange accepts it.
"""

from __future__ import annotations

from decimal import Decimal

from core.strategy.rounding import round_down_to_tick, round_up_to_tick
from core.strategy.types import CompensationDecision, OpenPosition


def _market_floor(current_price: Decimal, tick_size: Decimal) -> Decimal:
    """Lowest price we will rest a compensated sell at (one tick above market)."""
    return round_up_to_tick(current_price + tick_size, tick_size)


def select_compensation_target(
    positions: list[OpenPosition], current_price: Decimal, tick_size: Decimal
) -> OpenPosition | None:
    """Pick the position whose TP to pull down: second-nearest above the market.

    Only TPs strictly above the market floor are workable. The nearest is left to
    fill naturally; the one behind it gets the step down. With a single candidate,
    take it.
    """
    floor = _market_floor(current_price, tick_size)
    candidates = sorted(
        (p for p in positions if p.current_tp_price > floor),
        key=lambda p: p.current_tp_price,
    )
    if not candidates:
        return None
    return candidates[1] if len(candidates) > 1 else candidates[0]


def compute_compensation(
    *,
    target: OpenPosition,
    profit_from_other: Decimal,
    maker_fee: Decimal,  # kept for call-site symmetry; step size no longer depends on fees
    current_price: Decimal,
    tick_size: Decimal,
    tp_step: Decimal,
    min_order_amt: Decimal = Decimal(0),
) -> CompensationDecision | None:
    if profit_from_other <= 0 or tp_step <= 0:
        return None
    new_credit = target.compensation_credit + profit_from_other
    new_tp = round_down_to_tick(target.current_tp_price - tp_step, tick_size)
    floor = _market_floor(current_price, tick_size)
    if min_order_amt > 0:
        # The sell must clear the exchange's minimum notional to be accepted.
        floor = max(floor, round_up_to_tick(min_order_amt / target.qty, tick_size))
    if new_tp < floor:
        new_tp = floor  # rest as a maker order; never cross into a taker fill
    if new_tp >= target.current_tp_price:
        return None  # already at the floor — no improvement possible
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
    tp_step: Decimal,
    min_order_amt: Decimal = Decimal(0),
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
        tp_step=tp_step,
        min_order_amt=min_order_amt,
    )
