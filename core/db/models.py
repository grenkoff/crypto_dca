"""SQLAlchemy models for the ``trading_*`` tables (the source of truth).

Column types, indexes and constraints match the live schema (originally
created by Django, now owned here). Python-side defaults mirror the old
model defaults so construction is ergonomic without changing the DDL.
"""

from __future__ import annotations

import enum
from datetime import UTC, date, datetime, time
from decimal import Decimal

from sqlalchemy import (
    BigInteger,
    Boolean,
    CheckConstraint,
    Date,
    DateTime,
    ForeignKey,
    Identity,
    Index,
    Integer,
    Numeric,
    SmallInteger,
    String,
    Text,
    Time,
    UniqueConstraint,
    text,
)
from sqlalchemy.orm import Mapped, mapped_column

from core.db.base import Base

_AMOUNT = Numeric(28, 12)
_FEE = Numeric(10, 8)
_TS = DateTime(timezone=True)


def _utcnow() -> datetime:
    """Timezone-aware now (default for auto-timestamp columns)."""
    return datetime.now(tz=UTC)


class GridMode(enum.StrEnum):
    """Grid spacing mode (absolute step or percent)."""

    ABSOLUTE = "absolute"
    PERCENT = "percent"


class LevelStatus(enum.StrEnum):
    """Lifecycle status of a grid level."""

    IDLE = "idle"
    AWAITING_FILL = "awaiting_fill"
    FILLED = "filled"


class PositionStatus(enum.StrEnum):
    """Lifecycle status of a position."""

    OPEN = "open"
    CLOSED = "closed"


class OrderSide(enum.StrEnum):
    """Order side (buy or sell)."""

    BUY = "Buy"
    SELL = "Sell"


class StrategyConfig(Base):
    """Mirror of ``trading_strategyconfig`` (singleton, smallint id)."""

    __tablename__ = "trading_strategyconfig"
    __table_args__ = (
        CheckConstraint(
            "max_open_orders >= 0",
            name="trading_strategyconfig_max_open_orders_check",
        ),
    )

    id: Mapped[int] = mapped_column(
        SmallInteger, primary_key=True, autoincrement=False
    )
    symbol: Mapped[str] = mapped_column(String(32), default="BTCUSDT")
    grid_mode: Mapped[str] = mapped_column(
        String(16), default=GridMode.PERCENT
    )
    grid_step: Mapped[Decimal] = mapped_column(
        _AMOUNT, default=Decimal("0.005")
    )
    order_qty_quote: Mapped[Decimal] = mapped_column(
        _AMOUNT, default=Decimal("10")
    )
    top_anchor: Mapped[Decimal | None] = mapped_column(_AMOUNT)
    min_profit_quote: Mapped[Decimal] = mapped_column(
        _AMOUNT, default=Decimal("0.01")
    )
    maker_fee: Mapped[Decimal] = mapped_column(_FEE, default=Decimal("0.001"))
    max_open_orders: Mapped[int] = mapped_column(Integer, default=20)
    updated_at: Mapped[datetime] = mapped_column(_TS, default=_utcnow)
    tp_step: Mapped[Decimal] = mapped_column(
        _AMOUNT, default=Decimal("0.00005")
    )
    taker_fee: Mapped[Decimal] = mapped_column(
        _FEE, default=Decimal("0.00075")
    )
    comp_share_min: Mapped[Decimal] = mapped_column(
        _FEE, default=Decimal("0.20")
    )
    comp_share_max: Mapped[Decimal] = mapped_column(
        _FEE, default=Decimal("0.80")
    )
    comp_hole_offset: Mapped[Decimal] = mapped_column(
        _FEE, default=Decimal("0.03")
    )


class BotStatus(Base):
    """Mirror of ``trading_botstatus`` (singleton, smallint id)."""

    __tablename__ = "trading_botstatus"

    id: Mapped[int] = mapped_column(
        SmallInteger, primary_key=True, autoincrement=False
    )
    paused: Mapped[bool] = mapped_column(Boolean, default=False)
    last_heartbeat: Mapped[datetime | None] = mapped_column(_TS)
    last_error: Mapped[str] = mapped_column(Text, default="")
    started_at: Mapped[datetime | None] = mapped_column(_TS)
    applied_grid_step: Mapped[Decimal | None] = mapped_column(_AMOUNT)
    applied_order_qty: Mapped[Decimal | None] = mapped_column(_AMOUNT)
    pending_credit: Mapped[Decimal] = mapped_column(
        _AMOUNT, default=Decimal(0)
    )
    pocket_credit: Mapped[Decimal] = mapped_column(_AMOUNT, default=Decimal(0))


class NotificationSettings(Base):
    """Mirror of ``trading_notificationsettings`` (singleton)."""

    __tablename__ = "trading_notificationsettings"

    id: Mapped[int] = mapped_column(
        SmallInteger, primary_key=True, autoincrement=False
    )
    notify_errors: Mapped[bool] = mapped_column(Boolean, default=True)
    notify_closed: Mapped[bool] = mapped_column(Boolean, default=True)
    notify_compensation: Mapped[bool] = mapped_column(Boolean, default=True)
    notify_opened: Mapped[bool] = mapped_column(Boolean, default=True)
    notify_order_placed: Mapped[bool] = mapped_column(Boolean, default=True)
    digest_enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    digest_time_utc: Mapped[time] = mapped_column(Time, default=time(19, 0))
    digest_last_sent: Mapped[date | None] = mapped_column(Date)
    updated_at: Mapped[datetime] = mapped_column(_TS, default=_utcnow)
    notify_order_cancelled: Mapped[bool] = mapped_column(Boolean, default=True)


class GridLevel(Base):
    """Mirror of ``trading_gridlevel``."""

    __tablename__ = "trading_gridlevel"
    __table_args__ = (
        UniqueConstraint(
            "level_index", name="trading_gridlevel_level_index_key"
        ),
    )

    id: Mapped[int] = mapped_column(BigInteger, Identity(), primary_key=True)
    level_index: Mapped[int] = mapped_column(Integer)
    target_buy_price: Mapped[Decimal] = mapped_column(_AMOUNT)
    current_buy_order_id: Mapped[str] = mapped_column(String(64), default="")
    status: Mapped[str] = mapped_column(String(16), default=LevelStatus.IDLE)
    updated_at: Mapped[datetime] = mapped_column(_TS, default=_utcnow)


class Position(Base):
    """Mirror of ``trading_position``."""

    __tablename__ = "trading_position"
    __table_args__ = (
        Index("trading_pos_status_530703_idx", "status", "level_index"),
        Index("trading_pos_status_9e0ef0_idx", "status", "opened_at"),
    )

    id: Mapped[int] = mapped_column(BigInteger, Identity(), primary_key=True)
    level_index: Mapped[int] = mapped_column(Integer)
    entry_price: Mapped[Decimal] = mapped_column(_AMOUNT)
    qty: Mapped[Decimal] = mapped_column(_AMOUNT)
    fees_in: Mapped[Decimal] = mapped_column(_AMOUNT, default=Decimal(0))
    fees_out: Mapped[Decimal] = mapped_column(_AMOUNT, default=Decimal(0))
    tp_order_id: Mapped[str] = mapped_column(String(64), default="")
    tp_price: Mapped[Decimal | None] = mapped_column(_AMOUNT)
    status: Mapped[str] = mapped_column(
        String(16), default=PositionStatus.OPEN
    )
    realized_pnl: Mapped[Decimal] = mapped_column(_AMOUNT, default=Decimal(0))
    opened_at: Mapped[datetime] = mapped_column(_TS)
    closed_at: Mapped[datetime | None] = mapped_column(_TS)
    compensation_credit: Mapped[Decimal] = mapped_column(
        _AMOUNT, default=Decimal(0)
    )
    filled_qty: Mapped[Decimal] = mapped_column(_AMOUNT, default=Decimal(0))
    sell_value: Mapped[Decimal] = mapped_column(_AMOUNT, default=Decimal(0))

    @property
    def remaining_qty(self) -> Decimal:
        """Coins still held: total bought minus what a TP already sold."""
        return max(self.qty - self.filled_qty, Decimal(0))


class ExecutionLog(Base):
    """Mirror of ``trading_executionlog``."""

    __tablename__ = "trading_executionlog"
    __table_args__ = (
        UniqueConstraint("exec_id", name="trading_executionlog_exec_id_key"),
        Index("trading_exe_execute_cb8945_idx", text("executed_at DESC")),
        Index(
            "trading_executionlog_exec_id_27eb799d_like",
            "exec_id",
            postgresql_ops={"exec_id": "varchar_pattern_ops"},
        ),
        Index("trading_executionlog_order_id_5d3edfb6", "order_id"),
        Index(
            "trading_executionlog_order_id_5d3edfb6_like",
            "order_id",
            postgresql_ops={"order_id": "varchar_pattern_ops"},
        ),
    )

    id: Mapped[int] = mapped_column(BigInteger, Identity(), primary_key=True)
    exec_id: Mapped[str] = mapped_column(String(64))
    order_id: Mapped[str] = mapped_column(String(64))
    symbol: Mapped[str] = mapped_column(String(32))
    side: Mapped[str] = mapped_column(String(8))
    price: Mapped[Decimal] = mapped_column(_AMOUNT)
    qty: Mapped[Decimal] = mapped_column(_AMOUNT)
    fee: Mapped[Decimal] = mapped_column(_AMOUNT)
    fee_coin: Mapped[str] = mapped_column(String(16), default="")
    executed_at: Mapped[datetime] = mapped_column(_TS)
    received_at: Mapped[datetime] = mapped_column(_TS, default=_utcnow)


class CompensationLink(Base):
    """Mirror of ``trading_compensationlink`` (two FKs to position)."""

    __tablename__ = "trading_compensationlink"
    __table_args__ = (
        Index(
            "trading_compensationlink_compensated_position_id_5e8824ca",
            "compensated_position_id",
        ),
        Index(
            "trading_compensationlink_profitable_position_id_bf4056ae",
            "profitable_position_id",
        ),
    )

    id: Mapped[int] = mapped_column(BigInteger, Identity(), primary_key=True)
    profit_applied: Mapped[Decimal] = mapped_column(_AMOUNT)
    new_tp_price: Mapped[Decimal] = mapped_column(_AMOUNT)
    created_at: Mapped[datetime] = mapped_column(_TS, default=_utcnow)
    compensated_position_id: Mapped[int] = mapped_column(
        BigInteger,
        ForeignKey(
            "trading_position.id",
            name="trading_compensation_compensated_position_5e8824ca_fk_trading_p",
            deferrable=True,
            initially="DEFERRED",
        ),
    )
    profitable_position_id: Mapped[int] = mapped_column(
        BigInteger,
        ForeignKey(
            "trading_position.id",
            name="trading_compensation_profitable_position__bf4056ae_fk_trading_p",
            deferrable=True,
            initially="DEFERRED",
        ),
    )


class Transfer(Base):
    """A funding movement in or out of the account (cash, not trading)."""

    __tablename__ = "trading_transfer"
    __table_args__ = (
        UniqueConstraint("external_id", name="trading_transfer_external_key"),
        Index("trading_transfer_at_idx", "at"),
    )

    id: Mapped[int] = mapped_column(BigInteger, Identity(), primary_key=True)
    external_id: Mapped[str] = mapped_column(String(64))
    coin: Mapped[str] = mapped_column(String(16))
    amount: Mapped[Decimal] = mapped_column(_AMOUNT)
    at: Mapped[datetime] = mapped_column(_TS)
    recorded_at: Mapped[datetime] = mapped_column(_TS, default=_utcnow)


class TelegramUser(Base):
    """Mirror of ``trading_telegramuser``."""

    __tablename__ = "trading_telegramuser"
    __table_args__ = (
        UniqueConstraint("chat_id", name="trading_telegramuser_chat_id_key"),
    )

    id: Mapped[int] = mapped_column(BigInteger, Identity(), primary_key=True)
    chat_id: Mapped[int] = mapped_column(BigInteger)
    label: Mapped[str] = mapped_column(String(64), default="")
    is_admin: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at: Mapped[datetime] = mapped_column(_TS, default=_utcnow)
    control_token: Mapped[str | None] = mapped_column(String(64))


class WebuiAudit(Base):
    """Audit trail of dashboard control actions (who/what/when)."""

    __tablename__ = "webui_audit"

    id: Mapped[int] = mapped_column(BigInteger, Identity(), primary_key=True)
    actor: Mapped[str] = mapped_column(String(64))
    action: Mapped[str] = mapped_column(String(32))
    detail: Mapped[str] = mapped_column(Text, default="")
    created_at: Mapped[datetime] = mapped_column(_TS, default=_utcnow)
