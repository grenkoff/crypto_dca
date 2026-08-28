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
    next_tick_above,
    round_down_to_tick,
    round_up_to_tick,
)
from core.strategy.types import (
    CompensationContext,
    CompensationDecision,
    MarketExitDecision,
    OpenPosition,
)

_PROFIT_EPS = Decimal("1E-10")
_SPLIT_SCALE = Decimal("1E-12")
_MAX_HOLES_SCANNED = 40
_MAX_EXITS_SCANNED = 40


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


def plan_market_exit(
    open_positions: Sequence[OpenPosition], ctx: CompensationContext
) -> MarketExitDecision | None:
    """Retire the take-profit furthest from market, funded by the pool.

    A lot stranded far above market never sells and its capital is dead;
    selling it outright turns that back into working cash. The exchange
    minimum applies to that sale, so a lot too small to meet it alone
    leaves paired with the smallest lot that closes the gap and that the
    pool can still afford.
    """
    if ctx.pool <= 0 or ctx.current_price <= 0:
        return None
    lots = [lot for lot in open_positions if lot.filled_qty == 0]
    ranked = sorted(lots, key=lambda lot: lot.current_tp_price, reverse=True)
    for candidate in ranked[:_MAX_EXITS_SCANNED]:
        plan = _exit_for(candidate, lots, ctx)
        if plan is not None:
            return plan
    return None


def _exit_for(
    top: OpenPosition,
    lots: Sequence[OpenPosition],
    ctx: CompensationContext,
) -> MarketExitDecision | None:
    """Retire ``top`` alone or with a partner, if the pool can pay.

    A lot that would sell at market for a profit is not stranded — it is
    simply waiting for its own take-profit, and retiring it would throw
    that away. Only a lot the pool has to rescue is a candidate.
    """
    draw = _exit_draw((top,), ctx)
    if draw <= 0:
        return None
    if _market_value(top, ctx) >= ctx.min_order_amt:
        return _exit_decision((top,), draw) if draw <= ctx.pool else None
    return _paired_exit(top, lots, ctx)


def _paired_exit(
    top: OpenPosition,
    lots: Sequence[OpenPosition],
    ctx: CompensationContext,
) -> MarketExitDecision | None:
    """Smallest partner that clears the minimum and that the pool covers.

    Smallest first keeps the collateral damage down: a big healthy lot
    would have sold at its own take-profit for a profit, so it is the
    last thing that should be spent making up an order size.
    """
    top_value = _market_value(top, ctx)
    partners = sorted(
        (lot for lot in lots if lot.id != top.id),
        key=lambda lot: _market_value(lot, ctx),
    )
    for partner in partners:
        if top_value + _market_value(partner, ctx) < ctx.min_order_amt:
            continue
        draw = _exit_draw((top, partner), ctx)
        if draw <= ctx.pool:
            return _exit_decision((top, partner), draw)
    return None


def _market_value(lot: OpenPosition, ctx: CompensationContext) -> Decimal:
    """What the lot's coins fetch at the current price, before fees."""
    return lot.qty * ctx.current_price


def _exit_draw(
    lots: tuple[OpenPosition, ...], ctx: CompensationContext
) -> Decimal:
    """Pool needed to keep a market sale of ``lots`` out of loss."""
    pair = sum(
        (
            lot.qty * ctx.current_price * (Decimal(1) - ctx.taker_fee)
            - lot.entry_price * lot.qty
            - lot.fees_in
            + lot.compensation_credit
            for lot in lots
        ),
        Decimal(0),
    )
    return Decimal(0) if pair > 0 else -pair + _PROFIT_EPS


def _exit_decision(
    lots: tuple[OpenPosition, ...], draw: Decimal
) -> MarketExitDecision:
    """Bundle ``lots`` into one sale charged ``draw`` against the pool."""
    return MarketExitDecision(
        position_ids=tuple(lot.id for lot in lots),
        qty=sum((lot.qty for lot in lots), Decimal(0)),
        credit_drawn=draw,
    )


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
    """Fill the gap nearest market with the lot stranded furthest away.

    Walking the wall down slot by slot costs exactly what one long move
    costs — five lots giving up one step each is the same USDT as one lot
    giving up five — but it spends a cancel and a place per step. So the
    gap is filled in a single move by the highest take-profit that fits,
    which leaves the same wall behind for a fifth of the traffic.

    ``offset`` holds the search that fraction of the price above market,
    so a lot lands near the price rather than on top of it and is not
    sold into an immediate loss.
    """
    if ctx.pool <= 0 or ctx.grid_step <= 0 or not open_positions:
        return None
    occupied = {p.current_tp_price for p in open_positions}
    for hole in _holes(ctx, occupied, offset):
        decision = _fill_one(open_positions, ctx, hole)
        if decision is not None:
            return decision
    return _step_down(open_positions, ctx, occupied, offset)


def _step_down(
    open_positions: Sequence[OpenPosition],
    ctx: CompensationContext,
    occupied: set[Decimal],
    offset: Decimal,
) -> CompensationDecision | None:
    """Nudge the nearest lot one slot, when no long move is affordable.

    Reaching the gap by the market can cost more than the pool holds. A
    single step down is the cheap fallback: it compacts the wall a little
    instead of leaving the profit unspent.
    """
    floor = _wall_floor(ctx) + ctx.current_price * max(offset, Decimal(0))
    for lot in sorted(open_positions, key=lambda p: p.current_tp_price):
        if lot.filled_qty > 0:
            continue
        target = slot_below(lot.current_tp_price, ctx.grid_step)
        if target in occupied or target < floor:
            continue
        if ctx.min_order_amt > 0 and target * lot.qty < ctx.min_order_amt:
            continue
        realized = (
            target * lot.qty * (Decimal(1) - ctx.maker_fee)
            - lot.entry_price * lot.qty
            - lot.fees_in
        )
        pair = realized + lot.compensation_credit
        draw = Decimal(0) if pair > 0 else (-pair + _PROFIT_EPS)
        if draw > ctx.pool:
            return None
        return CompensationDecision(
            target_position_id=lot.id,
            new_tp_price=target,
            new_credit=lot.compensation_credit + draw,
            credit_drawn=draw,
        )
    return None


def _fill_one(
    open_positions: Sequence[OpenPosition],
    ctx: CompensationContext,
    hole: Decimal,
) -> CompensationDecision | None:
    """The furthest lot the pool can afford to drop into ``hole``."""
    best: OpenPosition | None = None
    best_draw = Decimal(0)
    for lot in open_positions:
        if lot.filled_qty > 0 or lot.current_tp_price <= hole:
            continue
        if best is not None and lot.current_tp_price <= best.current_tp_price:
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
        if draw > ctx.pool:
            continue
        best, best_draw = lot, draw
    if best is None:
        return None
    return CompensationDecision(
        target_position_id=best.id,
        new_tp_price=hole,
        new_credit=best.compensation_credit + best_draw,
        credit_drawn=best_draw,
    )


def _holes(
    ctx: CompensationContext, occupied: set[Decimal], offset: Decimal
) -> list[Decimal]:
    """Empty grid slots from market upward, nearest first.

    The nearest gap is the most useful one to fill, but reaching it can
    cost more than the pool holds; the ones above it are cheaper, so the
    planner walks outward until it finds a move it can pay for.
    """
    floor = _wall_floor(ctx) + ctx.current_price * max(offset, Decimal(0))
    slot = round_up_to_tick(floor, ctx.grid_step)
    highest = max(occupied) if occupied else slot
    found: list[Decimal] = []
    while slot <= highest and len(found) < _MAX_HOLES_SCANNED:
        if slot not in occupied:
            found.append(slot)
        slot += ctx.grid_step
    return found


def _wall_floor(ctx: CompensationContext) -> Decimal:
    """Lowest price a resting take-profit may be moved onto."""
    market_floor = next_tick_above(ctx.current_price, ctx.tick_size)
    if ctx.nearest_buy_price <= 0:
        return market_floor
    return max(
        market_floor, ctx.nearest_buy_price + ctx.tp_step + ctx.grid_step
    )
