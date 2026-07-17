"""Typed application settings loaded from the environment / .env."""

from __future__ import annotations

from functools import lru_cache

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class BybitSettings(BaseSettings):
    """Bybit API credentials and client settings (``BYBIT_*``)."""

    model_config = SettingsConfigDict(
        env_file=".env", extra="ignore", env_prefix="BYBIT_"
    )

    api_key: str = Field(default="")
    api_secret: str = Field(default="")
    testnet: bool = Field(default=True)
    recv_window: int = Field(default=5000)


class TraderSettings(BaseSettings):
    """Trader runtime flags (``TRADER_*``)."""

    model_config = SettingsConfigDict(
        env_file=".env", extra="ignore", env_prefix="TRADER_"
    )

    dry_run: bool = Field(default=False)
    skip_instance_guard: bool = Field(default=False)


class RedisSettings(BaseSettings):
    """Redis connection settings."""

    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    redis_url: str = Field(default="")


class TelegramSettings(BaseSettings):
    """Telegram bot settings (``TELEGRAM_*``)."""

    model_config = SettingsConfigDict(
        env_file=".env", extra="ignore", env_prefix="TELEGRAM_"
    )

    bot_token: str = Field(default="")


@lru_cache(maxsize=1)
def bybit_settings() -> BybitSettings:
    """Return the cached Bybit settings."""
    return BybitSettings()


@lru_cache(maxsize=1)
def trader_settings() -> TraderSettings:
    """Return the cached trader settings."""
    return TraderSettings()


@lru_cache(maxsize=1)
def redis_settings() -> RedisSettings:
    """Return the cached Redis settings."""
    return RedisSettings()


@lru_cache(maxsize=1)
def telegram_settings() -> TelegramSettings:
    """Return the cached Telegram settings."""
    return TelegramSettings()
