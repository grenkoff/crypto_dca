"""Standalone-entrypoint bootstrap: structured logging."""

from __future__ import annotations

from core.config.logging import configure_logging


def bootstrap() -> None:
    """Configure structured logging for an entrypoint."""
    configure_logging()
