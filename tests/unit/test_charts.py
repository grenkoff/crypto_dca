from __future__ import annotations

import math
from decimal import Decimal

from tgbot.charts import (
    Bar,
    _compact,
    _draw_volume,
    _moving_average,
    pnl_series,
    render_pnl_chart,
)


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
    ohlc: list[Bar | None] = [
        (0.028, 0.0282, 0.0279, 0.0281, 5000.0),
        None,
        (0.0281, 0.0284, 0.028, 0.0282, 5000.0),
    ]
    png = render_pnl_chart(days, Decimal("340"), locked, ohlc)
    assert png[:8] == b"\x89PNG\r\n\x1a\n"
    assert len(png) > 1000


def test_render_pnl_chart_handles_single_day() -> None:
    png = render_pnl_chart(
        _days([("01.07", "1")]),
        Decimal("340"),
        [Decimal("50")],
        [(0.028, 0.0281, 0.0279, 0.028, 5000.0)],
    )
    assert png[:8] == b"\x89PNG\r\n\x1a\n"


def test_render_pnl_chart_with_btc_candles() -> None:
    days = _days([("01.07", "1"), ("02.07", "0.5")])
    ohlc: list[Bar | None] = [
        (0.028, 0.0282, 0.0279, 0.0281, 5000.0),
        (0.0281, 0.0284, 0.028, 0.0282, 5000.0),
    ]
    btc: list[Bar | None] = [
        (0.0281, 0.0283, 0.028, 0.0281, 5000.0),
        (0.0282, 0.0285, 0.0281, 0.0283, 5000.0),
    ]
    png = render_pnl_chart(
        days, Decimal("340"), [Decimal("50"), Decimal("52")], ohlc, btc
    )
    assert png[:8] == b"\x89PNG\r\n\x1a\n"


def test_render_formulas_returns_png() -> None:
    from tgbot.charts import render_formulas

    png = render_formulas([r"\mathrm{APR} = \frac{a}{b}\times 100\%"])
    assert png[:8] == b"\x89PNG\r\n\x1a\n"
    assert len(png) > 1000


def test_compact_abbreviates_volume_ticks() -> None:
    assert _compact(950) == "950"
    assert _compact(1_200) == "1.2K"
    assert _compact(3_400_000) == "3.4M"
    assert _compact(2_500_000_000) == "2.5B"


def test_volume_panel_sits_below_the_price_panel() -> None:
    from matplotlib.figure import Figure

    fig = Figure()
    ax, vol_ax = fig.subplots(2, 1, sharex=True, height_ratios=(4, 1))
    _draw_volume(vol_ax, [(0.028, 0.029, 0.027, 0.0285, 1_000.0)])
    assert vol_ax.get_position().y1 <= ax.get_position().y0
    assert len(vol_ax.patches) == 1


def test_volume_bars_are_tinted_by_candle_direction() -> None:
    from matplotlib.figure import Figure

    fig = Figure()
    axis = fig.subplots()
    _draw_volume(
        axis,
        [
            (0.028, 0.029, 0.027, 0.0290, 10.0),
            (0.029, 0.029, 0.026, 0.0270, 20.0),
        ],
    )
    colours = [patch.get_facecolor() for patch in axis.patches]
    assert colours[0] != colours[1]


def test_volume_panel_survives_a_gap_in_candles() -> None:
    from matplotlib.figure import Figure

    fig = Figure()
    axis = fig.subplots()
    _draw_volume(axis, [None, (0.028, 0.029, 0.027, 0.0285, 5.0), None])
    assert len(axis.patches) == 1


def test_volume_panel_handles_no_candles_at_all() -> None:
    from matplotlib.figure import Figure

    fig = Figure()
    axis = fig.subplots()
    _draw_volume(axis, [None, None])
    assert list(axis.get_yticks()) == []
