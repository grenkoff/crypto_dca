"""Grid maintenance: lay the buy band, prune out-of-band, rebuild."""

from __future__ import annotations

import asyncio
from decimal import Decimal

import structlog

from core.exchange.types import Side
from core.services import repository
from core.services.events import EventBus
from core.services.order_manager import OrderManager
from core.strategy.grid import (
    buys_to_prune,
    fundable_targets,
    held_rungs,
    resting_buy_levels,
)

log = structlog.get_logger()


class GridMaintainer:
    """Maintain a contiguous band of resting buy orders below market."""

    def __init__(self, om: OrderManager, bus: EventBus) -> None:
        self._om = om
        self._bus = bus
        self._lock = asyncio.Lock()

    async def ensure(self, price: Decimal) -> None:
        """Reconcile the resting buy grid against the current price."""
        async with self._lock:
            await self._ensure_impl(price)

    async def _ensure_impl(self, price: Decimal) -> None:
        """Maintain a contiguous band of buy orders below the price.

        The band is the ``max_open_orders`` highest lattice rungs below
        market; gaps fill, out-of-band buys prune, held rungs skip.
        """
        if await repository.is_paused():
            return
        cfg = self._om.config
        per_order: Decimal = cfg.order_qty_quote
        if price <= 0 or per_order <= 0:
            return

        balances = await self._om.balances.snapshot()
        quote = balances.get(self._om.instrument.quote_coin)
        free = quote.free if quote is not None else Decimal(0)
        locked = quote.locked if quote is not None else Decimal(0)
        n = min(int((free + locked) / per_order), int(cfg.max_open_orders))

        lattice = self._om.geometry.lattice
        resting, entries = await repository.grid_state()
        held = held_rungs(entries, lattice)
        ceiling = await self._buy_ceiling()
        targets = resting_buy_levels(price, lattice, n, held, ceiling=ceiling)
        target_prices = {p for _, p in targets}
        prune = set(
            buys_to_prune(resting.keys(), target_prices, ceiling=ceiling)
        )
        freed = await self._prune_out_of_band(resting, prune)
        budget = free + freed * per_order
        await self._place_missing(targets, set(resting) | held, budget)

    async def _buy_ceiling(self) -> Decimal | None:
        """Highest price a resting buy may take, or None if unconstrained.

        Keeps the buy band two slots below the bottom of the take-profit
        wall, so a rising grid never crowds a resting TP.
        """
        lowest_tp = await repository.lowest_resting_tp()
        if lowest_tp is None:
            return None
        return self._om.geometry.buy_ceiling(lowest_tp)

    async def _prune_out_of_band(
        self,
        resting: dict[Decimal, tuple[int, str]],
        prune: set[Decimal],
    ) -> int:
        """Cancel and idle out-of-band resting buys; return the count freed."""
        freed = 0
        for p, (k, order_id) in list(resting.items()):
            if p not in prune:
                continue
            cancelled = False
            try:
                await self._om.client.cancel_order(self._om.symbol, order_id)
                cancelled = True
            except Exception as exc:
                if (
                    "170213" not in str(exc)
                    and "does not exist" not in str(exc).lower()
                ):
                    log.warning(
                        "grid.prune_failed", price=str(p), error=str(exc)
                    )
                    continue
            await repository.idle_level(k)
            log.info("grid.pruned", price=str(p))
            freed += 1
            if cancelled:
                await self._bus.publish("order.cancelled", {"price": str(p)})
        return freed

    async def _place_missing(
        self,
        targets: list[tuple[int, Decimal]],
        covered: set[Decimal],
        budget: Decimal,
    ) -> None:
        """Place a buy at each fundable target not already covered.

        Only as many orders as the free ``budget`` can fund are attempted, so
        a fully-deployed grid stops firing doomed insufficient-balance calls.
        """
        per_order = self._om.config.order_qty_quote
        for k, p in fundable_targets(targets, covered, budget, per_order):
            try:
                await self._om.place_buy_at_level(k, p)
            except Exception as exc:
                log.warning(
                    "grid.place_skipped", price=str(p), error=str(exc)[:100]
                )

    async def rebuild_on_param_change(self) -> None:
        """Rebuild the buy grid when grid geometry changed.

        Live orders are never resized, so on a geometry change we cancel
        every resting buy (TP sells untouched) and idle their levels.
        """
        cfg = self._om.config
        if not await repository.grid_params_changed(
            cfg.grid_step, cfg.order_qty_quote
        ):
            return
        log.warning(
            "grid.params_changed_rebuild",
            grid_step=str(cfg.grid_step),
            order_qty=str(cfg.order_qty_quote),
        )
        for order in await self._om.client.get_open_orders(self._om.symbol):
            if order.side != Side.BUY:
                continue
            try:
                await self._om.client.cancel_order(
                    self._om.symbol, order.order_id
                )
            except Exception as exc:
                log.warning(
                    "grid.rebuild_cancel_failed",
                    order_id=order.order_id,
                    error=str(exc),
                )
        await repository.reset_all_grid_levels()
        await repository.record_applied_grid_params(
            cfg.grid_step, cfg.order_qty_quote
        )
