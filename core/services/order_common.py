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


_OUR_PREFIXES = ("grid-buy", "grid-tp")


def link_id(prefix: str, level: int) -> str:
    """Build a unique order_link_id from prefix, level and ms clock."""
    return f"{prefix}-{level}-{int(datetime.now(tz=UTC).timestamp() * 1000)}"


def level_from_link(link: str) -> int | None:
    """The grid level a link id belongs to, or None if it is not ours.

    Ids look like ``grid-tp-comp-606-1787581174946``: our prefix, the
    level, then a millisecond stamp. Anything else was placed by hand or
    by another tool and must be left alone.
    """
    if not link.startswith(_OUR_PREFIXES):
        return None
    parts = link.rsplit("-", 2)
    if len(parts) != 3:
        return None
    try:
        return int(parts[1])
    except ValueError:
        return None
