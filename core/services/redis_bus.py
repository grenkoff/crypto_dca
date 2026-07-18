"""Redis pub/sub bridge: trader publishes, telegram subscriber listens."""

from __future__ import annotations

import json
import time
from collections.abc import AsyncIterator
from decimal import Decimal
from typing import Any, cast

import structlog
from redis.asyncio import Redis

log = structlog.get_logger()

CHANNEL = "crypto_dca:events"


def _encode(o: Any) -> str:
    if isinstance(o, Decimal):
        return str(o)
    raise TypeError(f"unserialisable type: {type(o).__name__}")


class RedisEventBus:
    """Redis pub/sub event bus (publisher and subscriber)."""

    def __init__(self, url: str) -> None:
        self._client: Redis = Redis.from_url(url, decode_responses=True)

    async def close(self) -> None:
        """Close the Redis connection."""
        await self._client.aclose()

    async def publish(self, event_type: str, payload: dict[str, Any]) -> None:
        """Publish an event to the channel (errors are logged)."""
        message = json.dumps(
            {"type": event_type, "payload": payload, "ts": time.time()},
            default=_encode,
        )
        try:
            await self._client.publish(CHANNEL, message)
        except Exception as exc:
            log.exception(
                "redis_bus.publish_failed",
                event_type=event_type,
                error=str(exc),
            )

    async def subscribe(self) -> AsyncIterator[dict[str, Any]]:
        """Yield decoded events from the channel until cancelled."""
        pubsub = self._client.pubsub()
        await pubsub.subscribe(CHANNEL)
        try:
            async for msg in pubsub.listen():
                if msg.get("type") != "message":
                    continue
                data = msg.get("data")
                if not isinstance(data, str):
                    continue
                try:
                    yield json.loads(data)
                except json.JSONDecodeError as exc:
                    log.warning(
                        "redis_bus.decode_failed",
                        error=str(exc),
                        data=data[:200],
                    )
        finally:
            await pubsub.unsubscribe(CHANNEL)
            await cast(Any, pubsub).aclose()
