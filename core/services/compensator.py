"""Compensator: compact the TP grid using banked profit (a credit pool)."""

from __future__ import annotations

from decimal import Decimal

import structlog
from asgiref.sync import sync_to_async
from django.db import transaction

from core.exchange.bybit import BybitClient
from core.exchange.types import Instrument, Side
from core.services import repository
from core.services.events import EventBus
from core.services.order_common import link_id, set_tp
from core.strategy.compensation import plan_compensation
from core.strategy.rounding import min_notional_price
from core.strategy.types import (
    CompensationContext,
    CompensationDecision,
    OpenPosition,
)
from core.trading.models import (
    BotStatus,
    CompensationLink,
    Position,
    PositionStatus,
    StrategyConfig,
)

log = structlog.get_logger()


class Compensator:
    """Pull one TP down onto its empty grid slot, funded by banked profit."""

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
        """Bank ``profit`` and compact one TP if the pool now funds it."""
        open_positions = await sync_to_async(_open_positions_view)()
        pending = await sync_to_async(_load_pending)()
        pool = pending + profit
        nearest_buy = await sync_to_async(repository.highest_resting_buy)()
        ctx = CompensationContext(
            pool=pool,
            maker_fee=self.config.maker_fee,
            current_price=current_price,
            tick_size=self.instrument.tick_size,
            grid_step=self.config.grid_step,
            tp_step=self.config.tp_step,
            nearest_buy_price=nearest_buy,
            min_order_amt=self.instrument.min_order_amt,
        )
        decision = plan_compensation(open_positions, ctx)
        if decision is not None and await self._execute(
            decision, pool, source_position_id
        ):
            return
        await sync_to_async(_bank_pending)(pool)

    async def _execute(
        self,
        decision: CompensationDecision,
        pool: Decimal,
        source_position_id: int,
    ) -> bool:
        """Do the exchange move and persist it; return True if applied."""
        symbol = str(self.config.symbol)
        target = await Position.objects.aget(id=decision.target_position_id)
        if not target.tp_order_id:
            log.warning("compensation.target_has_no_tp", id=target.id)
            return False
        if decision.new_tp_price * target.qty < self.instrument.min_order_amt:
            log.warning(
                "compensation.skip_below_min_notional",
                id=target.id,
                new_tp=str(decision.new_tp_price),
            )
            return False
        try:
            await self.client.cancel_order(symbol, target.tp_order_id)
        except Exception as exc:
            log.warning(
                "compensation.cancel_failed", id=target.id, error=str(exc)
            )
            return False
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
            return False
        await sync_to_async(_record_compensation)(
            target=target,
            new_tp_price=decision.new_tp_price,
            new_tp_order_id=new_tp_order_id,
            new_credit=decision.new_credit,
            credit_drawn=decision.credit_drawn,
            source_position_id=source_position_id,
            new_pending=pool - decision.credit_drawn,
        )
        log.info(
            "compensation.applied",
            id=target.id,
            new_tp=str(decision.new_tp_price),
            drawn=str(decision.credit_drawn),
        )
        await self.bus.publish(
            "compensation.applied",
            {
                "target_position": target.id,
                "source_position": source_position_id,
                "new_tp": str(decision.new_tp_price),
                "profit": str(decision.credit_drawn),
            },
        )
        return True

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
            log.exception(
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


def _load_pending() -> Decimal:
    return BotStatus.load().pending_credit


def _bank_pending(value: Decimal) -> None:
    status = BotStatus.load()
    status.pending_credit = value
    status.save(update_fields=["pending_credit"])


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
    credit_drawn: Decimal,
    source_position_id: int,
    new_pending: Decimal,
) -> None:
    with transaction.atomic():
        target.tp_price = new_tp_price
        target.tp_order_id = new_tp_order_id
        target.compensation_credit = new_credit
        target.save()
        CompensationLink.objects.create(
            profitable_position_id=source_position_id,
            compensated_position_id=target.id,
            profit_applied=credit_drawn,
            new_tp_price=new_tp_price,
        )
        status = BotStatus.load()
        status.pending_credit = new_pending
        status.save(update_fields=["pending_credit"])
