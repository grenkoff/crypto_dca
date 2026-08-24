"""Take-profit grid compaction: pull TPs down onto empty grid slots.

On each profitable close the profit is banked into a credit pool. One TP —
the nearest-to-market one that has an empty ``grid_step`` slot directly
below it — descends into that slot, funded so the compensated pair stays
strictly in profit; otherwise the profit stays banked until it can. This
compacts the TP wall toward market with no gaps and no off-lattice orders,
its bottom resting ``tp_step + grid_step`` above the nearest buy (a filled
buy's TP sits ``tp_step`` above it, and the next resting buy is one
``grid_step`` lower).
"""

from __future__ import annotations

from collections.abc import Sequence
from decimal import ROUND_DOWN, Decimal

from core.strategy.rounding import (
    min_notional_price,
    next_tick_above,
    round_down_to_tick,
    round_up_to_tick,
)
from core.strategy.types import (
    CompensationContext,
    CompensationDecision,
    OpenPosition,
)

_PROFIT_EPS = Decimal("1E-10")
_SPLIT_SCALE = Decimal("1E-12")


def account_load(
    positions: Sequence[OpenPosition],
    *,
    quote_total: Decimal,
    base_total: Decimal,
    price: Decimal,
    maker_fee: Decimal,
) -> Decimal:
    """Share of the account tied up in open lots, 0 when it is idle.

    Value is measured the way the funds line is: cash, every lot at the
    take-profit it currently carries, and base coin held outside any lot.
    """
    locked = Decimal(0)
    held = Decimal(0)
    exit_value = Decimal(0)
    for lot in positions:
        remaining = max(lot.qty - lot.filled_qty, Decimal(0))
        locked += lot.entry_price * lot.qty + lot.fees_in
        held += remaining
        exit_value += remaining * lot.current_tp_price
    spare = max(base_total - held, Decimal(0))
    funds = quote_total + exit_value * (Decimal(1) - maker_fee) + spare * price
    if funds <= 0:
        return Decimal(0)
    return locked / funds


def compensation_share(
    load_ratio: Decimal, *, low: Decimal, high: Decimal
) -> Decimal:
    """Share of a close's profit that may fund compensation.

    The share tracks how loaded the account is, but never leaves the
    ``low``..``high`` band: some profit always compensates, and some
    always stays in the pocket.
    """
    if high <= low:
        return max(high, Decimal(0))
    return min(max(load_ratio, low), high)


def split_profit(profit: Decimal, share: Decimal) -> tuple[Decimal, Decimal]:
    """Split a close's profit into (compensation budget, pocket).

    The budget rounds down to the stored scale and the pocket takes the
    remainder, so the two halves always add back to ``profit`` exactly.
    """
    if profit <= 0:
        return Decimal(0), profit
    budget = (profit * share).quantize(_SPLIT_SCALE, rounding=ROUND_DOWN)
    budget = min(max(budget, Decimal(0)), profit)
    return budget, profit - budget


def slot_below(tp_price: Decimal, grid_step: Decimal) -> Decimal:
    """The nearest ``grid_step`` level strictly below ``tp_price``.

    An on-grid price steps down a full ``grid_step``; an off-grid price
    snaps down to its grid level, pulling a stray TP back onto the lattice.
    """
    snapped = round_down_to_tick(tp_price, grid_step)
    if snapped < tp_price:
        return snapped
    return tp_price - grid_step


def plan_hole_fill(
    open_positions: Sequence[OpenPosition],
    ctx: CompensationContext,
    *,
    offset: Decimal = Decimal(0),
) -> CompensationDecision | None:
    """Fill the gap nearest market with the dearest lot the pool affords.

    The ordinary move always drops the nearest take-profit one slot, and
    that step is usually already profitable, so the pool never gets
    spent. This picks the target first — the empty grid slot closest to
    market — and then asks which stranded lot the pool can afford to move
    into it, preferring the one that uses the pool most fully.

    ``offset`` holds the search that fraction of the price above market,
    so a lot lands near the price rather than on top of it and is not
    sold into an immediate loss.
    """
    if ctx.pool <= 0 or ctx.grid_step <= 0 or not open_positions:
        return None
    occupied = {p.current_tp_price for p in open_positions}
    hole = _nearest_hole(ctx, occupied, offset)
    if hole is None:
        return None
    best: CompensationDecision | None = None
    spent = Decimal(-1)
    for lot in open_positions:
        if lot.filled_qty > 0 or lot.current_tp_price <= hole:
            continue
        if ctx.min_order_amt > 0 and hole * lot.qty < ctx.min_order_amt:
            continue
        realized = (
            hole * lot.qty * (Decimal(1) - ctx.maker_fee)
            - lot.entry_price * lot.qty
            - lot.fees_in
        )
        pair = realized + lot.compensation_credit
        draw = Decimal(0) if pair > 0 else (-pair + _PROFIT_EPS)
        if draw > ctx.pool or draw <= spent:
            continue
        spent = draw
        best = CompensationDecision(
            target_position_id=lot.id,
            new_tp_price=hole,
            new_credit=lot.compensation_credit + draw,
            credit_drawn=draw,
        )
    return best


def _nearest_hole(
    ctx: CompensationContext,
    occupied: set[Decimal],
    offset: Decimal = Decimal(0),
) -> Decimal | None:
    """The empty grid slot closest to market, or None if the wall is solid."""
    floor = _wall_floor(ctx) + ctx.current_price * max(offset, Decimal(0))
    slot = round_up_to_tick(floor, ctx.grid_step)
    highest = max(occupied) if occupied else slot
    while slot in occupied:
        slot += ctx.grid_step
        if slot > highest:
            return None
    return slot


def _wall_floor(ctx: CompensationContext) -> Decimal:
    """Lowest price a resting take-profit may be moved onto."""
    market_floor = next_tick_above(ctx.current_price, ctx.tick_size)
    if ctx.nearest_buy_price <= 0:
        return market_floor
    return max(
        market_floor, ctx.nearest_buy_price + ctx.tp_step + ctx.grid_step
    )


def plan_compensation(
    open_positions: list[OpenPosition], ctx: CompensationContext
) -> CompensationDecision | None:
    """Plan the next TP compaction move, or None to keep banking the pool.

    Picks the nearest-to-market TP whose grid slot directly below is empty
    and at or above the wall floor (``nearest_buy + tp_step + grid_step``,
    market, min notional), then moves it there if the pool funds a
    strictly-positive pair. If that nearest gap can't be funded yet, returns
    None so the profit keeps accumulating.
    """
    if ctx.pool <= 0 or ctx.grid_step <= 0 or not open_positions:
        return None

    market_floor = next_tick_above(ctx.current_price, ctx.tick_size)
    wall_floor = (
        ctx.nearest_buy_price + ctx.tp_step + ctx.grid_step
        if ctx.nearest_buy_price > 0
        else market_floor
    )
    floor = max(market_floor, wall_floor)

    occupied = {p.current_tp_price for p in open_positions}
    for victim in sorted(open_positions, key=lambda p: p.current_tp_price):
        if victim.filled_qty > 0:
            continue
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
