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
    """How much of ``target`` the rise from ``entry`` earns."""
    return (target - entry) / target


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


def test_percent_take_profit_clears_the_ratio_and_rests_on_a_rung() -> None:
    # the two fractions are independent: buys 0.11% apart, profit 0.66%,
    # so one take-profit reaches over several buy rungs — and lands on a
    # rung, which is where the compensator looks for it
    geometry = _geometry()
    lattice = geometry.lattice
    entry = Decimal("0.03634")
    target = geometry.tp_target(entry)
    assert target == Decimal("0.03659")
    assert _ratio_of(entry, target) >= _TP_RATIO
    assert lattice.price_at(lattice.index_of(target)) == target
    assert lattice.index_of(target) - lattice.index_of(entry) == 5


def test_percent_take_profit_holds_the_ratio_at_any_price() -> None:
    # the point of the relative grid: the profit does not drift as the
    # market falls, where an absolute step would. Snapping to a rung can
    # only overshoot, and never by more than that rung.
    geometry = _geometry()
    lattice = geometry.lattice
    for index in (300, 900, 1500, 1876):
        entry = lattice.price_at(index)
        earned = _ratio_of(entry, geometry.tp_target(entry))
        assert _TP_RATIO <= earned < _TP_RATIO + _STEP_RATIO * 2


def test_percent_wall_stays_on_the_ladder_all_the_way_down() -> None:
    # an off-ladder take-profit would sit in a slot the compensator reads
    # as empty, so it would keep paying to move lots into a full wall
    geometry = _geometry()
    lattice = geometry.lattice
    for index in range(1, 1877, 7):
        target = geometry.tp_target(lattice.price_at(index))
        assert lattice.price_at(lattice.index_of(target)) == target


def test_percent_buy_ceiling_keeps_a_fill_below_the_wall() -> None:
    # a buy at the ceiling must have room for its own take-profit under
    # the lowest resting one, plus a rung for the buy itself
    geometry = _geometry()
    wall = Decimal("0.03664")
    ceiling = geometry.buy_ceiling(wall)
    assert ceiling == Decimal("0.03630")
    assert geometry.tp_target(ceiling) < wall


def test_percent_buy_ceiling_clears_the_wall_at_every_rung() -> None:
    # rounding used to leave the ceiling a rung high, landing a fill's
    # take-profit exactly on the bottom of the wall
    geometry = _geometry()
    lattice = geometry.lattice
    for index in range(1, 1877, 11):
        wall = lattice.price_at(index)
        ceiling = geometry.buy_ceiling(wall)
        if ceiling > 0:
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
