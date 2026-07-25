from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest

from core.services import repository
from core.trading.models import (
    BotStatus,
    Position,
    PositionStatus,
    StrategyConfig,
)

pytestmark = pytest.mark.django_db(transaction=True)


def _closed(entry: str, pnl: str, closed_at: datetime) -> None:
    Position.objects.create(
        level_index=1,
        entry_price=Decimal(entry),
        qty=Decimal("100"),
        fees_in=Decimal("0"),
        tp_order_id="",
        status=PositionStatus.CLOSED,
        realized_pnl=Decimal(pnl),
        opened_at=closed_at - timedelta(days=1),
        closed_at=closed_at,
    )


def _open(level: int, entry: str, qty: str, tp: str | None) -> None:
    Position.objects.create(
        level_index=level,
        entry_price=Decimal(entry),
        qty=Decimal(qty),
        fees_in=Decimal("0"),
        tp_order_id=f"tp-{level}",
        tp_price=Decimal(tp) if tp is not None else None,
        status=PositionStatus.OPEN,
        opened_at=datetime(2026, 7, 1, tzinfo=UTC),
    )


def test_status_data_reports_pause_and_open_count() -> None:
    bot = BotStatus.load()
    bot.paused = True
    bot.save()
    _open(1, "0.02", "100", "0.03")
    _open(2, "0.025", "100", "0.035")
    paused, open_count, _started, _hb = repository._status_data()
    assert paused is True
    assert open_count == 2


def test_realized_pnl_since_windows() -> None:
    now = datetime.now(tz=UTC)
    _closed("0.02", "5", now - timedelta(hours=1))
    _closed("0.02", "3", now - timedelta(days=3))
    _closed("0.02", "2", now - timedelta(days=100))
    assert repository._realized_pnl_since(None) == Decimal("10")
    assert repository._realized_pnl_since(now - timedelta(days=1)) == Decimal(
        "5"
    )
    assert repository._realized_pnl_since(now - timedelta(days=7)) == Decimal(
        "8"
    )


def test_realized_pnl_since_empty_is_zero() -> None:
    assert repository._realized_pnl_since(None) == Decimal(0)


def test_orders_data_ordered_by_level() -> None:
    _open(5, "0.02", "100", "0.03")
    _open(2, "0.025", "50", None)
    rows = repository._orders_data()
    assert [r[0] for r in rows] == [2, 5]
    assert rows[0] == (2, Decimal("0.025"), Decimal("50"), None)
    assert rows[1][3] == Decimal("0.03")


def test_pnl_curve_data_buckets_by_day() -> None:
    day = datetime(2026, 7, 20, 15, 0, tzinfo=UTC)
    _closed("0.02", "4", day)
    _closed("0.02", "6", day + timedelta(hours=2))
    _open(1, "0.02", "100", "0.03")
    days, base_capital, locked, dates = repository._pnl_curve_data()
    assert days == [("20.07", Decimal("10"))]
    assert base_capital == Decimal("2")
    assert len(locked) == len(dates) == 1


def test_digest_metrics_counts_and_deployed() -> None:
    now = datetime.now(tz=UTC)
    _closed("0.02", "5", now - timedelta(hours=1))
    _open(1, "0.02", "100", "0.03")
    m = repository._digest_metrics()
    assert m["closed_24h"] == 1
    assert m["pnl_24h"] == Decimal("5")
    assert m["open_positions"] == 1
    assert m["deployed"] == Decimal("2")


def test_unlock_from_db_no_profit_returns_none() -> None:
    days, per_day = repository._unlock_from_db(Decimal("0.02"))
    assert days is None
    assert per_day == Decimal(0)


def test_symbol_reads_config() -> None:
    cfg = StrategyConfig.load()
    cfg.symbol = "KASUSDT"
    cfg.save()
    assert repository._symbol() == "KASUSDT"
