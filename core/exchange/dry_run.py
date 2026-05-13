"""Dry-run wrapper around BybitClient.

Reads pass through to the real client; mutating calls (place/cancel) are logged
and return fake order IDs without touching the exchange. Useful for verifying
that the trader produces the expected sequence of operations before risking
real funds.
"""

from __future__ import annotations

import uuid
from decimal import Decimal

import structlog

from core.exchange.bybit import BybitClient
from core.exchange.types import Balance, Execution, Instrument, Order, Side

log = structlog.get_logger()


class DryRunBybitClient:
    """Same surface as BybitClient — mutations are logged and faked."""

    def __init__(self, inner: BybitClient) -> None:
        self._inner = inner
        self._counter = 0

    # --- reads pass through -------------------------------------------------

    async def get_instrument(self, symbol: str) -> Instrument:
        return await self._inner.get_instrument(symbol)

    async def get_last_price(self, symbol: str) -> Decimal:
        return await self._inner.get_last_price(symbol)

    async def get_balances(self) -> dict[str, Balance]:
        return await self._inner.get_balances()

    async def get_open_orders(self, symbol: str) -> list[Order]:
        return await self._inner.get_open_orders(symbol)

    async def get_executions(self, symbol: str, *, limit: int = 50) -> list[Execution]:
        return await self._inner.get_executions(symbol, limit=limit)

    # --- mutations are no-ops ----------------------------------------------

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
        fake_id = f"dry-{self._counter}-{uuid.uuid4().hex[:8]}"
        log.info(
            "dry_run.place_limit",
            symbol=symbol,
            side=side.value,
            qty=str(qty),
            price=str(price),
            link=order_link_id,
            order_id=fake_id,
        )
        return fake_id

    async def cancel_order(self, symbol: str, order_id: str) -> None:
        log.info("dry_run.cancel_order", symbol=symbol, order_id=order_id)

    async def cancel_all(self, symbol: str) -> None:
        log.info("dry_run.cancel_all", symbol=symbol)
