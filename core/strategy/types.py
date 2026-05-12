from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from typing import Literal

GridMode = Literal["absolute", "percent"]


@dataclass(frozen=True)
class GridLevelSpec:
    level_index: int
    price: Decimal


@dataclass(frozen=True)
class OpenPosition:
    """View of an open position as the strategy engine needs it.

    Decoupled from the Django model so the engine is unit-testable without a DB.
    """

    id: int
    entry_price: Decimal
    qty: Decimal
    fees_in: Decimal
    current_tp_price: Decimal


@dataclass(frozen=True)
class CompensationDecision:
    target_position_id: int
    new_tp_price: Decimal
