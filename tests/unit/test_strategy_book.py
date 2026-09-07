from __future__ import annotations

from decimal import Decimal

import pytest

from core.strategy.book import build_ladder

_STEP = Decimal("0.00004")


def _order(
    price: str, qty: str = "200", is_buy: bool = False
) -> tuple[Decimal, Decimal, bool]:
    return Decimal(price), Decimal(qty), is_buy


def test_ladder_runs_from_the_top_down() -> None:
    rungs = build_ladder(
        [_order("0.03400"), _order("0.03408"), _order("0.03404")], _STEP
    )
    assert [r.price for r in rungs] == [
        Decimal("0.03408"),
        Decimal("0.03404"),
        Decimal("0.03400"),
    ]
    assert not any(r.is_gap for r in rungs)


def test_one_empty_level_becomes_one_gap_rung() -> None:
    rungs = build_ladder([_order("0.03408"), _order("0.03400")], _STEP)
    assert [r.is_gap for r in rungs] == [False, True, False]
    assert rungs[1].skipped == 1
    assert rungs[1].qty == 0


def test_a_long_empty_run_still_costs_one_rung() -> None:
    rungs = build_ladder([_order("0.05000"), _order("0.03000")], _STEP)
    gaps = [r for r in rungs if r.is_gap]
    assert len(gaps) == 1
    assert gaps[0].skipped == 499


def test_neighbouring_levels_leave_no_gap() -> None:
    rungs = build_ladder([_order("0.03404"), _order("0.03400")], _STEP)
    assert [r.is_gap for r in rungs] == [False, False]


def test_bids_and_asks_keep_their_side() -> None:
    rungs = build_ladder(
        [_order("0.03408"), _order("0.03400", is_buy=True)], _STEP
    )
    assert [r.is_buy for r in rungs if not r.is_gap] == [False, True]


def test_an_empty_book_is_an_empty_ladder() -> None:
    assert build_ladder([], _STEP) == []


def test_a_zero_grid_step_is_refused() -> None:
    with pytest.raises(ValueError, match="grid_step"):
        build_ladder([_order("0.03400")], Decimal(0))
