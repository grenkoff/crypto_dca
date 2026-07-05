"""Explicit structlog configuration shared by all services.

Without an explicit ``structlog.configure()`` the library falls back to lazy
defaults that other libraries (notably pybit touching stdlib ``logging``) can
perturb, silently dropping INFO events such as ``order.buy_placed``. Pinning the
config here makes every service log deterministically to stdout.

Call ``configure_logging()`` once at each entrypoint (trader, tgbot, web) before
anything logs. Set ``LOG_JSON=1`` for machine-readable output in production and
``LOG_LEVEL`` to change verbosity.
"""

from __future__ import annotations

import logging
import sys

import structlog
from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict

_configured = False


class LogSettings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore", env_prefix="LOG_")

    level: str = Field(default="INFO")
    use_json: bool = Field(default=False, validation_alias="LOG_JSON")


def configure_logging() -> None:
    global _configured
    if _configured:
        return
    settings = LogSettings()
    level = logging.getLevelNamesMapping().get(settings.level.upper(), logging.INFO)
    renderer: structlog.typing.Processor = (
        structlog.processors.JSONRenderer()
        if settings.use_json
        else structlog.dev.ConsoleRenderer(colors=sys.stdout.isatty())
    )
    structlog.configure(
        processors=[
            structlog.contextvars.merge_contextvars,
            structlog.processors.add_log_level,
            structlog.processors.TimeStamper(fmt="%Y-%m-%d %H:%M:%S", utc=False),
            structlog.processors.StackInfoRenderer(),
            structlog.processors.format_exc_info,
            renderer,
        ],
        wrapper_class=structlog.make_filtering_bound_logger(level),
        logger_factory=structlog.PrintLoggerFactory(),
        cache_logger_on_first_use=True,
    )
    _configured = True
