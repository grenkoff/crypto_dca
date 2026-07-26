"""Read-side assembly for the dashboard, built on the async DAO.

DB-only (no exchange I/O), so the page loads fast and cannot move money.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal

from core.services import repository


@dataclass(frozen=True)
class OrderRow:
    """One open position as the dashboard shows it."""

    level_index: int
    entry_price: Decimal
    qty: Decimal
    tp_price: Decimal | None


@dataclass(frozen=True)
class DashboardView:
    """Everything the dashboard renders in one snapshot."""

    symbol: str
    paused: bool
    open_positions: int
    started_at: datetime | None
    last_heartbeat: datetime | None
    pnl_today: Decimal
    pnl_24h: Decimal
    pnl_7d: Decimal
    pnl_30d: Decimal
    pnl_all: Decimal
    deployed: Decimal
    compensations_24h: int
    orders: list[OrderRow]
    generated_at: datetime


async def dashboard_data() -> DashboardView:
    """Assemble the dashboard snapshot from the DAO (DB-only)."""
    now = datetime.now(tz=UTC)
    midnight = now.replace(hour=0, minute=0, second=0, microsecond=0)
    (
        paused,
        open_count,
        started_at,
        last_heartbeat,
    ) = await repository.status_data()
    metrics = await repository.digest_metrics()
    orders = [
        OrderRow(
            level_index=level_index,
            entry_price=entry_price,
            qty=qty,
            tp_price=tp_price,
        )
        for level_index, entry_price, qty, tp_price in (
            await repository.orders_data()
        )
    ]
    return DashboardView(
        symbol=await repository.symbol(),
        paused=paused,
        open_positions=open_count,
        started_at=started_at,
        last_heartbeat=last_heartbeat,
        pnl_today=await repository.realized_pnl_since(midnight),
        pnl_24h=await repository.realized_pnl_since(now - timedelta(hours=24)),
        pnl_7d=await repository.realized_pnl_since(now - timedelta(days=7)),
        pnl_30d=await repository.realized_pnl_since(now - timedelta(days=30)),
        pnl_all=await repository.realized_pnl_since(None),
        deployed=metrics["deployed"],
        compensations_24h=metrics["compensations_24h"],
        orders=orders,
        generated_at=now,
    )
