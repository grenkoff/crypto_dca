"""Async wrapper over pybit.unified_trading.HTTP for Bybit Spot v5."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any, Protocol, cast

from core.exchange.errors import (
    BybitError,
    InsufficientBalanceError,
    OrderRejectedError,
    RateLimitedError,
)
from core.exchange.types import (
    Balance,
    Execution,
    Instrument,
    Order,
    OrderStatus,
    Side,
)

CATEGORY = "spot"


class _HTTP(Protocol):
    def get_instruments_info(self, **kwargs: Any) -> dict[str, Any]: ...
    def get_tickers(self, **kwargs: Any) -> dict[str, Any]: ...
    def get_wallet_balance(self, **kwargs: Any) -> dict[str, Any]: ...
    def place_order(self, **kwargs: Any) -> dict[str, Any]: ...
    def cancel_order(self, **kwargs: Any) -> dict[str, Any]: ...
    def cancel_all_orders(self, **kwargs: Any) -> dict[str, Any]: ...
    def get_open_orders(self, **kwargs: Any) -> dict[str, Any]: ...
    def get_executions(self, **kwargs: Any) -> dict[str, Any]: ...


def _raise_for_ret(response: dict[str, Any]) -> dict[str, Any]:
    code = int(response.get("retCode", 0))
    if code == 0:
        return cast(dict[str, Any], response["result"])
    msg = str(response.get("retMsg", ""))
    if code in (110007, 170131, 170033):
        raise InsufficientBalanceError(code, msg)
    if code in (10006, 10018):
        raise RateLimitedError(code, msg)
    if code in (10001, 110001, 170132, 170133):
        raise OrderRejectedError(code, msg)
    raise BybitError(code, msg)


def _ts(value: str | int) -> datetime:
    return datetime.fromtimestamp(int(value) / 1000, tz=UTC)


class BybitClient:
    """Thin async wrapper over the pybit unified HTTP client for spot
    trading."""

    def __init__(self, http: _HTTP) -> None:
        self._http = http

    @classmethod
    def from_credentials(
        cls,
        api_key: str,
        api_secret: str,
        *,
        testnet: bool,
        recv_window: int = 5000,
    ) -> BybitClient:
        """Build a client from API credentials."""
        from pybit.unified_trading import (
            HTTP,
        )

        http = HTTP(
            testnet=testnet,
            api_key=api_key,
            api_secret=api_secret,
            recv_window=recv_window,
        )
        return cls(http)

    async def get_instrument(self, symbol: str) -> Instrument:
        """Fetch instrument constraints for ``symbol``."""
        resp = await asyncio.to_thread(
            self._http.get_instruments_info, category=CATEGORY, symbol=symbol
        )
        result = _raise_for_ret(resp)
        items = result.get("list") or []
        if not items:
            raise BybitError(0, f"instrument not found: {symbol}")
        item = items[0]
        lot = item["lotSizeFilter"]
        price = item["priceFilter"]
        return Instrument(
            symbol=item["symbol"],
            base_coin=item["baseCoin"],
            quote_coin=item["quoteCoin"],
            tick_size=Decimal(str(price["tickSize"])),
            lot_size=Decimal(str(lot["basePrecision"])),
            min_order_qty=Decimal(str(lot["minOrderQty"])),
            min_order_amt=Decimal(str(lot["minOrderAmt"])),
        )

    async def get_last_price(self, symbol: str) -> Decimal:
        """Fetch the last traded price for ``symbol``."""
        resp = await asyncio.to_thread(
            self._http.get_tickers, category=CATEGORY, symbol=symbol
        )
        result = _raise_for_ret(resp)
        items = result.get("list") or []
        if not items:
            raise BybitError(0, f"no ticker for {symbol}")
        return Decimal(str(items[0]["lastPrice"]))

    async def get_balances(self) -> dict[str, Balance]:
        """Fetch unified-account balances keyed by coin."""
        resp = await asyncio.to_thread(
            self._http.get_wallet_balance, accountType="UNIFIED"
        )
        result = _raise_for_ret(resp)
        accounts = result.get("list") or []
        balances: dict[str, Balance] = {}
        for account in accounts:
            for coin in account.get("coin", []):
                wallet = Decimal(str(coin.get("walletBalance") or 0))
                locked = Decimal(str(coin.get("locked") or 0))
                balances[coin["coin"]] = Balance(
                    coin=coin["coin"], free=wallet - locked, locked=locked
                )
        return balances

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
        """Place a limit order and return its order id."""
        kwargs: dict[str, Any] = {
            "category": CATEGORY,
            "symbol": symbol,
            "side": side.value,
            "orderType": "Limit",
            "qty": str(qty),
            "price": str(price),
            "timeInForce": "PostOnly" if post_only else "GTC",
        }
        if order_link_id:
            kwargs["orderLinkId"] = order_link_id
        resp = await asyncio.to_thread(self._http.place_order, **kwargs)
        result = _raise_for_ret(resp)
        return str(result["orderId"])

    async def cancel_order(self, symbol: str, order_id: str) -> None:
        """Cancel a single order by id."""
        resp = await asyncio.to_thread(
            self._http.cancel_order,
            category=CATEGORY,
            symbol=symbol,
            orderId=order_id,
        )
        _raise_for_ret(resp)

    async def cancel_all(self, symbol: str) -> None:
        """Cancel all open orders for ``symbol``."""
        resp = await asyncio.to_thread(
            self._http.cancel_all_orders, category=CATEGORY, symbol=symbol
        )
        _raise_for_ret(resp)

    async def get_open_orders(self, symbol: str) -> list[Order]:
        """Fetch all open orders for ``symbol`` (paginated)."""
        orders: list[Order] = []
        cursor: str | None = None
        while True:
            kwargs: dict[str, Any] = {
                "category": CATEGORY,
                "symbol": symbol,
                "limit": 50,
            }
            if cursor:
                kwargs["cursor"] = cursor
            resp = await asyncio.to_thread(
                self._http.get_open_orders, **kwargs
            )
            result = _raise_for_ret(resp)
            orders.extend(
                _parse_order(item) for item in (result.get("list") or [])
            )
            cursor = result.get("nextPageCursor") or None
            if not cursor:
                break
        return orders

    async def get_executions(
        self, symbol: str, *, limit: int = 50
    ) -> list[Execution]:
        """Fetch recent executions for ``symbol``."""
        resp = await asyncio.to_thread(
            self._http.get_executions,
            category=CATEGORY,
            symbol=symbol,
            limit=limit,
        )
        result = _raise_for_ret(resp)
        return [_parse_execution(item) for item in (result.get("list") or [])]

    async def get_order_executions(
        self, symbol: str, order_id: str, *, limit: int = 50
    ) -> list[Execution]:
        """Executions for one specific order (7-day retention), regardless of
        age — used to settle a naked position whose fill fell outside the
        recent window."""
        resp = await asyncio.to_thread(
            self._http.get_executions,
            category=CATEGORY,
            symbol=symbol,
            orderId=order_id,
            limit=limit,
        )
        result = _raise_for_ret(resp)
        return [_parse_execution(item) for item in (result.get("list") or [])]


def _parse_order(item: dict[str, Any]) -> Order:
    return Order(
        order_id=str(item["orderId"]),
        symbol=str(item["symbol"]),
        side=Side(item["side"]),
        price=Decimal(str(item["price"])),
        qty=Decimal(str(item["qty"])),
        filled_qty=Decimal(str(item.get("cumExecQty", 0))),
        status=OrderStatus(item["orderStatus"]),
        created_at=_ts(item["createdTime"]),
        updated_at=_ts(item["updatedTime"]),
    )


def _parse_execution(item: dict[str, Any]) -> Execution:
    return Execution(
        exec_id=str(item["execId"]),
        order_id=str(item["orderId"]),
        symbol=str(item["symbol"]),
        side=Side(item["side"]),
        price=Decimal(str(item["execPrice"])),
        qty=Decimal(str(item["execQty"])),
        fee=Decimal(str(item.get("execFee", 0))),
        fee_coin=str(item.get("feeCurrency", "")),
        executed_at=_ts(item["execTime"]),
    )
