"""Notification preferences: event toggles and the daily-digest schedule.

The digest time lives in UTC in the DB; users see and set it in Astana local
time (UTC+5, no DST), so conversion is a fixed offset.
"""

from __future__ import annotations

from datetime import datetime, time, timedelta

from asgiref.sync import sync_to_async

from core.trading.models import NotificationSettings

# Astana is UTC+5 year-round (no daylight saving).
ASTANA_OFFSET = timedelta(hours=5)

# Event type -> NotificationSettings boolean field gating it. Types absent from
# this map are always delivered (treated as un-suppressible).
EVENT_TOGGLE: dict[str, str] = {
    "error": "notify_errors",
    "position.closed": "notify_closed",
    "compensation.applied": "notify_compensation",
    "position.opened": "notify_opened",
    "order.placed": "notify_order_placed",
    "order.cancelled": "notify_order_cancelled",
}

# Ordered (field, label) pairs for the /notify inline menu.
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


def utc_to_astana(t: time) -> time:
    """Render a stored UTC digest time as Astana local time."""
    base = datetime(2000, 1, 1, t.hour, t.minute)
    return (base + ASTANA_OFFSET).time()


def astana_to_utc(t: time) -> time:
    """Convert an Astana-local digest time to the UTC value we store."""
    base = datetime(2000, 1, 1, t.hour, t.minute)
    return (base - ASTANA_OFFSET).time()


@sync_to_async
def load_settings() -> NotificationSettings:
    return NotificationSettings.load()


@sync_to_async
def event_enabled(event_type: str) -> bool:
    field = EVENT_TOGGLE.get(event_type)
    if field is None:
        return True  # unknown/critical types are never suppressed
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
def set_digest_time_astana(astana: time) -> time:
    """Store a digest time given in Astana local; return the stored UTC
    value."""
    obj = NotificationSettings.load()
    obj.digest_time_utc = astana_to_utc(astana)
    obj.save(update_fields=["digest_time_utc", "updated_at"])
    return obj.digest_time_utc
