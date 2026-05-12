"""Pairwise compensation: redirect realized profit from a closed winner into
re-pricing the most-underwater open position's TP toward its breakeven.

Given profit X realized on one trade, we solve for a new sell price on the most
underwater open position such that its loss equals X (net), so the *portfolio*
PnL for that pair is zero:

    target_loss = X
    pnl_target = new_tp * qty * (1 - fee) - entry * qty - fees_in = -X
    new_tp = (entry * qty + fees_in - X) / (qty * (1 - fee))

Guards:
- ``new_tp <= current_price``: skip; the order would fill at market, no point
- ``new_tp >= existing_tp``: skip; the proposal is worse than what's already there
"""

from __future__ import annotations

from decimal import Decimal

from core.strategy.rounding import round_up_to_tick
from core.strategy.types import CompensationDecision, OpenPosition


def select_most_underwater(
    positions: list[OpenPosition], current_price: Decimal, maker_fee: Decimal
) -> OpenPosition | None:
    """Pick the open position with the largest unrealized loss at current price."""
    if not positions:
        return None
    fee_factor = Decimal(1) - maker_fee
    worst: OpenPosition | None = None
    worst_loss = Decimal(0)
    for p in positions:
        revenue = current_price * p.qty * fee_factor
        cost = p.entry_price * p.qty + p.fees_in
        loss = cost - revenue  # positive ⇒ underwater
        if loss > worst_loss:
            worst_loss = loss
            worst = p
    return worst


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
    raw = (target.entry_price * target.qty + target.fees_in - profit_from_other) / (
        target.qty * (Decimal(1) - maker_fee)
    )
    new_tp = round_up_to_tick(raw, tick_size)
    if new_tp <= current_price:
        return None  # would fill immediately — skip
    if new_tp >= target.current_tp_price:
        return None  # no improvement over existing TP
    return CompensationDecision(target_position_id=target.id, new_tp_price=new_tp)


def plan_compensation(
    *,
    open_positions: list[OpenPosition],
    profit_from_other: Decimal,
    maker_fee: Decimal,
    current_price: Decimal,
    tick_size: Decimal,
) -> CompensationDecision | None:
    """Convenience: pick victim and compute decision in one call."""
    victim = select_most_underwater(open_positions, current_price, maker_fee)
    if victim is None:
        return None
    return compute_compensation(
        target=victim,
        profit_from_other=profit_from_other,
        maker_fee=maker_fee,
        current_price=current_price,
        tick_size=tick_size,
    )
