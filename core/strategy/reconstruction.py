"""Reconstruct real entry prices of currently-held inventory from fill history.

When base coin ends up untracked (a limit buy that filled while the trader was
down, or a bug), we re-adopt it — but the take-profit must sit one ``tp_step``
above the *real* entry, not a guess. FIFO-matching the buy/sell fills yields the
unsold lots at their actual purchase prices.
"""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass
from decimal import Decimal


@dataclass(frozen=True)
class Fill:
    side: str  # "Buy" | "Sell"
    price: Decimal
    qty: Decimal


def fifo_residual(fills: list[Fill]) -> list[tuple[Decimal, Decimal]]:
    """FIFO-match sells against buys; return the unsold buy lots.

    ``fills`` must be in chronological order. Result is ``[(price, qty)]`` for the
    lots still held, oldest first. Sells beyond available inventory are ignored.
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
    """Pick lots totalling ``free_qty`` from the residual, cheapest entry first.

    The free (untracked) balance is the most recently accumulated inventory; taking
    the lowest-priced lots gives take-profits nearest the market, so they clear
    soonest. Returns ``[(entry_price, qty)]``; the last lot may be trimmed to fit.
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
