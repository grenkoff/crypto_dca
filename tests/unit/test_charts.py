from __future__ import annotations

from decimal import Decimal
from itertools import pairwise

from tgbot.charts import pnl_curve, render_pnl_chart


def _d(values: list[str]) -> list[Decimal]:
    return [Decimal(v) for v in values]


def test_pnl_curve_empty() -> None:
    assert pnl_curve([], []) == ([], 0)


def test_pnl_curve_actual_is_running_realized() -> None:
    cum, split = pnl_curve(_d(["1", "-0.5", "2"]), [])
    assert cum == _d(["1", "0.5", "2.5"])
    assert split == 3


def test_pnl_curve_projection_floors_losses_at_zero() -> None:
    # closed: +1 ; open TP gains: +1 (grid), -3 (bag), +0.5 (grid)
    cum, split = pnl_curve(_d(["1"]), _d(["1", "-3", "0.5"]))
    # actual [1], then +1 -> 2, +max(0,-3)=+0 -> 2, +0.5 -> 2.5
    assert cum == _d(["1", "2", "2", "2.5"])
    assert split == 1


def test_pnl_curve_projection_only_rises() -> None:
    cum, split = pnl_curve(_d(["0.5", "0.5"]), _d(["-1", "-2", "0.3"]))
    proj = cum[split:]
    assert all(b >= a for a, b in pairwise(proj))


def test_render_pnl_chart_returns_png_bytes() -> None:
    png = render_pnl_chart(_d(["1", "0.5", "2.5", "3"]), 3)
    assert png[:8] == b"\x89PNG\r\n\x1a\n"
    assert len(png) > 1000
