"""Notification preferences: event toggles and the daily-digest schedule.

The digest time lives in UTC in the DB and is set and shown in UTC.
Presentation constants live here; the DB access goes through the DAO.
"""

from __future__ import annotations

from datetime import time

from core.db.models import NotificationSettings
from core.services import repository

EVENT_TOGGLE: dict[str, str] = {
    "error": "notify_errors",
    "position.closed": "notify_closed",
    "compensation.applied": "notify_compensation",
    "position.opened": "notify_opened",
    "order.placed": "notify_order_placed",
    "order.cancelled": "notify_order_cancelled",
}

TOGGLE_LABELS: list[tuple[str, str]] = [
    ("notify_errors", "Errors / alerts"),
    ("notify_closed", "Closes (profit)"),
    ("notify_compensation", "Compensation"),
    ("notify_opened", "Position opened"),
    ("notify_order_placed", "Buy placed"),
    ("notify_order_cancelled", "Buy cancelled"),
    ("digest_enabled", "Daily digest"),
]

_ALLOWED_FIELDS = {f for f, _ in TOGGLE_LABELS}


async def load_settings() -> NotificationSettings:
    """Load the singleton notification settings row."""
    return await repository.load_notification_settings()


async def event_enabled(event_type: str) -> bool:
    """Whether notifications for the given event type are enabled."""
    field = EVENT_TOGGLE.get(event_type)
    if field is None:
        return True
    return await repository.notify_flag(field)


async def toggle_field(field: str) -> bool:
    """Flip a boolean toggle and return its new value.

    Rejects unknown fields.
    """
    if field not in _ALLOWED_FIELDS:
        raise ValueError(f"unknown notification field: {field}")
    return await repository.toggle_notify_flag(field)


async def set_digest_time_utc(t: time) -> time:
    """Store the daily-digest time (UTC); return the stored value."""
    return await repository.set_digest_time(t)
