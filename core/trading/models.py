from __future__ import annotations

from decimal import Decimal
from typing import Self

from django.db import models

PRICE_DIGITS = 28
PRICE_DECIMALS = 12


class _Singleton(models.Model):
    id = models.SmallIntegerField(primary_key=True, default=1, editable=False)

    class Meta:
        abstract = True

    @classmethod
    def load(cls) -> Self:
        obj, _ = cls._default_manager.get_or_create(pk=1)
        return obj


class GridMode(models.TextChoices):
    ABSOLUTE = "absolute", "Absolute (USDT step)"
    PERCENT = "percent", "Percent step"


class LevelStatus(models.TextChoices):
    IDLE = "idle", "Idle"
    AWAITING_FILL = "awaiting_fill", "Awaiting fill"
    FILLED = "filled", "Filled"


class PositionStatus(models.TextChoices):
    OPEN = "open", "Open"
    CLOSED = "closed", "Closed"


class OrderSide(models.TextChoices):
    BUY = "Buy", "Buy"
    SELL = "Sell", "Sell"


class StrategyConfig(_Singleton):
    symbol = models.CharField(max_length=32, default="BTCUSDT")
    grid_mode = models.CharField(max_length=16, choices=GridMode.choices, default=GridMode.PERCENT)
    grid_step = models.DecimalField(
        max_digits=PRICE_DIGITS, decimal_places=PRICE_DECIMALS, default=Decimal("0.005")
    )
    tp_step = models.DecimalField(
        max_digits=PRICE_DIGITS,
        decimal_places=PRICE_DECIMALS,
        default=Decimal("0.00005"),
        help_text="Absolute price offset above entry for the take-profit.",
    )
    order_qty_quote = models.DecimalField(
        max_digits=PRICE_DIGITS, decimal_places=PRICE_DECIMALS, default=Decimal("10")
    )
    top_anchor = models.DecimalField(
        max_digits=PRICE_DIGITS, decimal_places=PRICE_DECIMALS, null=True, blank=True
    )
    min_profit_quote = models.DecimalField(
        max_digits=PRICE_DIGITS, decimal_places=PRICE_DECIMALS, default=Decimal("0.01")
    )
    maker_fee = models.DecimalField(max_digits=10, decimal_places=8, default=Decimal("0.001"))
    taker_fee = models.DecimalField(max_digits=10, decimal_places=8, default=Decimal("0.00075"))
    max_open_orders = models.PositiveIntegerField(default=20)
    updated_at = models.DateTimeField(auto_now=True)

    def __str__(self) -> str:
        return f"StrategyConfig({self.symbol}, {self.grid_mode}, step={self.grid_step})"


class BotStatus(_Singleton):
    paused = models.BooleanField(default=False)
    last_heartbeat = models.DateTimeField(null=True, blank=True)
    last_error = models.TextField(blank=True)
    started_at = models.DateTimeField(null=True, blank=True)

    def __str__(self) -> str:
        return "paused" if self.paused else "running"


class GridLevel(models.Model):
    level_index = models.IntegerField(unique=True)
    target_buy_price = models.DecimalField(max_digits=PRICE_DIGITS, decimal_places=PRICE_DECIMALS)
    current_buy_order_id = models.CharField(max_length=64, blank=True)
    status = models.CharField(max_length=16, choices=LevelStatus.choices, default=LevelStatus.IDLE)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["level_index"]

    def __str__(self) -> str:
        return f"L{self.level_index}@{self.target_buy_price} ({self.status})"


class Position(models.Model):
    level_index = models.IntegerField()
    entry_price = models.DecimalField(max_digits=PRICE_DIGITS, decimal_places=PRICE_DECIMALS)
    qty = models.DecimalField(max_digits=PRICE_DIGITS, decimal_places=PRICE_DECIMALS)
    fees_in = models.DecimalField(
        max_digits=PRICE_DIGITS, decimal_places=PRICE_DECIMALS, default=Decimal(0)
    )
    fees_out = models.DecimalField(
        max_digits=PRICE_DIGITS, decimal_places=PRICE_DECIMALS, default=Decimal(0)
    )
    # Cumulative sold quantity and gross sell proceeds — support partial TP fills.
    filled_qty = models.DecimalField(
        max_digits=PRICE_DIGITS, decimal_places=PRICE_DECIMALS, default=Decimal(0)
    )
    sell_value = models.DecimalField(
        max_digits=PRICE_DIGITS, decimal_places=PRICE_DECIMALS, default=Decimal(0)
    )
    tp_order_id = models.CharField(max_length=64, blank=True)
    tp_price = models.DecimalField(
        max_digits=PRICE_DIGITS, decimal_places=PRICE_DECIMALS, null=True, blank=True
    )
    status = models.CharField(
        max_length=16, choices=PositionStatus.choices, default=PositionStatus.OPEN
    )
    realized_pnl = models.DecimalField(
        max_digits=PRICE_DIGITS, decimal_places=PRICE_DECIMALS, default=Decimal(0)
    )
    compensation_credit = models.DecimalField(
        max_digits=PRICE_DIGITS, decimal_places=PRICE_DECIMALS, default=Decimal(0)
    )
    opened_at = models.DateTimeField()
    closed_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        indexes = [
            models.Index(fields=["status", "level_index"]),
            models.Index(fields=["status", "opened_at"]),
        ]
        ordering = ["-opened_at"]

    @property
    def is_open(self) -> bool:
        return self.status == PositionStatus.OPEN

    def __str__(self) -> str:
        return f"Position(L{self.level_index}, {self.qty}@{self.entry_price}, {self.status})"


class ExecutionLog(models.Model):
    """Raw audit trail of fills received from Bybit (WS or REST reconciliation)."""

    exec_id = models.CharField(max_length=64, unique=True)
    order_id = models.CharField(max_length=64, db_index=True)
    symbol = models.CharField(max_length=32)
    side = models.CharField(max_length=8, choices=OrderSide.choices)
    price = models.DecimalField(max_digits=PRICE_DIGITS, decimal_places=PRICE_DECIMALS)
    qty = models.DecimalField(max_digits=PRICE_DIGITS, decimal_places=PRICE_DECIMALS)
    fee = models.DecimalField(max_digits=PRICE_DIGITS, decimal_places=PRICE_DECIMALS)
    fee_coin = models.CharField(max_length=16, blank=True)
    executed_at = models.DateTimeField()
    received_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-executed_at"]
        indexes = [models.Index(fields=["-executed_at"])]


class CompensationLink(models.Model):
    """Records that a profitable position's PnL was applied to compensate another's TP price."""

    profitable_position = models.ForeignKey(
        Position, on_delete=models.CASCADE, related_name="compensations_given"
    )
    compensated_position = models.ForeignKey(
        Position, on_delete=models.CASCADE, related_name="compensations_received"
    )
    profit_applied = models.DecimalField(max_digits=PRICE_DIGITS, decimal_places=PRICE_DECIMALS)
    new_tp_price = models.DecimalField(max_digits=PRICE_DIGITS, decimal_places=PRICE_DECIMALS)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-created_at"]


class TelegramUser(models.Model):
    chat_id = models.BigIntegerField(unique=True)
    label = models.CharField(max_length=64, blank=True)
    is_admin = models.BooleanField(default=False)
    created_at = models.DateTimeField(auto_now_add=True)

    def __str__(self) -> str:
        return f"{self.label or self.chat_id} ({'admin' if self.is_admin else 'user'})"
