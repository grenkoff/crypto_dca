from __future__ import annotations

from rest_framework import serializers

from core.trading.models import BotStatus, Position, StrategyConfig


class BotStatusSerializer(serializers.ModelSerializer):  # type: ignore[type-arg]
    class Meta:
        model = BotStatus
        fields = ["paused", "last_heartbeat", "last_error", "started_at"]


class PositionSerializer(serializers.ModelSerializer):  # type: ignore[type-arg]
    class Meta:
        model = Position
        fields = [
            "id",
            "level_index",
            "entry_price",
            "qty",
            "fees_in",
            "fees_out",
            "tp_order_id",
            "tp_price",
            "status",
            "realized_pnl",
            "opened_at",
            "closed_at",
        ]


class StrategyConfigSerializer(serializers.ModelSerializer):  # type: ignore[type-arg]
    class Meta:
        model = StrategyConfig
        fields = [
            "symbol",
            "grid_mode",
            "grid_step",
            "order_qty_quote",
            "top_anchor",
            "min_profit_quote",
            "maker_fee",
            "max_open_orders",
            "updated_at",
        ]
