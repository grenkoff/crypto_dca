"""Tests for OrderManager. Uses a fake BybitClient and the real DAO."""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

import pytest
from sqlalchemy import func, select

from core.db.models import (
    BotStatus,
    CompensationLink,
    ExecutionLog,
    GridLevel,
    LevelStatus,
    Position,
    PositionStatus,
    StrategyConfig,
)
from core.db.session import new_session
from core.exchange.types import Balance, Execution, Instrument, Side
from core.services import repository
from core.services.balances import BalanceCache
from core.services.events import RecordingEventBus
from core.services.order_manager import OrderManager
from core.services.protector import Protector
from tests.conftest import add_one

pytestmark = pytest.mark.db


async def _get_level(level_index: int) -> GridLevel:
    async with new_session() as session:
        level = await session.scalar(
            select(GridLevel).where(GridLevel.level_index == level_index)
        )
    assert level is not None
    return level


async def _position_at(level_index: int) -> Position:
    async with new_session() as session:
        pos = await session.scalar(
            select(Position).where(Position.level_index == level_index)
        )
    assert pos is not None
    return pos


async def _exec_exists(exec_id: str) -> bool:
    async with new_session() as session:
        found = await session.scalar(
            select(ExecutionLog.id).where(ExecutionLog.exec_id == exec_id)
        )
    return found is not None


async def _count(model: type[Any]) -> int:
    async with new_session() as session:
        n = await session.scalar(select(func.count()).select_from(model))
    return int(n or 0)


class FakeBybitClient:
    """Records place/cancel calls and returns deterministic order IDs."""

    def __init__(self) -> None:
        self.placed: list[dict[str, Any]] = []
        self.cancelled: list[tuple[str, str]] = []
        self._counter = 0
        self.next_id: str | None = None
        self.sold: list[dict[str, Any]] = []
        self.fills: dict[str, Decimal] = {}
        self.market_price = Decimal("40000")
        self.base_free = Decimal("0")
        self.recent: list[Execution] = []

    async def get_balances(self) -> dict[str, Balance]:
        return {
            "USDT": Balance(
                coin="USDT", free=Decimal("50"), locked=Decimal("0")
            ),
            "BTC": Balance(
                coin="BTC", free=self.base_free, locked=Decimal("0")
            ),
        }

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

    async def place_market(
        self,
        symbol: str,
        side: Side,
        qty: Decimal,
        *,
        order_link_id: str,
    ) -> str:
        self._counter += 1
        order_id = f"mkt-{self._counter}"
        self.sold.append({"symbol": symbol, "side": side, "qty": qty})
        self.fills[order_id] = qty
        return order_id

    async def get_order_executions(
        self, symbol: str, order_id: str, *, limit: int = 50
    ) -> list[Execution]:
        qty = self.fills[order_id]
        return [
            Execution(
                exec_id=f"x-{order_id}",
                order_id=order_id,
                symbol=symbol,
                side=Side.SELL,
                price=self.market_price,
                qty=qty,
                fee=self.market_price * qty * Decimal("0.00075"),
                fee_coin="USDT",
                executed_at=datetime(2026, 7, 4, tzinfo=UTC),
            )
        ]

    async def cancel_order(self, symbol: str, order_id: str) -> None:
        self.cancelled.append((symbol, order_id))

    async def get_executions(
        self, symbol: str, *, limit: int = 100
    ) -> list[Execution]:
        return self.recent

    async def get_open_orders(self, symbol: str) -> list[Any]:
        return []


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
    # In-memory SA config: the services only read its fields, never persist it.
    return StrategyConfig(
        symbol="BTCUSDT",
        grid_mode="absolute",
        grid_step=Decimal("10"),
        order_qty_quote=Decimal("20"),
        min_profit_quote=Decimal("0.05"),
        maker_fee=Decimal("0.001"),
        taker_fee=Decimal("0.00075"),
        max_open_orders=10,
        tp_step=Decimal("100"),  # BTC-scale absolute TP offset
        comp_share_min=Decimal("0.20"),
        comp_share_max=Decimal("0.80"),
        comp_hole_offset=Decimal("0"),
    )


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
        balances=BalanceCache(client),  # type: ignore[arg-type]
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
    level = await _get_level(0)
    assert level.status == LevelStatus.AWAITING_FILL
    assert level.current_buy_order_id == "ord-1"
    assert bus.events[0][0] == "order.placed"


async def test_place_buy_skips_below_minimum(
    om: OrderManager, client: FakeBybitClient, config: StrategyConfig
) -> None:
    config.order_qty_quote = Decimal("1")  # below min_order_amt of 5
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
    position = await _position_at(0)
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
    level = await _get_level(0)
    assert level.status == LevelStatus.FILLED
    # Execution logged
    assert await _exec_exists("e1")
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
    assert await _count(Position) == 0
    # no take-profit sell was placed
    assert not any(p["side"] == Side.SELL for p in client.placed)


async def test_handle_sell_fill_closes_position_and_runs_compensation(
    om: OrderManager, client: FakeBybitClient, bus: RecordingEventBus
) -> None:
    # Open two positions: one underwater, one about to close in profit
    underwater = await add_one(
        Position(
            level_index=1,
            entry_price=Decimal("60000"),
            qty=Decimal("0.001"),
            fees_in=Decimal("0.06"),
            tp_order_id="tp-old",
            tp_price=Decimal("60600"),
            status=PositionStatus.OPEN,
            opened_at=datetime.now(tz=UTC),
        )
    )
    winner = await add_one(
        Position(
            level_index=0,
            entry_price=Decimal("58000"),
            qty=Decimal("0.001"),
            fees_in=Decimal("0.058"),
            tp_order_id="tp-win",
            tp_price=Decimal("58580"),
            status=PositionStatus.OPEN,
            opened_at=datetime.now(tz=UTC),
        )
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
    winner = await repository.get_position(winner.id)
    assert winner.status == PositionStatus.CLOSED
    assert winner.realized_pnl > 0
    # Underwater position got a new TP, possibly several steps down
    underwater = await repository.get_position(underwater.id)
    assert underwater.tp_order_id not in ("", "tp-old")
    assert underwater.tp_price is not None and underwater.tp_price < Decimal(
        "60600"
    )
    # Old TP cancelled, new TP placed
    assert ("BTCUSDT", "tp-old") in client.cancelled
    # CompensationLink recorded
    async with new_session() as session:
        link = await session.scalar(
            select(CompensationLink).where(
                CompensationLink.compensated_position_id == underwater.id
            )
        )
    assert link is not None
    assert link.profitable_position_id == winner.id
    # Events
    kinds = [e[0] for e in bus.events]
    assert "position.closed" in kinds
    closed = [e for e in bus.events if e[0] == "position.closed"]
    assert closed and closed[0][1]["compensations"]
    move = closed[0][1]["compensations"][0]
    assert Decimal(move["old_tp"]) == Decimal("60600")
    assert Decimal(move["new_tp"]) < Decimal("60600")


async def _open_pos() -> Position:
    return await add_one(
        Position(
            level_index=5,
            entry_price=Decimal("60000"),
            qty=Decimal("0.001"),
            fees_in=Decimal("0.06"),
            tp_order_id="tp-partial",
            tp_price=Decimal("60600"),
            status=PositionStatus.OPEN,
            opened_at=datetime.now(tz=UTC),
        )
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
    pos = await repository.get_position(pos.id)
    assert pos.status == PositionStatus.OPEN
    assert pos.filled_qty == Decimal("0.0004")
    assert await _count(CompensationLink) == 0
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
    pos = await repository.get_position(pos.id)
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
    pos = await repository.get_position(pos.id)
    assert pos.filled_qty == Decimal("0.0004")  # not doubled


async def test_compensation_skips_below_min_notional_without_cancelling(
    om: OrderManager, client: FakeBybitClient
) -> None:
    # Underwater position so small that a re-priced sell would fall below the
    # $5 exchange minimum — compensation must SKIP and leave the old order
    # untouched.
    underwater = await add_one(
        Position(
            level_index=1,
            entry_price=Decimal("60000"),
            qty=Decimal("0.00005"),  # notional ~$3 — below min_order_amt
            tp_order_id="tp-under",
            tp_price=Decimal("61000"),
            status=PositionStatus.OPEN,
            opened_at=datetime.now(tz=UTC),
        )
    )
    await add_one(
        Position(
            level_index=0,
            entry_price=Decimal("58000"),
            qty=Decimal("0.001"),
            fees_in=Decimal("0.058"),
            tp_order_id="tp-win",
            tp_price=Decimal("58580"),
            status=PositionStatus.OPEN,
            opened_at=datetime.now(tz=UTC),
        )
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
    underwater = await repository.get_position(underwater.id)
    assert underwater.tp_order_id == "tp-under"
    assert ("BTCUSDT", "tp-under") not in client.cancelled
    assert await _count(CompensationLink) == 0


async def test_handle_sell_fill_no_compensation_when_all_profitable(
    om: OrderManager, client: FakeBybitClient
) -> None:
    # Only one position, the one being closed — no other open ones to
    # compensate
    pos = await add_one(
        Position(
            level_index=0,
            entry_price=Decimal("58000"),
            qty=Decimal("0.001"),
            fees_in=Decimal("0.058"),
            tp_order_id="tp-win",
            tp_price=Decimal("58580"),
            status=PositionStatus.OPEN,
            opened_at=datetime.now(tz=UTC),
        )
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
    pos = await repository.get_position(pos.id)
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
    pos = await add_one(
        Position(
            level_index=5,
            entry_price=Decimal("59000"),
            qty=Decimal("0.001"),
            tp_price=Decimal("59500"),
            status=PositionStatus.OPEN,
            opened_at=datetime.now(tz=UTC),
        )
    )
    # market ran up to 60000: the original TP (59500) now sits below market, so
    # the reprotected sell is floored one tick above market instead of
    # crossing.
    order_id = await protector.reprotect(pos, current_price=Decimal("60000"))
    placed = client.placed[-1]
    assert placed["side"] == Side.SELL
    assert placed["qty"] == Decimal("0.001")
    assert placed["price"] == Decimal("60000.01")  # one tick above market
    pos = await repository.get_position(pos.id)
    assert pos.tp_price == Decimal("60000.01")
    assert pos.tp_order_id == order_id


async def test_reprotect_covers_only_the_unsold_remainder(
    protector: Protector, client: FakeBybitClient
) -> None:
    pos = await add_one(
        Position(
            level_index=6,
            entry_price=Decimal("59000"),
            qty=Decimal("0.005"),
            filled_qty=Decimal("0.003"),
            tp_price=Decimal("59500"),
            status=PositionStatus.OPEN,
            opened_at=datetime.now(tz=UTC),
        )
    )
    await protector.reprotect(pos, current_price=Decimal("59000"))
    placed = client.placed[-1]
    # only the 0.002 still held is re-listed, never the full 0.005
    assert placed["qty"] == Decimal("0.002")


async def test_settle_phantom_closes_at_tp_and_frees_level(
    protector: Protector, config: StrategyConfig, bus: RecordingEventBus
) -> None:
    await add_one(
        GridLevel(
            level_index=7,
            target_buy_price=Decimal("59000"),
            status=LevelStatus.FILLED,
        )
    )
    pos = await add_one(
        Position(
            level_index=7,
            entry_price=Decimal("59000"),
            qty=Decimal("0.001"),
            tp_price=Decimal("59100"),
            status=PositionStatus.OPEN,
            opened_at=datetime.now(tz=UTC),
        )
    )
    realized = await protector.settle_phantom(pos)

    pos = await repository.get_position(pos.id)
    assert pos.status == PositionStatus.CLOSED
    assert pos.filled_qty == Decimal("0.001")
    # booked at the recorded TP price, net of the maker sell fee
    expected = Decimal("59100") * Decimal("0.001") * (
        Decimal(1) - config.maker_fee
    ) - Decimal("59000") * Decimal("0.001")
    assert pos.realized_pnl == expected
    assert realized == expected
    # its grid level is freed for re-use
    level = await _get_level(7)
    assert level.status == LevelStatus.IDLE
    # and a position.closed event is emitted
    assert any(t == "position.closed" for t, _ in bus.events)


async def test_a_close_fills_both_the_pool_and_the_pocket(
    om: OrderManager, client: FakeBybitClient
) -> None:
    await add_one(BotStatus(id=1))
    winner = await add_one(
        Position(
            level_index=0,
            entry_price=Decimal("58000"),
            qty=Decimal("0.001"),
            fees_in=Decimal("0.058"),
            tp_order_id="tp-win",
            tp_price=Decimal("58580"),
            status=PositionStatus.OPEN,
            opened_at=datetime.now(tz=UTC),
        )
    )
    execution = _exec(
        exec_id="split-1",
        order_id="tp-win",
        side=Side.SELL,
        price=Decimal("58580"),
        qty=Decimal("0.001"),
        fee=Decimal("0.0586"),
        fee_coin="USDT",
    )
    await om.handle_sell_fill(execution, current_price=Decimal("57000"))
    closed = await repository.get_position(winner.id)
    async with new_session() as session:
        bot = await session.get(BotStatus, 1)
    assert bot is not None
    assert bot.pending_credit > 0
    assert bot.pocket_credit > 0
    assert bot.pending_credit + bot.pocket_credit == closed.realized_pnl


async def test_a_light_load_sends_most_profit_to_the_pocket(
    om: OrderManager, client: FakeBybitClient
) -> None:
    await add_one(BotStatus(id=1))
    winner = await add_one(
        Position(
            level_index=0,
            entry_price=Decimal("58000"),
            qty=Decimal("0.001"),
            fees_in=Decimal("0.058"),
            tp_order_id="tp-win",
            tp_price=Decimal("58580"),
            status=PositionStatus.OPEN,
            opened_at=datetime.now(tz=UTC),
        )
    )
    execution = _exec(
        exec_id="split-2",
        order_id="tp-win",
        side=Side.SELL,
        price=Decimal("58580"),
        qty=Decimal("0.001"),
        fee=Decimal("0.0586"),
        fee_coin="USDT",
    )
    await om.handle_sell_fill(execution, current_price=Decimal("57000"))
    async with new_session() as session:
        bot = await session.get(BotStatus, 1)
    assert bot is not None
    closed = await repository.get_position(winner.id)
    share = bot.pending_credit / closed.realized_pnl
    assert share == Decimal("0.20")


async def test_drain_pool_stays_quiet_when_the_pool_is_empty(
    om: OrderManager, bus: RecordingEventBus
) -> None:
    await add_one(BotStatus(id=1, pending_credit=Decimal("0")))
    await om.drain_pool(Decimal("40000"))
    assert bus.events == []


async def test_drain_pool_moves_a_take_profit_rather_than_selling_it(
    om: OrderManager, bus: RecordingEventBus, client: FakeBybitClient
) -> None:
    # both lots still have room under them, so the pool buys moves; a
    # sale would realise a loss and give up the profit they will earn
    await add_one(BotStatus(id=1, pending_credit=Decimal("6000")))
    stranded = await add_one(
        Position(
            level_index=900,
            entry_price=Decimal("60000"),
            qty=Decimal("0.0005"),
            tp_order_id="tp-stranded",
            tp_price=Decimal("60100"),
            status=PositionStatus.OPEN,
            opened_at=datetime(2026, 7, 1, tzinfo=UTC),
        )
    )
    await add_one(
        Position(
            level_index=901,
            entry_price=Decimal("40000"),
            qty=Decimal("0.0005"),
            tp_order_id="tp-fresh",
            tp_price=Decimal("40100"),
            status=PositionStatus.OPEN,
            opened_at=datetime(2026, 7, 2, tzinfo=UTC),
        )
    )
    closed = await add_one(
        Position(
            level_index=902,
            entry_price=Decimal("40000"),
            qty=Decimal("0.0005"),
            status=PositionStatus.CLOSED,
            opened_at=datetime(2026, 7, 2, tzinfo=UTC),
            closed_at=datetime(2026, 7, 3, tzinfo=UTC),
        )
    )
    assert closed.id is not None
    await om.drain_pool(Decimal("40000"))
    kinds = [event_type for event_type, _ in bus.events]
    assert "pool.drained" in kinds
    async with new_session() as session:
        again = await session.get(Position, stranded.id)
    assert again is not None
    assert again.status == PositionStatus.OPEN
    assert again.tp_price is not None
    assert again.tp_price < Decimal("60100")
    assert client.sold == []
    assert await repository.pending_credit() < Decimal("6000")


async def test_percent_config_takes_the_profit_ratio_at_any_price(
    client: FakeBybitClient,
    instrument: Instrument,
    config: StrategyConfig,
    bus: RecordingEventBus,
) -> None:
    # percent mode: buys sit 0.11% apart and every lot sells for 0.66%,
    # whatever the price — the absolute distance is not fixed
    config.grid_mode = "percent"
    config.grid_step = Decimal("0.0011")
    config.tp_step = Decimal("0.0066")
    om = OrderManager(
        client=client,  # type: ignore[arg-type]
        instrument=instrument,
        config=config,
        bus=bus,
    )
    client.next_id = "buy-pct"
    await om.place_buy_at_level(0, Decimal("60000"))
    client.next_id = "tp-pct"
    await om.handle_buy_fill(
        _exec(
            exec_id="e-pct",
            order_id="buy-pct",
            side=Side.BUY,
            price=Decimal("60000"),
            qty=Decimal("0.000333"),
            fee=Decimal("0.000000333"),
            fee_coin="BTC",
        )
    )
    position = await _position_at(0)
    assert position.tp_price is not None
    entry = Decimal("60000")
    profit = (position.tp_price - entry) / position.tp_price
    assert profit >= Decimal("0.0066")
    # the take-profit snaps to a buy rung, so it can overshoot — but by
    # less than the rung it snapped to
    assert profit < Decimal("0.0066") + Decimal("0.0011")


async def test_settle_phantom_refuses_while_the_wallet_holds_the_coin(
    protector: Protector, client: FakeBybitClient
) -> None:
    # "insufficient balance" only means the *free* coin is short — this
    # lot's coin can be locked in another lot's resting sell, and writing
    # it off would hand real coin to nobody
    client.base_free = Decimal("0.005")
    pos = await add_one(
        Position(
            level_index=9,
            entry_price=Decimal("59000"),
            qty=Decimal("0.001"),
            tp_price=Decimal("59100"),
            status=PositionStatus.OPEN,
            opened_at=datetime.now(tz=UTC),
        )
    )
    assert await protector.settle_phantom(pos) is None
    still_open = await repository.get_position(pos.id)
    assert still_open.status == PositionStatus.OPEN
    assert still_open.filled_qty == Decimal(0)


async def test_settle_phantom_books_the_lot_the_wallet_cannot_cover(
    protector: Protector, client: FakeBybitClient
) -> None:
    # the book claims 0.003 and the wallet holds 0.001: one lot's worth
    # really did sell behind our back
    client.base_free = Decimal("0.001")
    for level in (10, 11, 12):
        await add_one(
            Position(
                level_index=level,
                entry_price=Decimal("59000"),
                qty=Decimal("0.001"),
                tp_price=Decimal("59100"),
                status=PositionStatus.OPEN,
                opened_at=datetime.now(tz=UTC),
            )
        )
    victim = await repository.open_position_at_level(10)
    assert victim is not None
    assert await protector.settle_phantom(victim) is not None
    assert (await repository.get_position(victim.id)).status == (
        PositionStatus.CLOSED
    )


async def test_a_fill_whose_level_was_pruned_is_still_booked(
    om: OrderManager, client: FakeBybitClient
) -> None:
    # a prune that raced the fill clears the level's order id; dropping
    # the fill then left the coin outside the book entirely
    execution = _exec(
        exec_id="e-orphan",
        order_id="vanished-level",
        side=Side.BUY,
        price=Decimal("60000"),
        qty=Decimal("0.000333"),
        fee=Decimal("0.000000333"),
        fee_coin="BTC",
    )
    level_index = await om.handle_buy_fill(execution)
    assert level_index is not None
    assert level_index >= repository.ADOPTED_LEVEL_BASE
    position = await _position_at(level_index)
    # booked at what it really cost, not at some later market price
    assert position.entry_price == Decimal("60000")
    assert position.qty == Decimal("0.000333")
    assert position.tp_order_id != ""
    assert await _exec_exists("e-orphan")


async def test_a_small_shortfall_costs_the_lot_only_what_is_missing(
    protector: Protector, client: FakeBybitClient, config: StrategyConfig
) -> None:
    # the wallet is short by a fraction of the lot: writing the whole lot
    # off would hand the coin that is still there to nobody
    client.base_free = Decimal("0.0008")
    pos = await add_one(
        Position(
            level_index=13,
            entry_price=Decimal("59000"),
            qty=Decimal("0.001"),
            tp_price=Decimal("59100"),
            status=PositionStatus.OPEN,
            opened_at=datetime.now(tz=UTC),
        )
    )
    assert await protector.settle_phantom(pos, Decimal("59000")) is None
    left = await repository.get_position(pos.id)
    assert left.status == PositionStatus.OPEN
    assert left.filled_qty == Decimal("0.0002")
    assert left.remaining_qty == Decimal("0.0008")
    # and the coin that is still there gets a fresh protective sell
    assert client.placed[-1]["side"] == Side.SELL
    assert client.placed[-1]["qty"] == Decimal("0.0008")


async def test_an_unbooked_buy_is_recovered_before_anything_claims_it(
    om: OrderManager, client: FakeBybitClient
) -> None:
    # an unbooked buy is indistinguishable from loose coin; booking it
    # here keeps its real fill price instead of a later market one
    from core.services.healer import Healer

    client.base_free = Decimal("1")
    client.recent = [
        _exec(
            exec_id="e-missed-buy",
            order_id="missed-1",
            side=Side.BUY,
            price=Decimal("58000"),
            qty=Decimal("0.000333"),
            fee=Decimal("0.000000333"),
            fee_coin="BTC",
        )
    ]
    await Healer(om).recover_unbooked_buys()
    assert await _exec_exists("e-missed-buy")
    positions = await repository.open_positions()
    assert len(positions) == 1
    assert positions[0].entry_price == Decimal("58000")


async def test_a_buy_whose_coin_is_gone_is_left_alone(
    om: OrderManager, client: FakeBybitClient
) -> None:
    # an old fill whose coin has since been sold would be booked over
    # coin that is not there; the wallet has to still hold it
    from core.services.healer import Healer

    client.base_free = Decimal("0")
    client.recent = [
        _exec(
            exec_id="e-spent",
            order_id="spent-1",
            side=Side.BUY,
            price=Decimal("58000"),
            qty=Decimal("0.000333"),
            fee=Decimal("0.000000333"),
            fee_coin="BTC",
        )
    ]
    await Healer(om).recover_unbooked_buys()
    assert await repository.open_positions() == []
    assert not await _exec_exists("e-spent")


async def test_a_buy_already_on_the_books_is_not_recovered_twice(
    om: OrderManager, client: FakeBybitClient
) -> None:
    from core.services.healer import Healer

    client.base_free = Decimal("1")
    client.recent = [
        _exec(
            exec_id="e-twice",
            order_id="missed-2",
            side=Side.BUY,
            price=Decimal("58000"),
            qty=Decimal("0.000333"),
            fee=Decimal("0.000000333"),
            fee_coin="BTC",
        )
    ]
    healer = Healer(om)
    await healer.recover_unbooked_buys()
    await healer.recover_unbooked_buys()
    assert len(await repository.open_positions()) == 1


async def test_a_written_off_remainder_is_still_sellable(
    protector: Protector, client: FakeBybitClient
) -> None:
    # the exchange rejects a quantity finer than the lot size, so what is
    # written off rounds up to the lot grid and the rest stays sellable
    client.base_free = Decimal("0.0008")
    pos = await add_one(
        Position(
            level_index=14,
            entry_price=Decimal("59000"),
            qty=Decimal("0.001"),
            tp_price=Decimal("59100"),
            status=PositionStatus.OPEN,
            opened_at=datetime.now(tz=UTC),
        )
    )
    await protector.settle_phantom(pos, Decimal("59000"))
    sold = client.placed[-1]["qty"]
    lot = Decimal("0.000001")
    assert sold % lot == 0
    assert sold > 0


async def test_the_same_buy_fill_never_opens_two_lots(
    om: OrderManager, client: FakeBybitClient
) -> None:
    # the stream, the healer and the pruner all feed fills in; booking one
    # twice rests two sells over one lot of coin and starves the next fill
    client.next_id = "buy-dup"
    await om.place_buy_at_level(0, Decimal("60000"))
    execution = _exec(
        exec_id="e-dup",
        order_id="buy-dup",
        side=Side.BUY,
        price=Decimal("60000"),
        qty=Decimal("0.000333"),
        fee=Decimal("0.000000333"),
        fee_coin="BTC",
    )
    first = await om.handle_buy_fill(execution)
    second = await om.handle_buy_fill(execution)
    assert first == 0
    assert second is None
    assert await _count(Position) == 1
    sells = [p for p in client.placed if p["side"] == Side.SELL]
    assert len(sells) == 1


async def test_concurrent_deliveries_of_one_fill_book_it_once(
    om: OrderManager, client: FakeBybitClient
) -> None:
    import asyncio

    client.next_id = "buy-race"
    await om.place_buy_at_level(1, Decimal("60000"))
    execution = _exec(
        exec_id="e-race",
        order_id="buy-race",
        side=Side.BUY,
        price=Decimal("60000"),
        qty=Decimal("0.000333"),
        fee=Decimal("0.000000333"),
        fee_coin="BTC",
    )
    booked = await asyncio.gather(
        om.handle_buy_fill(execution), om.handle_buy_fill(execution)
    )
    assert sorted(b is None for b in booked) == [False, True]
    assert await _count(Position) == 1


async def test_a_fill_under_the_minimum_is_left_for_the_sweep(
    om: OrderManager, client: FakeBybitClient
) -> None:
    # no sell can ever rest over it, so retrying it every tick is futile:
    # the coin joins the loose balance and the sweep folds it into a lot
    from core.services.healer import Healer

    client.base_free = Decimal("1")
    client.recent = [
        _exec(
            exec_id="e-dust",
            order_id="dust-1",
            side=Side.BUY,
            price=Decimal("60000"),
            qty=Decimal("0.00005"),  # $3, under the $5 minimum
            fee=Decimal("0.00000005"),
            fee_coin="BTC",
        )
    ]
    await Healer(om).recover_unbooked_buys()
    assert await repository.open_positions() == []
    assert not await _exec_exists("e-dust")


async def test_a_fill_under_the_minimum_claims_no_level(
    om: OrderManager,
) -> None:
    before = await repository.next_adopted_level()
    booked = await om.handle_buy_fill(
        _exec(
            exec_id="e-dust-2",
            order_id="dust-2",
            side=Side.BUY,
            price=Decimal("60000"),
            qty=Decimal("0.00005"),
            fee=Decimal("0.00000005"),
            fee_coin="BTC",
        )
    )
    assert booked is None
    assert await repository.next_adopted_level() == before
