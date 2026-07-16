from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest
from asgiref.sync import sync_to_async

from core.exchange.types import Execution, Side
from core.services.runtime import (
    _grid_params_changed,
    _grid_state,
    _record_applied_grid_params,
    _reset_all_grid_levels,
    another_instance_alive,
    buys_to_prune,
    naked_positions,
    plan_level_heal,
    resting_buy_levels,
)
from core.trading.models import BotStatus, GridLevel, LevelStatus, Position, PositionStatus


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
    # full-step clearance: 0.03080 sits only 0.00006 below 0.03086, so the top buy
    # is one step lower at 0.03070.
    levels = resting_buy_levels(Decimal("0.03086"), Decimal("0.0001"), 6, set())
    prices = [p for _, p in levels]
    assert prices == [
        Decimal("0.03070"),
        Decimal("0.03060"),
        Decimal("0.03050"),
        Decimal("0.03040"),
        Decimal("0.03030"),
        Decimal("0.03020"),
    ]
    assert Decimal("0.03086") - prices[0] >= Decimal("0.0001")  # a full step of clearance


def test_buys_to_prune_keeps_buys_the_falling_market_will_fill() -> None:
    # band (targets) is 0.02945..0.02925; the market has dropped and old near-market
    # buys 0.02965/60/55 sit ABOVE the band — they must be KEPT to fill, not pruned.
    targets = {
        Decimal("0.02945"),
        Decimal("0.02940"),
        Decimal("0.02935"),
        Decimal("0.02930"),
        Decimal("0.02925"),
    }
    resting = [Decimal("0.02965"), Decimal("0.02960"), Decimal("0.02955"), Decimal("0.02940")]
    assert buys_to_prune(resting, targets) == []


def test_buys_to_prune_cancels_only_below_band_bottom() -> None:
    # price rose: buys stranded strictly below the deepest target are pruned
    targets = {Decimal("0.02950"), Decimal("0.02945"), Decimal("0.02940")}
    resting = [Decimal("0.02945"), Decimal("0.02935"), Decimal("0.02930")]
    assert sorted(buys_to_prune(resting, targets)) == [Decimal("0.02930"), Decimal("0.02935")]


def test_buys_to_prune_empty_targets_prunes_nothing() -> None:
    assert buys_to_prune([Decimal("0.02950")], set()) == []


def test_naked_positions_flags_only_missing_tp_orders() -> None:
    candidates = [(1, "tp-live"), (2, "tp-gone"), (3, "tp-live2")]
    live = {"tp-live", "tp-live2", "some-buy"}
    assert naked_positions(candidates, live) == [(2, "tp-gone")]


def test_naked_positions_none_when_all_live() -> None:
    candidates = [(1, "a"), (2, "b")]
    assert naked_positions(candidates, {"a", "b"}) == []


def test_instance_guard_no_heartbeat_allows_start() -> None:
    now = datetime(2026, 7, 16, 12, 0, tzinfo=UTC)
    assert another_instance_alive(None, now, 90) is False


def test_instance_guard_fresh_heartbeat_blocks() -> None:
    now = datetime(2026, 7, 16, 12, 0, tzinfo=UTC)
    fresh = now - timedelta(seconds=20)  # peer beat 20s ago, within the 90s lease
    assert another_instance_alive(fresh, now, 90) is True


def test_instance_guard_stale_heartbeat_allows_start() -> None:
    now = datetime(2026, 7, 16, 12, 0, tzinfo=UTC)
    stale = now - timedelta(seconds=120)  # older than the lease ⇒ writer crashed
    assert another_instance_alive(stale, now, 90) is False


def test_resting_levels_full_step_gap() -> None:
    # fractional price: top buy a full step below (0.02978 -> 0.02970, not 0.02975)
    frac = resting_buy_levels(Decimal("0.02978"), Decimal("0.00005"), 3, set())
    assert [p for _, p in frac] == [
        Decimal("0.02970"),
        Decimal("0.02965"),
        Decimal("0.02960"),
    ]
    # on an exact round level the clearance is exactly one step: 0.02975 becomes a
    # buy only once the market reaches 0.02980.
    boundary = resting_buy_levels(Decimal("0.02980"), Decimal("0.00005"), 1, set())
    assert [p for _, p in boundary] == [Decimal("0.02975")]


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
    assert levels[0] == (307, Decimal("0.03070"))
    assert levels[1] == (306, Decimal("0.03060"))


def test_resting_levels_stop_at_zero() -> None:
    levels = resting_buy_levels(Decimal("0.0003"), Decimal("0.0001"), 10, set())
    assert [p for _, p in levels] == [Decimal("0.0002"), Decimal("0.0001")]


def test_resting_levels_empty_on_invalid_input() -> None:
    assert resting_buy_levels(Decimal("0"), Decimal("0.0001"), 6, set()) == []
    assert resting_buy_levels(Decimal("0.03"), Decimal("0"), 6, set()) == []
    assert resting_buy_levels(Decimal("0.03"), Decimal("0.0001"), 0, set()) == []


pytestmark_db = pytest.mark.django_db(transaction=True)


@pytestmark_db
def test_grid_params_first_run_adopts_without_change() -> None:
    bot = BotStatus.load()
    bot.applied_grid_step = None
    bot.applied_order_qty = None
    bot.save()
    # first sight: adopt current geometry, report "no change" (no spurious rebuild)
    assert _grid_params_changed(Decimal("0.0001"), Decimal("5")) is False
    bot.refresh_from_db()
    assert bot.applied_grid_step == Decimal("0.0001")
    assert bot.applied_order_qty == Decimal("5")


@pytestmark_db
def test_grid_params_detects_step_and_qty_change() -> None:
    _record_applied_grid_params(Decimal("0.0001"), Decimal("5"))
    assert _grid_params_changed(Decimal("0.0001"), Decimal("5")) is False
    assert _grid_params_changed(Decimal("0.00005"), Decimal("5")) is True  # step changed
    assert _grid_params_changed(Decimal("0.0001"), Decimal("10")) is True  # qty changed


@pytestmark_db
def test_reset_all_grid_levels_idles_awaiting() -> None:
    GridLevel.objects.create(
        level_index=291,
        target_buy_price=Decimal("0.0291"),
        status=LevelStatus.AWAITING_FILL,
        current_buy_order_id="ord-1",
    )
    GridLevel.objects.create(
        level_index=292,
        target_buy_price=Decimal("0.0292"),
        status=LevelStatus.FILLED,
        current_buy_order_id="",
    )
    _reset_all_grid_levels()
    g = GridLevel.objects.get(level_index=291)
    assert g.status == LevelStatus.IDLE
    assert g.current_buy_order_id == ""
    # a FILLED level (holds a position) is untouched
    assert GridLevel.objects.get(level_index=292).status == LevelStatus.FILLED


def _open_position(level_index: int, entry: str) -> None:
    Position.objects.create(
        level_index=level_index,
        entry_price=Decimal(entry),
        qty=Decimal("175"),
        fees_in=Decimal("0"),
        tp_order_id=f"tp-{level_index}",
        tp_price=Decimal(entry) + Decimal("0.0001"),
        status=PositionStatus.OPEN,
        opened_at=datetime(2026, 7, 8, tzinfo=UTC),
    )


@pytestmark_db
def test_grid_state_held_covers_every_open_position() -> None:
    step = Decimal("0.00005")
    _open_position(569, "0.02845")  # grid buy
    _open_position(3020, "0.02945")  # re-adopted lot (>=2000) must block its level too
    _open_position(1000, "0.052")  # manual bag (>=1000) must block its level too
    _resting, held = _grid_state(step)
    # every held price blocks a fresh buy there — one buy per level, no stacking
    assert held == {Decimal("0.02845"), Decimal("0.02945"), Decimal("0.052")}


@pytestmark_db
def test_grid_state_held_ignores_closed_positions() -> None:
    step = Decimal("0.00005")
    _open_position(569, "0.02845")
    Position.objects.create(
        level_index=570,
        entry_price=Decimal("0.0285"),
        qty=Decimal("175"),
        fees_in=Decimal("0"),
        tp_order_id="tp-closed",
        tp_price=Decimal("0.0286"),
        status=PositionStatus.CLOSED,
        opened_at=datetime(2026, 7, 8, tzinfo=UTC),
    )
    _resting, held = _grid_state(step)
    # a closed position frees its level for the grid again
    assert held == {Decimal("0.02845")}


async def test_heal_stale_buy_replay_failure_idles_level_no_loop() -> None:
    # A stale awaiting-fill level whose buy filled but whose TP can't be placed
    # (insufficient balance) must be idled, not crash the cycle or loop forever.
    from core.exchange.types import Instrument
    from core.services.order_manager import OrderManager
    from core.services.runtime import TraderRuntime
    from core.trading.models import StrategyConfig

    @sync_to_async
    def _setup() -> None:
        cfg = StrategyConfig.load()
        cfg.symbol = "KASUSDT"
        cfg.grid_mode = "absolute"
        cfg.grid_step = Decimal("0.00005")
        cfg.tp_step = Decimal("0.0001")
        cfg.order_qty_quote = Decimal("5")
        cfg.maker_fee = Decimal("0.000625")
        cfg.min_profit_quote = Decimal("0")
        cfg.save()
        GridLevel.objects.create(
            level_index=590,
            target_buy_price=Decimal("0.0295"),
            status=LevelStatus.AWAITING_FILL,
            current_buy_order_id="OID",
        )

    await _setup()
    cfg = await sync_to_async(StrategyConfig.load)()

    fill = Execution(
        exec_id="e-OID",
        order_id="OID",
        symbol="KASUSDT",
        side=Side.BUY,
        price=Decimal("0.0295"),
        qty=Decimal("170"),
        fee=Decimal("0"),
        fee_coin="KAS",
        executed_at=datetime(2026, 7, 12, tzinfo=UTC),
    )

    class FailingClient:
        async def get_open_orders(self, symbol: str) -> list:  # type: ignore[type-arg]
            return []  # OID no longer live -> stale

        async def get_executions(self, symbol: str, *, limit: int = 50) -> list:  # type: ignore[type-arg]
            return [fill]  # OID's buy did fill (recent history)

        async def place_limit(self, *a: object, **k: object) -> str:
            raise RuntimeError("Insufficient balance. (ErrCode: 170131)")

    instrument = Instrument(
        symbol="KASUSDT",
        base_coin="KAS",
        quote_coin="USDT",
        tick_size=Decimal("0.00001"),
        lot_size=Decimal("0.01"),
        min_order_qty=Decimal("0.01"),
        min_order_amt=Decimal("5"),
    )
    from core.services.events import NoOpEventBus

    om = OrderManager(client=FailingClient(), instrument=instrument, config=cfg, bus=NoOpEventBus())  # type: ignore[arg-type]
    rt = TraderRuntime()
    rt._om = om
    rt._current_price = Decimal("0.0295")

    await rt._heal_stale_buy_levels()  # must not raise

    level = await sync_to_async(GridLevel.objects.get)(level_index=590)
    assert level.status == LevelStatus.IDLE
    assert level.current_buy_order_id == ""


test_heal_stale_buy_replay_failure_idles_level_no_loop = pytest.mark.django_db(transaction=True)(
    test_heal_stale_buy_replay_failure_idles_level_no_loop
)


async def test_heal_stale_buy_replay_submin_fill_idles_level_no_loop() -> None:
    # A stale level whose buy only PARTIALLY filled below the min notional books no
    # position (handle_buy_fill returns None). It must still be idled — otherwise heal
    # replays the same sub-min fill every reconcile forever (the reconcile.drift loop).
    from core.exchange.types import Instrument
    from core.services.events import NoOpEventBus
    from core.services.order_manager import OrderManager
    from core.services.runtime import TraderRuntime
    from core.trading.models import StrategyConfig

    @sync_to_async
    def _setup() -> None:
        cfg = StrategyConfig.load()
        cfg.symbol = "KASUSDT"
        cfg.grid_mode = "absolute"
        cfg.grid_step = Decimal("0.00005")
        cfg.tp_step = Decimal("0.0001")
        cfg.order_qty_quote = Decimal("5")
        cfg.maker_fee = Decimal("0.000625")
        cfg.min_profit_quote = Decimal("0")
        cfg.save()
        GridLevel.objects.create(
            level_index=573,
            target_buy_price=Decimal("0.02865"),
            status=LevelStatus.AWAITING_FILL,
            current_buy_order_id="OID",
        )

    await _setup()
    cfg = await sync_to_async(StrategyConfig.load)()

    # 40 * 0.02865 = $1.15 notional — below the $5 minimum, so no TP/position is booked
    fill = Execution(
        exec_id="e-OID-partial",
        order_id="OID",
        symbol="KASUSDT",
        side=Side.BUY,
        price=Decimal("0.02865"),
        qty=Decimal("40"),
        fee=Decimal("0"),
        fee_coin="KAS",
        executed_at=datetime(2026, 7, 16, tzinfo=UTC),
    )

    class PartialFillClient:
        async def get_open_orders(self, symbol: str) -> list:  # type: ignore[type-arg]
            return []  # OID no longer live -> stale

        async def get_executions(self, symbol: str, *, limit: int = 50) -> list:  # type: ignore[type-arg]
            return [fill]

    instrument = Instrument(
        symbol="KASUSDT",
        base_coin="KAS",
        quote_coin="USDT",
        tick_size=Decimal("0.00001"),
        lot_size=Decimal("0.01"),
        min_order_qty=Decimal("0.01"),
        min_order_amt=Decimal("5"),
    )
    om = OrderManager(
        client=PartialFillClient(),  # type: ignore[arg-type]
        instrument=instrument,
        config=cfg,
        bus=NoOpEventBus(),
    )
    rt = TraderRuntime()
    rt._om = om
    rt._current_price = Decimal("0.0286")

    await rt._heal_stale_buy_levels()  # must not raise

    level = await sync_to_async(GridLevel.objects.get)(level_index=573)
    assert level.status == LevelStatus.IDLE
    assert level.current_buy_order_id == ""


test_heal_stale_buy_replay_submin_fill_idles_level_no_loop = pytest.mark.django_db(
    transaction=True
)(test_heal_stale_buy_replay_submin_fill_idles_level_no_loop)
