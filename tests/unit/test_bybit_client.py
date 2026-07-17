"""Unit tests for BybitClient with a stubbed pybit HTTP layer."""

from __future__ import annotations

from decimal import Decimal
from typing import Any

import pytest

from core.exchange.bybit import BybitClient
from core.exchange.errors import (
    BybitError,
    InsufficientBalanceError,
    OrderRejectedError,
    RateLimitedError,
)
from core.exchange.types import OrderStatus, Side


class FakeHTTP:
    """Records calls and returns canned responses."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, dict[str, Any]]] = []
        self.responses: dict[str, dict[str, Any]] = {}

    def _respond(self, method: str, kwargs: dict[str, Any]) -> dict[str, Any]:
        self.calls.append((method, kwargs))
        if method not in self.responses:
            raise AssertionError(f"unexpected call: {method}")
        return self.responses[method]

    def get_instruments_info(self, **kwargs: Any) -> dict[str, Any]:
        return self._respond("get_instruments_info", kwargs)

    def get_tickers(self, **kwargs: Any) -> dict[str, Any]:
        return self._respond("get_tickers", kwargs)

    def get_wallet_balance(self, **kwargs: Any) -> dict[str, Any]:
        return self._respond("get_wallet_balance", kwargs)

    def place_order(self, **kwargs: Any) -> dict[str, Any]:
        return self._respond("place_order", kwargs)

    def cancel_order(self, **kwargs: Any) -> dict[str, Any]:
        return self._respond("cancel_order", kwargs)

    def cancel_all_orders(self, **kwargs: Any) -> dict[str, Any]:
        return self._respond("cancel_all_orders", kwargs)

    def get_open_orders(self, **kwargs: Any) -> dict[str, Any]:
        return self._respond("get_open_orders", kwargs)

    def get_executions(self, **kwargs: Any) -> dict[str, Any]:
        return self._respond("get_executions", kwargs)


def _ok(result: dict[str, Any]) -> dict[str, Any]:
    return {
        "retCode": 0,
        "retMsg": "OK",
        "result": result,
        "time": 0,
        "retExtInfo": {},
    }


def _err(code: int, msg: str = "fail") -> dict[str, Any]:
    return {
        "retCode": code,
        "retMsg": msg,
        "result": {},
        "time": 0,
        "retExtInfo": {},
    }


@pytest.fixture
def http() -> FakeHTTP:
    return FakeHTTP()


@pytest.fixture
def client(http: FakeHTTP) -> BybitClient:
    return BybitClient(http)


async def test_get_instrument_parses_filters(
    http: FakeHTTP, client: BybitClient
) -> None:
    http.responses["get_instruments_info"] = _ok(
        {
            "list": [
                {
                    "symbol": "BTCUSDT",
                    "baseCoin": "BTC",
                    "quoteCoin": "USDT",
                    "priceFilter": {"tickSize": "0.01"},
                    "lotSizeFilter": {
                        "basePrecision": "0.000001",
                        "minOrderQty": "0.000048",
                        "minOrderAmt": "5",
                    },
                }
            ]
        }
    )
    instrument = await client.get_instrument("BTCUSDT")
    assert instrument.symbol == "BTCUSDT"
    assert instrument.tick_size == Decimal("0.01")
    assert instrument.min_order_amt == Decimal("5")
    assert http.calls[0][1] == {"category": "spot", "symbol": "BTCUSDT"}


async def test_get_instrument_not_found(
    http: FakeHTTP, client: BybitClient
) -> None:
    http.responses["get_instruments_info"] = _ok({"list": []})
    with pytest.raises(BybitError, match="not found"):
        await client.get_instrument("FOO")


async def test_get_last_price(http: FakeHTTP, client: BybitClient) -> None:
    http.responses["get_tickers"] = _ok({"list": [{"lastPrice": "60123.45"}]})
    price = await client.get_last_price("BTCUSDT")
    assert price == Decimal("60123.45")


async def test_get_balances(http: FakeHTTP, client: BybitClient) -> None:
    http.responses["get_wallet_balance"] = _ok(
        {
            "list": [
                {
                    "coin": [
                        {
                            "coin": "USDT",
                            "availableToWithdraw": "1000.50",
                            "locked": "10",
                            "walletBalance": "1010.50",
                        },
                        {
                            "coin": "BTC",
                            "availableToWithdraw": "0.01",
                            "locked": "0",
                            "walletBalance": "0.01",
                        },
                    ]
                }
            ]
        }
    )
    balances = await client.get_balances()
    assert balances["USDT"].free == Decimal("1000.50")
    assert balances["USDT"].locked == Decimal("10")
    assert balances["USDT"].total == Decimal("1010.50")
    assert balances["BTC"].free == Decimal("0.01")


async def test_place_limit_buy(http: FakeHTTP, client: BybitClient) -> None:
    http.responses["place_order"] = _ok(
        {"orderId": "abc-123", "orderLinkId": ""}
    )
    order_id = await client.place_limit(
        "BTCUSDT",
        Side.BUY,
        Decimal("0.001"),
        Decimal("60000"),
        order_link_id="grid-7",
    )
    assert order_id == "abc-123"
    call = http.calls[0][1]
    assert call["side"] == "Buy"
    assert call["price"] == "60000"
    assert call["qty"] == "0.001"
    assert call["timeInForce"] == "PostOnly"
    assert call["orderLinkId"] == "grid-7"


async def test_cancel_order_and_all(
    http: FakeHTTP, client: BybitClient
) -> None:
    http.responses["cancel_order"] = _ok({"orderId": "abc"})
    http.responses["cancel_all_orders"] = _ok({"list": []})
    await client.cancel_order("BTCUSDT", "abc")
    await client.cancel_all("BTCUSDT")
    assert {c[0] for c in http.calls} == {"cancel_order", "cancel_all_orders"}


async def test_get_open_orders(http: FakeHTTP, client: BybitClient) -> None:
    http.responses["get_open_orders"] = _ok(
        {
            "list": [
                {
                    "orderId": "o1",
                    "symbol": "BTCUSDT",
                    "side": "Buy",
                    "price": "59000",
                    "qty": "0.002",
                    "cumExecQty": "0",
                    "orderStatus": "New",
                    "createdTime": "1700000000000",
                    "updatedTime": "1700000000000",
                }
            ]
        }
    )
    orders = await client.get_open_orders("BTCUSDT")
    assert len(orders) == 1
    assert orders[0].order_id == "o1"
    assert orders[0].side == Side.BUY
    assert orders[0].status == OrderStatus.NEW


async def test_get_executions(http: FakeHTTP, client: BybitClient) -> None:
    http.responses["get_executions"] = _ok(
        {
            "list": [
                {
                    "execId": "e1",
                    "orderId": "o1",
                    "symbol": "BTCUSDT",
                    "side": "Buy",
                    "execPrice": "59000",
                    "execQty": "0.002",
                    "execFee": "0.118",
                    "feeCurrency": "USDT",
                    "execTime": "1700000001000",
                }
            ]
        }
    )
    executions = await client.get_executions("BTCUSDT", limit=10)
    assert executions[0].fee == Decimal("0.118")
    assert executions[0].fee_coin == "USDT"


@pytest.mark.parametrize(
    ("code", "exc"),
    [
        (110007, InsufficientBalanceError),
        (170131, InsufficientBalanceError),
        (10006, RateLimitedError),
        (10018, RateLimitedError),
        (10001, OrderRejectedError),
        (110001, OrderRejectedError),
        (99999, BybitError),
    ],
)
async def test_error_mapping(
    http: FakeHTTP, client: BybitClient, code: int, exc: type[BybitError]
) -> None:
    http.responses["place_order"] = _err(code, "boom")
    with pytest.raises(exc):
        await client.place_limit(
            "BTCUSDT", Side.BUY, Decimal("0.001"), Decimal("60000")
        )
