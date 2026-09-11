"""The trader must book a fill even when the market price call fails."""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal
from typing import Any, cast

from core.exchange.types import Execution, Side
from core.exchange.ws import StreamEvent
from core.services.runtime import TraderRuntime

_PRICE = Decimal("0.03500")


def _execution(side: Side) -> Execution:
    return Execution(
        exec_id="e-1",
        order_id="o-1",
        symbol="KASUSDT",
        side=side,
        price=_PRICE,
        qty=Decimal("190"),
        fee=Decimal("0.11"),
        fee_coin="KAS",
        executed_at=datetime.now(tz=UTC),
    )


class _BlindClient:
    """A client whose price endpoint is down."""

    async def get_last_price(self, symbol: str) -> Decimal:
        raise RuntimeError("Read timed out")


class _RecordingManager:
    """Records the fills handed to it."""

    symbol = "KASUSDT"

    def __init__(self) -> None:
        self.buys: list[Execution] = []
        self.sells: list[tuple[Execution, Decimal]] = []

    async def handle_buy_fill(self, execution: Execution) -> int | None:
        self.buys.append(execution)
        return 1

    async def handle_sell_fill(
        self, execution: Execution, price: Decimal
    ) -> int | None:
        self.sells.append((execution, price))
        return 1


class _SilentGrid:
    """A grid maintainer that records the price it was asked to work at."""

    def __init__(self) -> None:
        self.prices: list[Decimal] = []

    async def ensure(self, price: Decimal) -> None:
        self.prices.append(price)


def _runtime(om: _RecordingManager, grid: _SilentGrid) -> TraderRuntime:
    runtime = TraderRuntime()
    runtime._client = cast(Any, _BlindClient())
    runtime._om = cast(Any, om)
    runtime._grid = cast(Any, grid)
    runtime._current_price = _PRICE
    return runtime


async def test_a_buy_is_booked_when_the_price_call_fails() -> None:
    # letting the price error escape used to discard the execution, and
    # the coin it bought then sat in the wallet with no lot holding it
    om, grid = _RecordingManager(), _SilentGrid()
    runtime = _runtime(om, grid)
    await runtime._handle_event(
        StreamEvent(kind="execution", payload=_execution(Side.BUY))
    )
    assert len(om.buys) == 1
    assert grid.prices == [_PRICE]


async def test_a_sell_falls_back_to_the_last_known_price() -> None:
    om, grid = _RecordingManager(), _SilentGrid()
    runtime = _runtime(om, grid)
    await runtime._handle_event(
        StreamEvent(kind="execution", payload=_execution(Side.SELL))
    )
    assert om.sells[0][1] == _PRICE
