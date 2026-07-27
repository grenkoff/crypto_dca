"""Read-side queries used by the Telegram bot to build snapshots."""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from decimal import Decimal

import structlog

from core.exchange.bybit import BybitClient
from core.services import repository
from tgbot.formatters import (
    BalanceSnapshot,
    DigestSnapshot,
    OrderRow,
    OrdersSnapshot,
    PnlSnapshot,
    StatusSnapshot,
)

log = structlog.get_logger()


async def status_snapshot() -> StatusSnapshot:
    """Build the /status snapshot."""
    (
        paused,
        open_count,
        started_at,
        last_heartbeat,
    ) = await repository.status_data()
    return StatusSnapshot(
        paused=paused,
        open_positions=open_count,
        started_at=started_at,
        last_heartbeat=last_heartbeat,
    )


async def pnl_snapshot() -> PnlSnapshot:
    """Build the /pnl snapshot from closed positions.

    ``today`` is since UTC midnight; the rest are rolling from now (last 24
    hours, 7/30/365 days) plus all time.
    """
    now = datetime.now(tz=UTC)
    midnight = now.replace(hour=0, minute=0, second=0, microsecond=0)
    return PnlSnapshot(
        today=await repository.realized_pnl_since(midnight),
        last_24h=await repository.realized_pnl_since(
            now - timedelta(hours=24)
        ),
        last_7d=await repository.realized_pnl_since(now - timedelta(days=7)),
        last_30d=await repository.realized_pnl_since(now - timedelta(days=30)),
        last_365d=await repository.realized_pnl_since(
            now - timedelta(days=365)
        ),
        all_time=await repository.realized_pnl_since(None),
    )


async def pnl_curve_data() -> tuple[
    list[tuple[str, Decimal]], Decimal, list[Decimal], list[date]
]:
    """Chart inputs: daily realized profit, base, locked USDT, and dates.

    Realized PnL of closed trades is bucketed by UTC day (label, sum) to
    match the /pnl caption; ``base_capital`` is the cost basis of the open
    inventory; ``locked`` is the open-inventory cost basis at the end of each
    day; ``dates`` are the UTC days (aligned to the others) for the price line.
    """
    return await repository.pnl_curve_data()


async def daily_ohlc(
    dates: list[date],
) -> list[tuple[float, float, float, float] | None]:
    """Daily OHLC per UTC day, None if that day's candle is missing."""
    if not dates:
        return []
    bars: dict[date, tuple[Decimal, Decimal, Decimal, Decimal]] = {}
    try:
        client = BybitClient.from_settings()
        symbol = await repository.symbol()
        start = datetime(
            dates[0].year, dates[0].month, dates[0].day, tzinfo=UTC
        )
        bars = await client.get_daily_ohlc(
            symbol, int(start.timestamp() * 1000)
        )
    except Exception as exc:
        log.warning("pnl.price_line_failed", error=str(exc)[:100])
    out: list[tuple[float, float, float, float] | None] = []
    for d in dates:
        bar = bars.get(d)
        if bar is None:
            out.append(None)
        else:
            out.append(
                (float(bar[0]), float(bar[1]), float(bar[2]), float(bar[3]))
            )
    return out


async def unlock_estimate() -> tuple[Decimal | None, Decimal]:
    """Days to unlock the locked loss and the avg realized profit per day."""
    price: Decimal | None = None
    try:
        client = BybitClient.from_settings()
        price = await client.get_last_price(await repository.symbol())
    except Exception as exc:
        log.warning("pnl.price_fetch_failed", error=str(exc)[:100])
    return await repository.unlock_from_db(price)


async def orders_snapshot() -> OrdersSnapshot:
    """Build the /orders snapshot from open positions."""
    rows = [
        OrderRow(
            level_index=level_index,
            entry_price=entry_price,
            qty=qty,
            tp_price=tp_price,
        )
        for level_index, entry_price, qty, tp_price in (
            await repository.orders_data()
        )
    ]
    return OrdersSnapshot(open_positions=rows)


async def digest_snapshot() -> DigestSnapshot:
    """Build the daily digest snapshot (DB plus live price)."""
    db = await repository.digest_metrics()
    client = BybitClient.from_settings()
    free_usdt = Decimal(0)
    price: Decimal | None = None
    try:
        balances = await client.get_balances()
        usdt = balances.get("USDT")
        if usdt is not None:
            free_usdt = usdt.free
        price = await client.get_last_price(await repository.symbol())
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


async def balance_snapshot() -> BalanceSnapshot:
    """Build the /balance snapshot from wallet balances."""
    client = BybitClient.from_settings()
    balances = await client.get_balances()
    return BalanceSnapshot(
        balances={coin: b.free for coin, b in balances.items() if b.total > 0}
    )
