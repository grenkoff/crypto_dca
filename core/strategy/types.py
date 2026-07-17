"""Pure value types for the strategy engine (no Django dependency)."""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from typing import Literal

GridMode = Literal["absolute", "percent"]


@dataclass(frozen=True)
class GridLevelSpec:
    """A grid level: its index and step-aligned buy price."""

    level_index: int
    price: Decimal


@dataclass(frozen=True)
class OpenPosition:
    """View of an open position as the strategy engine needs it.

    Decoupled from the Django model so the engine is unit-testable without a
    DB.
    """

    id: int
    entry_price: Decimal
    qty: Decimal
    fees_in: Decimal
    current_tp_price: Decimal
    compensation_credit: Decimal = Decimal(0)


@dataclass(frozen=True)
class CompensationDecision:
    """A planned take-profit move funded by another lot's profit."""

    target_position_id: int
    new_tp_price: Decimal
    new_credit: Decimal
