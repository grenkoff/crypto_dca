"""Shared account-balance snapshot with a short time-to-live.

The grid maintainer already fetches balances on every fill and on its
reconcile tick; the compensator needs the same numbers on every close.
Caching them means the second reader costs nothing and the exchange sees
fewer calls than before, not more.
"""

from __future__ import annotations

from time import monotonic

import structlog

from core.exchange.bybit import BybitClient
from core.exchange.types import Balance

log = structlog.get_logger()

_TTL_SECONDS = 30.0


class BalanceCache:
    """Serve one balance snapshot to every reader within its lifetime."""

    def __init__(
        self, client: BybitClient, ttl_seconds: float = _TTL_SECONDS
    ) -> None:
        self._client = client
        self._ttl = ttl_seconds
        self._snapshot: dict[str, Balance] = {}
        self._stamped: float | None = None

    async def snapshot(self) -> dict[str, Balance]:
        """Current balances, refetched only once the cache goes stale.

        A failed refresh keeps serving the previous snapshot: stale
        balances size the grid slightly wrong, whereas raising here would
        abort a fill that has already happened on the exchange.
        """
        now = monotonic()
        if self._stamped is not None and now - self._stamped < self._ttl:
            return self._snapshot
        try:
            self._snapshot = await self._client.get_balances()
        except Exception as exc:
            log.warning("balances.refresh_failed", error=str(exc)[:120])
            return self._snapshot
        self._stamped = now
        return self._snapshot
