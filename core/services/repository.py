"""Database access for grid levels, positions, executions and status.

The public API is async so callers ``await`` it directly; the Django ORM
work runs in a thread via ``sync_to_async``. Phase 3 swaps the private
sync bodies for native async SQLAlchemy behind this same interface.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import ROUND_HALF_UP, Decimal
from typing import cast

from asgiref.sync import sync_to_async
from django.db.models import Max, Min

from core.trading.models import (
    BotStatus,
    ExecutionLog,
    GridLevel,
    LevelStatus,
    Position,
    PositionStatus,
)


def _existing_active_levels() -> set[int]:
    return set(
        GridLevel.objects.filter(status=LevelStatus.AWAITING_FILL).values_list(
            "level_index", flat=True
        )
    ) | set(
        Position.objects.filter(status=PositionStatus.OPEN).values_list(
            "level_index", flat=True
        )
    )


async def existing_active_levels() -> set[int]:
    """Level indices of awaiting-fill grid levels and open positions."""
    return await sync_to_async(_existing_active_levels)()


def _naked_candidates(min_age_seconds: int) -> list[tuple[int, str]]:
    cutoff = datetime.now(tz=UTC) - timedelta(seconds=min_age_seconds)
    return [
        (int(pid), str(oid))
        for pid, oid in Position.objects.filter(
            status=PositionStatus.OPEN, opened_at__lt=cutoff
        )
        .exclude(tp_order_id="")
        .values_list("id", "tp_order_id")
    ]


async def naked_candidates(min_age_seconds: int) -> list[tuple[int, str]]:
    """(id, tp_order_id) for open positions older than the guard window."""
    return await sync_to_async(_naked_candidates)(min_age_seconds)


def _get_open_position(pos_id: int) -> Position | None:
    return Position.objects.filter(
        id=pos_id, status=PositionStatus.OPEN
    ).first()


async def get_open_position(pos_id: int) -> Position | None:
    """The open position with ``pos_id``, or None."""
    return await sync_to_async(_get_open_position)(pos_id)


def _grid_state(
    step: Decimal,
) -> tuple[dict[Decimal, tuple[int, str]], set[Decimal]]:
    resting = {
        g.target_buy_price: (int(g.level_index), g.current_buy_order_id)
        for g in GridLevel.objects.filter(
            status=LevelStatus.AWAITING_FILL
        ).exclude(current_buy_order_id="")
    }
    held: set[Decimal] = set()
    for entry in Position.objects.filter(
        status=PositionStatus.OPEN
    ).values_list("entry_price", flat=True):
        k = int((entry / step).to_integral_value(rounding=ROUND_HALF_UP))
        held.add(Decimal(k) * step)
    return resting, held


async def grid_state(
    step: Decimal,
) -> tuple[dict[Decimal, tuple[int, str]], set[Decimal]]:
    """Resting buys keyed by price and the set of held round prices."""
    return await sync_to_async(_grid_state)(step)


def _idle_level(level_index: int) -> None:
    GridLevel.objects.filter(level_index=level_index).update(
        status=LevelStatus.IDLE, current_buy_order_id=""
    )


async def idle_level(level_index: int) -> None:
    """Idle a grid level and clear its buy-order id."""
    await sync_to_async(_idle_level)(level_index)


def _grid_params_changed(grid_step: Decimal, order_qty: Decimal) -> bool:
    bot = BotStatus.load()
    if bot.applied_grid_step is None or bot.applied_order_qty is None:
        bot.applied_grid_step = grid_step
        bot.applied_order_qty = order_qty
        bot.save(update_fields=["applied_grid_step", "applied_order_qty"])
        return False
    return (
        bot.applied_grid_step != grid_step
        or bot.applied_order_qty != order_qty
    )


async def grid_params_changed(grid_step: Decimal, order_qty: Decimal) -> bool:
    """Whether grid geometry differs from what it was last built with.

    On the first run the applied values are unset, so we adopt the current
    geometry without forcing a rebuild.
    """
    return await sync_to_async(_grid_params_changed)(grid_step, order_qty)


def _reset_all_grid_levels() -> None:
    GridLevel.objects.filter(status=LevelStatus.AWAITING_FILL).update(
        status=LevelStatus.IDLE, current_buy_order_id=""
    )


async def reset_all_grid_levels() -> None:
    """Idle every awaiting-fill grid level."""
    await sync_to_async(_reset_all_grid_levels)()


def _record_applied_grid_params(
    grid_step: Decimal, order_qty: Decimal
) -> None:
    bot = BotStatus.load()
    bot.applied_grid_step = grid_step
    bot.applied_order_qty = order_qty
    bot.save(update_fields=["applied_grid_step", "applied_order_qty"])


async def record_applied_grid_params(
    grid_step: Decimal, order_qty: Decimal
) -> None:
    """Record the grid geometry the buy grid was built with."""
    await sync_to_async(_record_applied_grid_params)(grid_step, order_qty)


def _awaiting_buy_levels() -> list[tuple[int, str]]:
    return [
        (int(idx), oid)
        for idx, oid in GridLevel.objects.filter(
            status=LevelStatus.AWAITING_FILL
        )
        .exclude(current_buy_order_id="")
        .values_list("level_index", "current_buy_order_id")
    ]


async def awaiting_buy_levels() -> list[tuple[int, str]]:
    """(level_index, order_id) for grid levels still expecting a buy fill."""
    return await sync_to_async(_awaiting_buy_levels)()


def _open_tp_order_ids() -> set[str]:
    return set(
        Position.objects.filter(status=PositionStatus.OPEN)
        .exclude(tp_order_id="")
        .values_list("tp_order_id", flat=True)
    )


async def open_tp_order_ids() -> set[str]:
    """TP order ids of all open positions."""
    return await sync_to_async(_open_tp_order_ids)()


def _exec_logged(exec_id: str) -> bool:
    return ExecutionLog.objects.filter(exec_id=exec_id).exists()


async def exec_logged(exec_id: str) -> bool:
    """Whether an execution with ``exec_id`` is already recorded."""
    return await sync_to_async(_exec_logged)(exec_id)


def _is_paused() -> bool:
    return bool(BotStatus.load().paused)


async def is_paused() -> bool:
    """Whether the bot is paused."""
    return await sync_to_async(_is_paused)()


def _highest_resting_buy() -> Decimal:
    top = (
        GridLevel.objects.filter(status=LevelStatus.AWAITING_FILL)
        .exclude(current_buy_order_id="")
        .aggregate(m=Max("target_buy_price"))["m"]
    )
    return top if top is not None else Decimal(0)


async def highest_resting_buy() -> Decimal:
    """Highest resting buy price (nearest market), or 0 if none."""
    return await sync_to_async(_highest_resting_buy)()


def _lowest_resting_tp() -> Decimal | None:
    bottom = (
        Position.objects.filter(status=PositionStatus.OPEN)
        .exclude(tp_order_id="")
        .exclude(tp_price__isnull=True)
        .aggregate(m=Min("tp_price"))["m"]
    )
    return cast("Decimal | None", bottom)


async def lowest_resting_tp() -> Decimal | None:
    """Lowest resting take-profit price (bottom of the wall), or None."""
    return await sync_to_async(_lowest_resting_tp)()
