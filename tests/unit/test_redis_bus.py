"""Roundtrip publish/subscribe through fakeredis."""

from __future__ import annotations

import asyncio
from decimal import Decimal

import fakeredis.aioredis
import pytest

from core.services.redis_bus import CHANNEL, RedisEventBus


@pytest.fixture
def bus(monkeypatch: pytest.MonkeyPatch) -> RedisEventBus:
    fake = fakeredis.aioredis.FakeRedis(decode_responses=True)

    def _from_url(
        _url: str, **_kwargs: object
    ) -> fakeredis.aioredis.FakeRedis:
        return fake

    monkeypatch.setattr("core.services.redis_bus.Redis.from_url", _from_url)
    return RedisEventBus("redis://fake")


async def test_publish_and_subscribe_roundtrip(bus: RedisEventBus) -> None:
    received: list[dict[str, object]] = []

    async def collect() -> None:
        async for event in bus.subscribe():
            received.append(event)
            if len(received) == 2:
                break

    task = asyncio.create_task(collect())
    await asyncio.sleep(0.05)  # give the subscriber time to attach
    await bus.publish(
        "position.opened", {"level": 3, "tp_price": Decimal("60123.45")}
    )
    await bus.publish(
        "position.closed", {"level": 3, "realized": Decimal("0.50")}
    )
    await asyncio.wait_for(task, timeout=2)

    assert received[0]["type"] == "position.opened"
    payload0 = received[0]["payload"]
    assert isinstance(payload0, dict)
    assert payload0["tp_price"] == "60123.45"  # Decimal stringified
    assert received[1]["type"] == "position.closed"
    payload1 = received[1]["payload"]
    assert isinstance(payload1, dict)
    assert payload1["realized"] == "0.50"

    await bus.close()


async def test_channel_constant() -> None:
    assert CHANNEL == "crypto_dca:events"
