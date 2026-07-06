from __future__ import annotations

from decimal import Decimal

import pytest

from core.strategy.grid import generate_levels


def test_absolute_grid_descending() -> None:
    levels = generate_levels(
        top_anchor=Decimal("60000"),
        mode="absolute",
        step=Decimal("100"),
        count=5,
        tick_size=Decimal("0.01"),
    )
    prices = [lv.price for lv in levels]
    assert prices == [
        Decimal("60000"),
        Decimal("59900"),
        Decimal("59800"),
        Decimal("59700"),
        Decimal("59600"),
    ]
    assert [lv.level_index for lv in levels] == [0, 1, 2, 3, 4]


def test_absolute_grid_snaps_anchor_to_round_prices() -> None:
    # Anchor 0.03118 with step 0.0001 snaps down to 0.03110, then round levels.
    levels = generate_levels(
        top_anchor=Decimal("0.03118"),
        mode="absolute",
        step=Decimal("0.0001"),
        count=4,
        tick_size=Decimal("0.00001"),
    )
    assert [lv.price for lv in levels] == [
        Decimal("0.03110"),
        Decimal("0.03100"),
        Decimal("0.03090"),
        Decimal("0.03080"),
    ]


def test_percent_grid_descending() -> None:
    levels = generate_levels(
        top_anchor=Decimal("60000"),
        mode="percent",
        step=Decimal("0.01"),  # 1%
        count=3,
        tick_size=Decimal("0.01"),
    )
    # 60000, 60000*0.99=59400, 60000*0.99^2 = 58806
    assert levels[0].price == Decimal("60000")
    assert levels[1].price == Decimal("59400")
    assert levels[2].price == Decimal("58806")


def test_absolute_grid_stops_at_zero() -> None:
    levels = generate_levels(
        top_anchor=Decimal("500"),
        mode="absolute",
        step=Decimal("100"),
        count=10,
        tick_size=Decimal("0.01"),
    )
    # 500, 400, 300, 200, 100 — next would be 0 → stop
    assert len(levels) == 5
    assert levels[-1].price == Decimal("100")


def test_percent_grid_floors_to_tick() -> None:
    levels = generate_levels(
        top_anchor=Decimal("60000"),
        mode="percent",
        step=Decimal("0.005"),
        count=2,
        tick_size=Decimal("1"),  # whole-dollar ticks
    )
    # 60000, 60000*0.995=59700 (rounded down to 59700 since tick=1)
    assert levels[0].price == Decimal("60000")
    assert levels[1].price == Decimal("59700")


def test_invalid_step_raises() -> None:
    with pytest.raises(ValueError):
        generate_levels(
            top_anchor=Decimal("60000"),
            mode="percent",
            step=Decimal("0"),
            count=1,
            tick_size=Decimal("0.01"),
        )
    with pytest.raises(ValueError):
        generate_levels(
            top_anchor=Decimal("60000"),
            mode="percent",
            step=Decimal("1.5"),
            count=1,
            tick_size=Decimal("0.01"),
        )


def test_count_zero() -> None:
    levels = generate_levels(
        top_anchor=Decimal("60000"),
        mode="absolute",
        step=Decimal("100"),
        count=0,
        tick_size=Decimal("0.01"),
    )
    assert levels == []
