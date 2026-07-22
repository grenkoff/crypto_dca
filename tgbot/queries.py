"""Read-side queries used by the Telegram bot to build snapshots."""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from typing import Any

import structlog
from asgiref.sync import sync_to_async
from django.db.models import F, QuerySet, Sum

from core.exchange.bybit import BybitClient
from core.trading.models import (
    BotStatus,
    CompensationLink,
    Position,
    PositionStatus,
    StrategyConfig,
)
from tgbot.formatters import (
    BalanceSnapshot,
    DigestSnapshot,
    OrderRow,
    OrdersSnapshot,
    PnlSnapshot,
    StatusSnapshot,
)

log = structlog.get_logger()


def _sum(qs: QuerySet[Position], field: str = "realized_pnl") -> Decimal:
    """Sum ``field`` over the queryset, treating an empty result as 0."""
    return qs.aggregate(s=Sum(field))["s"] or Decimal(0)


@sync_to_async
def status_snapshot() -> StatusSnapshot:
    """Build the /status snapshot."""
    bot = BotStatus.load()
    open_count = Position.objects.filter(status=PositionStatus.OPEN).count()
    return StatusSnapshot(
        paused=bot.paused,
        open_positions=open_count,
        started_at=bot.started_at,
        last_heartbeat=bot.last_heartbeat,
    )


@sync_to_async
def pnl_snapshot() -> PnlSnapshot:
    """Build the /pnl snapshot from closed positions.

    ``today`` is since UTC midnight; the rest are rolling from now (last 24
    hours, 7/30/365 days) plus all time.
    """
    now = datetime.now(tz=UTC)
    midnight = now.replace(hour=0, minute=0, second=0, microsecond=0)
    base = Position.objects.filter(status=PositionStatus.CLOSED)

    return PnlSnapshot(
        today=_sum(base.filter(closed_at__gte=midnight)),
        last_24h=_sum(base.filter(closed_at__gte=now - timedelta(hours=24))),
        last_7d=_sum(base.filter(closed_at__gte=now - timedelta(days=7))),
        last_30d=_sum(base.filter(closed_at__gte=now - timedelta(days=30))),
        last_365d=_sum(base.filter(closed_at__gte=now - timedelta(days=365))),
        all_time=_sum(base),
    )


def _locked_by_day(dates: list[date]) -> list[Decimal]:
    """USDT cost basis of positions open at the end of each given UTC day."""
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


@sync_to_async
def pnl_curve_data() -> tuple[
    list[tuple[str, Decimal]], Decimal, list[Decimal], list[date]
]:
    """Chart inputs: daily realized profit, base, locked USDT, and dates.

    Realized PnL of closed trades is bucketed by UTC day (label, sum) to
    match the /pnl caption; ``base_capital`` is the cost basis of the open
    inventory; ``locked`` is the open-inventory cost basis at the end of each
    day; ``dates`` are the UTC days (aligned to the others) for the price line.
    """
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


async def daily_close_line(dates: list[date]) -> list[float]:
    """Close price of the traded symbol for each UTC day (NaN if missing)."""
    if not dates:
        return []
    closes: dict[date, Decimal] = {}
    try:
        client = BybitClient.from_settings()
        symbol = str(await _symbol())
        start = datetime(
            dates[0].year, dates[0].month, dates[0].day, tzinfo=UTC
        )
        closes = await client.get_daily_closes(
            symbol, int(start.timestamp() * 1000)
        )
    except Exception as exc:
        log.warning("pnl.price_line_failed", error=str(exc)[:100])
    return [float(closes[d]) if d in closes else float("nan") for d in dates]


def _unlock_from_db(price: Decimal | None) -> tuple[Decimal | None, Decimal]:
    """Days to bank enough profit to absorb the loss locked in open lots.

    An underwater lot only unlocks once banked profit funds its loss at
    ``price``. Days = total locked loss at market / avg realized profit per
    day. Returns (days or None, profit per day).
    """
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


async def unlock_estimate() -> tuple[Decimal | None, Decimal]:
    """Days to unlock the locked loss and the avg realized profit per day."""
    price: Decimal | None = None
    try:
        client = BybitClient.from_settings()
        price = await client.get_last_price(await _symbol())
    except Exception as exc:
        log.warning("pnl.price_fetch_failed", error=str(exc)[:100])
    return await sync_to_async(_unlock_from_db)(price)


@sync_to_async
def orders_snapshot() -> OrdersSnapshot:
    """Build the /orders snapshot from open positions."""
    rows = [
        OrderRow(
            level_index=p.level_index,
            entry_price=p.entry_price,
            qty=p.qty,
            tp_price=p.tp_price,
        )
        for p in Position.objects.filter(status=PositionStatus.OPEN).order_by(
            "level_index"
        )
    ]
    return OrdersSnapshot(open_positions=rows)


@sync_to_async
def _digest_db() -> dict[str, Any]:
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


async def digest_snapshot() -> DigestSnapshot:
    """Build the daily digest snapshot (DB plus live price)."""
    db = await _digest_db()
    client = BybitClient.from_settings()
    free_usdt = Decimal(0)
    price: Decimal | None = None
    try:
        balances = await client.get_balances()
        usdt = balances.get("USDT")
        if usdt is not None:
            free_usdt = usdt.free
        cfg = await _symbol()
        price = await client.get_last_price(cfg)
    except Exception as exc:
        log.warning("digest.live_fetch_failed", error=str(exc)[:100])
    when_utc = datetime.now(tz=UTC).replace(tzinfo=None)
    return DigestSnapshot(
        when_utc=when_utc,
        closed_24h=db["closed_24h"],
        pnl_24h=db["pnl_24h"],
        pnl_week=db["pnl_week"],
        pnl_total=db["pnl_total"],
        compensations_24h=db["compensations_24h"],
        open_positions=db["open_positions"],
        deployed=db["deployed"],
        free_usdt=free_usdt,
        price=price,
    )


@sync_to_async
def _symbol() -> str:

    return str(StrategyConfig.objects.get(pk=1).symbol)


async def balance_snapshot() -> BalanceSnapshot:
    """Build the /balance snapshot from wallet balances."""
    client = BybitClient.from_settings()
    balances = await client.get_balances()
    return BalanceSnapshot(
        balances={coin: b.free for coin, b in balances.items() if b.total > 0}
    )
