"""Redis pub/sub bridge: trader publishes, telegram subscriber listens."""

from __future__ import annotations

import json
import time
from collections.abc import AsyncIterator
from decimal import Decimal
from typing import Any

import structlog
from redis.asyncio import Redis

log = structlog.get_logger()

CHANNEL = "crypto_dca:events"


def _encode(o: Any) -> str:
    if isinstance(o, Decimal):
        return str(o)
    raise TypeError(f"unserialisable type: {type(o).__name__}")


class RedisEventBus:
    def __init__(self, url: str) -> None:
        self._client: Redis = Redis.from_url(url, decode_responses=True)

    async def close(self) -> None:
        await self._client.aclose()

    async def publish(self, event_type: str, payload: dict[str, Any]) -> None:
        message = json.dumps(
            {"type": event_type, "payload": payload, "ts": time.time()},
            default=_encode,
        )
        try:
            await self._client.publish(CHANNEL, message)
        except Exception as exc:
            log.error(
                "redis_bus.publish_failed",
                event_type=event_type,
                error=str(exc),
            )

    async def subscribe(self) -> AsyncIterator[dict[str, Any]]:
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
            await pubsub.aclose()  # type: ignore[no-untyped-call]
