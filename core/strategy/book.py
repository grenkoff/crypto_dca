"""Order-book ladder: resting orders laid out on the grid lattice.

Every rung is a grid level. Levels that carry no order collapse into a
single gap rung however many of them run together, so a ladder stays
readable when the wall is sparse.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from decimal import Decimal

from core.strategy.lattice import Lattice


@dataclass(frozen=True)
class BookLevel:
    """One rung: a resting order, or a run of empty grid levels."""

    price: Decimal
    qty: Decimal
    is_buy: bool
    skipped: int = 0

    @property
    def is_gap(self) -> bool:
        """Whether this rung stands for levels nobody is resting on."""
        return self.skipped > 0


def build_ladder(
    orders: Sequence[tuple[Decimal, Decimal, bool]], lattice: Lattice
) -> list[BookLevel]:
    """Resting ``orders`` from the top down, gaps collapsed.

    Each order is ``(price, qty, is_buy)``. Between two neighbours the
    ladder counts the grid rungs nobody is resting on and emits one
    gap rung for the run, so a hundred empty levels cost one line.
    """
    ranked = sorted(orders, key=lambda row: row[0], reverse=True)
    rungs: list[BookLevel] = []
    previous: Decimal | None = None
    for price, qty, is_buy in ranked:
        if previous is not None:
            missing = lattice.index_of(previous) - lattice.index_of(price) - 1
            if missing > 0:
                rungs.append(
                    BookLevel(
                        price=lattice.above(price),
                        qty=Decimal(0),
                        is_buy=is_buy,
                        skipped=missing,
                    )
                )
        rungs.append(BookLevel(price=price, qty=qty, is_buy=is_buy))
        previous = price
    return rungs
