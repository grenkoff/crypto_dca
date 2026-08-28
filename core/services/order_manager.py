"""OrderManager: orchestrates exchange operations and corresponding DB state.

All exchange-touching methods are async (delegating to BybitClient); all
persistence goes through the async DAO (``core.services.repository``), which
keeps multi-statement writes inside a single transaction.
"""

from __future__ import annotations

from decimal import Decimal
from typing import cast

import structlog

from core.db.models import StrategyConfig
from core.exchange.bybit import BybitClient
from core.exchange.types import Execution as BybitExecution
from core.exchange.types import Instrument, Side
from core.services import repository
from core.services.balances import BalanceCache
from core.services.compensator import (
    CompensationOutcome,
    Compensator,
)
from core.services.events import EventBus
from core.services.order_common import link_id
from core.strategy.pricing import compute_tp_price
from core.strategy.rounding import (
    round_down_to_tick,
)
from core.strategy.types import GridMode

log = structlog.get_logger()


def fee_in_quote(execution: BybitExecution, quote_coin: str) -> Decimal:
    """Normalize exchange fee to quote currency (USDT)."""
    if execution.fee_coin == quote_coin:
        return execution.fee
    return execution.fee * execution.price


def compute_buy_qty(
    quote_amount: Decimal, price: Decimal, instrument: Instrument
) -> Decimal:
    """Base-coin qty for spending ~``quote_amount``, rounded to the lot size.

    Rounding down can drop the notional below ``min_order_amt`` at the
    boundary; bump one lot up so a min-sized order still clears.
    """
    qty = round_down_to_tick(quote_amount / price, instrument.lot_size)
    if qty * price < instrument.min_order_amt:
        qty += instrument.lot_size
    return qty


class OrderManager:
    """Orchestrates exchange operations and the matching DB state."""

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
        self.balances = BalanceCache(client)
        self._compensator = Compensator(
            balances=self.balances,
            client=client,
            instrument=instrument,
            config=config,
            bus=bus,
        )

    @property
    def symbol(self) -> str:
        """The configured trading symbol."""
        return str(self.config.symbol)

    @property
    def grid_mode(self) -> GridMode:
        """The configured grid mode (validated)."""
        mode = str(self.config.grid_mode)
        if mode not in ("absolute", "percent"):
            raise ValueError(f"unexpected grid_mode: {mode}")
        return cast(GridMode, mode)

    async def place_buy_at_level(
        self, level_index: int, price: Decimal
    ) -> str | None:
        """Place a grid buy at ``price`` and record the level."""
        qty = compute_buy_qty(
            self.config.order_qty_quote, price, self.instrument
        )
        if (
            qty < self.instrument.min_order_qty
            or qty * price < self.instrument.min_order_amt
        ):
            log.warning(
                "order.skipped_below_minimum",
                level=level_index,
                qty=str(qty),
                price=str(price),
            )
            return None
        order_id = await self.client.place_limit(
            self.symbol,
            Side.BUY,
            qty,
            price,
            order_link_id=link_id("grid-buy", level_index),
        )
        await repository.upsert_grid_level(level_index, price, order_id)
        log.info(
            "order.buy_placed",
            level=level_index,
            price=str(price),
            order_id=order_id,
        )
        await self.bus.publish(
            "order.placed",
            {
                "side": "buy",
                "level": level_index,
                "price": str(price),
                "order_id": order_id,
            },
        )
        return order_id

    async def handle_buy_fill(self, execution: BybitExecution) -> int | None:
        """Book a filled buy: open a position and rest its take-profit."""
        level = await repository.find_level_by_order_id(execution.order_id)
        if level is None:
            log.warning("buy_fill.no_level", order_id=execution.order_id)
            return None
        if execution.qty * execution.price < self.instrument.min_order_amt:
            log.warning(
                "buy_fill.too_small_left_free",
                level=level.level_index,
                qty=str(execution.qty),
                notional=str(execution.qty * execution.price),
            )
            return None
        fees_quote = fee_in_quote(execution, self.instrument.quote_coin)
        tp_price = compute_tp_price(
            entry_price=execution.price,
            qty=execution.qty,
            fees_in=fees_quote,
            tp_step=self.config.tp_step,
            min_profit_quote=self.config.min_profit_quote,
            maker_fee=self.config.maker_fee,
            tick_size=self.instrument.tick_size,
            min_order_amt=self.instrument.min_order_amt,
        )
        try:
            tp_order_id = await self.client.place_limit(
                self.symbol,
                Side.SELL,
                execution.qty,
                tp_price,
                order_link_id=link_id("grid-tp", level.level_index),
            )
        except Exception as exc:
            log.exception(
                "buy_fill.tp_failed_coin_free",
                level=level.level_index,
                qty=str(execution.qty),
                tp=str(tp_price),
                error=str(exc)[:100],
            )
            raise
        await repository.persist_buy_fill(
            execution=execution,
            level_index=level.level_index,
            fees_in=fees_quote,
            tp_price=tp_price,
            tp_order_id=tp_order_id,
        )
        log.info(
            "buy.filled",
            level=level.level_index,
            entry=str(execution.price),
            qty=str(execution.qty),
            tp=str(tp_price),
        )
        await self.bus.publish(
            "position.opened",
            {
                "level": level.level_index,
                "entry_price": str(execution.price),
                "tp_price": str(tp_price),
            },
        )
        return int(level.level_index)

    async def drain_pool(self, current_price: Decimal) -> None:
        """Spend the banked pool without waiting for a profitable close.

        The pool only ever grew on a close, so a lot it could already
        afford to retire sat idle until an unrelated trade happened to
        finish. Running the same spending on the reconcile cycle acts
        the moment the money is there.
        """
        if await repository.pending_credit() <= 0:
            return
        source = await repository.last_closed_position_id()
        if source is None:
            return
        moves = await self._compensator.drain_pool(current_price, source)
        if not moves:
            return
        await self.bus.publish(
            "pool.drained",
            {
                "compensations": moves,
                "pool": str(await repository.pending_credit()),
            },
        )

    async def handle_sell_fill(
        self, execution: BybitExecution, current_price: Decimal
    ) -> int | None:
        """Book a TP fill; close and compensate once fully filled."""
        position = await repository.find_open_position_by_tp_order(
            execution.order_id
        )
        if position is None:
            log.warning("sell_fill.no_position", order_id=execution.order_id)
            return None
        fees_out = fee_in_quote(execution, self.instrument.quote_coin)
        result = await repository.apply_sell_fill(
            position=position,
            execution=execution,
            fees_out=fees_out,
            lot_size=self.instrument.lot_size,
        )
        if not result.closed:
            log.info(
                "sell.partial",
                level=position.level_index,
                filled=str(result.filled_qty),
                remaining=str(result.remaining),
            )
            return None
        log.info(
            "sell.filled",
            level=position.level_index,
            realized=str(result.realized),
            qty=str(result.filled_qty),
        )
        outcome: CompensationOutcome | None = None
        if result.realized > 0:
            outcome = await self._compensator.apply(
                profit=result.realized,
                source_position_id=position.id,
                current_price=current_price,
            )
        await self.bus.publish(
            "position.closed",
            {
                "level": position.level_index,
                "realized": str(result.realized),
                "price": str(position.tp_price),
                "position_id": position.id,
                "compensation_credit": str(position.compensation_credit),
                "compensations": outcome.moves if outcome else [],
                "share": str(outcome.share) if outcome else "",
                "pool": str(outcome.pool_left) if outcome else "",
            },
        )
        return int(position.level_index)
