"""Shared primitives for order placement and position protection."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal


@dataclass
class SellFillResult:
    """Outcome of applying a sell fill to a position."""

    closed: bool
    realized: Decimal
    filled_qty: Decimal
    remaining: Decimal


def link_id(prefix: str, level: int) -> str:
    """Build a unique order_link_id from prefix, level and ms clock."""
    return f"{prefix}-{level}-{int(datetime.now(tz=UTC).timestamp() * 1000)}"
