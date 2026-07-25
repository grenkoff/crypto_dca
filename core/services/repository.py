"""Database access for grid levels, positions, executions and status.

The public API is async so callers ``await`` it directly; the Django ORM
work runs in a thread via ``sync_to_async``. Phase 3 swaps the private
sync bodies for native async SQLAlchemy behind this same interface.
"""

from __future__ import annotations

from datetime import UTC, date, datetime, time, timedelta
from decimal import ROUND_HALF_UP, Decimal
from typing import Any, cast

from asgiref.sync import sync_to_async
from django.db import transaction
from django.db.models import F, Max, Min, QuerySet, Sum

from core.exchange.types import Execution as BybitExecution
from core.services.order_common import SellFillResult
from core.trading.models import (
    BotStatus,
    CompensationLink,
    ExecutionLog,
    GridLevel,
    LevelStatus,
    NotificationSettings,
    OrderSide,
    Position,
    PositionStatus,
    StrategyConfig,
    TelegramUser,
)


def _sum(qs: QuerySet[Position], field: str = "realized_pnl") -> Decimal:
    """Sum ``field`` over the queryset, treating an empty result as 0."""
    return qs.aggregate(s=Sum(field))["s"] or Decimal(0)


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


def _status_data() -> tuple[bool, int, datetime | None, datetime | None]:
    bot = BotStatus.load()
    open_count = Position.objects.filter(status=PositionStatus.OPEN).count()
    return bot.paused, open_count, bot.started_at, bot.last_heartbeat


async def status_data() -> tuple[bool, int, datetime | None, datetime | None]:
    """(paused, open_position_count, started_at, last_heartbeat)."""
    return await sync_to_async(_status_data)()


def _realized_pnl_since(cutoff: datetime | None) -> Decimal:
    qs = Position.objects.filter(status=PositionStatus.CLOSED)
    if cutoff is not None:
        qs = qs.filter(closed_at__gte=cutoff)
    return _sum(qs)


async def realized_pnl_since(cutoff: datetime | None) -> Decimal:
    """Realized PnL of closed positions since ``cutoff`` (None = all time)."""
    return await sync_to_async(_realized_pnl_since)(cutoff)


def _locked_by_day(dates: list[date]) -> list[Decimal]:
    rows = list(
        Position.objects.values_list(
            "opened_at", "closed_at", "entry_price", "qty", "fees_in"
        )
    )
    out: list[Decimal] = []
    for d in dates:
        eod = datetime(d.year, d.month, d.day, tzinfo=UTC) + timedelta(days=1)
        out.append(
            sum(
                (
                    entry * qty + fees
                    for opened, closed, entry, qty, fees in rows
                    if opened <= eod and (closed is None or closed > eod)
                ),
                Decimal(0),
            )
        )
    return out


def _pnl_curve_data() -> tuple[
    list[tuple[str, Decimal]], Decimal, list[Decimal], list[date]
]:
    daily: dict[date, Decimal] = {}
    for closed_at, realized in (
        Position.objects.filter(status=PositionStatus.CLOSED)
        .exclude(closed_at__isnull=True)
        .values_list("closed_at", "realized_pnl")
    ):
        if closed_at is None:
            continue
        day = closed_at.date()
        daily[day] = daily.get(day, Decimal(0)) + realized
    sorted_dates = sorted(daily)
    days = [(d.strftime("%d.%m"), daily[d]) for d in sorted_dates]

    base_capital = Decimal(0)
    for p in Position.objects.filter(status=PositionStatus.OPEN):
        base_capital += p.entry_price * p.qty + p.fees_in
    locked = _locked_by_day(sorted_dates)
    return days, base_capital, locked, sorted_dates


async def pnl_curve_data() -> tuple[
    list[tuple[str, Decimal]], Decimal, list[Decimal], list[date]
]:
    """Chart inputs: daily realized profit, base, locked USDT, and dates."""
    return await sync_to_async(_pnl_curve_data)()


def _unlock_from_db(
    price: Decimal | None,
) -> tuple[Decimal | None, Decimal]:
    closed = Position.objects.filter(status=PositionStatus.CLOSED).exclude(
        closed_at__isnull=True
    )
    realized = closed.aggregate(s=Sum("realized_pnl"))["s"] or Decimal(0)
    first = (
        closed.order_by("closed_at")
        .values_list("closed_at", flat=True)
        .first()
    )
    if first is None or realized <= 0:
        return None, Decimal(0)
    now = datetime.now(tz=UTC)
    span_days = Decimal(str(max((now - first).total_seconds() / 86400, 1.0)))
    profit_per_day = realized / span_days

    fee = StrategyConfig.load().maker_fee
    if price is None or profit_per_day <= 0:
        return None, profit_per_day
    total_loss = Decimal(0)
    for entry, qty, fees_in in Position.objects.filter(
        status=PositionStatus.OPEN
    ).values_list("entry_price", "qty", "fees_in"):
        loss = entry * qty + fees_in - price * qty * (Decimal(1) - fee)
        if loss > 0:
            total_loss += loss
    return total_loss / profit_per_day, profit_per_day


async def unlock_from_db(
    price: Decimal | None,
) -> tuple[Decimal | None, Decimal]:
    """Days to unlock the locked loss and avg realized profit per day."""
    return await sync_to_async(_unlock_from_db)(price)


def _orders_data() -> list[tuple[int, Decimal, Decimal, Decimal | None]]:
    return [
        (p.level_index, p.entry_price, p.qty, p.tp_price)
        for p in Position.objects.filter(status=PositionStatus.OPEN).order_by(
            "level_index"
        )
    ]


async def orders_data() -> list[tuple[int, Decimal, Decimal, Decimal | None]]:
    """(level_index, entry_price, qty, tp_price) for open positions."""
    return await sync_to_async(_orders_data)()


def _digest_metrics() -> dict[str, Any]:
    now = datetime.now(tz=UTC)
    d24 = now - timedelta(hours=24)
    week = now - timedelta(days=7)
    closed = Position.objects.filter(status=PositionStatus.CLOSED)
    open_qs = Position.objects.filter(status=PositionStatus.OPEN)
    return {
        "closed_24h": closed.filter(closed_at__gte=d24).count(),
        "pnl_24h": _sum(closed.filter(closed_at__gte=d24)),
        "pnl_week": _sum(closed.filter(closed_at__gte=week)),
        "pnl_total": _sum(closed),
        "compensations_24h": CompensationLink.objects.filter(
            created_at__gte=d24
        ).count(),
        "open_positions": open_qs.count(),
        "deployed": open_qs.aggregate(s=Sum(F("entry_price") * F("qty")))["s"]
        or Decimal(0),
    }


async def digest_metrics() -> dict[str, Any]:
    """DB metrics for the daily digest (counts, PnL windows, deployed)."""
    return await sync_to_async(_digest_metrics)()


def _symbol() -> str:
    return str(StrategyConfig.objects.get(pk=1).symbol)


async def symbol() -> str:
    """The configured trading symbol."""
    return await sync_to_async(_symbol)()


def _is_admin(chat_id: int) -> bool:
    return TelegramUser.objects.filter(chat_id=chat_id, is_admin=True).exists()


async def is_admin(chat_id: int) -> bool:
    """Whether ``chat_id`` is an allow-listed bot admin."""
    return await sync_to_async(_is_admin)(chat_id)


def _admin_chat_ids() -> list[int]:
    return list(
        TelegramUser.objects.filter(is_admin=True).values_list(
            "chat_id", flat=True
        )
    )


async def admin_chat_ids() -> list[int]:
    """Chat ids of all admin Telegram users."""
    return await sync_to_async(_admin_chat_ids)()


def _upsert_admin(chat_id: int, label: str) -> bool:
    _user, created = TelegramUser.objects.update_or_create(
        chat_id=chat_id,
        defaults={"is_admin": True, "label": label},
    )
    return created


async def upsert_admin(chat_id: int, label: str) -> bool:
    """Grant admin to ``chat_id``; return True if newly created."""
    return await sync_to_async(_upsert_admin)(chat_id, label)


def _load_notification_settings() -> NotificationSettings:
    return NotificationSettings.load()


async def load_notification_settings() -> NotificationSettings:
    """Load the singleton notification-settings row."""
    return await sync_to_async(_load_notification_settings)()


def _notify_flag(field: str) -> bool:
    return bool(getattr(NotificationSettings.load(), field))


async def notify_flag(field: str) -> bool:
    """Current value of a boolean notification toggle."""
    return await sync_to_async(_notify_flag)(field)


def _toggle_notify_flag(field: str) -> bool:
    obj = NotificationSettings.load()
    new_value = not bool(getattr(obj, field))
    setattr(obj, field, new_value)
    obj.save(update_fields=[field, "updated_at"])
    return new_value


async def toggle_notify_flag(field: str) -> bool:
    """Flip a boolean notification toggle and return its new value."""
    return await sync_to_async(_toggle_notify_flag)(field)


def _set_digest_time(t: time) -> time:
    obj = NotificationSettings.load()
    obj.digest_time_utc = t
    obj.save(update_fields=["digest_time_utc", "updated_at"])
    return obj.digest_time_utc


async def set_digest_time(t: time) -> time:
    """Store the daily-digest time (UTC); return the stored value."""
    return await sync_to_async(_set_digest_time)(t)


def _claim_digest_due() -> bool:
    s = NotificationSettings.load()
    if not s.digest_enabled:
        return False
    now = datetime.now(tz=UTC)
    scheduled = datetime.combine(now.date(), s.digest_time_utc, tzinfo=UTC)
    if now < scheduled or s.digest_last_sent == now.date():
        return False
    s.digest_last_sent = now.date()
    s.save(update_fields=["digest_last_sent", "updated_at"])
    return True


async def claim_digest_due() -> bool:
    """Return True and stamp ``digest_last_sent`` iff the digest is due."""
    return await sync_to_async(_claim_digest_due)()


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


async def upsert_grid_level(
    level_index: int, price: Decimal, order_id: str
) -> None:
    """Create/refresh a grid level as awaiting-fill with its buy order."""
    await sync_to_async(_upsert_grid_level)(level_index, price, order_id)


def _find_level_by_order_id(order_id: str) -> GridLevel | None:
    return GridLevel.objects.filter(current_buy_order_id=order_id).first()


async def find_level_by_order_id(order_id: str) -> GridLevel | None:
    """The grid level resting the given buy order id, or None."""
    return await sync_to_async(_find_level_by_order_id)(order_id)


def _find_open_position_by_tp_order(order_id: str) -> Position | None:
    return Position.objects.filter(
        tp_order_id=order_id, status=PositionStatus.OPEN
    ).first()


async def find_open_position_by_tp_order(order_id: str) -> Position | None:
    """The open position whose take-profit is the given order id, or None."""
    return await sync_to_async(_find_open_position_by_tp_order)(order_id)


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


async def persist_buy_fill(
    *,
    execution: BybitExecution,
    level_index: int,
    fees_in: Decimal,
    tp_price: Decimal,
    tp_order_id: str,
) -> None:
    """Open a position, mark its level filled, log the fill (atomic)."""
    await sync_to_async(_persist_buy_fill)(
        execution=execution,
        level_index=level_index,
        fees_in=fees_in,
        tp_price=tp_price,
        tp_order_id=tp_order_id,
    )


def _apply_sell_fill(
    *,
    position: Position,
    execution: BybitExecution,
    fees_out: Decimal,
    lot_size: Decimal,
) -> SellFillResult:
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


async def apply_sell_fill(
    *,
    position: Position,
    execution: BybitExecution,
    fees_out: Decimal,
    lot_size: Decimal,
) -> SellFillResult:
    """Accumulate one (possibly partial) TP fill onto a position (atomic).

    The position closes only once the unsold remainder drops below one lot;
    realized PnL is from actual accumulated proceeds and the full entry cost.
    Idempotent on ``exec_id`` (the WS may redeliver).
    """
    return await sync_to_async(_apply_sell_fill)(
        position=position,
        execution=execution,
        fees_out=fees_out,
        lot_size=lot_size,
    )
