"""Render the /pnl funds-and-profit chart (equity line + daily bars)."""

from __future__ import annotations

import io
from decimal import Decimal
from math import isnan
from typing import Any


def _catmull(t: float, p0: float, p1: float, p2: float, p3: float) -> float:
    """One Catmull-Rom interpolation between p1 and p2 at parameter t."""
    t2 = t * t
    t3 = t2 * t
    return 0.5 * (
        2 * p1
        + (-p0 + p2) * t
        + (2 * p0 - 5 * p1 + 4 * p2 - p3) * t2
        + (-p0 + 3 * p1 - 3 * p2 + p3) * t3
    )


def _smooth(
    xs: list[float], ys: list[float], samples: int = 18
) -> tuple[list[float], list[float]]:
    """Catmull-Rom spline through the points (NaN skipped) for a soft curve."""
    pts = [(x, y) for x, y in zip(xs, ys, strict=False) if not isnan(y)]
    if len(pts) < 3:
        return [p[0] for p in pts], [p[1] for p in pts]
    ext = [pts[0], *pts, pts[-1]]
    ox: list[float] = []
    oy: list[float] = []
    for i in range(1, len(ext) - 2):
        seg = (ext[i - 1], ext[i], ext[i + 1], ext[i + 2])
        for s in range(samples):
            t = s / samples
            ox.append(_catmull(t, *(p[0] for p in seg)))
            oy.append(_catmull(t, *(p[1] for p in seg)))
    ox.append(pts[-1][0])
    oy.append(pts[-1][1])
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
_MA_WINDOW = 10


def _draw_candles(
    axis: Any, ohlc: list[tuple[float, float, float, float] | None]
) -> None:
    """Draw daily OHLC candlesticks: hollow up, filled down, black wicks."""
    from matplotlib.patches import Rectangle

    width = 0.3
    for i, bar in enumerate(ohlc):
        if bar is None:
            continue
        op, hi, lo, cl = bar
        face = "white" if cl >= op else _INK
        axis.plot([i, i], [lo, hi], color=_INK, linewidth=0.7, zorder=3)
        height = abs(cl - op) or (hi - lo) * 0.02
        axis.add_patch(
            Rectangle(
                (i - width / 2, min(op, cl)),
                width,
                height,
                facecolor=face,
                edgecolor=_INK,
                linewidth=0.6,
                zorder=3,
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
    ohlc: list[tuple[float, float, float, float] | None],
) -> bytes:
    """Render the funds-and-profit chart to PNG bytes.

    Locked USDT (amber) sits on the left axis; funds (green), daily realized
    profit (bars + MA), and the KAS price (daily candlesticks) each get their
    own right axis. ``matplotlib`` is imported lazily to keep start-up fast.
    """
    from matplotlib.figure import Figure
    from matplotlib.patches import Patch

    labels, profits, equity = pnl_series(days, base_capital)
    xs = list(range(len(equity)))

    fig = Figure(figsize=(8.4, 3.6), dpi=110)
    ax = fig.subplots()
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
    _draw_candles(price_ax, ohlc)
    for line_ax in (ax, funds_ax, price_ax):
        line_ax.set_zorder(bar_ax.get_zorder() + 1)
        line_ax.patch.set_visible(False)

    fig.suptitle("Funds & profit, USDT", y=0.965, fontsize=11)
    ax.set_xlabel("days")
    _style_yaxis(ax, "locked, USDT", _AMBER)
    _style_right(funds_ax, _GREEN, outward=0)
    _style_right(bar_ax, _MA, outward=34)
    _style_right(price_ax, _INK, outward=68)
    ax.grid(visible=True, alpha=0.3)
    _apply_xticks(ax, labels)

    h1, l1 = ax.get_legend_handles_labels()
    h2, l2 = funds_ax.get_legend_handles_labels()
    h3, l3 = bar_ax.get_legend_handles_labels()
    kas = Patch(facecolor="white", edgecolor=_INK, label="KAS price")
    handles = [*h1, *h2, *h3, kas]
    labels_all = [*l1, *l2, *l3, "KAS price"]
    fig.legend(
        handles,
        labels_all,
        loc="upper center",
        bbox_to_anchor=(0.5, 0.90),
        ncol=len(labels_all),
        fontsize=8,
        frameon=False,
    )
    fig.tight_layout(rect=(0, 0, 0.99, 0.86))
    buf = io.BytesIO()
    fig.savefig(buf, format="png")
    return buf.getvalue()
