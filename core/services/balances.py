"""Account balances: one shared snapshot, and what the book makes of it.

The grid maintainer already fetches balances on every fill and on its
reconcile tick; the compensator needs the same numbers on every close.
Caching them means the second reader costs nothing and the exchange sees
fewer calls than before, not more. The helpers below compare that
snapshot with the coin the open lots claim, which is how loose coin and a
short book are both spotted.
"""

from __future__ import annotations

from collections.abc import Sequence
from decimal import Decimal
from time import monotonic

import structlog

from core.db.models import Position
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

    def invalidate(self) -> None:
        """Drop the cached snapshot so the next read refetches.

        A caller about to commit money against these numbers cannot
        afford them to be up to a TTL old.
        """
        self._stamped = None


def coin_gap(
    balances: dict[str, Balance],
    positions: Sequence[Position],
    base_coin: str,
) -> Decimal:
    """Wallet coin minus what the open lots claim; negative means short."""
    base = balances.get(base_coin)
    wallet = base.total if base is not None else Decimal(0)
    claimed = sum((p.remaining_qty for p in positions), Decimal(0))
    return wallet - claimed


def spare_coin(
    balances: dict[str, Balance],
    positions: Sequence[Position],
    base_coin: str,
) -> Decimal:
    """Coin in the wallet that no open lot accounts for."""
    return max(coin_gap(balances, positions, base_coin), Decimal(0))


def book_shortfall(
    balances: dict[str, Balance],
    positions: Sequence[Position],
    base_coin: str,
) -> Decimal:
    """Coin the open lots claim that the wallet does not hold."""
    return max(-coin_gap(balances, positions, base_coin), Decimal(0))
