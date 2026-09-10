"""Protector: keep open positions covered by a resting protective sell."""

from __future__ import annotations

from decimal import Decimal

import structlog

from core.db.models import Position, StrategyConfig
from core.exchange.bybit import BybitClient
from core.exchange.types import Instrument, Side
from core.services import repository
from core.services.balances import BalanceCache, book_shortfall
from core.services.events import EventBus
from core.services.order_common import link_id
from core.strategy.rounding import min_notional_price, next_tick_above

log = structlog.get_logger()


class Protector:
    """Re-place or settle protective sells for otherwise-naked positions."""

    def __init__(
        self,
        *,
        client: BybitClient,
        instrument: Instrument,
        config: StrategyConfig,
        bus: EventBus,
        balances: BalanceCache,
    ) -> None:
        self.client = client
        self.instrument = instrument
        self.config = config
        self.bus = bus
        self.balances = balances

    async def reprotect(
        self, position: Position, current_price: Decimal
    ) -> str:
        """Re-place a protective take-profit for a position with no sell.

        Priced at the higher of the original TP, one tick above market, and
        the minimum notional, so it is never left naked. Returns the order id.
        """
        market_floor = next_tick_above(
            current_price, self.instrument.tick_size
        )
        qty = position.remaining_qty
        min_price = min_notional_price(
            self.instrument.min_order_amt,
            qty,
            self.instrument.tick_size,
        )
        price = max(position.tp_price or Decimal(0), market_floor, min_price)
        order_id = await self.client.place_limit(
            str(self.config.symbol),
            Side.SELL,
            qty,
            price,
            order_link_id=link_id("grid-tp-heal", position.level_index),
        )
        await repository.set_tp(
            target=position, tp_price=price, tp_order_id=order_id
        )
        log.warning("position.reprotected", id=position.id, price=str(price))
        return order_id

    async def _coin_is_gone(self, position: Position) -> bool:
        """Whether the wallet is short by at least this lot's remainder.

        The exchange refuses a sell for want of *free* coin, which says
        nothing about whether this lot's coin exists — it may be locked in
        another lot's resting sell. Only a book that claims more coin than
        the wallet holds proves something was really sold behind our back.
        """
        snapshot = await self.balances.snapshot()
        positions = await repository.open_positions()
        missing = book_shortfall(
            snapshot, positions, self.instrument.base_coin
        )
        return missing >= position.remaining_qty > 0

    async def settle_phantom(self, position: Position) -> Decimal | None:
        """Close a position whose coin the wallet no longer holds.

        The TP filled under a superseded order id we can't trace, so book
        it at its recorded TP price to match the wallet. Returns the
        realized PnL, or None when the coin turns out to still be there.
        """
        if not await self._coin_is_gone(position):
            log.warning(
                "position.phantom_refused",
                id=position.id,
                qty=str(position.remaining_qty),
            )
            return None
        price = position.tp_price or position.entry_price
        realized = await repository.close_at_price(
            position=position, price=price, maker_fee=self.config.maker_fee
        )
        log.warning(
            "position.settled_phantom",
            id=position.id,
            price=str(price),
            realized=str(realized),
        )
        await self.bus.publish(
            "position.closed",
            {
                "level": position.level_index,
                "realized": str(realized),
                "price": str(price),
                "position_id": position.id,
                "compensation_credit": str(position.compensation_credit),
            },
        )
        return realized
