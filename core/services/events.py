"""Event bus protocol used by the trader to publish state changes.

Stage 5 ships a NoOp implementation that just drops events on the floor; stage
6 replaces it with a Redis pub/sub implementation consumed by the Telegram bot.
"""

from __future__ import annotations

from typing import Any, Protocol


class EventBus(Protocol):
    async def publish(
        self, event_type: str, payload: dict[str, Any]
    ) -> None: ...


class NoOpEventBus:
    async def publish(self, event_type: str, payload: dict[str, Any]) -> None:
        return None


class RecordingEventBus:
    """Test helper: stores published events in memory."""

    def __init__(self) -> None:
        self.events: list[tuple[str, dict[str, Any]]] = []

    async def publish(self, event_type: str, payload: dict[str, Any]) -> None:
        self.events.append((event_type, payload))
