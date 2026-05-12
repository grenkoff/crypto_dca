"""Read-side queries used by the Telegram bot to build snapshots."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal

from asgiref.sync import sync_to_async
from django.db.models import Sum

from core.config.settings import bybit_settings
from core.exchange.bybit import BybitClient
from core.trading.models import BotStatus, Position, PositionStatus
from tgbot.formatters import (
    BalanceSnapshot,
    OrderRow,
    OrdersSnapshot,
    PnlSnapshot,
    StatusSnapshot,
)


@sync_to_async
def status_snapshot() -> StatusSnapshot:
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
    now = datetime.now(tz=UTC)
    today_start = now.replace(hour=0, minute=0, second=0, microsecond=0)
    week_start = today_start - timedelta(days=7)
    base = Position.objects.filter(status=PositionStatus.CLOSED)

    def _sum(qs) -> Decimal:  # type: ignore[no-untyped-def]
        return qs.aggregate(s=Sum("realized_pnl"))["s"] or Decimal(0)

    return PnlSnapshot(
        today=_sum(base.filter(closed_at__gte=today_start)),
        week=_sum(base.filter(closed_at__gte=week_start)),
        total=_sum(base),
    )


@sync_to_async
def orders_snapshot() -> OrdersSnapshot:
    rows = [
        OrderRow(
            level_index=p.level_index,
            entry_price=p.entry_price,
            qty=p.qty,
            tp_price=p.tp_price,
        )
        for p in Position.objects.filter(status=PositionStatus.OPEN).order_by("level_index")
    ]
    return OrdersSnapshot(open_positions=rows)


async def balance_snapshot() -> BalanceSnapshot:
    settings = bybit_settings()
    client = BybitClient.from_credentials(
        settings.api_key, settings.api_secret, testnet=settings.testnet
    )
    balances = await client.get_balances()
    return BalanceSnapshot(balances={coin: b.free for coin, b in balances.items() if b.total > 0})


@sync_to_async
def set_paused(paused: bool) -> None:
    bot = BotStatus.load()
    bot.paused = paused
    bot.save()


@sync_to_async
def list_open_buy_order_ids() -> list[str]:
    from core.trading.models import GridLevel, LevelStatus

    return list(
        GridLevel.objects.filter(status=LevelStatus.AWAITING_FILL)
        .exclude(current_buy_order_id="")
        .values_list("current_buy_order_id", flat=True)
    )
