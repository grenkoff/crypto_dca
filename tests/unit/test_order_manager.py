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

    async def get_balances(self) -> dict[str, Balance]:
        return {
            "USDT": Balance(
                coin="USDT", free=Decimal("50"), locked=Decimal("0")
            ),
            "BTC": Balance(coin="BTC", free=Decimal("0"), locked=Decimal("0")),
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
        grid_mode="percent",
        grid_step=Decimal("0.01"),
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


async def test_drain_pool_retires_a_stranded_lot_with_no_close(
    om: OrderManager, bus: RecordingEventBus, client: FakeBybitClient
) -> None:
    # a lot stranded far above market, plus a fresh one to carry the
    # order over the exchange minimum, and a pool that covers both
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
    assert again.status == PositionStatus.CLOSED
    assert await repository.pending_credit() < Decimal("6000")
