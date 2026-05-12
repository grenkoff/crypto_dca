from __future__ import annotations

from django import forms

from core.trading.models import StrategyConfig


class StrategyConfigForm(forms.ModelForm):  # type: ignore[type-arg]
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
        ]
