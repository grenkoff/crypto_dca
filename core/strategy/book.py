"""Order-book ladder: resting orders laid out on the grid lattice.

Every rung is a grid level. Levels that carry no order collapse into a
single gap rung however many of them run together, so a ladder stays
readable when the wall is sparse. A level the grid already holds a lot on
is shown for what it is: the grid rests one buy per price, so the missing
order there is a decision, not a hole.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from decimal import Decimal

from core.strategy.lattice import Lattice


@dataclass(frozen=True)
class BookLevel:
    """One rung: a resting order, a held level, or a run of empty ones."""

    price: Decimal
    qty: Decimal
    is_buy: bool
    skipped: int = 0
    is_held: bool = False

    @property
    def is_gap(self) -> bool:
        """Whether this rung stands for levels nobody is resting on."""
        return self.skipped > 0


def _gap(lattice: Lattice, upper: Decimal, lower: Decimal) -> int:
    """How many rungs sit strictly between two prices."""
    return lattice.index_of(upper) - lattice.index_of(lower) - 1


def _between(
    lattice: Lattice,
    upper: Decimal,
    lower: Decimal,
    held: Mapping[Decimal, Decimal],
    is_buy: bool,
) -> list[BookLevel]:
    """The rungs to show between two orders: held levels, then gaps."""
    inside = sorted(
        (price for price in held if lower < price < upper), reverse=True
    )
    rungs: list[BookLevel] = []
    top = upper
    for price in [*inside, lower]:
        missing = _gap(lattice, top, price)
        if missing > 0:
            rungs.append(
                BookLevel(
                    price=lattice.above(price),
                    qty=Decimal(0),
                    is_buy=is_buy,
                    skipped=missing,
                )
            )
        if price != lower:
            rungs.append(
                BookLevel(
                    price=price,
                    qty=held[price],
                    is_buy=is_buy,
                    is_held=True,
                )
            )
        top = price
    return rungs


def build_ladder(
    orders: Sequence[tuple[Decimal, Decimal, bool]],
    lattice: Lattice,
    held: Mapping[Decimal, Decimal] | None = None,
) -> list[BookLevel]:
    """Resting ``orders`` from the top down, gaps collapsed.

    Each order is ``(price, qty, is_buy)``. Between two neighbours the
    ladder counts the grid rungs nobody is resting on and emits one gap
    rung for the run, so a hundred empty levels cost one line — except a
    level in ``held``, which gets a rung of its own carrying the coin the
    lot bought there.
    """
    held = held or {}
    ranked = sorted(orders, key=lambda row: row[0], reverse=True)
    rungs: list[BookLevel] = []
    previous: Decimal | None = None
    for price, qty, is_buy in ranked:
        if previous is not None:
            rungs += _between(lattice, previous, price, held, is_buy)
        rungs.append(BookLevel(price=price, qty=qty, is_buy=is_buy))
        previous = price
    return rungs
