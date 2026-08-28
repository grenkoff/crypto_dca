"""Async wrapper over pybit.unified_trading.HTTP for Bybit Spot v5."""

from __future__ import annotations

import asyncio
from datetime import UTC, date, datetime
from decimal import Decimal
from typing import Any, Protocol, cast

import structlog

from core.config.settings import bybit_settings
from core.exchange.errors import (
    BybitError,
    InsufficientBalanceError,
    OrderRejectedError,
    RateLimitedError,
    TransientNetworkError,
)
from core.exchange.types import (
    Balance,
    Execution,
    Instrument,
    Order,
    OrderStatus,
    Side,
    Transfer,
)

log = structlog.get_logger()

CATEGORY = "spot"
_TRANSFER_WINDOW_MS = 7 * 24 * 60 * 60 * 1000 - 1000


_RETRIES = 3
_BACKOFF_SECONDS = 0.5


def _is_transient(exc: Exception) -> bool:
    """Whether a failed call may still have reached the exchange.

    Connection resets, dropped keep-alives and timeouts leave the outcome
    unknown; the request may or may not have been executed.
    """
    from requests.exceptions import RequestException

    return isinstance(exc, RequestException)


class _HTTP(Protocol):
    def get_instruments_info(self, **kwargs: Any) -> dict[str, Any]: ...
    def get_tickers(self, **kwargs: Any) -> dict[str, Any]: ...
    def get_kline(self, **kwargs: Any) -> dict[str, Any]: ...
    def get_wallet_balance(self, **kwargs: Any) -> dict[str, Any]: ...
    def place_order(self, **kwargs: Any) -> dict[str, Any]: ...
    def cancel_order(self, **kwargs: Any) -> dict[str, Any]: ...
    def cancel_all_orders(self, **kwargs: Any) -> dict[str, Any]: ...
    def get_open_orders(self, **kwargs: Any) -> dict[str, Any]: ...
    def get_executions(self, **kwargs: Any) -> dict[str, Any]: ...
    def get_order_history(self, **kwargs: Any) -> dict[str, Any]: ...
    def get_transaction_log(self, **kwargs: Any) -> dict[str, Any]: ...


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

    @classmethod
    def from_settings(cls) -> BybitClient:
        """Build a client from the configured Bybit settings."""
        s = bybit_settings()
        return cls.from_credentials(
            s.api_key,
            s.api_secret,
            testnet=s.testnet,
            recv_window=s.recv_window,
        )

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

    async def get_daily_ohlc(
        self, symbol: str, start_ms: int
    ) -> dict[date, tuple[Decimal, Decimal, Decimal, Decimal, Decimal]]:
        """Daily OHLC plus base volume, keyed by UTC date, from
        ``start_ms``."""
        resp = await asyncio.to_thread(
            self._http.get_kline,
            category=CATEGORY,
            symbol=symbol,
            interval="D",
            start=start_ms,
            limit=1000,
        )
        result = _raise_for_ret(resp)
        return {
            _ts(row[0]).date(): (
                Decimal(str(row[1])),
                Decimal(str(row[2])),
                Decimal(str(row[3])),
                Decimal(str(row[4])),
                Decimal(str(row[5])),
            )
            for row in result.get("list") or []
        }

    async def get_transfers(
        self, start_ms: int, end_ms: int
    ) -> list[Transfer]:
        """Funding moved in or out between two instants.

        The transaction log caps each request at seven days, so the range
        is walked in windows; every page is followed to its end.
        """
        out: list[Transfer] = []
        window = _TRANSFER_WINDOW_MS
        for kind in ("TRANSFER_IN", "TRANSFER_OUT"):
            start = start_ms
            while start < end_ms:
                stop = min(start + window, end_ms)
                out.extend(await self._transfer_page(kind, start, stop))
                start = stop
        return sorted(out, key=lambda t: t.at)

    async def _transfer_page(
        self, kind: str, start_ms: int, end_ms: int
    ) -> list[Transfer]:
        rows: list[Transfer] = []
        cursor: str | None = None
        while True:
            kwargs: dict[str, Any] = {
                "accountType": "UNIFIED",
                "type": kind,
                "startTime": start_ms,
                "endTime": end_ms,
                "limit": 50,
            }
            if cursor:
                kwargs["cursor"] = cursor
            resp = await asyncio.to_thread(
                self._http.get_transaction_log, **kwargs
            )
            result = _raise_for_ret(resp)
            page = result.get("list") or []
            rows.extend(
                Transfer(
                    external_id=str(row["id"]),
                    coin=str(row["currency"]),
                    amount=Decimal(str(row["change"])),
                    at=_ts(row["transactionTime"]),
                )
                for row in page
            )
            cursor = result.get("nextPageCursor") or None
            if not cursor or not page:
                return rows

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

    async def find_order_by_link_id(
        self, symbol: str, link_id: str
    ) -> str | None:
        """Order id carrying ``link_id``, searching open orders then
        history."""
        for fetch in (
            self._http.get_open_orders,
            self._http.get_order_history,
        ):
            resp = await asyncio.to_thread(
                fetch, category=CATEGORY, symbol=symbol, orderLinkId=link_id
            )
            rows = _raise_for_ret(resp).get("list") or []
            if rows:
                return str(rows[0]["orderId"])
        return None

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
        """Place a limit order and return its order id.

        A dropped connection hides whether the exchange accepted the
        order, and a blind retry would double it. With an
        ``order_link_id`` the retry first looks the order up by that id
        and adopts it if it landed; without one it cannot, so the error
        is raised instead of risking a duplicate.
        """
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
        return await self._submit(kwargs, symbol, order_link_id, "place_limit")

    async def place_market(
        self,
        symbol: str,
        side: Side,
        qty: Decimal,
        *,
        order_link_id: str,
    ) -> str:
        """Place a market order in base coin and return its order id.

        A market order must never be replayed blindly — a retry after a
        dropped connection would trade twice — so the link id is
        mandatory here and a failed attempt is resolved by looking it up
        before anything is sent again.
        """
        kwargs: dict[str, Any] = {
            "category": CATEGORY,
            "symbol": symbol,
            "side": side.value,
            "orderType": "Market",
            "qty": str(qty),
            "marketUnit": "baseCoin",
            "orderLinkId": order_link_id,
        }
        return await self._submit(
            kwargs, symbol, order_link_id, "place_market"
        )

    async def _submit(
        self,
        kwargs: dict[str, Any],
        symbol: str,
        link: str | None,
        what: str,
    ) -> str:
        """Send an order, adopting it if a dropped attempt
        already placed it."""
        last = ""
        for attempt in range(_RETRIES):
            try:
                resp = await asyncio.to_thread(
                    self._http.place_order, **kwargs
                )
            except Exception as exc:
                if not _is_transient(exc) or not link:
                    raise
                last = str(exc)
                log.warning(
                    "bybit.place_interrupted",
                    attempt=attempt + 1,
                    link_id=link,
                    error=last[:120],
                )
                landed = await self._adopt_after_drop(symbol, link)
                if landed is not None:
                    return landed
                await asyncio.sleep(_BACKOFF_SECONDS * (attempt + 1))
                continue
            return str(_raise_for_ret(resp)["orderId"])
        raise TransientNetworkError(
            f"{what} gave up after {_RETRIES} attempts: {last[:120]}"
        )

    async def _adopt_after_drop(self, symbol: str, link_id: str) -> str | None:
        """Order id if the dropped request actually reached the exchange."""
        try:
            found = await self.find_order_by_link_id(symbol, link_id)
        except Exception as exc:
            log.warning("bybit.lookup_failed", error=str(exc)[:120])
            return None
        if found is not None:
            log.info("bybit.place_adopted", link_id=link_id, order_id=found)
        return found

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
        order_link_id=str(item.get("orderLinkId") or ""),
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
