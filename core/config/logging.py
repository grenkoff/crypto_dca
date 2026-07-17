"""Explicit structlog configuration shared by all services.

Pin the config once at each entrypoint (before anything logs) so INFO
events are never silently dropped. ``LOG_JSON=1`` selects machine-readable
output; ``LOG_LEVEL`` sets verbosity.
"""

from __future__ import annotations

import logging
import sys

import structlog
from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict

_configured = False


class LogSettings(BaseSettings):
    """Logging configuration read from ``LOG_*`` environment variables."""

    model_config = SettingsConfigDict(
        env_file=".env", extra="ignore", env_prefix="LOG_"
    )

    level: str = Field(default="INFO")
    use_json: bool = Field(default=False, validation_alias="LOG_JSON")


def configure_logging() -> None:
    """Configure structlog once (idempotent) to log to stdout."""
    global _configured
    if _configured:
        return
    settings = LogSettings()
    level = logging.getLevelNamesMapping().get(
        settings.level.upper(), logging.INFO
    )
    renderer: structlog.typing.Processor = (
        structlog.processors.JSONRenderer()
        if settings.use_json
        else structlog.dev.ConsoleRenderer(colors=sys.stdout.isatty())
    )
    structlog.configure(
        processors=[
            structlog.contextvars.merge_contextvars,
            structlog.processors.add_log_level,
            structlog.processors.TimeStamper(
                fmt="%Y-%m-%d %H:%M:%S", utc=False
            ),
            structlog.processors.StackInfoRenderer(),
            structlog.processors.format_exc_info,
            renderer,
        ],
        wrapper_class=structlog.make_filtering_bound_logger(level),
        logger_factory=structlog.PrintLoggerFactory(),
        cache_logger_on_first_use=True,
    )
    _configured = True
