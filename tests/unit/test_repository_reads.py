from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest
from sqlalchemy import select

from core.db.models import (
    BotStatus,
    CompensationLink,
    ExecutionLog,
    Position,
    PositionStatus,
    StrategyConfig,
)
from core.db.session import new_session
from core.exchange.types import Transfer as BybitTransfer
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


async def test_pnl_curve_data_fills_gap_days_with_zero() -> None:
    await _closed("0.02", "5", datetime(2026, 7, 20, 12, 0, tzinfo=UTC))
    await _closed("0.02", "5", datetime(2026, 7, 23, 12, 0, tzinfo=UTC))
    days, _base, locked, dates = await repository.pnl_curve_data()
    assert [label for label, _ in days] == [
        "20.07",
        "21.07",
        "22.07",
        "23.07",
    ]
    assert [profit for _, profit in days] == [
        Decimal("5"),
        Decimal("0"),
        Decimal("0"),
        Decimal("5"),
    ]
    assert len(locked) == len(dates) == 4


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


async def test_pnl_curve_data_caps_at_100_days() -> None:
    base = datetime(2026, 1, 1, 12, 0, tzinfo=UTC)
    await add_rows(
        *[
            Position(
                level_index=1,
                entry_price=Decimal("0.02"),
                qty=Decimal("100"),
                status=PositionStatus.CLOSED,
                realized_pnl=Decimal("1"),
                opened_at=base + timedelta(days=i) - timedelta(hours=1),
                closed_at=base + timedelta(days=i),
            )
            for i in range(130)
        ]
    )
    days, _base, locked, dates = await repository.pnl_curve_data()
    assert len(days) == len(dates) == len(locked) == 100
    assert dates[0] == (base + timedelta(days=30)).date()
    assert dates[-1] == (base + timedelta(days=129)).date()


async def test_profit_rate_data() -> None:
    now = datetime.now(tz=UTC)
    await _closed("0.02", "5", now - timedelta(days=1))
    await _closed("0.02", "3", now - timedelta(days=10))
    await _open(1, "0.02", "100", "0.03")  # deployed cost 2, always open
    realized, span, avg_deployed = await repository.profit_rate_data()
    assert realized == Decimal("8")
    assert Decimal("9") < span < Decimal("11")
    # the always-open position (cost 2) is deployed every day of the span,
    # so the time-average deployed is at least its cost
    assert avg_deployed >= Decimal("2")


async def _cfg(maker_fee: str = "0", grid_step: str = "0.00005") -> None:
    await add_rows(
        StrategyConfig(
            id=1,
            symbol="KASUSDT",
            grid_step=Decimal(grid_step),
            tp_step=Decimal("0.0002"),
            order_qty_quote=Decimal("5"),
            maker_fee=Decimal(maker_fee),
        )
    )


async def test_tp_projection_counts_open_lots_at_their_tp() -> None:
    await _cfg()
    await _open(1, "0.02", "100", "0.03")
    series = await repository.tp_projection_series(Decimal("10"), 1)
    assert len(series) == 1
    assert series[0][1] == Decimal("10") + Decimal("100") * Decimal("0.03")


async def test_tp_projection_applies_maker_fee_to_the_sale() -> None:
    await _cfg(maker_fee="0.001")
    await _open(1, "0.02", "100", "0.03")
    _day, value = (await repository.tp_projection_series(Decimal("10"), 1))[0]
    assert value == Decimal("10") + Decimal("3") * Decimal("0.999")


async def test_tp_projection_rewinds_usdt_over_later_fills() -> None:
    now = datetime.now(tz=UTC)
    await _cfg()
    await add_rows(
        ExecutionLog(
            exec_id="e1",
            order_id="o1",
            symbol="KASUSDT",
            side="Sell",
            price=Decimal("0.03"),
            qty=Decimal("100"),
            fee=Decimal("0.5"),
            fee_coin="USDT",
            executed_at=now - timedelta(hours=1),
        ),
        ExecutionLog(
            exec_id="e2",
            order_id="o2",
            symbol="KASUSDT",
            side="Buy",
            price=Decimal("0.02"),
            qty=Decimal("50"),
            fee=Decimal("0.03"),
            fee_coin="KAS",
            executed_at=now - timedelta(hours=1),
        ),
    )
    series = await repository.tp_projection_series(Decimal("100"), 2)
    yesterday = series[0][1]
    assert yesterday == Decimal("100") - (Decimal("2.5") - Decimal("1"))


async def test_tp_projection_uses_the_tp_a_lot_carried_back_then() -> None:
    now = datetime.now(tz=UTC)
    await _cfg()
    await _open(1, "0.02", "100", "0.03")
    async with new_session() as session:
        pos_id = await session.scalar(select(Position.id))
    assert pos_id is not None
    await add_rows(
        CompensationLink(
            profit_applied=Decimal("0.1"),
            new_tp_price=Decimal("0.03"),
            created_at=now - timedelta(hours=2),
            compensated_position_id=pos_id,
            profitable_position_id=pos_id,
        )
    )
    series = await repository.tp_projection_series(Decimal("0"), 2)
    before, after = series[0][1], series[1][1]
    assert after == Decimal("100") * Decimal("0.03")
    assert before == Decimal("100") * (Decimal("0.03") + Decimal("0.00005"))


async def test_tp_projection_rejects_a_non_positive_window() -> None:
    with pytest.raises(ValueError, match="days must be positive"):
        await repository.tp_projection_series(Decimal("0"), 0)


def _transfer(ext: str, amount: str, at: datetime) -> BybitTransfer:
    return BybitTransfer(
        external_id=ext, coin="USDT", amount=Decimal(amount), at=at
    )


async def test_transfers_are_stored_once_per_external_id() -> None:
    now = datetime.now(tz=UTC)
    rows = [_transfer("a", "50", now), _transfer("b", "-20", now)]
    assert await repository.record_transfers(rows) == 2
    assert await repository.record_transfers(rows) == 0
    assert (
        await repository.record_transfers([*rows, _transfer("c", "1", now)])
        == 1
    )


async def test_last_transfer_at_reports_the_newest() -> None:
    now = datetime.now(tz=UTC)
    assert await repository.last_transfer_at() is None
    await repository.record_transfers(
        [
            _transfer("old", "10", now - timedelta(days=3)),
            _transfer("new", "10", now - timedelta(hours=1)),
        ]
    )
    latest = await repository.last_transfer_at()
    assert latest is not None
    assert (now - latest).total_seconds() < 7200


async def test_projection_rewinds_a_deposit_out_of_the_past() -> None:
    now = datetime.now(tz=UTC)
    await _cfg()
    await _open(1, "0.02", "100", "0.03")
    await repository.record_transfers(
        [_transfer("dep", "40", now - timedelta(hours=2))]
    )
    series = await repository.tp_projection_series(Decimal("100"), 2)
    yesterday, today = series[0][1], series[1][1]
    assert today - yesterday == Decimal("40")


async def test_account_value_adds_base_held_outside_positions() -> None:
    await _cfg()
    await _open(1, "0.02", "100", "0.03")
    days = [datetime.now(tz=UTC).date()]
    with_spare = await repository.account_value_series(
        Decimal("10"), Decimal("500"), Decimal("0.03"), days
    )
    without = await repository.account_value_series(
        Decimal("10"), Decimal(0), Decimal("0.03"), days
    )
    assert with_spare[0][1] - without[0][1] == Decimal("500") * Decimal("0.03")


async def test_account_value_of_no_days_is_empty() -> None:
    await _cfg()
    assert (
        await repository.account_value_series(
            Decimal("10"), Decimal(0), Decimal("0.03"), []
        )
        == []
    )


async def test_open_base_qty_counts_only_unsold_coins() -> None:
    await _cfg()
    await _open(1, "0.02", "100", "0.03")
    await _open(2, "0.021", "50", "0.031")
    assert await repository.open_base_qty() == Decimal("150")
