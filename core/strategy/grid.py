"""Pick the grid rungs a resting buy band should occupy."""

from __future__ import annotations

from collections.abc import Iterable
from decimal import Decimal

from core.strategy.lattice import Lattice, nearest_rung


def held_rungs(
    entry_prices: Iterable[Decimal], lattice: Lattice
) -> set[Decimal]:
    """The rungs already covered by open positions."""
    return {nearest_rung(lattice, price) for price in entry_prices}


def resting_buy_levels(
    price: Decimal,
    lattice: Lattice,
    count: int,
    held: set[Decimal],
    ceiling: Decimal | None = None,
) -> list[tuple[int, Decimal]]:
    """The ``count`` highest rungs below ``price`` that are not held.

    Walks the lattice down from a full rung below market — a buy placed
    just under the price would fill on noise — skipping held rungs, until
    ``count`` are collected or the ladder bottoms out. A
    ``ceiling`` caps the band top: rungs above it are skipped so buys keep
    clear of the resting take-profit wall.
    """
    if price <= 0 or count <= 0:
        return []
    top = lattice.below(lattice.snap_down(price))
    if ceiling is not None:
        top = min(top, lattice.snap_down(ceiling))
    if top <= 0:
        return []
    index = lattice.index_of(top)
    levels: list[tuple[int, Decimal]] = []
    while len(levels) < count and index >= 0:
        rung = lattice.price_at(index)
        if rung <= 0:
            break
        if rung not in held:
            levels.append((index, rung))
        index -= 1
    return levels


def buys_to_prune(
    resting_prices: Iterable[Decimal],
    target_prices: set[Decimal],
    ceiling: Decimal | None = None,
) -> list[Decimal]:
    """Resting buy prices to cancel: below the band, or above the ceiling.

    Buys stranded below the deepest target redeploy near price; buys in-band
    or above (a falling market will fill them) are kept — except any above
    ``ceiling``, which crowd the take-profit wall and must clear out.
    """
    if not target_prices:
        return (
            []
            if ceiling is None
            else [p for p in resting_prices if p > ceiling]
        )
    bottom = min(target_prices)
    return [
        p
        for p in resting_prices
        if p < bottom or (ceiling is not None and p > ceiling)
    ]


def fundable_targets(
    targets: list[tuple[int, Decimal]],
    covered: set[Decimal],
    budget: Decimal,
    per_order: Decimal,
) -> list[tuple[int, Decimal]]:
    """Targets to actually place this cycle, capped by free budget.

    Skips prices already covered (resting or held); places nearest-market
    first and stops once the remaining free budget can't fund another
    ``per_order`` — the deeper levels wait for capital to free up next cycle.
    """
    if per_order <= 0:
        return []
    out: list[tuple[int, Decimal]] = []
    remaining = budget
    for k, p in targets:
        if p in covered:
            continue
        if remaining < per_order:
            break
        out.append((k, p))
        remaining -= per_order
    return out
