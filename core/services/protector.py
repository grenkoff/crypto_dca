"""Protector: keep open positions covered by a resting protective sell."""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal

import structlog
from asgiref.sync import sync_to_async
from django.db import transaction

from core.exchange.bybit import BybitClient
from core.exchange.types import Instrument, Side
from core.services.events import EventBus
from core.services.order_common import link_id, set_tp
from core.strategy.rounding import min_notional_price, next_tick_above
from core.trading.models import (
    GridLevel,
    LevelStatus,
    Position,
    PositionStatus,
    StrategyConfig,
)

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
    ) -> None:
        self.client = client
        self.instrument = instrument
        self.config = config
        self.bus = bus

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
        min_price = min_notional_price(
            self.instrument.min_order_amt,
            position.qty,
            self.instrument.tick_size,
        )
        price = max(position.tp_price or Decimal(0), market_floor, min_price)
        order_id = await self.client.place_limit(
            str(self.config.symbol),
            Side.SELL,
            position.qty,
            price,
            order_link_id=link_id("grid-tp-heal", position.level_index),
        )
        await sync_to_async(set_tp)(
            target=position, tp_price=price, tp_order_id=order_id
        )
        log.warning("position.reprotected", id=position.id, price=str(price))
        return order_id

    async def settle_phantom(self, position: Position) -> Decimal:
        """Close a phantom-open position whose coin is already gone.

        The TP filled under a superseded order id we can't trace, so book it
        at its recorded TP price to match the wallet. Returns realized PnL.
        """
        price = position.tp_price or position.entry_price
        realized = await sync_to_async(_close_at_price)(
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


def _close_at_price(
    *, position: Position, price: Decimal, maker_fee: Decimal
) -> Decimal:
    """Mark a position sold in full at ``price`` (maker) and free its
    grid level."""
    with transaction.atomic():
        sell_value = price * position.qty
        fees_out = sell_value * maker_fee
        realized = (
            sell_value
            - fees_out
            - position.entry_price * position.qty
            - position.fees_in
        )
        position.filled_qty = position.qty
        position.sell_value = sell_value
        position.fees_out = fees_out
        position.realized_pnl = realized
        position.status = PositionStatus.CLOSED
        position.closed_at = datetime.now(tz=UTC)
        position.save(
            update_fields=[
                "filled_qty",
                "sell_value",
                "fees_out",
                "realized_pnl",
                "status",
                "closed_at",
            ]
        )
        GridLevel.objects.filter(level_index=position.level_index).update(
            status=LevelStatus.IDLE, current_buy_order_id=""
        )
    return realized
