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

_RATIO = Decimal("0.0066")
_TICK = Decimal("0.00001")
_MARKET = Decimal("0.03640")


def _ladder() -> PercentLattice:
    return PercentLattice(_RATIO, _TICK)


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


def test_percent_take_profit_is_the_rung_above_the_entry() -> None:
    geometry = PercentGeometry(lattice=_ladder())
    assert geometry.tp_target(Decimal("0.03613")) == Decimal("0.03638")


def test_percent_take_profit_keeps_the_ratio_for_an_off_ladder_entry() -> None:
    # a merged lot's weighted entry sits between rungs; its take-profit
    # still has to clear the ratio, not just reach the next rung
    geometry = PercentGeometry(lattice=_ladder())
    entry = Decimal("0.03616")
    target = geometry.tp_target(entry)
    assert (target - entry) / target >= _RATIO


def test_percent_buy_ceiling_leaves_two_rungs_under_the_wall() -> None:
    geometry = PercentGeometry(lattice=_ladder())
    assert geometry.buy_ceiling(Decimal("0.03663")) == Decimal("0.03613")
    assert geometry.wall_floor(Decimal("0.03613")) == Decimal("0.03663")


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
        step=_RATIO,
        tp_step=Decimal("0.00024"),
        tick_size=_TICK,
    )
    absolute = build_geometry(
        mode="absolute",
        step=Decimal("0.00004"),
        tp_step=Decimal("0.00024"),
        tick_size=_TICK,
    )
    assert isinstance(percent, PercentGeometry)
    assert isinstance(absolute, AbsoluteGeometry)
    assert absolute.tp_step == Decimal("0.00024")
