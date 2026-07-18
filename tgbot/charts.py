"""Render the /pnl cumulative-profit chart (actual + projection)."""

from __future__ import annotations

import io
from decimal import Decimal


def pnl_curve(
    closed_realized: list[Decimal], open_tp_profit: list[Decimal]
) -> tuple[list[Decimal], int]:
    """Cumulative PnL points: actual realized, then a floored projection.

    Sums realized PnL of closed trades in order, then continues by adding
    each open position's take-profit gain floored at 0 — a below-entry TP
    (the bag being worked to break-even) never counts as a loss, so the
    projected tail only rises. Returns the cumulative points and the index
    where the projection begins.
    """
    cum: list[Decimal] = []
    total = Decimal(0)
    for realized in closed_realized:
        total += realized
        cum.append(total)
    split = len(cum)
    for gain in open_tp_profit:
        total += max(Decimal(0), gain)
        cum.append(total)
    return cum, split


def render_pnl_chart(cum: list[Decimal], split: int) -> bytes:
    """Render the cumulative PnL curve to PNG bytes.

    Actual is a solid line, the projection dashed. ``matplotlib`` is imported
    lazily so start-up and the other commands don't pay for the heavy import.
    """
    from matplotlib.figure import Figure

    xs = list(range(len(cum)))
    ys = [float(v) for v in cum]
    fig = Figure(figsize=(7.5, 3.6), dpi=110)
    ax = fig.subplots()
    if split > 0:
        ax.plot(xs[:split], ys[:split], color="#16a34a", label="факт")
    if split < len(cum):
        start = max(0, split - 1)
        ax.plot(
            xs[start:],
            ys[start:],
            color="#2563eb",
            linestyle="--",
            label="прогноз (открытые на TP)",
        )
        ax.axvline(start, color="#9ca3af", linestyle=":", linewidth=1)
    ax.set_title("PnL: факт → прогноз, USDT")
    ax.set_xlabel("сделки")
    ax.set_ylabel("USDT")
    ax.grid(visible=True, alpha=0.3)
    ax.legend(loc="upper left", fontsize=8)
    fig.tight_layout()
    buf = io.BytesIO()
    fig.savefig(buf, format="png")
    return buf.getvalue()
