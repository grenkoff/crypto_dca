from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal

import pytest
from django.db import IntegrityError

from core.trading.models import (
    BotStatus,
    ExecutionLog,
    GridLevel,
    OrderSide,
    Position,
    PositionStatus,
    StrategyConfig,
    TelegramUser,
)

pytestmark = pytest.mark.django_db


def test_strategy_config_singleton() -> None:
    a = StrategyConfig.load()
    a.grid_step = Decimal("100")
    a.order_qty_quote = Decimal("20")
    a.save()
    b = StrategyConfig.load()
    assert a.pk == b.pk == 1
    assert b.grid_step == Decimal("100")


def test_bot_status_singleton_defaults() -> None:
    s = BotStatus.load()
    assert s.paused is False
    assert s.pk == 1


def test_grid_level_unique_index() -> None:
    GridLevel.objects.create(level_index=0, target_buy_price=Decimal("60000"))
    with pytest.raises(IntegrityError):
        GridLevel.objects.create(
            level_index=0, target_buy_price=Decimal("59000")
        )


def test_position_is_open() -> None:
    p = Position.objects.create(
        level_index=3,
        entry_price=Decimal("60000"),
        qty=Decimal("0.001"),
        opened_at=datetime.now(tz=UTC),
    )
    assert p.is_open is True
    p.status = PositionStatus.CLOSED
    p.save()
    assert p.is_open is False


def test_execution_log_exec_id_unique() -> None:
    ExecutionLog.objects.create(
        exec_id="e1",
        order_id="o1",
        symbol="BTCUSDT",
        side=OrderSide.BUY,
        price=Decimal("60000"),
        qty=Decimal("0.001"),
        fee=Decimal("0.06"),
        fee_coin="USDT",
        executed_at=datetime.now(tz=UTC),
    )
    with pytest.raises(IntegrityError):
        ExecutionLog.objects.create(
            exec_id="e1",
            order_id="o2",
            symbol="BTCUSDT",
            side=OrderSide.BUY,
            price=Decimal("60001"),
            qty=Decimal("0.001"),
            fee=Decimal("0.06"),
            fee_coin="USDT",
            executed_at=datetime.now(tz=UTC),
        )


def test_telegram_user_chat_id_unique() -> None:
    TelegramUser.objects.create(chat_id=12345, label="me", is_admin=True)
    with pytest.raises(IntegrityError):
        TelegramUser.objects.create(chat_id=12345)
