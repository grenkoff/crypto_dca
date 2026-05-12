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


@lru_cache(maxsize=1)
def bybit_settings() -> BybitSettings:
    return BybitSettings()
