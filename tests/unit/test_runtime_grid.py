from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal

from core.exchange.types import Execution, Side
from core.services.runtime import plan_level_heal, resting_buy_levels


def _exec(order_id: str) -> Execution:
    return Execution(
        exec_id=f"x-{order_id}",
        order_id=order_id,
        symbol="KASUSDT",
        side=Side.BUY,
        price=Decimal("0.0291"),
        qty=Decimal("171"),
        fee=Decimal(0),
        fee_coin="KAS",
        executed_at=datetime(2026, 7, 8, tzinfo=UTC),
    )


def test_plan_level_heal_idles_vanished_without_fill() -> None:
    # order 'gone' left the exchange with no fill -> idle the level for re-placement
    awaiting = [(291, "gone"), (290, "live")]
    idle, replay = plan_level_heal(awaiting, {"live"}, {})
    assert idle == [291]
    assert replay == []


def test_plan_level_heal_replays_vanished_with_fill() -> None:
    # order 'filled' vanished but has a matching fill -> replay to book the position
    fill = _exec("filled")
    awaiting = [(291, "filled")]
    idle, replay = plan_level_heal(awaiting, set(), {"filled": fill})
    assert idle == []
    assert replay == [(291, fill)]


def test_plan_level_heal_skips_levels_still_on_exchange() -> None:
    awaiting = [(291, "live1"), (290, "live2")]
    idle, replay = plan_level_heal(awaiting, {"live1", "live2"}, {})
    assert idle == []
    assert replay == []


def test_resting_levels_contiguous_when_nothing_held() -> None:
    levels = resting_buy_levels(Decimal("0.03086"), Decimal("0.0001"), 6, set())
    prices = [p for _, p in levels]
    assert prices == [
        Decimal("0.03080"),
        Decimal("0.03070"),
        Decimal("0.03060"),
        Decimal("0.03050"),
        Decimal("0.03040"),
        Decimal("0.03030"),
    ]
    assert all(p < Decimal("0.03086") for p in prices)


def test_resting_levels_skip_held_and_go_deeper() -> None:
    # keep 4 *resting* buys — held levels are skipped, deeper unheld ones take their place
    held = {Decimal("0.03080"), Decimal("0.03060")}
    levels = resting_buy_levels(Decimal("0.03086"), Decimal("0.0001"), 4, held)
    assert [p for _, p in levels] == [
        Decimal("0.03070"),
        Decimal("0.03050"),
        Decimal("0.03040"),
        Decimal("0.03030"),
    ]


def test_resting_levels_excludes_price_on_round_level() -> None:
    levels = resting_buy_levels(Decimal("0.03090"), Decimal("0.0001"), 3, set())
    assert [p for _, p in levels] == [
        Decimal("0.03080"),
        Decimal("0.03070"),
        Decimal("0.03060"),
    ]


def test_resting_levels_index_matches_price_over_step() -> None:
    levels = resting_buy_levels(Decimal("0.03086"), Decimal("0.0001"), 2, set())
    assert levels[0] == (308, Decimal("0.03080"))
    assert levels[1] == (307, Decimal("0.03070"))


def test_resting_levels_stop_at_zero() -> None:
    levels = resting_buy_levels(Decimal("0.0003"), Decimal("0.0001"), 10, set())
    assert [p for _, p in levels] == [Decimal("0.0002"), Decimal("0.0001")]


def test_resting_levels_empty_on_invalid_input() -> None:
    assert resting_buy_levels(Decimal("0"), Decimal("0.0001"), 6, set()) == []
    assert resting_buy_levels(Decimal("0.03"), Decimal("0"), 6, set()) == []
    assert resting_buy_levels(Decimal("0.03"), Decimal("0.0001"), 0, set()) == []
