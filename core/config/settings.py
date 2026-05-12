from __future__ import annotations

from functools import lru_cache

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class BybitSettings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore", env_prefix="BYBIT_")

    api_key: str = Field(default="")
    api_secret: str = Field(default="")
    testnet: bool = Field(default=True)
    recv_window: int = Field(default=5000)


class RedisSettings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    redis_url: str = Field(default="")


class TelegramSettings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore", env_prefix="TELEGRAM_")

    bot_token: str = Field(default="")
    allowed_chat_ids: str = Field(default="")

    def allowed_chat_id_set(self) -> set[int]:
        return {int(x.strip()) for x in self.allowed_chat_ids.split(",") if x.strip()}


@lru_cache(maxsize=1)
def bybit_settings() -> BybitSettings:
    return BybitSettings()


@lru_cache(maxsize=1)
def redis_settings() -> RedisSettings:
    return RedisSettings()


@lru_cache(maxsize=1)
def telegram_settings() -> TelegramSettings:
    return TelegramSettings()
