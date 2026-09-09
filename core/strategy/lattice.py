"""Price lattices: the rungs a grid may rest its orders on.

``AbsoluteLattice`` spaces rungs a fixed price step apart; ``PercentLattice``
spaces them a fixed fraction apart, widening to one tick where the tick is
coarser than that fraction. Both answer the same questions, so the buy grid
and the take-profit wall are written once against either.
"""

from __future__ import annotations

from bisect import bisect_right
from dataclasses import dataclass
from decimal import Decimal
from functools import lru_cache
from typing import Protocol

from core.strategy.rounding import (
    round_down_to_tick,
    round_up_to_tick,
)
from core.strategy.types import GridMode

_MAX_RUNGS = 100_000


class Lattice(Protocol):
    """The rungs a grid rests on, indexed upward from the bottom."""

    def snap_down(self, price: Decimal) -> Decimal:
        """The highest rung at or below ``price``."""
        ...

    def snap_up(self, price: Decimal) -> Decimal:
        """The lowest rung at or above ``price``."""
        ...

    def below(self, price: Decimal) -> Decimal:
        """The highest rung strictly below ``price``."""
        ...

    def above(self, price: Decimal) -> Decimal:
        """The lowest rung strictly above ``price``."""
        ...

    def index_of(self, price: Decimal) -> int:
        """Index of the highest rung at or below ``price``."""
        ...

    def price_at(self, index: int) -> Decimal:
        """The price of rung ``index``."""
        ...

    def step_at(self, price: Decimal) -> Decimal:
        """The gap between neighbouring rungs around ``price``."""
        ...


@dataclass(frozen=True)
class AbsoluteLattice:
    """Rungs a fixed ``step`` apart, pinned at zero."""

    step: Decimal

    def __post_init__(self) -> None:
        if self.step <= 0:
            raise ValueError("step must be positive")

    def snap_down(self, price: Decimal) -> Decimal:
        """The highest rung at or below ``price``."""
        return round_down_to_tick(price, self.step)

    def snap_up(self, price: Decimal) -> Decimal:
        """The lowest rung at or above ``price``."""
        return round_up_to_tick(price, self.step)

    def below(self, price: Decimal) -> Decimal:
        """The highest rung strictly below ``price``."""
        snapped = self.snap_down(price)
        return snapped - self.step if snapped >= price else snapped

    def above(self, price: Decimal) -> Decimal:
        """The lowest rung strictly above ``price``."""
        snapped = self.snap_up(price)
        return snapped + self.step if snapped <= price else snapped

    def index_of(self, price: Decimal) -> int:
        """Index of the highest rung at or below ``price``."""
        return int(self.snap_down(price) / self.step)

    def price_at(self, index: int) -> Decimal:
        """The price of rung ``index``."""
        return Decimal(index) * self.step

    def step_at(self, price: Decimal) -> Decimal:
        """The gap between neighbouring rungs around ``price``."""
        return self.step


class PercentLattice:
    """Rungs a fixed fraction apart, pinned one tick above zero.

    Rung 0 is one tick and each rung above is the cheapest tick-aligned
    price whose drop to the rung below is at least ``ratio``. Where the
    tick is coarser than ``ratio`` the gap widens to a single tick, so the
    ladder never stalls and stays usable all the way down to the tick.
    """

    def __init__(self, ratio: Decimal, tick: Decimal) -> None:
        if not 0 < ratio < 1:
            raise ValueError("ratio must be in (0, 1)")
        if tick <= 0:
            raise ValueError("tick must be positive")
        self.ratio = ratio
        self.tick = tick
        self._rungs: list[Decimal] = [tick]

    def _extend(self) -> None:
        """Append one rung above the current top."""
        if len(self._rungs) >= _MAX_RUNGS:
            raise ValueError("lattice exceeded its rung limit")
        top = self._rungs[-1]
        self._rungs.append(
            round_up_to_tick(top / (Decimal(1) - self.ratio), self.tick)
        )

    def _grow_to(self, price: Decimal) -> None:
        """Extend the ladder until its top rung reaches ``price``."""
        while self._rungs[-1] < price:
            self._extend()

    def index_of(self, price: Decimal) -> int:
        """Index of the highest rung at or below ``price`` (-1 if none)."""
        if price < self.tick:
            return -1
        self._grow_to(price)
        return bisect_right(self._rungs, price) - 1

    def price_at(self, index: int) -> Decimal:
        """The price of rung ``index``."""
        if index < 0:
            raise ValueError("index must be non-negative")
        while len(self._rungs) <= index:
            self._extend()
        return self._rungs[index]

    def snap_down(self, price: Decimal) -> Decimal:
        """The highest rung at or below ``price``, 0 below the floor."""
        index = self.index_of(price)
        return self.price_at(index) if index >= 0 else Decimal(0)

    def snap_up(self, price: Decimal) -> Decimal:
        """The lowest rung at or above ``price``."""
        index = self.index_of(price)
        if index >= 0 and self.price_at(index) == price:
            return price
        return self.price_at(index + 1)

    def below(self, price: Decimal) -> Decimal:
        """The highest rung strictly below ``price``, 0 below the floor."""
        index = self.index_of(price)
        if index >= 0 and self.price_at(index) >= price:
            index -= 1
        return self.price_at(index) if index >= 0 else Decimal(0)

    def above(self, price: Decimal) -> Decimal:
        """The lowest rung strictly above ``price``."""
        return self.price_at(self.index_of(price) + 1)

    def step_at(self, price: Decimal) -> Decimal:
        """The gap between neighbouring rungs around ``price``."""
        rung = self.snap_up(price)
        return rung - self.below(rung)


def nearest_rung(lattice: Lattice, price: Decimal) -> Decimal:
    """The rung closest to ``price``, ties going up.

    A buy fills at or below its limit, so an entry that is not exactly on
    a rung still belongs to the rung it was placed at.
    """
    down = lattice.snap_down(price)
    up = lattice.snap_up(price)
    return up if price - down >= up - price else down


@lru_cache(maxsize=8)
def percent_lattice(ratio: Decimal, tick: Decimal) -> PercentLattice:
    """A shared ``PercentLattice``; its rungs are built once and reused."""
    return PercentLattice(ratio, tick)


class GridGeometry(Protocol):
    """Where buys rest, and where their take-profits go."""

    @property
    def lattice(self) -> Lattice:
        """The rungs this geometry rests its orders on."""
        ...

    def tp_target(self, entry_price: Decimal) -> Decimal:
        """The take-profit target above ``entry_price``, before floors."""
        ...

    def buy_ceiling(self, lowest_tp: Decimal) -> Decimal:
        """Highest price a resting buy may take below the wall."""
        ...

    def wall_floor(self, nearest_buy: Decimal) -> Decimal:
        """Lowest price a resting take-profit may be moved onto."""
        ...


@dataclass(frozen=True)
class AbsoluteGeometry:
    """A take-profit sits a fixed ``tp_step`` above its entry."""

    lattice: Lattice
    tp_step: Decimal

    def tp_target(self, entry_price: Decimal) -> Decimal:
        """The take-profit target above ``entry_price``, before floors."""
        return entry_price + self.tp_step

    def buy_ceiling(self, lowest_tp: Decimal) -> Decimal:
        """Highest price a resting buy may take below the wall.

        Room for the take-profit a fill would rest, and for the buy
        itself, so a rising grid never crowds the bottom of the wall.
        """
        return lowest_tp - self.tp_step - self.lattice.step_at(lowest_tp)

    def wall_floor(self, nearest_buy: Decimal) -> Decimal:
        """Lowest price a resting take-profit may be moved onto."""
        return nearest_buy + self.tp_step + self.lattice.step_at(nearest_buy)


@dataclass(frozen=True)
class PercentGeometry:
    """A take-profit sits one rung above its entry.

    One ratio drives both the buy grid and the profit each lot takes, so
    a fill on any rung sells into the rung above it.
    """

    lattice: PercentLattice

    def tp_target(self, entry_price: Decimal) -> Decimal:
        """The rung above ``entry_price``, never nearer than the ratio.

        An entry off the ladder — a merged lot's weighted average — would
        otherwise sell for less than the ratio it was bought under.
        """
        rung = self.lattice.above(entry_price)
        floor = entry_price / (Decimal(1) - self.lattice.ratio)
        return max(rung, self.lattice.snap_up(floor))

    def buy_ceiling(self, lowest_tp: Decimal) -> Decimal:
        """Highest price a resting buy may take below the wall.

        One rung for the take-profit a fill would rest, one for the buy
        itself, so a rising grid never crowds the bottom of the wall.
        """
        return self.lattice.below(self.lattice.below(lowest_tp))

    def wall_floor(self, nearest_buy: Decimal) -> Decimal:
        """Lowest price a resting take-profit may be moved onto."""
        return self.lattice.above(self.lattice.above(nearest_buy))


def build_geometry(
    *,
    mode: GridMode,
    step: Decimal,
    tp_step: Decimal,
    tick_size: Decimal,
) -> GridGeometry:
    """Grid geometry for a strategy config's mode and steps."""
    if mode == "percent":
        return PercentGeometry(lattice=percent_lattice(step, tick_size))
    return AbsoluteGeometry(lattice=AbsoluteLattice(step), tp_step=tp_step)
