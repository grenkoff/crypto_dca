from __future__ import annotations

import math
from decimal import Decimal

from tgbot.charts import _moving_average, pnl_series, render_pnl_chart


def _days(pairs: list[tuple[str, str]]) -> list[tuple[str, Decimal]]:
    return [(label, Decimal(v)) for label, v in pairs]


def test_moving_average_is_nan_until_window_full() -> None:
    ma = _moving_average([Decimal(v) for v in ("2", "4", "6", "9")], 3)
    # first two lack a full 3-day window -> NaN; then real averages
    assert math.isnan(ma[0])
    assert math.isnan(ma[1])
    assert ma[2] == 4.0  # (2+4+6)/3
    assert round(ma[3], 2) == 6.33  # (4+6+9)/3


def test_pnl_series_empty() -> None:
    labels, profits, equity = pnl_series([], Decimal("100"))
    assert labels == [] and profits == [] and equity == []


def test_pnl_series_equity_is_base_plus_running_profit() -> None:
    days = _days([("01.07", "1"), ("02.07", "-0.5"), ("03.07", "2")])
    labels, profits, equity = pnl_series(days, Decimal("100"))
    assert labels == ["01.07", "02.07", "03.07"]
    assert profits == [Decimal("1"), Decimal("-0.5"), Decimal("2")]
    assert equity == [Decimal("101"), Decimal("100.5"), Decimal("102.5")]


def test_render_pnl_chart_returns_png_bytes() -> None:
    days = _days([("01.07", "1"), ("02.07", "0.5"), ("03.07", "-0.2")])
    locked = [Decimal("300"), Decimal("320"), Decimal("310")]
    ohlc: list[tuple[float, float, float, float] | None] = [
        (0.028, 0.0282, 0.0279, 0.0281),
        None,
        (0.0281, 0.0284, 0.028, 0.0282),
    ]
    png = render_pnl_chart(days, Decimal("340"), locked, ohlc)
    assert png[:8] == b"\x89PNG\r\n\x1a\n"
    assert len(png) > 1000


def test_render_pnl_chart_handles_single_day() -> None:
    png = render_pnl_chart(
        _days([("01.07", "1")]),
        Decimal("340"),
        [Decimal("50")],
        [(0.028, 0.0281, 0.0279, 0.028)],
    )
    assert png[:8] == b"\x89PNG\r\n\x1a\n"
