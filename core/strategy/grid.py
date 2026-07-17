"""Generate descending grid buy levels (absolute or percent step)."""

from __future__ import annotations

from collections.abc import Iterable
from decimal import Decimal

from core.strategy.rounding import round_down_to_tick
from core.strategy.types import GridLevelSpec, GridMode


def generate_levels(
    *,
    top_anchor: Decimal,
    mode: GridMode,
    step: Decimal,
    count: int,
    tick_size: Decimal,
) -> list[GridLevelSpec]:
    """Generate up to ``count`` buy levels descending from ``top_anchor``.

    ``percent`` step is a fraction ((1 - step) per level); ``absolute`` is a
    price delta off a step-snapped anchor. Prices floor to ``tick_size``;
    generation stops once a level would be non-positive.
    """
    if count <= 0:
        return []
    if step <= 0:
        raise ValueError("step must be positive")
    if mode == "percent" and step >= 1:
        raise ValueError("percent step must be < 1")
    if top_anchor <= 0:
        return []

    if mode == "absolute":
        top_anchor = round_down_to_tick(top_anchor, step)

    levels: list[GridLevelSpec] = []
    for i in range(count):
        raw = (
            top_anchor * (Decimal(1) - step) ** i
            if mode == "percent"
            else top_anchor - step * i
        )
        price = round_down_to_tick(raw, tick_size)
        if price <= 0:
            break
        levels.append(GridLevelSpec(level_index=i, price=price))
    return levels


def resting_buy_levels(
    price: Decimal, step: Decimal, count: int, held: set[Decimal]
) -> list[tuple[int, Decimal]]:
    """The ``count`` highest step-aligned prices below ``price`` not held.

    Walks round levels down from a full step below market, skipping held
    levels, until ``count`` are collected or price reaches zero.
    """
    if step <= 0 or price <= 0 or count <= 0:
        return []
    k_floor = int(price / step)
    if Decimal(k_floor) * step > price:
        k_floor -= 1
    k_top = k_floor - 1
    levels: list[tuple[int, Decimal]] = []
    k = k_top
    while len(levels) < count:
        p = Decimal(k) * step
        if p <= 0:
            break
        if p not in held:
            levels.append((k, p))
        k -= 1
    return levels


def buys_to_prune(
    resting_prices: Iterable[Decimal], target_prices: set[Decimal]
) -> list[Decimal]:
    """Resting buy prices to cancel: only those below the band bottom.

    Buys stranded below the deepest target redeploy near price; buys in-band
    or above (a falling market will fill them) are kept.
    """
    if not target_prices:
        return []
    bottom = min(target_prices)
    return [p for p in resting_prices if p < bottom]
