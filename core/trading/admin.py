from __future__ import annotations

from django.contrib import admin

from core.trading.models import (
    BotStatus,
    CompensationLink,
    ExecutionLog,
    GridLevel,
    Position,
    StrategyConfig,
    TelegramUser,
)


@admin.register(StrategyConfig)
class StrategyConfigAdmin(admin.ModelAdmin):  # type: ignore[type-arg]
    list_display = ("symbol", "grid_mode", "grid_step", "order_qty_quote", "max_open_orders")


@admin.register(BotStatus)
class BotStatusAdmin(admin.ModelAdmin):  # type: ignore[type-arg]
    list_display = ("paused", "last_heartbeat", "started_at")


@admin.register(GridLevel)
class GridLevelAdmin(admin.ModelAdmin):  # type: ignore[type-arg]
    list_display = ("level_index", "target_buy_price", "status", "current_buy_order_id")
    list_filter = ("status",)
    ordering = ("level_index",)


@admin.register(Position)
class PositionAdmin(admin.ModelAdmin):  # type: ignore[type-arg]
    list_display = (
        "id",
        "level_index",
        "entry_price",
        "qty",
        "tp_price",
        "status",
        "realized_pnl",
        "opened_at",
    )
    list_filter = ("status",)
    search_fields = ("tp_order_id",)


@admin.register(ExecutionLog)
class ExecutionLogAdmin(admin.ModelAdmin):  # type: ignore[type-arg]
    list_display = ("exec_id", "order_id", "side", "price", "qty", "fee", "executed_at")
    list_filter = ("side",)
    search_fields = ("exec_id", "order_id")


@admin.register(CompensationLink)
class CompensationLinkAdmin(admin.ModelAdmin):  # type: ignore[type-arg]
    list_display = (
        "profitable_position",
        "compensated_position",
        "profit_applied",
        "new_tp_price",
        "created_at",
    )


@admin.register(TelegramUser)
class TelegramUserAdmin(admin.ModelAdmin):  # type: ignore[type-arg]
    list_display = ("chat_id", "label", "is_admin", "created_at")
    list_filter = ("is_admin",)
