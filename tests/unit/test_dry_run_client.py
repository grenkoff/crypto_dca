"""DryRunBybitClient forwards reads and fakes writes."""

from __future__ import annotations

from decimal import Decimal
from typing import Any
from unittest.mock import AsyncMock

import pytest

from core.exchange.bybit import BybitClient
from core.exchange.dry_run import DryRunBybitClient
from core.exchange.types import Balance, Instrument, Side


@pytest.fixture
def inner() -> Any:
    client = AsyncMock(spec=BybitClient)
    client.get_balances.return_value = {
        "USDT": Balance(coin="USDT", free=Decimal("100"), locked=Decimal(0))
    }
    client.get_last_price.return_value = Decimal("60000")
    client.get_instrument.return_value = Instrument(
        symbol="BTCUSDT",
        base_coin="BTC",
        quote_coin="USDT",
        tick_size=Decimal("0.01"),
        lot_size=Decimal("0.000001"),
        min_order_qty=Decimal("0.000001"),
        min_order_amt=Decimal("5"),
    )
    client.get_open_orders.return_value = []
    client.get_executions.return_value = []
    return client


async def test_reads_pass_through(inner: AsyncMock) -> None:
    dry = DryRunBybitClient(inner)
    assert (await dry.get_last_price("BTCUSDT")) == Decimal("60000")
    assert (await dry.get_balances())["USDT"].free == Decimal("100")
    inst = await dry.get_instrument("BTCUSDT")
    assert inst.tick_size == Decimal("0.01")
    inner.get_last_price.assert_awaited_with("BTCUSDT")
    inner.get_instrument.assert_awaited_with("BTCUSDT")


async def test_place_limit_does_not_call_inner(inner: AsyncMock) -> None:
    dry = DryRunBybitClient(inner)
    order_id = await dry.place_limit(
        "BTCUSDT",
        Side.BUY,
        Decimal("0.001"),
        Decimal("60000"),
        order_link_id="grid-7",
    )
    assert order_id.startswith("dry-1-")
    inner.place_limit.assert_not_awaited()


async def test_two_placements_get_different_ids(inner: AsyncMock) -> None:
    dry = DryRunBybitClient(inner)
    a = await dry.place_limit(
        "BTCUSDT", Side.BUY, Decimal("0.001"), Decimal("60000")
    )
    b = await dry.place_limit(
        "BTCUSDT", Side.BUY, Decimal("0.001"), Decimal("59000")
    )
    assert a != b


async def test_cancel_does_not_call_inner(inner: AsyncMock) -> None:
    dry = DryRunBybitClient(inner)
    await dry.cancel_order("BTCUSDT", "anything")
    await dry.cancel_all("BTCUSDT")
    inner.cancel_order.assert_not_awaited()
    inner.cancel_all.assert_not_awaited()
