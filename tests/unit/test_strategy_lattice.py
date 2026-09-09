from __future__ import annotations

from decimal import Decimal

import pytest

from core.strategy.lattice import (
    AbsoluteGeometry,
    AbsoluteLattice,
    PercentGeometry,
    PercentLattice,
    build_geometry,
    nearest_rung,
    percent_lattice,
)

_TICK = Decimal("0.00001")
_MARKET = Decimal("0.03640")
# a coarse ladder, so the lattice's own properties are easy to read
_RATIO = Decimal("0.0066")
# the live pair: buys 0.11% apart, each lot taking 0.66%
_STEP_RATIO = Decimal("0.0011")
_TP_RATIO = Decimal("0.0066")


def _ladder() -> PercentLattice:
    return PercentLattice(_RATIO, _TICK)


def _ratio_of(entry: Decimal, target: Decimal) -> Decimal:
    """How much of ``target`` the rise from ``entry`` earns, to 10 places."""
    return ((target - entry) / target).quantize(Decimal("1E-10"))


def _geometry() -> PercentGeometry:
    return PercentGeometry(
        lattice=percent_lattice(_STEP_RATIO, _TICK), tp_ratio=_TP_RATIO
    )


def test_absolute_rungs_are_round_multiples_of_the_step() -> None:
    lattice = AbsoluteLattice(Decimal("0.00004"))
    assert lattice.snap_down(Decimal("0.03642")) == Decimal("0.03640")
    assert lattice.below(Decimal("0.03640")) == Decimal("0.03636")
    assert lattice.above(Decimal("0.03640")) == Decimal("0.03644")
    assert lattice.snap_up(Decimal("0.03641")) == Decimal("0.03644")
    assert lattice.step_at(Decimal("0.03640")) == Decimal("0.00004")


def test_absolute_index_round_trips_through_price() -> None:
    lattice = AbsoluteLattice(Decimal("0.0001"))
    index = lattice.index_of(Decimal("0.03118"))
    assert lattice.price_at(index) == Decimal("0.03110")


def test_percent_ladder_never_gives_away_more_than_the_ratio() -> None:
    # every rung sells into the one above it for at least the ratio
    lattice = _ladder()
    top = lattice.index_of(_MARKET)
    for index in range(top):
        low = lattice.price_at(index)
        high = lattice.price_at(index + 1)
        assert (high - low) / high >= _RATIO


def test_percent_ladder_reaches_the_tick_without_stalling() -> None:
    # below tick/ratio a single tick already exceeds the ratio, so the
    # rungs widen to one tick instead of repeating a price
    lattice = _ladder()
    assert lattice.price_at(0) == _TICK
    assert [lattice.price_at(i) for i in range(4)] == [
        Decimal("0.00001"),
        Decimal("0.00002"),
        Decimal("0.00003"),
        Decimal("0.00004"),
    ]
    assert lattice.index_of(_MARKET) == 570


def test_percent_rungs_step_down_by_the_ratio_near_market() -> None:
    lattice = _ladder()
    rung = lattice.below(_MARKET)
    assert rung == Decimal("0.03638")
    assert lattice.below(rung) == Decimal("0.03613")
    assert lattice.above(rung) == Decimal("0.03663")


def test_percent_lattice_bottoms_out_at_zero() -> None:
    lattice = _ladder()
    assert lattice.below(_TICK) == Decimal(0)
    assert lattice.snap_down(Decimal("0.000005")) == Decimal(0)
    assert lattice.index_of(Decimal("0.000005")) == -1


def test_percent_lattice_rejects_a_ratio_outside_the_unit_range() -> None:
    with pytest.raises(ValueError):
        PercentLattice(Decimal("0"), _TICK)
    with pytest.raises(ValueError):
        PercentLattice(Decimal("1.5"), _TICK)
    with pytest.raises(ValueError):
        PercentLattice(_RATIO, Decimal("0"))


def test_percent_lattice_is_shared_per_ratio_and_tick() -> None:
    assert percent_lattice(_RATIO, _TICK) is percent_lattice(_RATIO, _TICK)


def test_nearest_rung_claims_the_level_a_fill_was_placed_at() -> None:
    # a buy fills at or below its limit, so 0.03637 belongs to 0.03638
    lattice = _ladder()
    assert nearest_rung(lattice, Decimal("0.03637")) == Decimal("0.03638")
    assert nearest_rung(lattice, Decimal("0.03638")) == Decimal("0.03638")


def test_percent_take_profit_is_the_profit_ratio_above_the_entry() -> None:
    # the two fractions are independent: buys 0.11% apart, profit 0.66%,
    # so one take-profit reaches over several buy rungs
    geometry = _geometry()
    entry = Decimal("0.03634")
    target = geometry.tp_target(entry)
    assert _ratio_of(entry, target) == _TP_RATIO
    lattice = geometry.lattice
    assert lattice.index_of(target) - lattice.index_of(entry) == 4


def test_percent_take_profit_holds_the_ratio_at_any_price() -> None:
    # the point of the relative grid: the profit does not drift as the
    # market falls, where an absolute step would
    geometry = _geometry()
    for entry in (Decimal("0.03634"), Decimal("0.0088"), Decimal("0.00042")):
        assert _ratio_of(entry, geometry.tp_target(entry)) == _TP_RATIO


def test_percent_buy_ceiling_keeps_a_fill_below_the_wall() -> None:
    # a buy at the ceiling must have room for its own take-profit under
    # the lowest resting one, plus a rung for the buy itself
    geometry = _geometry()
    wall = Decimal("0.03664")
    ceiling = geometry.buy_ceiling(wall)
    assert ceiling == Decimal("0.03634")
    assert geometry.tp_target(ceiling) < wall


def test_percent_wall_floor_clears_the_nearest_buy() -> None:
    geometry = _geometry()
    buy = Decimal("0.03616")
    floor = geometry.wall_floor(buy)
    assert floor == Decimal("0.03649")
    assert floor > geometry.tp_target(buy)


def test_percent_geometry_rejects_a_profit_outside_the_unit_range() -> None:
    with pytest.raises(ValueError, match="tp_ratio"):
        PercentGeometry(
            lattice=percent_lattice(_STEP_RATIO, _TICK), tp_ratio=Decimal("1")
        )


def test_absolute_geometry_keeps_its_own_take_profit_step() -> None:
    geometry = AbsoluteGeometry(
        lattice=AbsoluteLattice(Decimal("0.00004")),
        tp_step=Decimal("0.00024"),
    )
    assert geometry.tp_target(Decimal("0.03640")) == Decimal("0.03664")
    assert geometry.buy_ceiling(Decimal("0.03664")) == Decimal("0.03636")
    assert geometry.wall_floor(Decimal("0.03636")) == Decimal("0.03664")


def test_build_geometry_picks_the_mode() -> None:
    percent = build_geometry(
        mode="percent",
        step=_STEP_RATIO,
        tp_step=_TP_RATIO,
        tick_size=_TICK,
    )
    absolute = build_geometry(
        mode="absolute",
        step=Decimal("0.00004"),
        tp_step=Decimal("0.00024"),
        tick_size=_TICK,
    )
    assert isinstance(percent, PercentGeometry)
    assert percent.tp_ratio == _TP_RATIO
    assert percent.lattice.ratio == _STEP_RATIO
    assert isinstance(absolute, AbsoluteGeometry)
    assert absolute.tp_step == Decimal("0.00024")
