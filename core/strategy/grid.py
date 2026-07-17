"""Generate descending grid buy levels (absolute or percent step)."""

from __future__ import annotations

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
