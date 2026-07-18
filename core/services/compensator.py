"""Compensator: lower a position's take-profit using another's profit."""

from __future__ import annotations

from decimal import Decimal

import structlog
from asgiref.sync import sync_to_async
from django.db import transaction

from core.exchange.bybit import BybitClient
from core.exchange.types import Instrument, Side
from core.services.events import EventBus
from core.services.order_common import link_id, set_tp
from core.strategy.compensation import plan_compensation
from core.strategy.rounding import min_notional_price
from core.strategy.types import OpenPosition
from core.trading.models import (
    CompensationLink,
    Position,
    PositionStatus,
    StrategyConfig,
)

log = structlog.get_logger()


class Compensator:
    """Apply pairwise loss compensation by lowering a target's TP."""

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

    async def apply(
        self,
        *,
        profit: Decimal,
        source_position_id: int,
        current_price: Decimal,
    ) -> None:
        """Pull the best target position's TP down by ``profit``."""
        symbol = str(self.config.symbol)
        open_positions = await sync_to_async(_open_positions_view)()
        decision = plan_compensation(
            open_positions=open_positions,
            profit_from_other=profit,
            maker_fee=self.config.maker_fee,
            current_price=current_price,
            tick_size=self.instrument.tick_size,
            step=self.config.grid_step,
            min_order_amt=self.instrument.min_order_amt,
        )
        if decision is None:
            return
        target = await Position.objects.aget(id=decision.target_position_id)
        if not target.tp_order_id:
            log.warning("compensation.target_has_no_tp", id=target.id)
            return
        new_notional = decision.new_tp_price * target.qty
        if new_notional < self.instrument.min_order_amt:
            log.warning(
                "compensation.skip_below_min_notional",
                id=target.id,
                new_tp=str(decision.new_tp_price),
                notional=str(new_notional),
            )
            return
        try:
            await self.client.cancel_order(symbol, target.tp_order_id)
        except Exception as exc:
            log.warning(
                "compensation.cancel_failed", id=target.id, error=str(exc)
            )
            return
        try:
            new_tp_order_id = await self.client.place_limit(
                symbol,
                Side.SELL,
                target.qty,
                decision.new_tp_price,
                order_link_id=link_id("grid-tp-comp", target.level_index),
            )
        except Exception as exc:
            await self._restore_protection(target, exc)
            return
        await sync_to_async(_record_compensation)(
            target=target,
            new_tp_price=decision.new_tp_price,
            new_tp_order_id=new_tp_order_id,
            new_credit=decision.new_credit,
            profit_applied=profit,
            source_position_id=source_position_id,
        )
        log.info(
            "compensation.applied",
            id=target.id,
            new_tp=str(decision.new_tp_price),
            profit=str(profit),
        )
        await self.bus.publish(
            "compensation.applied",
            {
                "target_position": target.id,
                "source_position": source_position_id,
                "new_tp": str(decision.new_tp_price),
                "profit": str(profit),
            },
        )

    async def _restore_protection(
        self, target: Position, place_error: Exception
    ) -> None:
        """Re-place a protective sell after a failed compensation placement.

        Priced at the higher of the old TP and the minimum notional price so
        it always clears the exchange minimum — never left naked.
        """
        min_price = min_notional_price(
            self.instrument.min_order_amt,
            target.qty,
            self.instrument.tick_size,
        )
        price = max(target.tp_price or Decimal(0), min_price)
        try:
            order_id = await self.client.place_limit(
                str(self.config.symbol),
                Side.SELL,
                target.qty,
                price,
                order_link_id=link_id("grid-tp-restore", target.level_index),
            )
        except Exception as restore_error:
            log.error(
                "compensation.restore_failed",
                id=target.id,
                place_error=str(place_error),
                restore_error=str(restore_error),
            )
            return
        await sync_to_async(set_tp)(
            target=target, tp_price=price, tp_order_id=order_id
        )
        log.error(
            "compensation.restored_after_place_failure",
            id=target.id,
            price=str(price),
            error=str(place_error),
        )


def _open_positions_view() -> list[OpenPosition]:
    return [
        OpenPosition(
            id=int(p.id),
            entry_price=p.entry_price,
            qty=p.qty,
            fees_in=p.fees_in,
            current_tp_price=p.tp_price
            if p.tp_price is not None
            else Decimal(0),
            compensation_credit=p.compensation_credit,
        )
        for p in Position.objects.filter(status=PositionStatus.OPEN)
    ]


def _record_compensation(
    *,
    target: Position,
    new_tp_price: Decimal,
    new_tp_order_id: str,
    new_credit: Decimal,
    profit_applied: Decimal,
    source_position_id: int,
) -> None:
    with transaction.atomic():
        target.tp_price = new_tp_price
        target.tp_order_id = new_tp_order_id
        target.compensation_credit = new_credit
        target.save()
        CompensationLink.objects.create(
            profitable_position_id=source_position_id,
            compensated_position_id=target.id,
            profit_applied=profit_applied,
            new_tp_price=new_tp_price,
        )
