from __future__ import annotations

from decimal import Decimal
from typing import Any, cast

from core.exchange.bybit import BybitClient
from core.exchange.types import Balance
from core.services.balances import BalanceCache


class _CountingClient:
    """Counts balance fetches and can be told to start failing."""

    def __init__(self) -> None:
        self.calls = 0
        self.fail = False
        self.free = Decimal("10")

    async def get_balances(self) -> dict[str, Balance]:
        self.calls += 1
        if self.fail:
            raise RuntimeError("connection dropped")
        return {
            "USDT": Balance(coin="USDT", free=self.free, locked=Decimal("0"))
        }


def _cache(client: _CountingClient, ttl: float = 30.0) -> BalanceCache:
    return BalanceCache(cast(BybitClient, cast(Any, client)), ttl)


async def test_readers_inside_the_ttl_share_one_fetch() -> None:
    client = _CountingClient()
    cache = _cache(client)
    first = await cache.snapshot()
    second = await cache.snapshot()
    assert client.calls == 1
    assert first == second


async def test_a_stale_cache_refetches() -> None:
    client = _CountingClient()
    cache = _cache(client, ttl=0.0)
    await cache.snapshot()
    await cache.snapshot()
    assert client.calls == 2


async def test_a_failed_refresh_keeps_serving_the_last_snapshot() -> None:
    client = _CountingClient()
    cache = _cache(client, ttl=0.0)
    warm = await cache.snapshot()
    client.fail = True
    stale = await cache.snapshot()
    assert stale == warm
    assert stale["USDT"].free == Decimal("10")


async def test_a_failure_before_any_success_yields_nothing() -> None:
    client = _CountingClient()
    client.fail = True
    assert await _cache(client).snapshot() == {}


async def test_a_refreshed_snapshot_reflects_new_balances() -> None:
    client = _CountingClient()
    cache = _cache(client, ttl=0.0)
    await cache.snapshot()
    client.free = Decimal("42")
    assert (await cache.snapshot())["USDT"].free == Decimal("42")
