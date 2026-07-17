"""Reconstruct real entry prices of held inventory from fill history.

When base coin ends up untracked, re-adopt it with a take-profit one
``tp_step`` above its *real* entry — recovered by FIFO-matching the
buy/sell fills into unsold lots at their actual purchase prices.
"""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass
from decimal import Decimal


@dataclass(frozen=True)
class Fill:
    """A single executed fill: side, price, quantity."""

    side: str
    price: Decimal
    qty: Decimal


def fifo_residual(fills: list[Fill]) -> list[tuple[Decimal, Decimal]]:
    """FIFO-match sells against buys; return the unsold buy lots.

    ``fills`` must be in chronological order. Result is ``[(price, qty)]`` for
    the lots still held, oldest first. Sells beyond available inventory are
    ignored.
    """
    lots: deque[list[Decimal]] = deque()
    for f in fills:
        if f.side == "Buy":
            lots.append([f.price, f.qty])
            continue
        remaining = f.qty
        while remaining > 0 and lots:
            lot = lots[0]
            take = min(remaining, lot[1])
            lot[1] -= take
            remaining -= take
            if lot[1] <= 0:
                lots.popleft()
    return [(price, qty) for price, qty in lots if qty > 0]


def select_free_lots(
    residual: list[tuple[Decimal, Decimal]], free_qty: Decimal
) -> list[tuple[Decimal, Decimal]]:
    """Pick lots totalling ``free_qty`` from the residual, cheapest first.

    The cheapest lots give TPs nearest the market. Returns
    ``[(entry_price, qty)]``; the last lot may be trimmed to fit.
    """
    if free_qty <= 0:
        return []
    chosen: list[tuple[Decimal, Decimal]] = []
    remaining = free_qty
    for price, qty in sorted(residual, key=lambda lot: lot[0]):
        if remaining <= 0:
            break
        take = min(qty, remaining)
        chosen.append((price, take))
        remaining -= take
    return chosen
