"""Live smoke tests against Bybit testnet.

These are marked `integration` and skipped unless API credentials are present
in the environment. Run explicitly with:

    BYBIT_API_KEY=... BYBIT_API_SECRET=... BYBIT_TESTNET=1 \\
        uv run pytest -m integration tests/integration

They place tiny limit orders far from market price and cancel them; nothing
should ever fill. Use a dedicated testnet key.
"""

from __future__ import annotations

import os
from decimal import Decimal

import pytest

from core.config.settings import bybit_settings
from core.exchange.bybit import BybitClient
from core.exchange.types import Side

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(
        not os.environ.get("BYBIT_API_KEY"),
        reason="BYBIT_API_KEY not set",
    ),
]


@pytest.fixture
def client() -> BybitClient:
    settings = bybit_settings()
    return BybitClient.from_credentials(
        settings.api_key, settings.api_secret, testnet=settings.testnet
    )


async def test_instrument_btcusdt(client: BybitClient) -> None:
    instrument = await client.get_instrument("BTCUSDT")
    assert instrument.base_coin == "BTC"
    assert instrument.quote_coin == "USDT"
    assert instrument.tick_size > 0


async def test_balances(client: BybitClient) -> None:
    balances = await client.get_balances()
    assert isinstance(balances, dict)


async def test_place_and_cancel_buy(client: BybitClient) -> None:
    instrument = await client.get_instrument("BTCUSDT")
    last = await client.get_last_price("BTCUSDT")
    # 50% below market — should never fill
    price = (last * Decimal("0.5")).quantize(instrument.tick_size)
    qty = (instrument.min_order_amt / price).quantize(instrument.lot_size)
    if qty * price < instrument.min_order_amt:
        qty += instrument.lot_size
    order_id = await client.place_limit("BTCUSDT", Side.BUY, qty, price)
    try:
        open_orders = await client.get_open_orders("BTCUSDT")
        assert any(o.order_id == order_id for o in open_orders)
    finally:
        await client.cancel_order("BTCUSDT", order_id)
