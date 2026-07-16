from __future__ import annotations

from decimal import Decimal

import pytest

from core.strategy.rounding import round_down_to_tick, round_up_to_tick


@pytest.mark.parametrize(
    ("price", "tick", "expected"),
    [
        ("60050.127", "0.01", "60050.12"),
        ("60050.120", "0.01", "60050.12"),
        ("60050.001", "0.10", "60050.00"),
        ("0.05", "0.10", "0.00"),
    ],
)
def test_round_down(price: str, tick: str, expected: str) -> None:
    assert round_down_to_tick(Decimal(price), Decimal(tick)) == Decimal(
        expected
    )


@pytest.mark.parametrize(
    ("price", "tick", "expected"),
    [
        ("60050.121", "0.01", "60050.13"),
        ("60050.120", "0.01", "60050.12"),
        ("60050.001", "0.10", "60050.10"),
        ("0.05", "0.10", "0.10"),
    ],
)
def test_round_up(price: str, tick: str, expected: str) -> None:
    assert round_up_to_tick(Decimal(price), Decimal(tick)) == Decimal(expected)
