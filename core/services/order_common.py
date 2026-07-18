"""Shared primitives for order placement and position protection."""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal

from core.trading.models import Position


def link_id(prefix: str, level: int) -> str:
    """Build a unique order_link_id from prefix, level and ms clock."""
    return f"{prefix}-{level}-{int(datetime.now(tz=UTC).timestamp() * 1000)}"


def set_tp(*, target: Position, tp_price: Decimal, tp_order_id: str) -> None:
    """Persist a new take-profit price and order id on a position."""
    target.tp_price = tp_price
    target.tp_order_id = tp_order_id
    target.save(update_fields=["tp_price", "tp_order_id"])
