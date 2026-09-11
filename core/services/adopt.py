"""Put coin the book does not cover back to work as grid lots.

Coin can end up outside every open position — a bag taken over by hand, or
a close that booked a sale which never reached the exchange. Sitting there
it earns nothing, so it is split into grid-sized lots at the current price
and given the same resting take-profit a fresh fill would get.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from decimal import Decimal

import structlog

from core.config.settings import grid_settings
from core.exchange.types import Instrument, Side
from core.services import repository
from core.services.balances import spare_coin
from core.services.events import EventBus
from core.services.order_common import link_id
from core.services.order_manager import OrderManager, compute_buy_qty
from core.strategy.pricing import compute_tp_price

log = structlog.get_logger()

_SETTLE_TICKS = 3
_MAX_LOTS_PER_SWEEP = 40
_PLACE_PAUSE_SECONDS = 0.05


@dataclass(frozen=True)
class AdoptionPlan:
    """Grid-sized lots to open over coin the book does not cover."""

    entry_price: Decimal
    lot_qty: Decimal
    lots: int
    leftover: Decimal

    @property
    def total_qty(self) -> Decimal:
        """Coin the plan puts back to work."""
        return self.lot_qty * self.lots


def plan_adoption(
    *,
    spare: Decimal,
    entry_price: Decimal,
    order_qty_quote: Decimal,
    instrument: Instrument,
) -> AdoptionPlan:
    """Split ``spare`` into lots the grid would have bought itself.

    Sizing them like a grid buy means the cash they release lands as whole
    grid levels; anything under one lot stays where it is.
    """
    empty = AdoptionPlan(entry_price, Decimal(0), 0, max(spare, Decimal(0)))
    if spare <= 0 or entry_price <= 0 or order_qty_quote <= 0:
        return empty
    lot_qty = compute_buy_qty(order_qty_quote, entry_price, instrument)
    if lot_qty <= 0 or lot_qty > spare:
        return empty
    lots = int(spare / lot_qty)
    return AdoptionPlan(
        entry_price=entry_price,
        lot_qty=lot_qty,
        lots=lots,
        leftover=spare - lot_qty * lots,
    )


class SpareAdopter:
    """Adopt coin the book does not cover, once it looks settled."""

    def __init__(self, om: OrderManager, bus: EventBus) -> None:
        self._om = om
        self._bus = bus
        self._settled_ticks = 0

    async def plan(self, price: Decimal) -> AdoptionPlan:
        """What adopting the wallet's uncovered coin would open."""
        balances = await self._om.balances.snapshot()
        positions = await repository.open_positions()
        spare = spare_coin(balances, positions, self._om.instrument.base_coin)
        return plan_adoption(
            spare=spare,
            entry_price=price,
            order_qty_quote=self._om.config.order_qty_quote,
            instrument=self._om.instrument,
        )

    async def sweep(self, price: Decimal) -> int:
        """Adopt uncovered coin that has been there for a few ticks.

        A fill that has not rested its take-profit yet looks exactly like
        loose coin, so the sweep waits for the same spare to survive
        several reconcile ticks before it books anything — and re-reads
        the balances at the last moment, because adopting coin a sell has
        just taken would claim it twice.
        """
        if not grid_settings().auto_adopt or await repository.is_paused():
            self._settled_ticks = 0
            return 0
        plan = await self.plan(price)
        if plan.lots <= 0:
            self._settled_ticks = 0
            return 0
        self._settled_ticks += 1
        if self._settled_ticks < _SETTLE_TICKS:
            log.info(
                "adopt.spare_seen",
                lots=plan.lots,
                qty=str(plan.total_qty),
                ticks=self._settled_ticks,
            )
            return 0
        self._settled_ticks = 0
        self._om.balances.invalidate()
        fresh = await self.plan(price)
        if fresh.lots <= 0:
            return 0
        return await self.commit(fresh)

    async def commit(
        self, plan: AdoptionPlan, *, limit: int = _MAX_LOTS_PER_SWEEP
    ) -> int:
        """Open the planned lots and rest a take-profit over each.

        ``limit`` caps one pass, so a large bag is adopted over several
        ticks instead of one burst of orders at the exchange.
        """
        opened = 0
        level = await repository.next_adopted_level()
        for _ in range(min(plan.lots, limit)):
            if not await self._adopt_one(plan, level):
                break
            opened += 1
            level += 1
            await asyncio.sleep(_PLACE_PAUSE_SECONDS)
        if opened:
            log.warning(
                "adopt.committed",
                lots=opened,
                qty=str(plan.lot_qty * opened),
                entry=str(plan.entry_price),
            )
            await self._bus.publish(
                "coin.adopted",
                {
                    "lots": str(opened),
                    "qty": str(plan.lot_qty * opened),
                    "entry": str(plan.entry_price),
                },
            )
        return opened

    async def _adopt_one(self, plan: AdoptionPlan, level: int) -> bool:
        """Rest a take-profit over one lot, then book it. False on failure."""
        instrument = self._om.instrument
        config = self._om.config
        tp_price = compute_tp_price(
            entry_price=plan.entry_price,
            qty=plan.lot_qty,
            fees_in=Decimal(0),
            geometry=self._om.geometry,
            min_profit_quote=config.min_profit_quote,
            maker_fee=config.maker_fee,
            tick_size=instrument.tick_size,
            min_order_amt=instrument.min_order_amt,
        )
        try:
            order_id = await self._om.client.place_limit(
                self._om.symbol,
                Side.SELL,
                plan.lot_qty,
                tp_price,
                order_link_id=link_id("grid-tp-adopt", level),
            )
        except Exception as exc:
            log.warning("adopt.place_failed", error=str(exc)[:120])
            return False
        await repository.adopt_position(
            level_index=level,
            entry_price=plan.entry_price,
            qty=plan.lot_qty,
            tp_price=tp_price,
            tp_order_id=order_id,
        )
        log.info(
            "adopt.lot_opened",
            level=level,
            qty=str(plan.lot_qty),
            tp=str(tp_price),
        )
        return True
