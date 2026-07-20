"""Database access for grid levels, positions, executions and status."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import ROUND_HALF_UP, Decimal
from typing import cast

from django.db.models import Max, Min

from core.trading.models import (
    BotStatus,
    ExecutionLog,
    GridLevel,
    LevelStatus,
    Position,
    PositionStatus,
)


def existing_active_levels() -> set[int]:
    """Level indices of awaiting-fill grid levels and open positions."""
    return set(
        GridLevel.objects.filter(status=LevelStatus.AWAITING_FILL).values_list(
            "level_index", flat=True
        )
    ) | set(
        Position.objects.filter(status=PositionStatus.OPEN).values_list(
            "level_index", flat=True
        )
    )


def naked_candidates(min_age_seconds: int) -> list[tuple[int, str]]:
    """(id, tp_order_id) for open positions older than the guard window."""
    cutoff = datetime.now(tz=UTC) - timedelta(seconds=min_age_seconds)
    return [
        (int(pid), str(oid))
        for pid, oid in Position.objects.filter(
            status=PositionStatus.OPEN, opened_at__lt=cutoff
        )
        .exclude(tp_order_id="")
        .values_list("id", "tp_order_id")
    ]


def get_open_position(pos_id: int) -> Position | None:
    """The open position with ``pos_id``, or None."""
    return Position.objects.filter(
        id=pos_id, status=PositionStatus.OPEN
    ).first()


def grid_state(
    step: Decimal,
) -> tuple[dict[Decimal, tuple[int, str]], set[Decimal]]:
    """Resting buys keyed by price and the set of held round prices."""
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


def idle_level(level_index: int) -> None:
    """Idle a grid level and clear its buy-order id."""
    GridLevel.objects.filter(level_index=level_index).update(
        status=LevelStatus.IDLE, current_buy_order_id=""
    )


def grid_params_changed(grid_step: Decimal, order_qty: Decimal) -> bool:
    """Whether grid geometry differs from what it was last built with.

    On the first run the applied values are unset, so we adopt the current
    geometry without forcing a rebuild.
    """
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


def reset_all_grid_levels() -> None:
    """Idle every awaiting-fill grid level."""
    GridLevel.objects.filter(status=LevelStatus.AWAITING_FILL).update(
        status=LevelStatus.IDLE, current_buy_order_id=""
    )


def record_applied_grid_params(grid_step: Decimal, order_qty: Decimal) -> None:
    """Record the grid geometry the buy grid was built with."""
    bot = BotStatus.load()
    bot.applied_grid_step = grid_step
    bot.applied_order_qty = order_qty
    bot.save(update_fields=["applied_grid_step", "applied_order_qty"])


def awaiting_buy_levels() -> list[tuple[int, str]]:
    """(level_index, order_id) for grid levels still expecting a buy fill."""
    return [
        (int(idx), oid)
        for idx, oid in GridLevel.objects.filter(
            status=LevelStatus.AWAITING_FILL
        )
        .exclude(current_buy_order_id="")
        .values_list("level_index", "current_buy_order_id")
    ]


def open_tp_order_ids() -> set[str]:
    """TP order ids of all open positions."""
    return set(
        Position.objects.filter(status=PositionStatus.OPEN)
        .exclude(tp_order_id="")
        .values_list("tp_order_id", flat=True)
    )


def exec_logged(exec_id: str) -> bool:
    """Whether an execution with ``exec_id`` is already recorded."""
    return ExecutionLog.objects.filter(exec_id=exec_id).exists()


def is_paused() -> bool:
    """Whether the bot is paused."""
    return bool(BotStatus.load().paused)


def highest_resting_buy() -> Decimal:
    """Highest resting buy price (nearest market), or 0 if none."""
    top = (
        GridLevel.objects.filter(status=LevelStatus.AWAITING_FILL)
        .exclude(current_buy_order_id="")
        .aggregate(m=Max("target_buy_price"))["m"]
    )
    return top if top is not None else Decimal(0)


def lowest_resting_tp() -> Decimal | None:
    """Lowest resting take-profit price (bottom of the wall), or None."""
    bottom = (
        Position.objects.filter(status=PositionStatus.OPEN)
        .exclude(tp_order_id="")
        .exclude(tp_price__isnull=True)
        .aggregate(m=Min("tp_price"))["m"]
    )
    return cast("Decimal | None", bottom)
