"""Event bus protocol used by the trader to publish state changes.

Stage 5 ships a NoOp implementation that just drops events on the floor; stage
6 replaces it with a Redis pub/sub implementation consumed by the Telegram bot.
"""

from __future__ import annotations

from typing import Any, Protocol


class EventBus(Protocol):
    """Protocol for publishing trader state-change events."""

    async def publish(self, event_type: str, payload: dict[str, Any]) -> None:
        """Publish an event with a type and JSON-able payload."""
        ...


class NoOpEventBus:
    """Event bus that drops all events (used when Redis is absent)."""

    async def publish(self, event_type: str, payload: dict[str, Any]) -> None:
        """Discard the event."""
        return None


class RecordingEventBus:
    """Test helper: stores published events in memory."""

    def __init__(self) -> None:
        self.events: list[tuple[str, dict[str, Any]]] = []

    async def publish(self, event_type: str, payload: dict[str, Any]) -> None:
        """Record the event in the in-memory list."""
        self.events.append((event_type, payload))
