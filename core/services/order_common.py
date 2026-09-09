"""Shared primitives for order placement and position protection."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal
from typing import cast

from core.db.models import StrategyConfig
from core.strategy.lattice import GridGeometry, build_geometry
from core.strategy.types import GridMode


def config_geometry(
    config: StrategyConfig, tick_size: Decimal
) -> GridGeometry:
    """Grid geometry for a strategy config, validating its mode."""
    mode = str(config.grid_mode)
    if mode not in ("absolute", "percent"):
        raise ValueError(f"unexpected grid_mode: {mode}")
    return build_geometry(
        mode=cast(GridMode, mode),
        step=config.grid_step,
        tp_step=config.tp_step,
        tick_size=tick_size,
    )


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
