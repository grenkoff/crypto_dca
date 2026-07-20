"""Notification preferences: event toggles and the daily-digest schedule.

The digest time lives in UTC in the DB and is set and shown in UTC.
"""

from __future__ import annotations

from datetime import time

from asgiref.sync import sync_to_async

from core.trading.models import NotificationSettings

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


@sync_to_async
def load_settings() -> NotificationSettings:
    """Load the singleton notification settings row."""
    return NotificationSettings.load()


@sync_to_async
def event_enabled(event_type: str) -> bool:
    """Whether notifications for the given event type are enabled."""
    field = EVENT_TOGGLE.get(event_type)
    if field is None:
        return True
    return bool(getattr(NotificationSettings.load(), field))


@sync_to_async
def toggle_field(field: str) -> bool:
    """Flip a boolean toggle and return its new value.

    Rejects unknown fields.
    """
    if field not in _ALLOWED_FIELDS:
        raise ValueError(f"unknown notification field: {field}")
    obj = NotificationSettings.load()
    new_value = not bool(getattr(obj, field))
    setattr(obj, field, new_value)
    obj.save(update_fields=[field, "updated_at"])
    return new_value


@sync_to_async
def set_digest_time_utc(t: time) -> time:
    """Store the daily-digest time (UTC); return the stored value."""
    obj = NotificationSettings.load()
    obj.digest_time_utc = t
    obj.save(update_fields=["digest_time_utc", "updated_at"])
    return obj.digest_time_utc
