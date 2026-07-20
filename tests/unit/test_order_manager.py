"""Tests for OrderManager. Uses a fake BybitClient and the real ORM."""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

import pytest
from asgiref.sync import sync_to_async

from core.exchange.types import Execution, Instrument, Side
from core.services.events import RecordingEventBus
from core.services.order_manager import OrderManager
from core.services.protector import Protector
from core.trading.models import (
    CompensationLink,
    ExecutionLog,
    GridLevel,
    LevelStatus,
    Position,
    PositionStatus,
    StrategyConfig,
)

pytestmark = pytest.mark.django_db(transaction=True)


class FakeBybitClient:
    """Records place/cancel calls and returns deterministic order IDs."""

    def __init__(self) -> None:
        self.placed: list[dict[str, Any]] = []
        self.cancelled: list[tuple[str, str]] = []
        self._counter = 0
        self.next_id: str | None = None

    async def place_limit(
        self,
        symbol: str,
        side: Side,
        qty: Decimal,
        price: Decimal,
        *,
        order_link_id: str | None = None,
        post_only: bool = True,
    ) -> str:
        self._counter += 1
        order_id = self.next_id or f"ord-{self._counter}"
        self.next_id = None
        self.placed.append(
            {
                "symbol": symbol,
                "side": side,
                "qty": qty,
                "price": price,
                "link": order_link_id,
                "order_id": order_id,
            }
        )
        return order_id

    async def cancel_order(self, symbol: str, order_id: str) -> None:
        self.cancelled.append((symbol, order_id))


@pytest.fixture
def instrument() -> Instrument:
    return Instrument(
        symbol="BTCUSDT",
        base_coin="BTC",
        quote_coin="USDT",
        tick_size=Decimal("0.01"),
        lot_size=Decimal("0.000001"),
        min_order_qty=Decimal("0.000001"),
        min_order_amt=Decimal("5"),
    )


@pytest.fixture
def client() -> FakeBybitClient:
    return FakeBybitClient()


@pytest.fixture
def config() -> StrategyConfig:
    cfg = StrategyConfig.load()
    cfg.symbol = "BTCUSDT"
    cfg.grid_mode = "percent"
    cfg.grid_step = Decimal("0.01")
    cfg.order_qty_quote = Decimal("20")
    cfg.min_profit_quote = Decimal("0.05")
    cfg.maker_fee = Decimal("0.001")
    cfg.max_open_orders = 10
    cfg.tp_step = Decimal("100")  # BTC-scale absolute TP offset
    cfg.save()
    return cfg


@pytest.fixture
def bus() -> RecordingEventBus:
    return RecordingEventBus()


@pytest.fixture
def om(
    client: FakeBybitClient,
    instrument: Instrument,
    config: StrategyConfig,
    bus: RecordingEventBus,
) -> OrderManager:
    return OrderManager(
        client=client,  # type: ignore[arg-type]
        instrument=instrument,
        config=config,
        bus=bus,
    )


@pytest.fixture
def protector(
    client: FakeBybitClient,
    instrument: Instrument,
    config: StrategyConfig,
    bus: RecordingEventBus,
) -> Protector:
    return Protector(
        client=client,  # type: ignore[arg-type]
        instrument=instrument,
        config=config,
        bus=bus,
    )


async def test_place_buy_at_level_persists_and_calls_client(
    om: OrderManager, client: FakeBybitClient, bus: RecordingEventBus
) -> None:
    order_id = await om.place_buy_at_level(0, Decimal("60000"))
    assert order_id == "ord-1"
    assert len(client.placed) == 1
    placed = client.placed[0]
    assert placed["side"] == Side.BUY
    # qty = 20 / 60000 floored to lot_size (0.000001) → 0.000333
    assert placed["qty"] == Decimal("0.000333")
    level = await GridLevel.objects.aget(level_index=0)
    assert level.status == LevelStatus.AWAITING_FILL
    assert level.current_buy_order_id == "ord-1"
    assert bus.events[0][0] == "order.placed"


async def test_place_buy_skips_below_minimum(
    om: OrderManager, client: FakeBybitClient, config: StrategyConfig
) -> None:
    config.order_qty_quote = Decimal("1")  # below min_order_amt of 5
    await sync_to_async(config.save)()
    om.config = config
    order_id = await om.place_buy_at_level(0, Decimal("60000"))
    assert order_id is None
    assert client.placed == []


async def test_handle_buy_fill_creates_position_and_places_tp(
    om: OrderManager, client: FakeBybitClient, bus: RecordingEventBus
) -> None:
    # Pre-place a buy order
    client.next_id = "buy-1"
    await om.place_buy_at_level(0, Decimal("60000"))
    client.next_id = "tp-1"
    execution = _exec(
        exec_id="e1",
        order_id="buy-1",
        side=Side.BUY,
        price=Decimal("60000"),
        qty=Decimal("0.000333"),
        fee=Decimal("0.000000333"),  # in BTC
        fee_coin="BTC",
    )
    level_index = await om.handle_buy_fill(execution)
    assert level_index == 0
    # Position created
    position = await Position.objects.aget(level_index=0)
    assert position.status == PositionStatus.OPEN
    assert position.tp_order_id == "tp-1"
    assert position.tp_price is not None and position.tp_price > Decimal(
        "60000"
    )
    # TP placed
    assert any(
        p["side"] == Side.SELL and p["order_id"] == "tp-1"
        for p in client.placed
    )
    # Grid level marked filled
    level = await GridLevel.objects.aget(level_index=0)
    assert level.status == LevelStatus.FILLED
    # Execution logged
    assert await ExecutionLog.objects.filter(exec_id="e1").aexists()
    # Event published
    assert any(e[0] == "position.opened" for e in bus.events)


async def test_handle_buy_fill_too_small_leaves_coin_free(
    om: OrderManager, client: FakeBybitClient
) -> None:
    # A dust partial fill (notional below the $5 minimum) must not create a
    # position with an absurd min-notional TP — leave the coin free.
    client.next_id = "buy-dust"
    await om.place_buy_at_level(0, Decimal("60000"))
    execution = _exec(
        exec_id="ed",
        order_id="buy-dust",
        side=Side.BUY,
        price=Decimal("60000"),
        qty=Decimal("0.00001"),  # $0.60 < $5 min
        fee=Decimal("0"),
        fee_coin="BTC",
    )
    assert await om.handle_buy_fill(execution) is None
    assert not await Position.objects.filter(level_index=0).aexists()
    # no take-profit sell was placed
    assert not any(p["side"] == Side.SELL for p in client.placed)


async def test_handle_buy_fill_with_no_matching_level_warns_and_returns_none(
    om: OrderManager,
) -> None:
    execution = _exec(
        exec_id="e0",
        order_id="orphan",
        side=Side.BUY,
        price=Decimal("60000"),
        qty=Decimal("0.001"),
        fee=Decimal("0.06"),
        fee_coin="USDT",
    )
    assert await om.handle_buy_fill(execution) is None


async def test_handle_sell_fill_closes_position_and_runs_compensation(
    om: OrderManager, client: FakeBybitClient, bus: RecordingEventBus
) -> None:
    # Open two positions: one underwater, one about to close in profit
    underwater = await Position.objects.acreate(
        level_index=1,
        entry_price=Decimal("60000"),
        qty=Decimal("0.001"),
        fees_in=Decimal("0.06"),
        tp_order_id="tp-old",
        tp_price=Decimal("60600"),
        status=PositionStatus.OPEN,
        opened_at=datetime.now(tz=UTC),
    )
    winner = await Position.objects.acreate(
        level_index=0,
        entry_price=Decimal("58000"),
        qty=Decimal("0.001"),
        fees_in=Decimal("0.058"),
        tp_order_id="tp-win",
        tp_price=Decimal("58580"),
        status=PositionStatus.OPEN,
        opened_at=datetime.now(tz=UTC),
    )
    client.next_id = "tp-new"
    execution = _exec(
        exec_id="es1",
        order_id="tp-win",
        side=Side.SELL,
        price=Decimal("58580"),
        qty=Decimal("0.001"),
        fee=Decimal("0.0586"),
        fee_coin="USDT",
    )
    level_index = await om.handle_sell_fill(
        execution, current_price=Decimal("57000")
    )
    assert level_index == 0
    # Winner closed
    await winner.arefresh_from_db()
    assert winner.status == PositionStatus.CLOSED
    assert winner.realized_pnl > 0
    # Underwater position got a new TP
    await underwater.arefresh_from_db()
    assert underwater.tp_order_id == "tp-new"
    assert underwater.tp_price is not None and underwater.tp_price < Decimal(
        "60600"
    )
    # Old TP cancelled, new TP placed
    assert ("BTCUSDT", "tp-old") in client.cancelled
    # CompensationLink recorded
    link = await CompensationLink.objects.aget(
        compensated_position=underwater.id
    )
    assert link.profitable_position_id == winner.id
    # Events
    kinds = [e[0] for e in bus.events]
    assert "position.closed" in kinds
    assert "compensation.applied" in kinds
    comp = next(p for k, p in bus.events if k == "compensation.applied")
    assert Decimal(comp["old_tp"]) == Decimal("60600")
    assert Decimal(comp["new_tp"]) < Decimal("60600")


async def _open_pos() -> Position:
    return await Position.objects.acreate(
        level_index=5,
        entry_price=Decimal("60000"),
        qty=Decimal("0.001"),
        fees_in=Decimal("0.06"),
        tp_order_id="tp-partial",
        tp_price=Decimal("60600"),
        status=PositionStatus.OPEN,
        opened_at=datetime.now(tz=UTC),
    )


async def test_sell_partial_fill_keeps_position_open(
    om: OrderManager, bus: RecordingEventBus
) -> None:
    pos = await _open_pos()
    execution = _exec(
        exec_id="p1",
        order_id="tp-partial",
        side=Side.SELL,
        price=Decimal("60600"),
        qty=Decimal("0.0004"),  # partial of 0.001
        fee=Decimal("0.024"),
        fee_coin="USDT",
    )
    result = await om.handle_sell_fill(
        execution, current_price=Decimal("60000")
    )
    assert result is None  # not fully closed
    await pos.arefresh_from_db()
    assert pos.status == PositionStatus.OPEN
    assert pos.filled_qty == Decimal("0.0004")
    assert await CompensationLink.objects.acount() == 0
    assert "position.closed" not in [e[0] for e in bus.events]


async def test_sell_completing_fill_closes_with_correct_pnl(
    om: OrderManager,
) -> None:
    pos = await _open_pos()
    for eid, q in (("c1", "0.0004"), ("c2", "0.0006")):
        await om.handle_sell_fill(
            _exec(
                exec_id=eid,
                order_id="tp-partial",
                side=Side.SELL,
                price=Decimal("60600"),
                qty=Decimal(q),
                fee=Decimal("60600") * Decimal(q) * Decimal("0.001"),
                fee_coin="USDT",
            ),
            current_price=Decimal("60000"),
        )
    await pos.arefresh_from_db()
    assert pos.status == PositionStatus.CLOSED
    assert pos.filled_qty == Decimal("0.001")
    # PnL from full proceeds and full cost, not a partial-vs-full mismatch.
    proceeds = Decimal("60600") * Decimal("0.001")
    expected = (
        proceeds
        - pos.fees_out
        - Decimal("60000") * Decimal("0.001")
        - Decimal("0.06")
    )
    assert pos.realized_pnl == expected
    assert pos.realized_pnl > 0


async def test_sell_fill_idempotent_on_exec_id(om: OrderManager) -> None:
    pos = await _open_pos()
    ex = _exec(
        exec_id="dup",
        order_id="tp-partial",
        side=Side.SELL,
        price=Decimal("60600"),
        qty=Decimal("0.0004"),
        fee=Decimal("0.024"),
        fee_coin="USDT",
    )
    await om.handle_sell_fill(ex, current_price=Decimal("60000"))
    await om.handle_sell_fill(
        ex, current_price=Decimal("60000")
    )  # redelivered
    await pos.arefresh_from_db()
    assert pos.filled_qty == Decimal("0.0004")  # not doubled


async def test_compensation_skips_below_min_notional_without_cancelling(
    om: OrderManager, client: FakeBybitClient
) -> None:
    # Underwater position so small that a re-priced sell would fall below the
    # $5 exchange minimum — compensation must SKIP and leave the old order
    # untouched.
    underwater = await Position.objects.acreate(
        level_index=1,
        entry_price=Decimal("60000"),
        qty=Decimal("0.00005"),  # notional ~$3 — below min_order_amt
        fees_in=Decimal("0"),
        tp_order_id="tp-under",
        tp_price=Decimal("61000"),
        status=PositionStatus.OPEN,
        opened_at=datetime.now(tz=UTC),
    )
    await Position.objects.acreate(
        level_index=0,
        entry_price=Decimal("58000"),
        qty=Decimal("0.001"),
        fees_in=Decimal("0.058"),
        tp_order_id="tp-win",
        tp_price=Decimal("58580"),
        status=PositionStatus.OPEN,
        opened_at=datetime.now(tz=UTC),
    )
    execution = _exec(
        exec_id="es3",
        order_id="tp-win",
        side=Side.SELL,
        price=Decimal("58580"),
        qty=Decimal("0.001"),
        fee=Decimal("0.0586"),
        fee_coin="USDT",
    )
    await om.handle_sell_fill(execution, current_price=Decimal("57000"))
    # Old order left in place, nothing cancelled, no compensation recorded.
    await underwater.arefresh_from_db()
    assert underwater.tp_order_id == "tp-under"
    assert ("BTCUSDT", "tp-under") not in client.cancelled
    assert await CompensationLink.objects.acount() == 0


async def test_handle_sell_fill_no_compensation_when_all_profitable(
    om: OrderManager, client: FakeBybitClient
) -> None:
    # Only one position, the one being closed — no other open ones to
    # compensate
    pos = await Position.objects.acreate(
        level_index=0,
        entry_price=Decimal("58000"),
        qty=Decimal("0.001"),
        fees_in=Decimal("0.058"),
        tp_order_id="tp-win",
        tp_price=Decimal("58580"),
        status=PositionStatus.OPEN,
        opened_at=datetime.now(tz=UTC),
    )
    execution = _exec(
        exec_id="es2",
        order_id="tp-win",
        side=Side.SELL,
        price=Decimal("58580"),
        qty=Decimal("0.001"),
        fee=Decimal("0.0586"),
        fee_coin="USDT",
    )
    await om.handle_sell_fill(execution, current_price=Decimal("58600"))
    await pos.arefresh_from_db()
    assert pos.status == PositionStatus.CLOSED
    # No cancellations / new TPs
    assert client.cancelled == []


def _exec(
    *,
    exec_id: str,
    order_id: str,
    side: Side,
    price: Decimal,
    qty: Decimal,
    fee: Decimal,
    fee_coin: str,
) -> Execution:
    return Execution(
        exec_id=exec_id,
        order_id=order_id,
        symbol="BTCUSDT",
        side=side,
        price=price,
        qty=qty,
        fee=fee,
        fee_coin=fee_coin,
        executed_at=datetime.now(tz=UTC),
    )


async def test_reprotect_places_maker_sell_above_market(
    protector: Protector, client: FakeBybitClient
) -> None:
    pos = await sync_to_async(Position.objects.create)(
        level_index=5,
        entry_price=Decimal("59000"),
        qty=Decimal("0.001"),
        tp_price=Decimal("59500"),
        status=PositionStatus.OPEN,
        opened_at=datetime.now(tz=UTC),
    )
    # market ran up to 60000: the original TP (59500) now sits below market, so
    # the reprotected sell is floored one tick above market instead of
    # crossing.
    order_id = await protector.reprotect(pos, current_price=Decimal("60000"))
    placed = client.placed[-1]
    assert placed["side"] == Side.SELL
    assert placed["qty"] == Decimal("0.001")
    assert placed["price"] == Decimal("60000.01")  # one tick above market
    await sync_to_async(pos.refresh_from_db)()
    assert pos.tp_price == Decimal("60000.01")
    assert pos.tp_order_id == order_id


async def test_reprotect_covers_only_the_unsold_remainder(
    protector: Protector, client: FakeBybitClient
) -> None:
    pos = await sync_to_async(Position.objects.create)(
        level_index=6,
        entry_price=Decimal("59000"),
        qty=Decimal("0.005"),
        filled_qty=Decimal("0.003"),
        tp_price=Decimal("59500"),
        status=PositionStatus.OPEN,
        opened_at=datetime.now(tz=UTC),
    )
    await protector.reprotect(pos, current_price=Decimal("59000"))
    placed = client.placed[-1]
    # only the 0.002 still held is re-listed, never the full 0.005
    assert placed["qty"] == Decimal("0.002")


async def test_settle_phantom_closes_at_tp_and_frees_level(
    protector: Protector, config: StrategyConfig, bus: RecordingEventBus
) -> None:
    await sync_to_async(GridLevel.objects.create)(
        level_index=7,
        target_buy_price=Decimal("59000"),
        status=LevelStatus.FILLED,
    )
    pos = await sync_to_async(Position.objects.create)(
        level_index=7,
        entry_price=Decimal("59000"),
        qty=Decimal("0.001"),
        fees_in=Decimal("0"),
        tp_price=Decimal("59100"),
        status=PositionStatus.OPEN,
        opened_at=datetime.now(tz=UTC),
    )
    realized = await protector.settle_phantom(pos)

    await sync_to_async(pos.refresh_from_db)()
    assert pos.status == PositionStatus.CLOSED
    assert pos.filled_qty == Decimal("0.001")
    # booked at the recorded TP price, net of the maker sell fee
    expected = Decimal("59100") * Decimal("0.001") * (
        Decimal(1) - config.maker_fee
    ) - Decimal("59000") * Decimal("0.001")
    assert pos.realized_pnl == expected
    assert realized == expected
    # its grid level is freed for re-use
    level = await sync_to_async(GridLevel.objects.get)(level_index=7)
    assert level.status == LevelStatus.IDLE
    # and a position.closed event is emitted
    assert any(t == "position.closed" for t, _ in bus.events)
