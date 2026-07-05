from __future__ import annotations

from decimal import Decimal

from core.services.runtime import band_levels


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
