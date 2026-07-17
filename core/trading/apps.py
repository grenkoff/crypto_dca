"""Django app configuration for the trading app."""

from __future__ import annotations

from django.apps import AppConfig


class TradingConfig(AppConfig):
    """App config for the trading app (models and commands)."""

    default_auto_field = "django.db.models.BigAutoField"
    name = "core.trading"
    label = "trading"
