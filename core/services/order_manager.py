"""OrderManager: orchestrates exchange operations and corresponding DB state.

All exchange-touching methods are async (delegating to BybitClient). Django ORM
access uses the async API where possible, with multi-statement atomic blocks
wrapped in `sync_to_async`.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from typing import cast

import structlog
from asgiref.sync import sync_to_async
from django.db import transaction

from core.exchange.bybit import BybitClient
from core.exchange.types import Execution as BybitExecution
from core.exchange.types import Instrument, Side
from core.services.compensator import Compensator
from core.services.events import EventBus
from core.services.order_common import link_id
from core.strategy.pricing import compute_tp_price
from core.strategy.rounding import (
    round_down_to_tick,
)
from core.strategy.types import GridMode
from core.trading.models import (
    ExecutionLog,
    GridLevel,
    LevelStatus,
    OrderSide,
    Position,
    PositionStatus,
    StrategyConfig,
)

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
        self._compensator = Compensator(
            client=client, instrument=instrument, config=config, bus=bus
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
        await sync_to_async(_upsert_grid_level)(level_index, price, order_id)
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
        level = await sync_to_async(_find_level_by_order_id)(
            execution.order_id
        )
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
        await sync_to_async(_persist_buy_fill)(
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

    async def handle_sell_fill(
        self, execution: BybitExecution, current_price: Decimal
    ) -> int | None:
        """Book a TP fill; close and compensate once fully filled."""
        position = await sync_to_async(_find_open_position_by_tp_order)(
            execution.order_id
        )
        if position is None:
            log.warning("sell_fill.no_position", order_id=execution.order_id)
            return None
        fees_out = fee_in_quote(execution, self.instrument.quote_coin)
        result = await sync_to_async(_apply_sell_fill)(
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
        await self.bus.publish(
            "position.closed",
            {
                "level": position.level_index,
                "realized": str(result.realized),
                "price": str(position.tp_price),
                "position_id": position.id,
                "compensation_credit": str(position.compensation_credit),
            },
        )
        if result.realized > 0:
            await self._compensator.apply(
                profit=result.realized,
                source_position_id=position.id,
                current_price=current_price,
            )
        return int(position.level_index)


def _upsert_grid_level(
    level_index: int, price: Decimal, order_id: str
) -> None:
    GridLevel.objects.update_or_create(
        level_index=level_index,
        defaults={
            "target_buy_price": price,
            "current_buy_order_id": order_id,
            "status": LevelStatus.AWAITING_FILL,
        },
    )


def _find_level_by_order_id(order_id: str) -> GridLevel | None:
    return GridLevel.objects.filter(current_buy_order_id=order_id).first()


def _find_open_position_by_tp_order(order_id: str) -> Position | None:
    return Position.objects.filter(
        tp_order_id=order_id, status=PositionStatus.OPEN
    ).first()


def _log_execution(execution: BybitExecution) -> None:
    ExecutionLog.objects.update_or_create(
        exec_id=execution.exec_id,
        defaults={
            "order_id": execution.order_id,
            "symbol": execution.symbol,
            "side": OrderSide(execution.side.value),
            "price": execution.price,
            "qty": execution.qty,
            "fee": execution.fee,
            "fee_coin": execution.fee_coin,
            "executed_at": execution.executed_at,
        },
    )


def _persist_buy_fill(
    *,
    execution: BybitExecution,
    level_index: int,
    fees_in: Decimal,
    tp_price: Decimal,
    tp_order_id: str,
) -> None:
    with transaction.atomic():
        Position.objects.create(
            level_index=level_index,
            entry_price=execution.price,
            qty=execution.qty,
            fees_in=fees_in,
            tp_order_id=tp_order_id,
            tp_price=tp_price,
            status=PositionStatus.OPEN,
            opened_at=execution.executed_at,
        )
        GridLevel.objects.filter(level_index=level_index).update(
            status=LevelStatus.FILLED, current_buy_order_id=""
        )
        _log_execution(execution)


@dataclass
class SellFillResult:
    """Outcome of applying a sell fill to a position."""

    closed: bool
    realized: Decimal
    filled_qty: Decimal
    remaining: Decimal


def _apply_sell_fill(
    *,
    position: Position,
    execution: BybitExecution,
    fees_out: Decimal,
    lot_size: Decimal,
) -> SellFillResult:
    """Accumulate one (possibly partial) TP fill onto the position.

    The position closes only once the unsold remainder drops below one lot;
    realized PnL is then computed from the *actual* accumulated proceeds and
    the full entry cost. Idempotent on ``exec_id`` (WS may redeliver).
    """
    with transaction.atomic():
        if ExecutionLog.objects.filter(exec_id=execution.exec_id).exists():
            remaining = max(position.qty - position.filled_qty, Decimal(0))
            return SellFillResult(
                closed=position.status == PositionStatus.CLOSED,
                realized=position.realized_pnl,
                filled_qty=position.filled_qty,
                remaining=remaining,
            )
        position.filled_qty += execution.qty
        position.sell_value += execution.price * execution.qty
        position.fees_out += fees_out
        remaining = position.qty - position.filled_qty
        closed = remaining < lot_size
        if closed:
            realized = (
                position.sell_value
                - position.fees_out
                - position.entry_price * position.qty
                - position.fees_in
            )
            position.realized_pnl = realized
            position.status = PositionStatus.CLOSED
            position.closed_at = execution.executed_at
        else:
            realized = Decimal(0)
        position.save()
        if closed:
            GridLevel.objects.filter(level_index=position.level_index).update(
                status=LevelStatus.IDLE, current_buy_order_id=""
            )
        _log_execution(execution)
    return SellFillResult(
        closed=closed,
        realized=realized,
        filled_qty=position.filled_qty,
        remaining=max(remaining, Decimal(0)),
    )
