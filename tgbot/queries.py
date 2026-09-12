"""Read-side queries used by the Telegram bot to build snapshots."""

from __future__ import annotations

from collections.abc import Mapping
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal

import structlog

from core.exchange.bybit import BybitClient
from core.exchange.types import Side
from core.services import repository
from core.services.order_common import config_geometry
from core.strategy.book import BookLevel, build_ladder
from core.strategy.lattice import nearest_rung
from tgbot.formatters import (
    AprSnapshot,
    DigestSnapshot,
    PnlSnapshot,
)

log = structlog.get_logger()

Bar = tuple[float, float, float, float, float]

_PROJECTION_DAYS = 10
_TRANSFER_BACKFILL = 400


async def pnl_snapshot() -> PnlSnapshot:
    """Build the /pnl snapshot from closed positions.

    Reports banked profit, matching the chart's green line: a close the
    pool paid for never claws back profit already reported. ``today``
    is since UTC midnight; the rest roll back from now.
    """
    now = datetime.now(tz=UTC)
    midnight = now.replace(hour=0, minute=0, second=0, microsecond=0)
    return PnlSnapshot(
        today=await repository.banked_pnl_since(midnight),
        last_24h=await repository.banked_pnl_since(now - timedelta(hours=24)),
        last_7d=await repository.banked_pnl_since(now - timedelta(days=7)),
        last_30d=await repository.banked_pnl_since(now - timedelta(days=30)),
        last_365d=await repository.banked_pnl_since(now - timedelta(days=365)),
        all_time=await repository.banked_pnl_since(None),
    )


async def pnl_curve_data() -> tuple[
    list[tuple[str, Decimal]],
    Decimal,
    list[Decimal],
    list[date],
    list[Decimal],
]:
    """Chart inputs: daily profit kept, pool balance, base, locked, dates.

    Closes are bucketed by UTC day: ``days`` is the share that stays in
    the pocket, ``pool`` what the credit pool held at the end of that
    day. ``base_capital`` is the cost basis of the open inventory;
    ``locked`` is that basis at the end of each day; ``dates`` are the
    UTC days for the price line.
    """
    return await repository.pnl_curve_data()


async def funds_curve(dates: list[date]) -> list[Decimal]:
    """Profit banked for good, accumulated from zero, per chart day.

    Only the pocket half of each close counts, so deposits and the
    pool's compensation spending leave it alone. Funding is still
    synced first because the digest's projection needs it, but a
    failure there costs nothing here.
    """
    if not dates:
        return []
    try:
        await _sync_transfers(BybitClient.from_settings())
    except Exception as exc:
        log.warning("pnl.transfer_sync_failed", error=str(exc)[:100])
    series = await repository.banked_value_series(dates)
    return [value for _, value in series]


async def _sync_transfers(client: BybitClient) -> None:
    """Pull funding movements the database has not seen yet."""
    last = await repository.last_transfer_at()
    start = last or (datetime.now(tz=UTC) - timedelta(days=_TRANSFER_BACKFILL))
    now = datetime.now(tz=UTC)
    if start >= now:
        return
    rows = await client.get_transfers(
        int(start.timestamp() * 1000), int(now.timestamp() * 1000)
    )
    stored = await repository.record_transfers(rows)
    if stored:
        log.info("pnl.transfers_recorded", count=stored)


async def daily_ohlc(
    dates: list[date], symbol: str | None = None
) -> list[Bar | None]:
    """Daily OHLC plus volume per UTC day, None if the candle is missing.

    Defaults to the configured trading symbol; pass ``symbol`` for another.
    """
    if not dates:
        return []
    bars: Mapping[date, tuple[Decimal, ...]] = {}
    try:
        client = BybitClient.from_settings()
        sym = symbol or await repository.symbol()
        start = datetime(
            dates[0].year, dates[0].month, dates[0].day, tzinfo=UTC
        )
        bars = await client.get_daily_ohlc(sym, int(start.timestamp() * 1000))
    except Exception as exc:
        log.warning(
            "pnl.price_line_failed", error=str(exc)[:100], symbol=symbol
        )
    out: list[Bar | None] = []
    for d in dates:
        bar = bars.get(d)
        if bar is None:
            out.append(None)
        else:
            out.append(
                (
                    float(bar[0]),
                    float(bar[1]),
                    float(bar[2]),
                    float(bar[3]),
                    float(bar[4]),
                )
            )
    return out


def rescale_ohlc(
    ohlc: list[Bar | None], ref: list[Bar | None]
) -> list[Bar | None]:
    """Scale ``ohlc`` so its first candle closes on ``ref``'s first close.

    Aligns the two series at the earliest day both have a candle, so their
    relative moves (correlation) read on one price axis. Returns ``ohlc``
    unchanged if there is no shared day or the close is non-positive.
    """
    factor: float | None = None
    for cand, base in zip(ohlc, ref, strict=False):
        if cand is not None and base is not None and cand[3] > 0:
            factor = base[3] / cand[3]
            break
    if factor is None:
        return ohlc
    f = factor
    return [
        None if b is None else (b[0] * f, b[1] * f, b[2] * f, b[3] * f, b[4])
        for b in ohlc
    ]


async def btc_daily_ohlc(
    dates: list[date], ref_ohlc: list[Bar | None]
) -> list[Bar | None]:
    """BTCUSDT daily candles scaled to align first close with ``ref_ohlc``."""
    raw = await daily_ohlc(dates, "BTCUSDT")
    return rescale_ohlc(raw, ref_ohlc)


async def unlock_estimate() -> tuple[Decimal | None, Decimal]:
    """Days to unlock the locked loss and the avg realized profit per day."""
    price: Decimal | None = None
    try:
        client = BybitClient.from_settings()
        price = await client.get_last_price(await repository.symbol())
    except Exception as exc:
        log.warning("pnl.price_fetch_failed", error=str(exc)[:100])
    return await repository.unlock_from_db(price)


async def account_equity() -> Decimal | None:
    """What the whole account is worth right now, in USDT.

    Cash and coin alike, resting orders included, valued at the last
    price — the figure the exchange's own dashboard shows. ``None``
    when the exchange cannot be reached, so the report drops the line
    rather than inventing a balance.
    """
    try:
        client = BybitClient.from_settings()
        symbol = await repository.symbol()
        balances = await client.get_balances()
        price = await client.get_last_price(symbol)
    except Exception as exc:
        log.warning("pnl.equity_failed", error=str(exc)[:100])
        return None
    total = Decimal(0)
    for coin, balance in balances.items():
        total += balance.total * (Decimal(1) if coin == "USDT" else price)
    return total


async def book_snapshot() -> tuple[list[BookLevel], Decimal, str] | None:
    """Resting orders as a ladder, with the price and symbol to label it.

    Read from the exchange rather than the database: the ladder should
    show what is actually resting, including anything the bot has not
    booked yet. Levels the grid already holds a lot on are marked below
    market, where they explain a buy that is deliberately not there;
    above market the buy band does not reach, so a held level would say
    nothing the take-profit resting over it does not already say.
    ``None`` when the exchange cannot be reached.
    """
    try:
        client = BybitClient.from_settings()
        symbol = await repository.symbol()
        orders = await client.get_open_orders(symbol)
        price = await client.get_last_price(symbol)
        instrument = await client.get_instrument(symbol)
    except Exception as exc:
        log.warning("book.fetch_failed", error=str(exc)[:100])
        return None
    config = await repository.load_config()
    geometry = config_geometry(config, instrument.tick_size)
    positions = await repository.open_positions()
    held: dict[Decimal, Decimal] = {}
    for position in positions:
        rung = nearest_rung(geometry.lattice, position.entry_price)
        if rung >= price:
            continue
        held[rung] = held.get(rung, Decimal(0)) + position.remaining_qty
    rungs = build_ladder(
        [(order.price, order.qty, order.side == Side.BUY) for order in orders],
        geometry.lattice,
        held,
    )
    return rungs, price, symbol


async def digest_snapshot() -> DigestSnapshot:
    """Build the daily digest snapshot (DB plus live price)."""
    db = await repository.digest_metrics()
    client = BybitClient.from_settings()
    free_usdt = Decimal(0)
    total_usdt = Decimal(0)
    price: Decimal | None = None
    try:
        balances = await client.get_balances()
        usdt = balances.get("USDT")
        if usdt is not None:
            free_usdt = usdt.free
            total_usdt = usdt.total
        price = await client.get_last_price(await repository.symbol())
    except Exception as exc:
        log.warning("digest.live_fetch_failed", error=str(exc)[:100])
    projection = [
        value
        for _, value in await repository.tp_projection_series(
            total_usdt, _PROJECTION_DAYS
        )
    ]
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
        tp_projection=projection,
    )


async def apr_estimate() -> AprSnapshot:
    """Assemble the /apr estimate: realized rate over avg committed capital."""
    realized, days, avg_deployed = await repository.profit_rate_data()
    free = Decimal(0)
    try:
        client = BybitClient.from_settings()
        balances = await client.get_balances()
        usdt = balances.get("USDT")
        if usdt is not None:
            free = usdt.free
    except Exception as exc:
        log.warning("apr.balance_fetch_failed", error=str(exc)[:100])
    profit_per_day = realized / days if days > 0 else Decimal(0)
    committed = avg_deployed + free
    apr = (
        profit_per_day * 365 / committed * 100
        if realized > 0 and committed > 0
        else None
    )
    return AprSnapshot(
        realized=realized,
        days=days,
        avg_deployed=avg_deployed,
        free=free,
        profit_per_day=profit_per_day,
        apr=apr,
    )
