"""Pruning a resting buy that turns out to have filled already."""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

import pytest
from sqlalchemy import select

from core.db.models import GridLevel, LevelStatus, StrategyConfig
from core.db.session import new_session
from core.exchange.types import Balance, Execution, Instrument, Side
from core.services import repository
from core.services.events import NoOpEventBus
from core.services.grid_maintainer import GridMaintainer
from core.services.order_manager import OrderManager
from tests.conftest import add_one

pytestmark = pytest.mark.db

_INSTRUMENT = Instrument(
    symbol="KASUSDT",
    base_coin="KAS",
    quote_coin="USDT",
    tick_size=Decimal("0.00001"),
    lot_size=Decimal("0.01"),
    min_order_qty=Decimal("0.01"),
    min_order_amt=Decimal("5"),
)
_PRICE = Decimal("0.03600")
_GONE = "Order does not exist. (ErrCode: 170213)"


class _RacingClient:
    """Refuses the cancel the way Bybit does for an order that filled."""

    def __init__(self, *, filled: bool) -> None:
        self.filled = filled
        self.placed: list[dict[str, Any]] = []

    async def get_balances(self) -> dict[str, Balance]:
        return {
            "USDT": Balance(
                coin="USDT", free=Decimal("50"), locked=Decimal("0")
            ),
            "KAS": Balance(coin="KAS", free=Decimal("0"), locked=Decimal("0")),
        }

    async def cancel_order(self, symbol: str, order_id: str) -> None:
        raise RuntimeError(_GONE)

    async def get_order_executions(
        self, symbol: str, order_id: str, *, limit: int = 50
    ) -> list[Execution]:
        if not self.filled:
            return []
        return [
            Execution(
                exec_id=f"x-{order_id}",
                order_id=order_id,
                symbol=symbol,
                side=Side.BUY,
                price=_PRICE,
                qty=Decimal("194.44"),
                fee=Decimal("0.12"),
                fee_coin="KAS",
                executed_at=datetime.now(tz=UTC),
            )
        ]

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
        self.placed.append({"side": side, "qty": qty, "price": price})
        return f"tp-{len(self.placed)}"


def _config() -> StrategyConfig:
    return StrategyConfig(
        symbol="KASUSDT",
        grid_mode="percent",
        grid_step=Decimal("0.0011"),
        tp_step=Decimal("0.0066"),
        order_qty_quote=Decimal("7"),
        min_profit_quote=Decimal("0"),
        maker_fee=Decimal("0.000625"),
        taker_fee=Decimal("0.00075"),
        max_open_orders=50,
        comp_share_min=Decimal("0.20"),
        comp_share_max=Decimal("0.80"),
        comp_hole_offset=Decimal("0"),
    )


def _maintainer(client: _RacingClient) -> GridMaintainer:
    om = OrderManager(
        client=client,  # type: ignore[arg-type]
        instrument=_INSTRUMENT,
        config=_config(),
        bus=NoOpEventBus(),
    )
    return GridMaintainer(om, NoOpEventBus())


async def _level(order_id: str) -> GridLevel:
    return await add_one(
        GridLevel(
            level_index=1875,
            target_buy_price=_PRICE,
            current_buy_order_id=order_id,
            status=LevelStatus.AWAITING_FILL,
        )
    )


async def _level_status(level_index: int) -> str:
    async with new_session() as session:
        level = await session.scalar(
            select(GridLevel).where(GridLevel.level_index == level_index)
        )
    assert level is not None
    return str(level.status)


async def test_a_buy_that_filled_as_we_cancelled_is_booked_not_idled() -> None:
    # "order does not exist" also answers a cancel for an order that has
    # just filled; idling the level there loses the fill and the coin
    client = _RacingClient(filled=True)
    await _level("raced-1")
    freed = await _maintainer(client)._prune_out_of_band(
        {_PRICE: (1875, "raced-1")}, {_PRICE}
    )
    # its money went into a lot, so it never returns to the buy budget
    assert freed == 0
    positions = await repository.open_positions()
    assert len(positions) == 1
    assert positions[0].entry_price == _PRICE
    assert positions[0].level_index == 1875
    assert client.placed[0]["side"] == Side.SELL
    assert await _level_status(1875) == LevelStatus.FILLED


async def test_a_buy_that_really_vanished_still_idles_its_level() -> None:
    client = _RacingClient(filled=False)
    await _level("gone-1")
    freed = await _maintainer(client)._prune_out_of_band(
        {_PRICE: (1875, "gone-1")}, {_PRICE}
    )
    assert freed == 1
    assert await repository.open_positions() == []
    assert await _level_status(1875) == LevelStatus.IDLE


async def test_a_fill_already_on_the_books_is_not_booked_twice() -> None:
    client = _RacingClient(filled=True)
    await _level("raced-2")
    prune = ({_PRICE: (1875, "raced-2")}, {_PRICE})
    maintainer = _maintainer(client)
    assert await maintainer._prune_out_of_band(*prune) == 0
    assert await maintainer._prune_out_of_band(*prune) == 0
    assert len(await repository.open_positions()) == 1


async def test_a_lookup_that_fails_leaves_the_level_to_the_healer() -> None:
    class _Blind(_RacingClient):
        async def get_order_executions(
            self, symbol: str, order_id: str, *, limit: int = 50
        ) -> list[Execution]:
            raise RuntimeError("network down")

    client = _Blind(filled=True)
    await _level("blind-1")
    freed = await _maintainer(client)._prune_out_of_band(
        {_PRICE: (1875, "blind-1")}, {_PRICE}
    )
    assert freed == 1
    assert await _level_status(1875) == LevelStatus.IDLE


async def test_a_position_is_never_lost_when_the_level_is_gone() -> None:
    # the fill arrives after the level was already idled: it is booked on
    # a level of its own instead of being dropped
    client = _RacingClient(filled=True)
    om = _maintainer(client)._om
    execution = (await client.get_order_executions("KASUSDT", "late-1"))[0]
    level_index = await om.handle_buy_fill(execution)
    assert level_index is not None
    assert level_index >= repository.ADOPTED_LEVEL_BASE
    position = (await repository.open_positions())[0]
    assert position.entry_price == _PRICE
    assert position.qty == Decimal("194.44")
