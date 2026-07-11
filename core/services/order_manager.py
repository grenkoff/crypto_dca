"""OrderManager: orchestrates exchange operations and corresponding DB state.

All exchange-touching methods are async (delegating to BybitClient). Django
ORM access uses the async API where possible, with multi-statement atomic
blocks wrapped in `sync_to_async`.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal

import structlog
from asgiref.sync import sync_to_async
from django.db import transaction

from core.exchange.bybit import BybitClient
from core.exchange.types import Execution as BybitExecution
from core.exchange.types import Instrument, Side
from core.services.events import EventBus
from core.strategy.compensation import plan_compensation
from core.strategy.pricing import compute_tp_price
from core.strategy.rounding import round_down_to_tick, round_up_to_tick
from core.strategy.types import GridMode, OpenPosition
from core.trading.models import (
    CompensationLink,
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


def compute_buy_qty(quote_amount: Decimal, price: Decimal, instrument: Instrument) -> Decimal:
    """Base-coin qty for spending ~``quote_amount``, rounded to the lot size.

    Rounding down can drop the notional a hair below ``min_order_amt`` at the
    boundary (e.g. $5 target → $4.9998), which the exchange rejects. Bump one lot
    up in that case so a min-sized order still clears — the same nudge you'd do by
    hand.
    """
    qty = round_down_to_tick(quote_amount / price, instrument.lot_size)
    if qty * price < instrument.min_order_amt:
        qty += instrument.lot_size
    return qty


def _link_id(prefix: str, level: int) -> str:
    return f"{prefix}-{level}-{int(datetime.now(tz=UTC).timestamp() * 1000)}"


class OrderManager:
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

    @property
    def symbol(self) -> str:
        return str(self.config.symbol)

    @property
    def grid_mode(self) -> GridMode:
        mode = str(self.config.grid_mode)
        if mode not in ("absolute", "percent"):
            raise ValueError(f"unexpected grid_mode: {mode}")
        return mode  # type: ignore[return-value]

    async def place_buy_at_level(self, level_index: int, price: Decimal) -> str | None:
        qty = compute_buy_qty(self.config.order_qty_quote, price, self.instrument)
        if qty < self.instrument.min_order_qty or qty * price < self.instrument.min_order_amt:
            log.warning(
                "order.skipped_below_minimum",
                level=level_index,
                qty=str(qty),
                price=str(price),
            )
            return None
        order_id = await self.client.place_limit(
            self.symbol, Side.BUY, qty, price, order_link_id=_link_id("grid-buy", level_index)
        )
        await sync_to_async(_upsert_grid_level)(level_index, price, order_id)
        log.info("order.buy_placed", level=level_index, price=str(price), order_id=order_id)
        await self.bus.publish(
            "order.placed",
            {"side": "buy", "level": level_index, "price": str(price), "order_id": order_id},
        )
        return order_id

    async def handle_buy_fill(self, execution: BybitExecution) -> int | None:
        level = await sync_to_async(_find_level_by_order_id)(execution.order_id)
        if level is None:
            log.warning("buy_fill.no_level", order_id=execution.order_id)
            return None
        # A fill too small to carry a valid sell (notional below the exchange
        # minimum) is left as free coin — placing a min-notional TP here would sit
        # absurdly far from the market. `readopt_free_balance` merges such dust into
        # a proper position; the stale grid level is idled by the prune pass.
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
                order_link_id=_link_id("grid-tp", level.level_index),
            )
        except Exception as exc:
            # TP could not be placed — the bought coin is now unmanaged. Log loudly
            # (recover with `readopt_free_balance`) rather than silently swallow.
            log.error(
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
        position = await sync_to_async(_find_open_position_by_tp_order)(execution.order_id)
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
            # Partial TP fill: keep the position open, accumulate, do nothing else
            # until the remainder fills. (Prevents phantom loss + orphaned base coin.)
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
            },
        )
        if result.realized > 0:
            await self._apply_compensation(
                profit=result.realized, source_position_id=position.id, current_price=current_price
            )
        return int(position.level_index)

    async def _apply_compensation(
        self, *, profit: Decimal, source_position_id: int, current_price: Decimal
    ) -> None:
        open_positions = await sync_to_async(_open_positions_view)()
        decision = plan_compensation(
            open_positions=open_positions,
            profit_from_other=profit,
            maker_fee=self.config.maker_fee,
            current_price=current_price,
            tick_size=self.instrument.tick_size,
            tp_step=self.config.tp_step,
            min_order_amt=self.instrument.min_order_amt,
        )
        if decision is None:
            return
        target = await Position.objects.aget(id=decision.target_position_id)
        if not target.tp_order_id:
            log.warning("compensation.target_has_no_tp", id=target.id)
            return
        # Guard BEFORE cancelling: a re-priced sell below the exchange minimum would
        # be rejected, leaving the position with no protective order. Skip instead.
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
            await self.client.cancel_order(self.symbol, target.tp_order_id)
        except Exception as exc:
            log.warning("compensation.cancel_failed", id=target.id, error=str(exc))
            return
        try:
            new_tp_order_id = await self.client.place_limit(
                self.symbol,
                Side.SELL,
                target.qty,
                decision.new_tp_price,
                order_link_id=_link_id("grid-tp-comp", target.level_index),
            )
        except Exception as exc:
            # Cancelled but could not re-place: restore protection so it is never naked.
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

    async def reprotect(self, position: Position, current_price: Decimal) -> str:
        """Re-place a protective take-profit for a position whose sell order vanished.

        Priced at the higher of the original TP, one tick above the market (so it
        rests as a maker and never crosses), and the exchange minimum notional — so a
        position recovered from a lost/cancelled TP is never left naked. Returns the
        new order id.
        """
        market_floor = round_up_to_tick(
            current_price + self.instrument.tick_size, self.instrument.tick_size
        )
        min_price = round_up_to_tick(
            self.instrument.min_order_amt / position.qty, self.instrument.tick_size
        )
        price = max(position.tp_price or Decimal(0), market_floor, min_price)
        order_id = await self.client.place_limit(
            self.symbol,
            Side.SELL,
            position.qty,
            price,
            order_link_id=_link_id("grid-tp-heal", position.level_index),
        )
        await sync_to_async(_set_tp)(target=position, tp_price=price, tp_order_id=order_id)
        log.warning("position.reprotected", id=position.id, price=str(price))
        return order_id

    async def settle_phantom(self, position: Position) -> Decimal:
        """Close a phantom-open position whose coin is already gone — its TP filled
        under a superseded order id we can no longer trace (so we can't reprotect: the
        base coin isn't there). Book it at its recorded TP price (the maker price it
        would have filled at) so the DB matches the wallet. Returns realized PnL.
        """
        price = position.tp_price or position.entry_price
        realized = await sync_to_async(_close_at_price)(
            position=position, price=price, maker_fee=self.config.maker_fee
        )
        log.warning(
            "position.settled_phantom", id=position.id, price=str(price), realized=str(realized)
        )
        await self.bus.publish(
            "position.closed",
            {
                "level": position.level_index,
                "realized": str(realized),
                "price": str(price),
                "position_id": position.id,
            },
        )
        return realized

    async def _restore_protection(self, target: Position, place_error: Exception) -> None:
        """Re-place a protective sell after a failed compensation placement.

        Priced at the higher of the old TP and the minimum notional price so it
        always clears the exchange minimum — the position is never left naked.
        """
        min_price = round_up_to_tick(
            self.instrument.min_order_amt / target.qty, self.instrument.tick_size
        )
        price = max(target.tp_price or Decimal(0), min_price)
        try:
            order_id = await self.client.place_limit(
                self.symbol,
                Side.SELL,
                target.qty,
                price,
                order_link_id=_link_id("grid-tp-restore", target.level_index),
            )
        except Exception as restore_error:
            log.error(
                "compensation.restore_failed",
                id=target.id,
                place_error=str(place_error),
                restore_error=str(restore_error),
            )
            return
        await sync_to_async(_set_tp)(target=target, tp_price=price, tp_order_id=order_id)
        log.error(
            "compensation.restored_after_place_failure",
            id=target.id,
            price=str(price),
            error=str(place_error),
        )


# --- sync helpers (invoked via sync_to_async from async methods) -----------


def _set_tp(*, target: Position, tp_price: Decimal, tp_order_id: str) -> None:
    target.tp_price = tp_price
    target.tp_order_id = tp_order_id
    target.save(update_fields=["tp_price", "tp_order_id"])


def _close_at_price(*, position: Position, price: Decimal, maker_fee: Decimal) -> Decimal:
    """Mark a position sold in full at ``price`` (maker) and free its grid level."""
    with transaction.atomic():
        sell_value = price * position.qty
        fees_out = sell_value * maker_fee
        realized = sell_value - fees_out - position.entry_price * position.qty - position.fees_in
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


def _upsert_grid_level(level_index: int, price: Decimal, order_id: str) -> None:
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
    return Position.objects.filter(tp_order_id=order_id, status=PositionStatus.OPEN).first()


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
    realized PnL is then computed from the *actual* accumulated proceeds and the
    full entry cost. Idempotent on ``exec_id`` (WS may redeliver).
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


def _open_positions_view() -> list[OpenPosition]:
    return [
        OpenPosition(
            id=int(p.id),
            entry_price=p.entry_price,
            qty=p.qty,
            fees_in=p.fees_in,
            current_tp_price=p.tp_price if p.tp_price is not None else Decimal(0),
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
