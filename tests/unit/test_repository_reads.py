from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest

from core.db.models import (
    BotStatus,
    Position,
    PositionStatus,
    StrategyConfig,
)
from core.services import repository
from tests.conftest import add_rows

pytestmark = pytest.mark.db


async def _closed(entry: str, pnl: str, closed_at: datetime) -> None:
    await add_rows(
        Position(
            level_index=1,
            entry_price=Decimal(entry),
            qty=Decimal("100"),
            status=PositionStatus.CLOSED,
            realized_pnl=Decimal(pnl),
            opened_at=closed_at - timedelta(days=1),
            closed_at=closed_at,
        )
    )


async def _open(level: int, entry: str, qty: str, tp: str | None) -> None:
    await add_rows(
        Position(
            level_index=level,
            entry_price=Decimal(entry),
            qty=Decimal(qty),
            tp_order_id=f"tp-{level}",
            tp_price=Decimal(tp) if tp is not None else None,
            status=PositionStatus.OPEN,
            opened_at=datetime(2026, 7, 1, tzinfo=UTC),
        )
    )


async def test_status_data_reports_pause_and_open_count() -> None:
    await add_rows(BotStatus(id=1, paused=True))
    await _open(1, "0.02", "100", "0.03")
    await _open(2, "0.025", "100", "0.035")
    paused, open_count, _started, _hb = await repository.status_data()
    assert paused is True
    assert open_count == 2


async def test_realized_pnl_since_windows() -> None:
    now = datetime.now(tz=UTC)
    await _closed("0.02", "5", now - timedelta(hours=1))
    await _closed("0.02", "3", now - timedelta(days=3))
    await _closed("0.02", "2", now - timedelta(days=100))
    assert await repository.realized_pnl_since(None) == Decimal("10")
    assert await repository.realized_pnl_since(
        now - timedelta(days=1)
    ) == Decimal("5")
    assert await repository.realized_pnl_since(
        now - timedelta(days=7)
    ) == Decimal("8")


async def test_realized_pnl_since_empty_is_zero() -> None:
    assert await repository.realized_pnl_since(None) == Decimal(0)


async def test_orders_data_ordered_by_level() -> None:
    await _open(5, "0.02", "100", "0.03")
    await _open(2, "0.025", "50", None)
    rows = await repository.orders_data()
    assert [r[0] for r in rows] == [2, 5]
    assert rows[0] == (2, Decimal("0.025"), Decimal("50"), None)
    assert rows[1][3] == Decimal("0.03")


async def test_pnl_curve_data_buckets_by_day() -> None:
    day = datetime(2026, 7, 20, 15, 0, tzinfo=UTC)
    await _closed("0.02", "4", day)
    await _closed("0.02", "6", day + timedelta(hours=2))
    await _open(1, "0.02", "100", "0.03")
    days, base_capital, locked, dates = await repository.pnl_curve_data()
    assert days == [("20.07", Decimal("10"))]
    assert base_capital == Decimal("2")
    assert len(locked) == len(dates) == 1


async def test_digest_metrics_counts_and_deployed() -> None:
    now = datetime.now(tz=UTC)
    await _closed("0.02", "5", now - timedelta(hours=1))
    await _open(1, "0.02", "100", "0.03")
    m = await repository.digest_metrics()
    assert m["closed_24h"] == 1
    assert m["pnl_24h"] == Decimal("5")
    assert m["open_positions"] == 1
    assert m["deployed"] == Decimal("2")


async def test_unlock_from_db_no_profit_returns_none() -> None:
    await add_rows(StrategyConfig(id=1))
    days, per_day = await repository.unlock_from_db(Decimal("0.02"))
    assert days is None
    assert per_day == Decimal(0)


async def test_symbol_reads_config() -> None:
    await add_rows(StrategyConfig(id=1, symbol="KASUSDT"))
    assert await repository.symbol() == "KASUSDT"
