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
from tgbot.notify_settings import ASTANA_OFFSET

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
    """Build the /pnl snapshot from closed positions."""
    now = datetime.now(tz=UTC)
    today_start = now.replace(hour=0, minute=0, second=0, microsecond=0)
    week_start = today_start - timedelta(days=7)
    month_start = today_start - timedelta(days=30)
    year_start = today_start - timedelta(days=365)
    base = Position.objects.filter(status=PositionStatus.CLOSED)

    return PnlSnapshot(
        today=_sum(base.filter(closed_at__gte=today_start)),
        week=_sum(base.filter(closed_at__gte=week_start)),
        month=_sum(base.filter(closed_at__gte=month_start)),
        year=_sum(base.filter(closed_at__gte=year_start)),
        total=_sum(base),
    )


@sync_to_async
def pnl_curve_data() -> tuple[list[tuple[str, Decimal]], Decimal, Decimal]:
    """Chart inputs: daily realized profit, capital base, and TP projection.

    Realized PnL of closed trades is bucketed by UTC day (label, sum) to
    match the /pnl caption; ``base_capital`` is the cost basis of the open
    inventory; ``projection`` is the gain every open lot books at its TP.
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
    days = [(d.strftime("%d.%m"), v) for d, v in sorted(daily.items())]

    fee = StrategyConfig.load().maker_fee
    base_capital = Decimal(0)
    projection = Decimal(0)
    for p in Position.objects.filter(status=PositionStatus.OPEN):
        base_capital += p.entry_price * p.qty + p.fees_in
        if p.tp_price is not None:
            projection += (
                p.tp_price * p.qty * (Decimal(1) - fee)
                - p.entry_price * p.qty
                - p.fees_in
            )
    return days, base_capital, projection


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
    when_astana = (datetime.now(tz=UTC) + ASTANA_OFFSET).replace(tzinfo=None)
    return DigestSnapshot(
        when_astana=when_astana,
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
