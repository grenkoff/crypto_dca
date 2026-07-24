from __future__ import annotations

from datetime import UTC, date, datetime, time
from decimal import Decimal

import pytest
from sqlalchemy import Engine
from sqlalchemy.orm import Session

from core.db import models as sa
from core.trading import models as dj

pytestmark = pytest.mark.django_db(transaction=True)


def _get[T](engine: Engine, model: type[T], pk: int) -> T | None:
    with Session(engine) as session:
        return session.get(model, pk)


def test_position_parity(sa_sync_engine: Engine) -> None:
    opened = datetime(2026, 7, 20, 12, 0, tzinfo=UTC)
    p = dj.Position.objects.create(
        level_index=7,
        entry_price=Decimal("0.027700000000"),
        qty=Decimal("123.456789012000"),
        fees_in=Decimal("0.001"),
        tp_order_id="tp-1",
        tp_price=Decimal("0.030000000000"),
        realized_pnl=Decimal("0"),
        opened_at=opened,
    )
    row = _get(sa_sync_engine, sa.Position, p.id)
    assert row is not None
    assert row.level_index == 7
    assert row.entry_price == Decimal("0.0277")
    assert row.qty == Decimal("123.456789012")
    assert row.tp_price == Decimal("0.03")
    assert row.status == "open"
    assert row.opened_at == opened
    assert row.closed_at is None


def test_strategyconfig_parity(sa_sync_engine: Engine) -> None:
    cfg = dj.StrategyConfig.load()
    cfg.symbol = "KASUSDT"
    cfg.maker_fee = Decimal("0.00100000")
    cfg.taker_fee = Decimal("0.00075000")
    cfg.max_open_orders = 42
    cfg.save()
    row = _get(sa_sync_engine, sa.StrategyConfig, 1)
    assert row is not None
    assert row.symbol == "KASUSDT"
    assert row.maker_fee == Decimal("0.001")
    assert row.taker_fee == Decimal("0.00075")
    assert row.max_open_orders == 42


def test_executionlog_parity(sa_sync_engine: Engine) -> None:
    ts = datetime(2026, 7, 21, 9, 30, tzinfo=UTC)
    e = dj.ExecutionLog.objects.create(
        exec_id="exec-abc",
        order_id="ord-xyz",
        symbol="KASUSDT",
        side=dj.OrderSide.BUY,
        price=Decimal("0.027700000000"),
        qty=Decimal("100"),
        fee=Decimal("0.000750000000"),
        executed_at=ts,
    )
    row = _get(sa_sync_engine, sa.ExecutionLog, e.id)
    assert row is not None
    assert row.exec_id == "exec-abc"
    assert row.order_id == "ord-xyz"
    assert row.side == "Buy"
    assert row.price == Decimal("0.0277")
    assert row.executed_at == ts


def test_gridlevel_parity(sa_sync_engine: Engine) -> None:
    g = dj.GridLevel.objects.create(
        level_index=3,
        target_buy_price=Decimal("0.025000000000"),
        status=dj.LevelStatus.AWAITING_FILL,
    )
    row = _get(sa_sync_engine, sa.GridLevel, g.id)
    assert row is not None
    assert row.level_index == 3
    assert row.target_buy_price == Decimal("0.025")
    assert row.status == "awaiting_fill"


def test_botstatus_parity(sa_sync_engine: Engine) -> None:
    status = dj.BotStatus.load()
    status.paused = True
    status.pending_credit = Decimal("1.230000000000")
    status.save()
    row = _get(sa_sync_engine, sa.BotStatus, 1)
    assert row is not None
    assert row.paused is True
    assert row.pending_credit == Decimal("1.23")
    assert row.last_heartbeat is None


def test_notificationsettings_parity(sa_sync_engine: Engine) -> None:
    ns = dj.NotificationSettings.load()
    ns.digest_time_utc = time(19, 0)
    ns.digest_last_sent = date(2026, 7, 24)
    ns.notify_order_cancelled = False
    ns.save()
    row = _get(sa_sync_engine, sa.NotificationSettings, 1)
    assert row is not None
    assert row.digest_time_utc == time(19, 0)
    assert row.digest_last_sent == date(2026, 7, 24)
    assert row.notify_order_cancelled is False


def test_telegramuser_parity(sa_sync_engine: Engine) -> None:
    u = dj.TelegramUser.objects.create(
        chat_id=8_123_456_789, label="me", is_admin=True
    )
    row = _get(sa_sync_engine, sa.TelegramUser, u.id)
    assert row is not None
    assert row.chat_id == 8_123_456_789
    assert row.label == "me"
    assert row.is_admin is True


def test_compensationlink_parity(sa_sync_engine: Engine) -> None:
    opened = datetime(2026, 7, 20, 12, 0, tzinfo=UTC)
    winner = dj.Position.objects.create(
        level_index=1,
        entry_price=Decimal("0.02"),
        qty=Decimal("1"),
        tp_order_id="",
        realized_pnl=Decimal("0"),
        opened_at=opened,
    )
    loser = dj.Position.objects.create(
        level_index=2,
        entry_price=Decimal("0.03"),
        qty=Decimal("1"),
        tp_order_id="",
        realized_pnl=Decimal("0"),
        opened_at=opened,
    )
    link = dj.CompensationLink.objects.create(
        profitable_position=winner,
        compensated_position=loser,
        profit_applied=Decimal("0.500000000000"),
        new_tp_price=Decimal("0.029000000000"),
    )
    row = _get(sa_sync_engine, sa.CompensationLink, link.id)
    assert row is not None
    assert row.profitable_position_id == winner.id
    assert row.compensated_position_id == loser.id
    assert row.profit_applied == Decimal("0.5")
    assert row.new_tp_price == Decimal("0.029")
