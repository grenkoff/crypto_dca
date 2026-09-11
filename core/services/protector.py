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
from core.strategy.rounding import (
    min_notional_price,
    next_tick_above,
    round_down_to_tick,
    round_up_to_tick,
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
        the minimum notional, so it is never left naked. The quantity is
        floored to the lot size — the exchange rejects anything finer, and
        a written-off remainder need not land on the grid. Returns the
        order id.
        """
        market_floor = next_tick_above(
            current_price, self.instrument.tick_size
        )
        qty = round_down_to_tick(
            position.remaining_qty, self.instrument.lot_size
        )
        if qty <= 0:
            raise ValueError(
                f"position {position.id} has less than one lot left"
            )
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

    async def _missing_coin(self) -> Decimal:
        """Coin the open lots claim that the wallet does not hold.

        The exchange refuses a sell for want of *free* coin, which says
        nothing about whether a lot's coin exists — it may be locked in
        another lot's resting sell. Only a book that claims more coin than
        the wallet holds proves something was really sold behind our back.
        """
        snapshot = await self.balances.snapshot()
        positions = await repository.open_positions()
        return book_shortfall(snapshot, positions, self.instrument.base_coin)

    async def _write_off_part(
        self,
        position: Position,
        missing: Decimal,
        current_price: Decimal | None,
    ) -> None:
        """Book the missing slice of a lot and re-protect what is left."""
        price = position.tp_price or position.entry_price
        booked = round_up_to_tick(missing, self.instrument.lot_size)
        left = await repository.write_off_missing(
            position=position,
            qty=booked,
            price=price,
            maker_fee=self.config.maker_fee,
        )
        log.warning(
            "position.partially_written_off",
            id=position.id,
            missing=str(booked),
            left=str(left),
        )
        if left <= 0 or current_price is None:
            return
        fresh = await repository.get_position(int(position.id))
        try:
            await self.reprotect(fresh, current_price)
        except Exception as exc:
            log.warning(
                "position.reprotect_after_write_off_failed",
                id=position.id,
                error=str(exc)[:120],
            )

    async def settle_phantom(
        self, position: Position, current_price: Decimal | None = None
    ) -> Decimal | None:
        """Reconcile a lot with a wallet that no longer holds its coin.

        Short by the whole lot: book it closed at its recorded TP price,
        the way its sale would have been booked. Short by less: write off
        only what is missing and re-protect the rest, so a small gap can
        never cost a whole lot. Returns the realized PnL of a full close,
        None when nothing was written off or the lot stays open.
        """
        missing = await self._missing_coin()
        remaining = position.remaining_qty
        if missing <= 0 or remaining <= 0:
            log.warning(
                "position.phantom_refused",
                id=position.id,
                qty=str(remaining),
            )
            return None
        if missing < remaining:
            await self._write_off_part(position, missing, current_price)
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
