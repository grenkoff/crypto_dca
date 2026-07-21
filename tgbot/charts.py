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


def render_pnl_chart(
    days: list[tuple[str, Decimal]],
    base_capital: Decimal,
    projection: Decimal,
    locked: list[Decimal],
) -> bytes:
    """Render the funds-and-profit chart to PNG bytes.

    A green equity line over days with a dashed projection segment to the
    take-profit total, an amber line of USDT locked in open trades, plus
    daily realized profit as bars on a second axis. ``matplotlib`` is
    imported lazily to keep start-up and other commands fast.
    """
    from matplotlib.figure import Figure

    labels, profits, equity, proj = pnl_series(days, base_capital, projection)
    xs = list(range(len(equity)))
    last_x = xs[-1] if xs else 0
    last_eq = float(equity[-1]) if equity else float(base_capital)
    proj_x = last_x + 1

    fig = Figure(figsize=(7.5, 3.6), dpi=110)
    ax = fig.subplots()
    bar_ax = ax.twinx()
    bar_ax.bar(
        xs,
        [float(v) for v in profits],
        color="#86efac",
        width=0.7,
        label="profit/day",
    )
    bar_ax.set_ylabel("profit/day, USDT", fontsize=8)

    ax.plot(xs, [float(v) for v in equity], color="#16a34a", label="funds")
    ax.plot(xs, [float(v) for v in locked], color="#f59e0b", label="locked")
    ax.plot(
        [last_x, proj_x],
        [last_eq, float(proj)],
        color="#2563eb",
        linestyle="--",
        label="projection (at TP)",
    )
    ax.axvline(last_x, color="#9ca3af", linestyle=":", linewidth=1)
    ax.set_zorder(bar_ax.get_zorder() + 1)
    ax.patch.set_visible(False)

    ax.set_title("Funds & profit, USDT")
    ax.set_xlabel("days")
    ax.set_ylabel("funds, USDT")
    ax.grid(visible=True, alpha=0.3)
    _apply_xticks(ax, labels, proj_x)

    handles = ax.get_legend_handles_labels()
    bars = bar_ax.get_legend_handles_labels()
    ax.legend(
        handles[0] + bars[0],
        handles[1] + bars[1],
        loc="upper left",
        fontsize=8,
    )
    fig.tight_layout()
    buf = io.BytesIO()
    fig.savefig(buf, format="png")
    return buf.getvalue()
