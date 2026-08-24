"""Database access for grid levels, positions, executions and status.

The sole seam between the money core and persistence. Every function is
async and runs natively on SQLAlchemy ``AsyncSession``: reads open a
short session, writes wrap a ``session.begin()`` transaction so a
mid-way failure never leaves a half-applied state.
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import UTC, date, datetime, time, timedelta
from decimal import ROUND_HALF_UP, Decimal
from typing import Any

from sqlalchemy import delete, func, or_, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from core.db.models import (
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
    Transfer,
    WebuiAudit,
)
from core.db.session import new_session
from core.exchange.types import Execution as BybitExecution
from core.exchange.types import Transfer as BybitTransfer
from core.services.order_common import SellFillResult

_OPEN = str(PositionStatus.OPEN)
_CLOSED = str(PositionStatus.CLOSED)
_SELL = str(OrderSide.SELL)
_MAX_CHART_DAYS = 100
_AWAITING = str(LevelStatus.AWAITING_FILL)
_IDLE = str(LevelStatus.IDLE)
_FILLED = str(LevelStatus.FILLED)


def _now() -> datetime:
    return datetime.now(tz=UTC)


async def _load_bot(session: AsyncSession) -> BotStatus:
    """Get-or-create the singleton bot-status row (pk=1)."""
    obj = await session.get(BotStatus, 1)
    if obj is None:
        obj = BotStatus(id=1)
        session.add(obj)
        await session.flush()
    return obj


async def _load_notif(session: AsyncSession) -> NotificationSettings:
    """Get-or-create the singleton notification-settings row (pk=1)."""
    obj = await session.get(NotificationSettings, 1)
    if obj is None:
        obj = NotificationSettings(id=1)
        session.add(obj)
        await session.flush()
    return obj


async def _get_config(session: AsyncSession) -> StrategyConfig:
    """Load the singleton strategy config (pk=1); raise if absent."""
    cfg = await session.get(StrategyConfig, 1)
    if cfg is None:
        raise ValueError("StrategyConfig row is missing")
    return cfg


async def _sum(session: AsyncSession, column: Any, *conds: Any) -> Decimal:
    """Sum ``column`` over rows matching ``conds`` (0 when empty)."""
    val = await session.scalar(select(func.sum(column)).where(*conds))
    return val if val is not None else Decimal(0)


async def _count(session: AsyncSession, *conds: Any) -> int:
    """Count positions matching ``conds``."""
    val = await session.scalar(select(func.count(Position.id)).where(*conds))
    return int(val or 0)


async def existing_active_levels() -> set[int]:
    """Level indices of awaiting-fill grid levels and open positions."""
    async with new_session() as session:
        levels = await session.scalars(
            select(GridLevel.level_index).where(GridLevel.status == _AWAITING)
        )
        positions = await session.scalars(
            select(Position.level_index).where(Position.status == _OPEN)
        )
        return set(levels.all()) | set(positions.all())


async def naked_candidates(min_age_seconds: int) -> list[tuple[int, str]]:
    """(id, tp_order_id) for open positions older than the guard window."""
    cutoff = _now() - timedelta(seconds=min_age_seconds)
    async with new_session() as session:
        rows = await session.execute(
            select(Position.id, Position.tp_order_id).where(
                Position.status == _OPEN,
                Position.opened_at < cutoff,
                Position.tp_order_id != "",
            )
        )
        return [(int(pid), str(oid)) for pid, oid in rows.all()]


async def get_open_position(pos_id: int) -> Position | None:
    """The open position with ``pos_id``, or None."""
    async with new_session() as session:
        found: Position | None = await session.scalar(
            select(Position).where(
                Position.id == pos_id, Position.status == _OPEN
            )
        )
        return found


async def grid_state(
    step: Decimal,
) -> tuple[dict[Decimal, tuple[int, str]], set[Decimal]]:
    """Resting buys keyed by price and the set of held round prices."""
    async with new_session() as session:
        rows = await session.execute(
            select(
                GridLevel.target_buy_price,
                GridLevel.level_index,
                GridLevel.current_buy_order_id,
            ).where(
                GridLevel.status == _AWAITING,
                GridLevel.current_buy_order_id != "",
            )
        )
        resting = {price: (int(idx), oid) for price, idx, oid in rows.all()}
        held: set[Decimal] = set()
        entries = await session.scalars(
            select(Position.entry_price).where(Position.status == _OPEN)
        )
        for entry in entries.all():
            k = int((entry / step).to_integral_value(rounding=ROUND_HALF_UP))
            held.add(Decimal(k) * step)
    return resting, held


async def idle_level(level_index: int) -> None:
    """Idle a grid level and clear its buy-order id."""
    async with new_session() as session, session.begin():
        await session.execute(
            update(GridLevel)
            .where(GridLevel.level_index == level_index)
            .values(status=_IDLE, current_buy_order_id="", updated_at=_now())
        )


async def grid_params_changed(grid_step: Decimal, order_qty: Decimal) -> bool:
    """Whether grid geometry differs from what it was last built with.

    On the first run the applied values are unset, so we adopt the current
    geometry without forcing a rebuild.
    """
    async with new_session() as session, session.begin():
        bot = await _load_bot(session)
        if bot.applied_grid_step is None or bot.applied_order_qty is None:
            bot.applied_grid_step = grid_step
            bot.applied_order_qty = order_qty
            return False
        return (
            bot.applied_grid_step != grid_step
            or bot.applied_order_qty != order_qty
        )


async def reset_all_grid_levels() -> None:
    """Idle every awaiting-fill grid level."""
    async with new_session() as session, session.begin():
        await session.execute(
            update(GridLevel)
            .where(GridLevel.status == _AWAITING)
            .values(status=_IDLE, current_buy_order_id="", updated_at=_now())
        )


async def record_applied_grid_params(
    grid_step: Decimal, order_qty: Decimal
) -> None:
    """Record the grid geometry the buy grid was built with."""
    async with new_session() as session, session.begin():
        bot = await _load_bot(session)
        bot.applied_grid_step = grid_step
        bot.applied_order_qty = order_qty


async def awaiting_buy_levels() -> list[tuple[int, str]]:
    """(level_index, order_id) for grid levels still expecting a buy fill."""
    async with new_session() as session:
        rows = await session.execute(
            select(
                GridLevel.level_index, GridLevel.current_buy_order_id
            ).where(
                GridLevel.status == _AWAITING,
                GridLevel.current_buy_order_id != "",
            )
        )
        return [(int(idx), oid) for idx, oid in rows.all()]


async def open_tp_order_ids() -> set[str]:
    """TP order ids of all open positions."""
    async with new_session() as session:
        rows = await session.scalars(
            select(Position.tp_order_id).where(
                Position.status == _OPEN, Position.tp_order_id != ""
            )
        )
        return set(rows.all())


async def exec_logged(exec_id: str) -> bool:
    """Whether an execution with ``exec_id`` is already recorded."""
    async with new_session() as session:
        found = await session.scalar(
            select(ExecutionLog.id).where(ExecutionLog.exec_id == exec_id)
        )
        return found is not None


async def is_paused() -> bool:
    """Whether the bot is paused."""
    async with new_session() as session, session.begin():
        bot = await _load_bot(session)
        return bool(bot.paused)


async def highest_resting_buy() -> Decimal:
    """Highest resting buy price (nearest market), or 0 if none."""
    async with new_session() as session:
        top = await session.scalar(
            select(func.max(GridLevel.target_buy_price)).where(
                GridLevel.status == _AWAITING,
                GridLevel.current_buy_order_id != "",
            )
        )
        return top if top is not None else Decimal(0)


async def lowest_resting_tp() -> Decimal | None:
    """Lowest resting take-profit price (bottom of the wall), or None."""
    async with new_session() as session:
        return await session.scalar(
            select(func.min(Position.tp_price)).where(
                Position.status == _OPEN,
                Position.tp_order_id != "",
                Position.tp_price.is_not(None),
            )
        )


async def status_data() -> tuple[bool, int, datetime | None, datetime | None]:
    """(paused, open_position_count, started_at, last_heartbeat)."""
    async with new_session() as session, session.begin():
        bot = await _load_bot(session)
        open_count = await _count(session, Position.status == _OPEN)
        return bot.paused, open_count, bot.started_at, bot.last_heartbeat


async def realized_pnl_since(cutoff: datetime | None) -> Decimal:
    """Realized PnL of closed positions since ``cutoff`` (None = all time)."""
    conds = [Position.status == _CLOSED]
    if cutoff is not None:
        conds.append(Position.closed_at >= cutoff)
    async with new_session() as session:
        return await _sum(session, Position.realized_pnl, *conds)


def _fill_day_gaps(daily: dict[date, Decimal]) -> list[date]:
    """Continuous day range from the first close through the last close.

    Days without a close are included so the chart shows flat-zero periods
    (e.g. no free capital to trade) instead of skipping them.
    """
    if not daily:
        return []
    start, end = min(daily), max(daily)
    span = (end - start).days
    return [start + timedelta(days=i) for i in range(span + 1)]


def _locked_by_day(
    rows: Sequence[Any],
    dates: list[date],
) -> list[Decimal]:
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


async def pnl_curve_data() -> tuple[
    list[tuple[str, Decimal]], Decimal, list[Decimal], list[date]
]:
    """Chart inputs: daily realized profit, base, locked USDT, and dates.

    Capped to the most recent ``_MAX_CHART_DAYS`` days so the chart stays
    readable as history grows.
    """
    async with new_session() as session:
        closed_rows = await session.execute(
            select(Position.closed_at, Position.realized_pnl).where(
                Position.status == _CLOSED, Position.closed_at.is_not(None)
            )
        )
        daily: dict[date, Decimal] = {}
        for closed_at, realized in closed_rows.all():
            if closed_at is None:
                continue
            day = closed_at.date()
            daily[day] = daily.get(day, Decimal(0)) + realized
        sorted_dates = _fill_day_gaps(daily)[-_MAX_CHART_DAYS:]
        days = [
            (d.strftime("%d.%m"), daily.get(d, Decimal(0)))
            for d in sorted_dates
        ]

        base_rows = await session.execute(
            select(Position.entry_price, Position.qty, Position.fees_in).where(
                Position.status == _OPEN
            )
        )
        base_capital = sum(
            (entry * qty + fees for entry, qty, fees in base_rows.all()),
            Decimal(0),
        )
        all_rows = await session.execute(
            select(
                Position.opened_at,
                Position.closed_at,
                Position.entry_price,
                Position.qty,
                Position.fees_in,
            )
        )
        locked = _locked_by_day(all_rows.all(), sorted_dates)
    return days, base_capital, locked, sorted_dates


async def unlock_from_db(
    price: Decimal | None,
) -> tuple[Decimal | None, Decimal]:
    """Days to unlock the locked loss and avg realized profit per day."""
    async with new_session() as session:
        realized = await _sum(
            session,
            Position.realized_pnl,
            Position.status == _CLOSED,
            Position.closed_at.is_not(None),
        )
        first = await session.scalar(
            select(func.min(Position.closed_at)).where(
                Position.status == _CLOSED, Position.closed_at.is_not(None)
            )
        )
        if first is None or realized <= 0:
            return None, Decimal(0)
        span = Decimal(str(max((_now() - first).total_seconds() / 86400, 1.0)))
        profit_per_day = realized / span

        fee = (await _get_config(session)).maker_fee
        if price is None or profit_per_day <= 0:
            return None, profit_per_day
        open_rows = await session.execute(
            select(Position.entry_price, Position.qty, Position.fees_in).where(
                Position.status == _OPEN
            )
        )
        total_loss = Decimal(0)
        for entry, qty, fees_in in open_rows.all():
            loss = entry * qty + fees_in - price * qty * (Decimal(1) - fee)
            if loss > 0:
                total_loss += loss
    return total_loss / profit_per_day, profit_per_day


async def profit_rate_data() -> tuple[Decimal, Decimal, Decimal]:
    """(all-time realized PnL, span days, avg deployed cost over the span).

    ``avg_deployed`` is the time-average of the open-inventory cost basis
    over each day since the first close — a fairer base than a point-in-time
    snapshot for annualising an averaged profit rate.
    """
    async with new_session() as session:
        realized = await _sum(
            session,
            Position.realized_pnl,
            Position.status == _CLOSED,
            Position.closed_at.is_not(None),
        )
        first = await session.scalar(
            select(func.min(Position.closed_at)).where(
                Position.status == _CLOSED, Position.closed_at.is_not(None)
            )
        )
        if first is None:
            return realized, Decimal(1), Decimal(0)
        span = Decimal(str(max((_now() - first).total_seconds() / 86400, 1.0)))
        rows = list(
            (
                await session.execute(
                    select(
                        Position.opened_at,
                        Position.closed_at,
                        Position.entry_price,
                        Position.qty,
                        Position.fees_in,
                    )
                )
            ).all()
        )
    start = first.date()
    today = _now().date()
    grid = [start + timedelta(days=i) for i in range((today - start).days + 1)]
    series = _locked_by_day(rows, grid)
    avg_deployed = (
        sum(series, Decimal(0)) / len(series) if series else Decimal(0)
    )
    return realized, span, avg_deployed


async def orders_data() -> list[tuple[int, Decimal, Decimal, Decimal | None]]:
    """(level_index, entry_price, qty, tp_price) for open positions."""
    async with new_session() as session:
        rows = await session.execute(
            select(
                Position.level_index,
                Position.entry_price,
                Position.qty,
                Position.tp_price,
            )
            .where(Position.status == _OPEN)
            .order_by(Position.level_index)
        )
        return [(int(idx), e, q, tp) for idx, e, q, tp in rows.all()]


def _usdt_added_after(rows: Sequence[Any], moment: datetime) -> Decimal:
    """USDT that fills executed after ``moment`` added to the balance.

    A sell credits its notional minus the quote-side fee; a buy debits its
    notional (its fee is charged in the base coin and never touches USDT).
    """
    return sum(
        (
            price * qty - fee if side == _SELL else -(price * qty)
            for side, price, qty, fee, executed_at in rows
            if executed_at > moment
        ),
        Decimal(0),
    )


def _tp_at(
    comps: Sequence[tuple[datetime, Decimal]],
    moment: datetime,
    current_tp: Decimal,
    grid_step: Decimal,
) -> Decimal:
    """The take-profit price a lot carried at ``moment``.

    Compensation only ever moves a TP down onto the grid slot below, so
    before the earliest logged move the price was one ``grid_step`` higher.
    """
    past = [tp for created, tp in comps if created <= moment]
    if past:
        return past[-1]
    if comps:
        return comps[0][1] + grid_step
    return current_tp


def _tp_gross_at(
    rows: Sequence[Any],
    comps: dict[int, list[tuple[datetime, Decimal]]],
    moment: datetime,
    grid_step: Decimal,
) -> Decimal:
    """Gross USDT the lots open at ``moment`` would fetch at their TPs."""
    total = Decimal(0)
    for pid, opened, closed, qty, filled, tp in rows:
        if tp is None or opened > moment:
            continue
        if closed is not None and closed <= moment:
            continue
        held = qty - filled if closed is None else qty
        total += held * _tp_at(comps.get(pid, []), moment, tp, grid_step)
    return total


async def open_base_qty() -> Decimal:
    """Base coin still held by open positions."""
    async with new_session() as session:
        return await _sum(
            session,
            Position.qty - Position.filled_qty,
            Position.status == _OPEN,
        )


async def record_transfers(rows: Sequence[BybitTransfer]) -> int:
    """Store funding movements, skipping ones already known."""
    if not rows:
        return 0
    async with new_session() as session, session.begin():
        known = set(
            (
                await session.scalars(
                    select(Transfer.external_id).where(
                        Transfer.external_id.in_([r.external_id for r in rows])
                    )
                )
            ).all()
        )
        fresh = [r for r in rows if r.external_id not in known]
        session.add_all(
            [
                Transfer(
                    external_id=r.external_id,
                    coin=r.coin,
                    amount=r.amount,
                    at=r.at,
                    recorded_at=_now(),
                )
                for r in fresh
            ]
        )
        return len(fresh)


async def last_transfer_at() -> datetime | None:
    """Timestamp of the most recent stored transfer, if any."""
    async with new_session() as session:
        return await session.scalar(select(func.max(Transfer.at)))


def _transferred_after(
    rows: Sequence[tuple[datetime, Decimal]], moment: datetime
) -> Decimal:
    """Net funding that arrived after ``moment``."""
    return sum((amount for at, amount in rows if at > moment), Decimal(0))


async def tp_projection_series(
    usdt_now: Decimal, days: int
) -> list[tuple[date, Decimal]]:
    """USDT the account would hold if every open lot sold at its TP.

    One point per UTC day over the last ``days`` days, rebuilt from the
    execution log (USDT flow) and the compensation log (past TP prices).
    """
    if days <= 0:
        raise ValueError("days must be positive")
    now = _now()
    dates = [(now - timedelta(days=i)).date() for i in reversed(range(days))]
    return await projection_for_days(usdt_now, dates)


async def projection_for_days(
    usdt_now: Decimal, dates: list[date]
) -> list[tuple[date, Decimal]]:
    """The all-TPs-filled projection on each of ``dates`` (UTC days)."""
    if not dates:
        return []
    now = _now()
    first = datetime.combine(dates[0], time.min, tzinfo=UTC)
    async with new_session() as session:
        cfg = await _get_config(session)
        transfers = [
            (at, amount)
            for at, amount in (
                await session.execute(
                    select(Transfer.at, Transfer.amount).where(
                        Transfer.at > first
                    )
                )
            ).all()
        ]
        execs = (
            await session.execute(
                select(
                    ExecutionLog.side,
                    ExecutionLog.price,
                    ExecutionLog.qty,
                    ExecutionLog.fee,
                    ExecutionLog.executed_at,
                ).where(ExecutionLog.executed_at > first)
            )
        ).all()
        positions = (
            await session.execute(
                select(
                    Position.id,
                    Position.opened_at,
                    Position.closed_at,
                    Position.qty,
                    Position.filled_qty,
                    Position.tp_price,
                ).where(
                    or_(
                        Position.closed_at.is_(None),
                        Position.closed_at > first,
                    )
                )
            )
        ).all()
        comp_rows = (
            (
                await session.execute(
                    select(
                        CompensationLink.compensated_position_id,
                        CompensationLink.created_at,
                        CompensationLink.new_tp_price,
                    )
                    .where(
                        CompensationLink.compensated_position_id.in_(
                            [row.id for row in positions]
                        )
                    )
                    .order_by(CompensationLink.created_at)
                )
            ).all()
            if positions
            else []
        )
    comps: dict[int, list[tuple[datetime, Decimal]]] = {}
    for pid, created, tp in comp_rows:
        comps.setdefault(pid, []).append((created, tp))
    net = Decimal(1) - cfg.maker_fee
    out: list[tuple[date, Decimal]] = []
    for day in dates:
        eod = min(
            datetime.combine(day, time.min, tzinfo=UTC) + timedelta(days=1),
            now,
        )
        usdt = (
            usdt_now
            - _usdt_added_after(execs, eod)
            - _transferred_after(transfers, eod)
        )
        gross = _tp_gross_at(positions, comps, eod, cfg.grid_step)
        out.append((day, usdt + gross * net))
    return out


async def account_value_series(
    usdt_now: Decimal,
    spare_base: Decimal,
    price: Decimal,
    dates: list[date],
) -> list[tuple[date, Decimal]]:
    """What the whole account is worth per day, in USDT.

    Cash (including what rests in buy orders), every open lot valued at
    the take-profit it carried that day, and base coin held outside any
    position valued at ``price``. Deposits and withdrawals are rewound
    with the trades, so the line steps when funding moves.
    """
    projection = await projection_for_days(usdt_now, dates)
    spare = spare_base * price
    return [(day, value + spare) for day, value in projection]


async def digest_metrics() -> dict[str, Any]:
    """DB metrics for the daily digest (counts, PnL windows, deployed)."""
    now = _now()
    d24 = now - timedelta(hours=24)
    week = now - timedelta(days=7)
    closed = Position.status == _CLOSED
    async with new_session() as session:
        comp_24h = await session.scalar(
            select(func.count(CompensationLink.id)).where(
                CompensationLink.created_at >= d24
            )
        )
        deployed = await _sum(
            session,
            Position.entry_price * Position.qty,
            Position.status == _OPEN,
        )
        return {
            "closed_24h": await _count(
                session, closed, Position.closed_at >= d24
            ),
            "pnl_24h": await _sum(
                session,
                Position.realized_pnl,
                closed,
                Position.closed_at >= d24,
            ),
            "pnl_week": await _sum(
                session,
                Position.realized_pnl,
                closed,
                Position.closed_at >= week,
            ),
            "pnl_total": await _sum(session, Position.realized_pnl, closed),
            "compensations_24h": int(comp_24h or 0),
            "open_positions": await _count(session, Position.status == _OPEN),
            "deployed": deployed,
        }


async def symbol() -> str:
    """The configured trading symbol."""
    async with new_session() as session:
        return str((await _get_config(session)).symbol)


async def get_config() -> StrategyConfig:
    """Load the singleton strategy config (detached); raise if absent."""
    async with new_session() as session:
        return await _get_config(session)


async def load_config() -> StrategyConfig:
    """Get-or-create the singleton strategy config with sane defaults.

    Used by the manual CLIs (preflight/consolidate); the live trader uses
    the strict :func:`get_config`, which refuses to run unconfigured.
    """
    async with new_session() as session, session.begin():
        cfg = await session.get(StrategyConfig, 1)
        if cfg is None:
            cfg = StrategyConfig(id=1)
            session.add(cfg)
            await session.flush()
        return cfg


_CFG_POSITIVE = frozenset({"grid_step", "tp_step", "order_qty_quote"})
_CFG_NONNEG = frozenset({"min_profit_quote"})
_CFG_FEES = frozenset({"maker_fee", "taker_fee"})
_CFG_FIELDS = (
    _CFG_POSITIVE
    | _CFG_NONNEG
    | _CFG_FEES
    | frozenset({"max_open_orders", "grid_mode", "symbol"})
)


def _validate_config_updates(updates: dict[str, Any]) -> None:
    """Raise ``ValueError`` on any unknown key or out-of-range value."""
    for key, value in updates.items():
        if key not in _CFG_FIELDS:
            raise ValueError(f"unknown config field: {key}")
        if key in _CFG_POSITIVE and value <= 0:
            raise ValueError(f"{key} must be > 0")
        if key in _CFG_NONNEG and value < 0:
            raise ValueError(f"{key} must be >= 0")
        if key in _CFG_FEES and not 0 <= value < Decimal("0.01"):
            raise ValueError(f"{key} must be in [0, 0.01)")
        if key == "max_open_orders" and value <= 0:
            raise ValueError("max_open_orders must be > 0")
        if key == "grid_mode" and value not in ("absolute", "percent"):
            raise ValueError("grid_mode must be 'absolute' or 'percent'")
        if key == "symbol" and not value:
            raise ValueError("symbol must be non-empty")


async def update_config(
    *, actor: str, updates: dict[str, Any]
) -> StrategyConfig:
    """Apply validated strategy-config changes and audit them (atomic)."""
    _validate_config_updates(updates)
    async with new_session() as session, session.begin():
        cfg = await _get_config(session)
        for key, value in updates.items():
            setattr(cfg, key, value)
        cfg.updated_at = _now()
        detail = ", ".join(f"{k}={v}" for k, v in sorted(updates.items()))
        session.add(WebuiAudit(actor=actor, action="config", detail=detail))
        return cfg


async def last_heartbeat() -> datetime | None:
    """The bot's last-heartbeat time (None if never stamped)."""
    async with new_session() as session, session.begin():
        return (await _load_bot(session)).last_heartbeat


async def mark_started() -> None:
    """Stamp the start time and clear the last error (atomic)."""
    async with new_session() as session, session.begin():
        bot = await _load_bot(session)
        bot.started_at = _now()
        bot.last_error = ""


async def is_admin(chat_id: int) -> bool:
    """Whether ``chat_id`` is an allow-listed bot admin."""
    async with new_session() as session:
        found = await session.scalar(
            select(TelegramUser.id).where(
                TelegramUser.chat_id == chat_id,
                TelegramUser.is_admin.is_(True),
            )
        )
        return found is not None


async def admin_chat_ids() -> list[int]:
    """Chat ids of all admin Telegram users."""
    async with new_session() as session:
        rows = await session.scalars(
            select(TelegramUser.chat_id).where(TelegramUser.is_admin.is_(True))
        )
        return [int(c) for c in rows.all()]


async def upsert_admin(chat_id: int, label: str) -> bool:
    """Grant admin to ``chat_id``; return True if newly created."""
    async with new_session() as session, session.begin():
        user = await session.scalar(
            select(TelegramUser).where(TelegramUser.chat_id == chat_id)
        )
        if user is None:
            session.add(
                TelegramUser(
                    chat_id=chat_id,
                    label=label,
                    is_admin=True,
                    created_at=_now(),
                )
            )
            return True
        user.is_admin = True
        user.label = label
        return False


async def issue_control_token(*, chat_id: int, token_hash: str) -> bool:
    """Store a control-token hash on an admin user; True if applied."""
    async with new_session() as session, session.begin():
        user = await session.scalar(
            select(TelegramUser).where(
                TelegramUser.chat_id == chat_id,
                TelegramUser.is_admin.is_(True),
            )
        )
        if user is None:
            return False
        user.control_token = token_hash
        return True


async def find_admin_by_token(token_hash: str) -> TelegramUser | None:
    """The admin user whose control-token hash matches, or None."""
    async with new_session() as session:
        found: TelegramUser | None = await session.scalar(
            select(TelegramUser).where(
                TelegramUser.control_token == token_hash,
                TelegramUser.is_admin.is_(True),
            )
        )
        return found


async def set_paused(*, paused: bool, actor: str) -> None:
    """Set the paused flag and record the action (atomic)."""
    async with new_session() as session, session.begin():
        bot = await _load_bot(session)
        bot.paused = paused
        session.add(
            WebuiAudit(actor=actor, action="pause" if paused else "resume")
        )


async def recent_audit(limit: int = 20) -> list[WebuiAudit]:
    """The most recent control-audit rows, newest first."""
    async with new_session() as session:
        rows = await session.scalars(
            select(WebuiAudit)
            .order_by(WebuiAudit.created_at.desc())
            .limit(limit)
        )
        return list(rows.all())


async def load_notification_settings() -> NotificationSettings:
    """Load the singleton notification-settings row."""
    async with new_session() as session, session.begin():
        return await _load_notif(session)


async def notify_flag(field: str) -> bool:
    """Current value of a boolean notification toggle."""
    async with new_session() as session, session.begin():
        return bool(getattr(await _load_notif(session), field))


async def toggle_notify_flag(field: str) -> bool:
    """Flip a boolean notification toggle and return its new value."""
    async with new_session() as session, session.begin():
        obj = await _load_notif(session)
        new_value = not bool(getattr(obj, field))
        setattr(obj, field, new_value)
        obj.updated_at = _now()
        return new_value


async def set_digest_time(t: time) -> time:
    """Store the daily-digest time (UTC); return the stored value."""
    async with new_session() as session, session.begin():
        obj = await _load_notif(session)
        obj.digest_time_utc = t
        obj.updated_at = _now()
        return obj.digest_time_utc


async def claim_digest_due() -> bool:
    """Return True and stamp ``digest_last_sent`` iff the digest is due."""
    async with new_session() as session, session.begin():
        s = await _load_notif(session)
        if not s.digest_enabled:
            return False
        now = _now()
        scheduled = datetime.combine(now.date(), s.digest_time_utc, tzinfo=UTC)
        if now < scheduled or s.digest_last_sent == now.date():
            return False
        s.digest_last_sent = now.date()
        s.updated_at = now
        return True


async def upsert_grid_level(
    level_index: int, price: Decimal, order_id: str
) -> None:
    """Create/refresh a grid level as awaiting-fill with its buy order."""
    async with new_session() as session, session.begin():
        level = await session.scalar(
            select(GridLevel).where(GridLevel.level_index == level_index)
        )
        if level is None:
            session.add(
                GridLevel(
                    level_index=level_index,
                    target_buy_price=price,
                    current_buy_order_id=order_id,
                    status=_AWAITING,
                    updated_at=_now(),
                )
            )
            return
        level.target_buy_price = price
        level.current_buy_order_id = order_id
        level.status = _AWAITING
        level.updated_at = _now()


async def find_level_by_order_id(order_id: str) -> GridLevel | None:
    """The grid level resting the given buy order id, or None."""
    async with new_session() as session:
        found: GridLevel | None = await session.scalar(
            select(GridLevel).where(GridLevel.current_buy_order_id == order_id)
        )
        return found


async def find_open_position_by_tp_order(order_id: str) -> Position | None:
    """The open position whose take-profit is the given order id, or None."""
    async with new_session() as session:
        found: Position | None = await session.scalar(
            select(Position).where(
                Position.tp_order_id == order_id, Position.status == _OPEN
            )
        )
        return found


async def _log_execution(
    session: AsyncSession, execution: BybitExecution
) -> None:
    """Upsert one execution row (idempotent audit trail)."""
    row = await session.scalar(
        select(ExecutionLog).where(ExecutionLog.exec_id == execution.exec_id)
    )
    if row is None:
        session.add(
            ExecutionLog(
                exec_id=execution.exec_id,
                order_id=execution.order_id,
                symbol=execution.symbol,
                side=execution.side.value,
                price=execution.price,
                qty=execution.qty,
                fee=execution.fee,
                fee_coin=execution.fee_coin,
                executed_at=execution.executed_at,
                received_at=_now(),
            )
        )
        return
    row.order_id = execution.order_id
    row.symbol = execution.symbol
    row.side = execution.side.value
    row.price = execution.price
    row.qty = execution.qty
    row.fee = execution.fee
    row.fee_coin = execution.fee_coin
    row.executed_at = execution.executed_at


async def persist_buy_fill(
    *,
    execution: BybitExecution,
    level_index: int,
    fees_in: Decimal,
    tp_price: Decimal,
    tp_order_id: str,
) -> None:
    """Open a position, mark its level filled, log the fill (atomic)."""
    async with new_session() as session, session.begin():
        session.add(
            Position(
                level_index=level_index,
                entry_price=execution.price,
                qty=execution.qty,
                fees_in=fees_in,
                fees_out=Decimal(0),
                filled_qty=Decimal(0),
                sell_value=Decimal(0),
                tp_order_id=tp_order_id,
                tp_price=tp_price,
                status=_OPEN,
                realized_pnl=Decimal(0),
                compensation_credit=Decimal(0),
                opened_at=execution.executed_at,
            )
        )
        await session.execute(
            update(GridLevel)
            .where(GridLevel.level_index == level_index)
            .values(status=_FILLED, current_buy_order_id="", updated_at=_now())
        )
        await _log_execution(session, execution)


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
    async with new_session() as session, session.begin():
        pos = await session.get(Position, position.id, with_for_update=True)
        if pos is None:
            raise ValueError(f"position {position.id} vanished")
        already = await session.scalar(
            select(ExecutionLog.id).where(
                ExecutionLog.exec_id == execution.exec_id
            )
        )
        if already is not None:
            remaining = max(pos.qty - pos.filled_qty, Decimal(0))
            return SellFillResult(
                closed=pos.status == _CLOSED,
                realized=pos.realized_pnl,
                filled_qty=pos.filled_qty,
                remaining=remaining,
            )
        pos.filled_qty += execution.qty
        pos.sell_value += execution.price * execution.qty
        pos.fees_out += fees_out
        remaining = pos.qty - pos.filled_qty
        closed = remaining < lot_size
        if closed:
            realized = (
                pos.sell_value
                - pos.fees_out
                - pos.entry_price * pos.qty
                - pos.fees_in
            )
            pos.realized_pnl = realized
            pos.status = _CLOSED
            pos.closed_at = execution.executed_at
        else:
            realized = Decimal(0)
        if closed:
            await session.execute(
                update(GridLevel)
                .where(GridLevel.level_index == pos.level_index)
                .values(
                    status=_IDLE, current_buy_order_id="", updated_at=_now()
                )
            )
        await _log_execution(session, execution)
        return SellFillResult(
            closed=closed,
            realized=realized,
            filled_qty=pos.filled_qty,
            remaining=max(remaining, Decimal(0)),
        )


async def set_tp(
    *, target: Position, tp_price: Decimal, tp_order_id: str
) -> None:
    """Persist a new take-profit price and order id on a position."""
    async with new_session() as session, session.begin():
        await session.execute(
            update(Position)
            .where(Position.id == target.id)
            .values(tp_price=tp_price, tp_order_id=tp_order_id)
        )


async def get_position(pos_id: int) -> Position:
    """Fetch a position by id (raises if absent)."""
    async with new_session() as session:
        pos = await session.get(Position, pos_id)
        if pos is None:
            raise ValueError(f"position {pos_id} not found")
        return pos


async def open_positions() -> list[Position]:
    """All open positions (callers map to their own value objects)."""
    async with new_session() as session:
        rows = await session.scalars(
            select(Position).where(Position.status == _OPEN)
        )
        return list(rows.all())


async def load_pending() -> Decimal:
    """The banked pending-compensation credit."""
    async with new_session() as session, session.begin():
        return (await _load_bot(session)).pending_credit


async def bank_pending(value: Decimal) -> None:
    """Persist the banked pending-compensation credit."""
    async with new_session() as session, session.begin():
        (await _load_bot(session)).pending_credit = value


async def active_buy_order_ids() -> set[str]:
    """Order ids of all resting (awaiting-fill) buy levels."""
    async with new_session() as session:
        rows = await session.scalars(
            select(GridLevel.current_buy_order_id).where(
                GridLevel.status == _AWAITING,
                GridLevel.current_buy_order_id != "",
            )
        )
        return set(rows.all())


async def heartbeat() -> None:
    """Stamp the bot's last-heartbeat time."""
    async with new_session() as session, session.begin():
        (await _load_bot(session)).last_heartbeat = _now()


async def close_at_price(
    *, position: Position, price: Decimal, maker_fee: Decimal
) -> Decimal:
    """Mark a position sold in full at ``price`` (maker), free its level."""
    async with new_session() as session, session.begin():
        pos = await session.get(Position, position.id, with_for_update=True)
        if pos is None:
            raise ValueError(f"position {position.id} vanished")
        remaining = pos.remaining_qty
        sell_value = pos.sell_value + price * remaining
        fees_out = pos.fees_out + price * remaining * maker_fee
        realized = (
            sell_value - fees_out - pos.entry_price * pos.qty - pos.fees_in
        )
        pos.filled_qty = pos.qty
        pos.sell_value = sell_value
        pos.fees_out = fees_out
        pos.realized_pnl = realized
        pos.status = _CLOSED
        pos.closed_at = _now()
        await session.execute(
            update(GridLevel)
            .where(GridLevel.level_index == pos.level_index)
            .values(status=_IDLE, current_buy_order_id="", updated_at=_now())
        )
        return realized


async def record_compensation(
    *,
    target: Position,
    new_tp_price: Decimal,
    new_tp_order_id: str,
    new_credit: Decimal,
    credit_drawn: Decimal,
    source_position_id: int,
    new_pending: Decimal,
) -> None:
    """Apply a compensation move and bank the remaining pool (atomic)."""
    async with new_session() as session, session.begin():
        tgt = await session.get(Position, target.id, with_for_update=True)
        if tgt is None:
            raise ValueError(f"position {target.id} vanished")
        tgt.tp_price = new_tp_price
        tgt.tp_order_id = new_tp_order_id
        tgt.compensation_credit = new_credit
        session.add(
            CompensationLink(
                profitable_position_id=source_position_id,
                compensated_position_id=tgt.id,
                profit_applied=credit_drawn,
                new_tp_price=new_tp_price,
                created_at=_now(),
            )
        )
        (await _load_bot(session)).pending_credit = new_pending


async def apply_merge(
    *,
    survivor_id: int,
    combined_qty: Decimal,
    weighted_entry: Decimal,
    combined_fees_in: Decimal,
    new_tp_order_id: str,
    new_tp_price: Decimal,
    absorbed_ids: list[int],
) -> None:
    """Rewrite the survivor lot and delete the absorbed ones (atomic)."""
    async with new_session() as session, session.begin():
        survivor = await session.get(
            Position, survivor_id, with_for_update=True
        )
        if survivor is None:
            raise ValueError(f"position {survivor_id} vanished")
        survivor.qty = combined_qty
        survivor.entry_price = weighted_entry
        survivor.fees_in = combined_fees_in
        survivor.tp_order_id = new_tp_order_id
        survivor.tp_price = new_tp_price
        await session.execute(
            delete(Position).where(Position.id.in_(absorbed_ids))
        )
