from __future__ import annotations

from decimal import Decimal

from core.services.runtime import band_levels, sell_band_gaps


def test_sell_band_gaps_fills_holes_in_near_cluster() -> None:
    # near cluster 0.0306..0.0312 with holes at 0.0307/0.0308; bag far above is ignored
    sells = [
        Decimal("0.03060"),
        Decimal("0.03090"),
        Decimal("0.03100"),
        Decimal("0.03110"),
        Decimal("0.03120"),
        Decimal("0.04275"),  # bag
        Decimal("0.05325"),  # bag
    ]
    gaps = sell_band_gaps(
        sells,
        step=Decimal("0.0001"),
        max_cluster_gap=Decimal("0.001"),
        min_sell=Decimal("0.03055"),
    )
    assert gaps == [Decimal("0.03070"), Decimal("0.03080")]


def test_sell_band_gaps_excludes_bag_entirely() -> None:
    # a contiguous near cluster with no holes -> nothing, bag never considered
    sells = [Decimal("0.03060"), Decimal("0.03070"), Decimal("0.05000")]
    gaps = sell_band_gaps(
        sells,
        step=Decimal("0.0001"),
        max_cluster_gap=Decimal("0.001"),
        min_sell=Decimal("0.03055"),
    )
    assert gaps == []


def test_sell_band_gaps_respects_min_sell_floor() -> None:
    sells = [Decimal("0.03060"), Decimal("0.03090")]
    # floor at 0.03080 drops 0.03070 (too close to market / below break-even)
    gaps = sell_band_gaps(
        sells,
        step=Decimal("0.0001"),
        max_cluster_gap=Decimal("0.001"),
        min_sell=Decimal("0.03080"),
    )
    assert gaps == [Decimal("0.03080")]


def test_sell_band_gaps_empty_input() -> None:
    assert sell_band_gaps([], Decimal("0.0001"), Decimal("0.001"), Decimal("0.03")) == []


def test_band_is_contiguous_and_strictly_below_price() -> None:
    levels = band_levels(Decimal("0.03086"), Decimal("0.0001"), 6)
    prices = [p for _, p in levels]
    assert prices == [
        Decimal("0.03080"),
        Decimal("0.03070"),
        Decimal("0.03060"),
        Decimal("0.03050"),
        Decimal("0.03040"),
        Decimal("0.03030"),
    ]
    # every level strictly below the market, evenly spaced by one step
    assert all(p < Decimal("0.03086") for p in prices)
    gaps = {prices[i] - prices[i + 1] for i in range(len(prices) - 1)}
    assert gaps == {Decimal("0.0001")}


def test_band_excludes_price_when_price_is_on_a_round_level() -> None:
    # price exactly on a round level -> top band level is one step below it
    levels = band_levels(Decimal("0.03090"), Decimal("0.0001"), 3)
    assert [p for _, p in levels] == [
        Decimal("0.03080"),
        Decimal("0.03070"),
        Decimal("0.03060"),
    ]


def test_band_level_index_matches_price_over_step() -> None:
    levels = band_levels(Decimal("0.03086"), Decimal("0.0001"), 2)
    assert levels[0] == (308, Decimal("0.03080"))
    assert levels[1] == (307, Decimal("0.03070"))


def test_band_stops_at_zero() -> None:
    levels = band_levels(Decimal("0.0003"), Decimal("0.0001"), 10)
    assert [p for _, p in levels] == [Decimal("0.0002"), Decimal("0.0001")]


def test_band_empty_on_invalid_input() -> None:
    assert band_levels(Decimal("0"), Decimal("0.0001"), 6) == []
    assert band_levels(Decimal("0.03"), Decimal("0"), 6) == []
    assert band_levels(Decimal("0.03"), Decimal("0.0001"), 0) == []
