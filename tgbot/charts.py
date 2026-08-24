"""Render the /pnl funds-and-profit chart (equity line + daily bars)."""

from __future__ import annotations

import io
from decimal import Decimal
from math import isnan, sqrt
from typing import Any

Bar = tuple[float, float, float, float, float]


def _tangents(xs: list[float], ys: list[float]) -> list[float]:
    """Fritsch-Carlson tangents: slopes that cannot overshoot the data.

    Plain cubic interpolation dips below and climbs above its own points
    around a step, which on a money line draws balances the account never
    held. Limiting the tangents keeps every segment inside its endpoints.
    """
    n = len(xs)
    deltas = [(ys[i + 1] - ys[i]) / (xs[i + 1] - xs[i]) for i in range(n - 1)]
    tangents = [deltas[0]]
    tangents += [(deltas[i - 1] + deltas[i]) / 2 for i in range(1, n - 1)]
    tangents.append(deltas[-1])
    for i, delta in enumerate(deltas):
        if delta == 0:
            tangents[i] = tangents[i + 1] = 0.0
            continue
        alpha = tangents[i] / delta
        beta = tangents[i + 1] / delta
        size = alpha * alpha + beta * beta
        if size > 9:
            scale = 3.0 / sqrt(size)
            tangents[i] = scale * alpha * delta
            tangents[i + 1] = scale * beta * delta
    return tangents


def _hermite(t: float, y0: float, y1: float, m0: float, m1: float) -> float:
    """Cubic Hermite value between two points with given tangents."""
    t2 = t * t
    t3 = t2 * t
    return (
        (2 * t3 - 3 * t2 + 1) * y0
        + (t3 - 2 * t2 + t) * m0
        + (-2 * t3 + 3 * t2) * y1
        + (t3 - t2) * m1
    )


def _smooth(
    xs: list[float], ys: list[float], samples: int = 18
) -> tuple[list[float], list[float]]:
    """Monotone spline through the points (NaN skipped) for a soft curve."""
    pts = [(x, y) for x, y in zip(xs, ys, strict=False) if not isnan(y)]
    if len(pts) < 3:
        return [p[0] for p in pts], [p[1] for p in pts]
    px = [p[0] for p in pts]
    py = [p[1] for p in pts]
    tangents = _tangents(px, py)
    ox: list[float] = []
    oy: list[float] = []
    for i in range(len(pts) - 1):
        span = px[i + 1] - px[i]
        for s in range(samples):
            t = s / samples
            ox.append(px[i] + t * span)
            oy.append(
                _hermite(
                    t,
                    py[i],
                    py[i + 1],
                    tangents[i] * span,
                    tangents[i + 1] * span,
                )
            )
    ox.append(px[-1])
    oy.append(py[-1])
    return ox, oy


def pnl_series(
    days: list[tuple[str, Decimal]], base_capital: Decimal
) -> tuple[list[str], list[Decimal], list[Decimal]]:
    """Daily labels, daily profits, and the equity line.

    Equity each day is ``base_capital`` plus the running sum of daily realized
    profit.
    """
    labels = [label for label, _ in days]
    profits = [profit for _, profit in days]
    equity: list[Decimal] = []
    total = base_capital
    for profit in profits:
        total += profit
        equity.append(total)
    return labels, profits, equity


def _apply_xticks(ax: Any, labels: list[str]) -> None:
    """Thin the day labels to avoid crowding."""
    ticks = list(range(len(labels)))
    step = max(1, len(ticks) // 10)
    ax.set_xticks(ticks[::step])
    ax.set_xticklabels(labels[::step], fontsize=7, rotation=45)


def _style_yaxis(
    axis: Any, label: str, color: str, outward: float | None = None
) -> None:
    """Label and colour a y-axis, optionally pushing its spine outward."""
    axis.set_ylabel(label, fontsize=8, color=color)
    axis.tick_params(axis="y", labelcolor=color, labelsize=8)
    if outward is not None:
        axis.spines["right"].set_position(("outward", outward))


def _style_right(axis: Any, color: str, outward: float) -> None:
    """Colour a right y-axis and offset its spine, with no vertical title."""
    axis.tick_params(axis="y", labelcolor=color, labelsize=8)
    axis.spines["right"].set_position(("outward", outward))


_GREEN = "#16a34a"
_AMBER = "#f59e0b"
_BAR = "#7dd3fc"
_MA = "#2563eb"
_INK = "black"
_GREY = "#b8b8b8"
_VOL_UP = "#8fd3b6"
_VOL_DOWN = "#f0a8a8"
_MA_WINDOW = 10


def _draw_volume(axis: Any, ohlc: list[Bar | None]) -> None:
    """Fill the lower panel with per-day volume, tinted by candle direction."""
    from matplotlib.ticker import FuncFormatter, MaxNLocator

    xs = [i for i, bar in enumerate(ohlc) if bar is not None]
    bars = [bar for bar in ohlc if bar is not None]
    if not bars:
        axis.set_yticks([])
        return
    colours = [_VOL_UP if bar[3] >= bar[0] else _VOL_DOWN for bar in bars]
    axis.bar(xs, [bar[4] for bar in bars], color=colours, width=0.7)
    axis.tick_params(axis="y", labelsize=7, labelcolor=_GREY)
    axis.yaxis.set_major_locator(MaxNLocator(nbins=2))
    axis.yaxis.set_major_formatter(FuncFormatter(_compact))
    for side in ("top", "right"):
        axis.spines[side].set_visible(False)
    axis.grid(visible=True, axis="y", alpha=0.2)


def _compact(value: float, _pos: int = 0) -> str:
    """Abbreviate a volume tick as 1.2K / 3.4M."""
    for cut, suffix in ((1e9, "B"), (1e6, "M"), (1e3, "K")):
        if abs(value) >= cut:
            return f"{value / cut:.1f}{suffix}"
    return f"{value:.0f}"


def _draw_candles(
    axis: Any,
    ohlc: list[Bar | None],
    *,
    color: str = _INK,
    zorder: int = 3,
) -> None:
    """Draw OHLC candles in ``color``: hollow up, filled down."""
    from matplotlib.patches import Rectangle

    width = 0.3
    for i, bar in enumerate(ohlc):
        if bar is None:
            continue
        op, hi, lo, cl = bar[:4]
        face = "white" if cl >= op else color
        axis.plot([i, i], [lo, hi], color=color, linewidth=0.7, zorder=zorder)
        height = abs(cl - op) or (hi - lo) * 0.02
        axis.add_patch(
            Rectangle(
                (i - width / 2, min(op, cl)),
                width,
                height,
                facecolor=face,
                edgecolor=color,
                linewidth=0.6,
                zorder=zorder,
            )
        )


def _moving_average(values: list[Decimal], window: int) -> list[float]:
    """Trailing simple moving average; NaN until a full window accrues."""
    out: list[float] = []
    for i in range(len(values)):
        if i + 1 < window:
            out.append(float("nan"))
            continue
        chunk = values[i - window + 1 : i + 1]
        out.append(float(sum(chunk, Decimal(0)) / window))
    return out


def render_pnl_chart(
    days: list[tuple[str, Decimal]],
    base_capital: Decimal,
    locked: list[Decimal],
    ohlc: list[Bar | None],
    btc_ohlc: list[Bar | None] | None = None,
    funds: list[Decimal] | None = None,
) -> bytes:
    """Render the funds-and-profit chart to PNG bytes.

    Locked USDT (amber) sits on the left axis; funds (green), daily realized
    profit (bars + MA), and the KAS price (daily candlesticks) each get their
    own right axis. ``funds`` is the whole account's worth per day when
    given; without it the line falls back to cost basis plus realized
    profit. ``btc_ohlc`` (already rescaled to KAS units) is drawn as
    lighter grey candles on the same price axis to gauge BTC correlation.
    KAS volume sits in its own panel below, above the date axis.
    ``matplotlib`` is imported lazily to keep start-up fast.
    """
    from matplotlib.figure import Figure
    from matplotlib.patches import Patch

    labels, profits, computed = pnl_series(days, base_capital)
    equity = funds if funds else computed
    xs = list(range(len(equity)))

    fig = Figure(figsize=(8.4, 6.0), dpi=110)
    ax, vol_ax = fig.subplots(
        2,
        1,
        sharex=True,
        height_ratios=(4, 1),
        gridspec_kw={"hspace": 0.06},
    )
    funds_ax = ax.twinx()
    bar_ax = ax.twinx()
    price_ax = ax.twinx()

    bar_ax.bar(
        xs,
        [float(v) for v in profits],
        color=_BAR,
        width=0.7,
        label="profit/day",
    )
    fxs = [float(x) for x in xs]
    ma_x, ma_y = _smooth(fxs, _moving_average(profits, _MA_WINDOW))
    bar_ax.plot(
        ma_x, ma_y, color=_MA, linewidth=1.5, label=f"profit MA({_MA_WINDOW}d)"
    )
    lk_x, lk_y = _smooth(fxs, [float(v) for v in locked])
    ax.plot(lk_x, lk_y, color=_AMBER, label="locked")
    fn_x, fn_y = _smooth(fxs, [float(v) for v in equity])
    funds_ax.plot(fn_x, fn_y, color=_GREEN, label="funds")
    if btc_ohlc is not None:
        _draw_candles(price_ax, btc_ohlc, color=_GREY, zorder=2)
    _draw_candles(price_ax, ohlc)
    for line_ax in (ax, funds_ax, price_ax):
        line_ax.set_zorder(bar_ax.get_zorder() + 1)
        line_ax.patch.set_visible(False)

    _draw_volume(vol_ax, ohlc)

    fig.suptitle("Funds & profit, USDT", y=0.965, fontsize=11)
    vol_ax.set_xlabel("days")
    _style_yaxis(ax, "locked, USDT", _AMBER)
    _style_right(funds_ax, _GREEN, outward=0)
    _style_right(bar_ax, _MA, outward=34)
    _style_right(price_ax, _INK, outward=68)
    ax.grid(visible=True, alpha=0.3)
    ax.tick_params(axis="x", labelbottom=False)
    _apply_xticks(vol_ax, labels)

    h1, l1 = ax.get_legend_handles_labels()
    h2, l2 = funds_ax.get_legend_handles_labels()
    h3, l3 = bar_ax.get_legend_handles_labels()
    kas = Patch(facecolor="white", edgecolor=_INK, label="KAS price")
    volume = Patch(facecolor=_VOL_UP, edgecolor="none", label="volume")
    handles = [*h1, *h2, *h3, kas, volume]
    labels_all = [*l1, *l2, *l3, "KAS price", "volume"]
    if btc_ohlc is not None:
        handles.append(Patch(facecolor="white", edgecolor=_GREY, label="BTC"))
        labels_all.append("BTC price")
    fig.legend(
        handles,
        labels_all,
        loc="upper center",
        bbox_to_anchor=(0.5, 0.945),
        ncol=len(labels_all),
        fontsize=8,
        frameon=False,
    )
    fig.subplots_adjust(
        left=0.085, right=0.80, top=0.875, bottom=0.135, hspace=0.07
    )
    buf = io.BytesIO()
    fig.savefig(buf, format="png")
    return buf.getvalue()


def render_formulas(lines: list[str]) -> bytes:
    """Render LaTeX (mathtext) formula lines to a PNG image."""
    from matplotlib.figure import Figure

    fig = Figure(figsize=(7.2, 1.0 * len(lines) + 0.2), dpi=200)
    ax = fig.subplots()
    ax.axis("off")
    for i, line in enumerate(lines):
        ax.text(
            0.5,
            1 - (i + 0.5) / len(lines),
            f"${line}$",
            ha="center",
            va="center",
            fontsize=18,
        )
    buf = io.BytesIO()
    fig.savefig(buf, format="png", bbox_inches="tight", pad_inches=0.25)
    return buf.getvalue()
