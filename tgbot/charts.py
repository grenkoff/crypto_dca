"""Render the /pnl funds-and-profit chart (equity line + daily bars)."""

from __future__ import annotations

import io
from decimal import Decimal
from typing import Any


def pnl_series(
    days: list[tuple[str, Decimal]],
    base_capital: Decimal,
    projection: Decimal,
) -> tuple[list[str], list[Decimal], list[Decimal], Decimal]:
    """Daily labels, daily profits, the equity line, and the projected total.

    Equity each day is ``base_capital`` plus the running sum of daily realized
    profit; the projection adds every open lot's take-profit gain onto the
    last day's equity as a single point.
    """
    labels = [label for label, _ in days]
    profits = [profit for _, profit in days]
    equity: list[Decimal] = []
    total = base_capital
    for profit in profits:
        total += profit
        equity.append(total)
    last = equity[-1] if equity else base_capital
    return labels, profits, equity, last + projection


def _apply_xticks(ax: Any, labels: list[str], proj_x: int) -> None:
    """Thin the day labels (plus the projection tick) to avoid crowding."""
    ticks = [*range(len(labels)), proj_x]
    names = [*labels, "proj."]
    step = max(1, len(ticks) // 10)
    ax.set_xticks(ticks[::step])
    ax.set_xticklabels(names[::step], fontsize=7, rotation=45)


def _style_yaxis(
    axis: Any, label: str, color: str, outward: float | None = None
) -> None:
    """Label and colour a y-axis, optionally pushing its spine outward."""
    axis.set_ylabel(label, fontsize=8, color=color)
    axis.tick_params(axis="y", labelcolor=color, labelsize=8)
    if outward is not None:
        axis.spines["right"].set_position(("outward", outward))


_GREEN = "#16a34a"
_AMBER = "#f59e0b"
_BAR = "#7dd3fc"
_MA = "#2563eb"
_MA_WINDOW = 10


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
    projection: Decimal,
    locked: list[Decimal],
) -> bytes:
    """Render the funds-and-profit chart to PNG bytes.

    Locked USDT (amber) sits on the left axis; funds (green) with its dashed
    take-profit projection sit on their own right axis so their small drift
    is visible; daily realized profit (bars) sit on a second, outer right
    axis. ``matplotlib`` is imported lazily to keep start-up fast.
    """
    from matplotlib.figure import Figure

    labels, profits, equity, proj = pnl_series(days, base_capital, projection)
    xs = list(range(len(equity)))
    last_x = xs[-1] if xs else 0
    last_eq = float(equity[-1]) if equity else float(base_capital)
    proj_x = last_x + 1

    fig = Figure(figsize=(8.0, 3.6), dpi=110)
    ax = fig.subplots()
    funds_ax = ax.twinx()
    bar_ax = ax.twinx()

    bar_ax.bar(
        xs,
        [float(v) for v in profits],
        color=_BAR,
        width=0.7,
        label="profit/day",
    )
    bar_ax.plot(
        xs,
        _moving_average(profits, _MA_WINDOW),
        color=_MA,
        linewidth=1.5,
        label=f"profit MA({_MA_WINDOW}d)",
    )
    ax.plot(xs, [float(v) for v in locked], color=_AMBER, label="locked")
    funds_ax.plot(xs, [float(v) for v in equity], color=_GREEN, label="funds")
    funds_ax.plot(
        [last_x, proj_x],
        [last_eq, float(proj)],
        color=_GREEN,
        linestyle="--",
        label="projection (at TP)",
    )
    ax.axvline(last_x, color="#9ca3af", linestyle=":", linewidth=1)
    for line_ax in (ax, funds_ax):
        line_ax.set_zorder(bar_ax.get_zorder() + 1)
        line_ax.patch.set_visible(False)

    fig.suptitle("Funds & profit, USDT", y=0.99, fontsize=11)
    ax.set_xlabel("days")
    _style_yaxis(ax, "locked, USDT", _AMBER)
    _style_yaxis(funds_ax, "funds, USDT", _GREEN)
    _style_yaxis(bar_ax, "profit/day, USDT", "#4b5563", outward=46)
    ax.grid(visible=True, alpha=0.3)
    _apply_xticks(ax, labels, proj_x)

    h1, l1 = ax.get_legend_handles_labels()
    h2, l2 = funds_ax.get_legend_handles_labels()
    h3, l3 = bar_ax.get_legend_handles_labels()
    fig.legend(
        h1 + h2 + h3,
        l1 + l2 + l3,
        loc="upper center",
        bbox_to_anchor=(0.5, 0.94),
        ncol=len(l1 + l2 + l3),
        fontsize=8,
        frameon=False,
    )
    fig.tight_layout(rect=(0, 0, 1, 0.88))
    buf = io.BytesIO()
    fig.savefig(buf, format="png")
    return buf.getvalue()
