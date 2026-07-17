"""Typed exchange models: instruments, balances, orders, executions."""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field


class Side(StrEnum):
    """Order side."""

    BUY = "Buy"
    SELL = "Sell"


class OrderStatus(StrEnum):
    """Bybit order status values."""

    NEW = "New"
    PARTIALLY_FILLED = "PartiallyFilled"
    PARTIALLY_FILLED_CANCELED = "PartiallyFilledCanceled"
    FILLED = "Filled"
    CANCELLED = "Cancelled"
    REJECTED = "Rejected"
    UNTRIGGERED = "Untriggered"
    TRIGGERED = "Triggered"
    DEACTIVATED = "Deactivated"


class _Frozen(BaseModel):
    model_config = ConfigDict(frozen=True)


class Instrument(_Frozen):
    """Trading instrument constraints (tick, lot, minimums)."""

    symbol: str
    base_coin: str
    quote_coin: str
    tick_size: Decimal
    lot_size: Decimal
    min_order_qty: Decimal
    min_order_amt: Decimal


class Balance(_Frozen):
    """A coin balance split into free and locked amounts."""

    coin: str
    free: Decimal
    locked: Decimal

    @property
    def total(self) -> Decimal:
        """Free plus locked balance."""
        return self.free + self.locked


class Order(_Frozen):
    """An exchange order snapshot."""

    order_id: str
    symbol: str
    side: Side
    price: Decimal
    qty: Decimal
    filled_qty: Decimal = Field(default=Decimal(0))
    status: OrderStatus
    created_at: datetime
    updated_at: datetime


class Execution(_Frozen):
    """A single trade execution (fill)."""

    exec_id: str
    order_id: str
    symbol: str
    side: Side
    price: Decimal
    qty: Decimal
    fee: Decimal
    fee_coin: str
    executed_at: datetime
