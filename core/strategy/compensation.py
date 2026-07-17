"""Pairwise compensation: profit pulls a near-market TP one step down.

Targets the second-nearest TP above market, bounded by a credit floor so
the pair never nets below zero; the new TP rests as a maker above market.
"""

from __future__ import annotations

from decimal import Decimal

from core.strategy.rounding import round_down_to_tick, round_up_to_tick
from core.strategy.types import CompensationDecision, OpenPosition


def _market_floor(current_price: Decimal, tick_size: Decimal) -> Decimal:
    """Lowest price we will rest a compensated sell at (one tick above
    market)."""
    return round_up_to_tick(current_price + tick_size, tick_size)


def select_compensation_target(
    positions: list[OpenPosition], current_price: Decimal, tick_size: Decimal
) -> OpenPosition | None:
    """Pick the position whose TP to pull down: second-nearest above market.

    Only TPs above the market floor are workable; the nearest fills on its
    own, so the one behind it gets the step (or the sole candidate).
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
    maker_fee: Decimal,
    current_price: Decimal,
    tick_size: Decimal,
    step: Decimal,
    min_order_amt: Decimal = Decimal(0),
) -> CompensationDecision | None:
    """Compute the compensated TP move for ``target``, or None if unfunded."""
    if profit_from_other <= 0 or step <= 0:
        return None
    new_credit = target.compensation_credit + profit_from_other

    step_target = round_down_to_tick(target.current_tp_price - step, tick_size)
    credit_floor = round_up_to_tick(
        (target.entry_price * target.qty + target.fees_in - new_credit)
        / (target.qty * (Decimal(1) - maker_fee)),
        tick_size,
    )
    floor = max(credit_floor, _market_floor(current_price, tick_size))
    if min_order_amt > 0:
        floor = max(
            floor, round_up_to_tick(min_order_amt / target.qty, tick_size)
        )

    new_tp = max(step_target, floor)
    if new_tp >= target.current_tp_price:
        return None
    return CompensationDecision(
        target_position_id=target.id,
        new_tp_price=new_tp,
        new_credit=new_credit,
    )


def plan_compensation(
    *,
    open_positions: list[OpenPosition],
    profit_from_other: Decimal,
    maker_fee: Decimal,
    current_price: Decimal,
    tick_size: Decimal,
    step: Decimal,
    min_order_amt: Decimal = Decimal(0),
) -> CompensationDecision | None:
    """Convenience: pick victim and compute decision in one call."""
    victim = select_compensation_target(
        open_positions, current_price, tick_size
    )
    if victim is None:
        return None
    return compute_compensation(
        target=victim,
        profit_from_other=profit_from_other,
        maker_fee=maker_fee,
        current_price=current_price,
        tick_size=tick_size,
        step=step,
        min_order_amt=min_order_amt,
    )
