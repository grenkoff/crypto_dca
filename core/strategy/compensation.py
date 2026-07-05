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


def select_compensation_target(
    positions: list[OpenPosition], current_price: Decimal
) -> OpenPosition | None:
    """Pick the open position whose take-profit sits highest above the market.

    This is the hardest-to-fill "tail" of the bag; compensation walks it down
    toward the market first, unloading the most stranded orders. Positions whose
    TP is already at/below the market are skipped (nothing to lower into)."""
    candidates = [p for p in positions if p.current_tp_price > current_price]
    if not candidates:
        return None
    return max(candidates, key=lambda p: p.current_tp_price)


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
    victim = select_compensation_target(open_positions, current_price)
    if victim is None:
        return None
    return compute_compensation(
        target=victim,
        profit_from_other=profit_from_other,
        maker_fee=maker_fee,
        current_price=current_price,
        tick_size=tick_size,
    )
