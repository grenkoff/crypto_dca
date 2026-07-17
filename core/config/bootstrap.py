"""Standalone-entrypoint bootstrap: logging plus Django setup."""

from __future__ import annotations

import os

import django

from core.config.logging import configure_logging


def bootstrap_django() -> None:
    """Configure logging and initialise Django for an entrypoint."""
    configure_logging()
    os.environ.setdefault("DJANGO_SETTINGS_MODULE", "web.settings")
    django.setup()
