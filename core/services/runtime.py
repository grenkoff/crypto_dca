"""Trader runtime: ties BybitClient, WS stream, and OrderManager into a live loop.

Lifecycle:

1. ``bootstrap``: load config from DB, fetch the instrument, snapshot the
   current price, and ensure the grid is populated with buy orders.
2. ``run``: open the private WebSocket, dispatch execution events to the
   OrderManager, and periodically reconcile state + heartbeat.
3. ``shutdown``: stop the WS, wait for in-flight tasks.
"""

from __future__ import annotations

import asyncio
import signal
from datetime import UTC, datetime
from decimal import ROUND_CEILING, ROUND_HALF_UP, Decimal
from typing import cast

import structlog
from asgiref.sync import sync_to_async

from core.config.settings import bybit_settings, trader_settings
from core.exchange.bybit import BybitClient
from core.exchange.dry_run import DryRunBybitClient
from core.exchange.types import Side
from core.exchange.ws import BybitPrivateStream, StreamEvent
from core.services.events import EventBus, NoOpEventBus
from core.services.order_manager import OrderManager
from core.services.reconciliation import reconcile_once
from core.strategy.grid import generate_levels
from core.strategy.rounding import round_up_to_tick
from core.trading.models import (
    BotStatus,
    GridLevel,
    LevelStatus,
    Position,
    PositionStatus,
    StrategyConfig,
)

log = structlog.get_logger()

RECONCILE_INTERVAL_S = 30
# A gap wider than this many steps splits the near-market TP ladder from the bag.
_NEAR_CLUSTER_GAP_STEPS = 10


class TraderRuntime:
    def __init__(self, *, bus: EventBus | None = None) -> None:
        self._bus: EventBus = bus or NoOpEventBus()
        self._stop = asyncio.Event()
        self._om: OrderManager | None = None
        self._client: BybitClient | None = None
        self._stream: BybitPrivateStream | None = None
        self._current_price = Decimal(0)
        # Serialise grid maintenance: the event handler and the reconcile loop both
        # call _ensure_grid; without this they can race and double-place a level.
        self._grid_lock = asyncio.Lock()

    async def bootstrap(self) -> None:
        settings = bybit_settings()
        real_client = BybitClient.from_credentials(
            settings.api_key, settings.api_secret, testnet=settings.testnet
        )
        if trader_settings().dry_run:
            log.warning("trader.dry_run_enabled")
            self._client = cast(BybitClient, DryRunBybitClient(real_client))
        else:
            self._client = real_client
        config = await sync_to_async(StrategyConfig.load)()
        instrument = await self._client.get_instrument(str(config.symbol))
        self._current_price = await self._client.get_last_price(str(config.symbol))
        self._om = OrderManager(
            client=self._client, instrument=instrument, config=config, bus=self._bus
        )
        await self._mark_started()
        log.info(
            "trader.bootstrap",
            symbol=config.symbol,
            current_price=str(self._current_price),
            tick=str(instrument.tick_size),
        )
        await self._ensure_grid()

    async def _ensure_grid(self) -> None:
        # One grid maintenance pass at a time — callers (event handler, reconcile
        # loop, bootstrap) must not interleave or they double-place levels.
        async with self._grid_lock:
            await self._ensure_grid_impl()

    async def _ensure_grid_impl(self) -> None:
        """Maintain a contiguous band of buy orders at round, step-aligned prices
        just below the current price.

        The band is the ``max_open_orders`` highest multiples of ``grid_step`` that
        sit strictly below the market. A level's identity is its price (index
        ``k = price / step``), so nothing drifts as the market moves: gaps get
        filled, buys outside the band are pruned, and the band tracks the price.
        Held positions (a filled buy awaiting its TP) count as covering their level.
        """
        assert self._om is not None
        if await sync_to_async(_is_paused)():
            return
        if self._om.grid_mode != "absolute":
            await self._ensure_grid_percent()
            return
        cfg = self._om.config
        step: Decimal = cfg.grid_step
        price = self._current_price
        per_order: Decimal = cfg.order_qty_quote
        if step <= 0 or price <= 0 or per_order <= 0:
            return

        # Size the grid to available capital: as many buys as our total USDT can
        # fund (free + already locked in resting buys), capped by max_open_orders as
        # a safety limit. As buys fill into held inventory USDT drops and the grid
        # shrinks; as positions sell it grows back.
        balances = await self._om.client.get_balances()
        quote = balances.get(self._om.instrument.quote_coin)
        total_quote = (quote.free + quote.locked) if quote is not None else Decimal(0)
        n = min(int(total_quote / per_order), int(cfg.max_open_orders))

        resting, held = await sync_to_async(_grid_state)(step)
        targets = resting_buy_levels(price, step, n, held)
        target_prices = {p for _, p in targets}
        # Prune resting buys that fell outside the target set (market moved / held).
        for p, (k, order_id) in list(resting.items()):
            if p in target_prices:
                continue
            try:
                await self._om.client.cancel_order(self._om.symbol, order_id)
            except Exception as exc:
                # "order does not exist" ⇒ it already filled/cancelled; idle the stale
                # level anyway so it doesn't linger as phantom drift.
                if "170213" not in str(exc) and "does not exist" not in str(exc).lower():
                    log.warning("grid.prune_failed", price=str(p), error=str(exc))
                    continue
            await sync_to_async(_idle_level)(k)
            log.info("grid.pruned", price=str(p))
        # Fill any missing band levels (skip ones already resting or held).
        # A single placement failure (e.g. insufficient USDT when capital is fully
        # deployed) must not abort the pass or spam tracebacks — skip and go on.
        for k, p in targets:
            if p in resting or p in held:
                continue
            try:
                await self._om.place_buy_at_level(k, p)
            except Exception as exc:
                log.warning("grid.place_skipped", price=str(p), error=str(exc)[:100])

        await self._ensure_sell_band(price, step)

    async def _ensure_sell_band(self, price: Decimal, step: Decimal) -> None:
        """Keep the near-market take-profit ladder contiguous: fill holes between the
        lowest and highest near TP by buying inventory at market and resting a TP.

        The far-away manual bag is isolated by the cluster-gap split, so only the
        active near-market ladder is kept gap-free. Levels below the fee break-even
        floor are skipped."""
        assert self._om is not None
        cfg = self._om.config
        tick = self._om.instrument.tick_size
        min_sell = round_up_to_tick(
            price * (Decimal(1) + cfg.taker_fee) / (Decimal(1) - cfg.maker_fee), tick
        )
        sells = await sync_to_async(_open_sell_prices)()
        gaps = sell_band_gaps(sells, step, step * _NEAR_CLUSTER_GAP_STEPS, min_sell)
        for tp in gaps:
            try:
                await self._om.seed_sell_level(tp, price)
            except Exception as exc:
                log.warning("sellband.seed_skipped", tp=str(tp), error=str(exc)[:100])

    async def _ensure_grid_percent(self) -> None:
        """Legacy percent-mode grid (relative levels off a moving anchor)."""
        assert self._om is not None
        config = self._om.config
        anchor = config.top_anchor if config.top_anchor is not None else self._current_price
        specs = generate_levels(
            top_anchor=anchor,
            mode=self._om.grid_mode,
            step=config.grid_step,
            count=config.max_open_orders,
            tick_size=self._om.instrument.tick_size,
        )
        existing = await sync_to_async(_existing_active_levels)()
        for spec in specs:
            if spec.level_index in existing:
                continue
            await self._om.place_buy_at_level(spec.level_index, spec.price)

    async def run(self) -> None:
        if self._om is None or self._client is None:
            raise RuntimeError("bootstrap() must be awaited first")
        settings = bybit_settings()
        self._stream = BybitPrivateStream(
            settings.api_key, settings.api_secret, testnet=settings.testnet
        )
        await self._stream.start()
        self._install_signal_handlers()
        log.info("trader.running", symbol=self._om.symbol)
        await asyncio.gather(
            self._dispatch_events(),
            self._reconcile_loop(),
            self._wait_for_stop(),
        )

    async def shutdown(self) -> None:
        self._stop.set()
        if self._stream is not None:
            await self._stream.stop()
        log.info("trader.stopped")

    async def _dispatch_events(self) -> None:
        assert self._stream is not None and self._om is not None
        async for event in self._stream.events():
            if self._stop.is_set():
                break
            try:
                await self._handle_event(event)
            except Exception as exc:  # keep the loop alive
                log.exception("trader.event_error", error=str(exc), kind=event.kind)

    async def _handle_event(self, event: StreamEvent) -> None:
        assert self._om is not None and self._client is not None
        if event.kind != "execution":
            return
        from core.exchange.types import Execution as BybitExecution

        if not isinstance(event.payload, BybitExecution):
            return
        # Refresh last price for compensation + grid maintenance.
        self._current_price = await self._client.get_last_price(self._om.symbol)
        if event.payload.side == Side.BUY:
            await self._om.handle_buy_fill(event.payload)
        else:
            await self._om.handle_sell_fill(event.payload, self._current_price)
        # Re-derive the contiguous band from the fresh price (fills the vacated /
        # newly-deeper level, prunes stale ones).
        await self._ensure_grid()

    async def _reconcile_loop(self) -> None:
        assert self._client is not None and self._om is not None
        while not self._stop.is_set():
            try:
                self._current_price = await self._client.get_last_price(self._om.symbol)
                await reconcile_once(self._client, self._om.symbol)
                await self._ensure_grid()  # self-heal the band even without fills
            except Exception as exc:
                log.exception("reconcile.error", error=str(exc))
            try:
                await asyncio.wait_for(self._stop.wait(), timeout=RECONCILE_INTERVAL_S)
            except TimeoutError:
                continue

    async def _wait_for_stop(self) -> None:
        await self._stop.wait()
        await self.shutdown()

    def _install_signal_handlers(self) -> None:
        loop = asyncio.get_running_loop()
        for sig in (signal.SIGINT, signal.SIGTERM):
            loop.add_signal_handler(sig, self._stop.set)

    async def _mark_started(self) -> None:
        def _persist() -> None:
            status = BotStatus.load()
            status.started_at = datetime.now(tz=UTC)
            status.last_error = ""
            status.save()

        await sync_to_async(_persist)()


def _existing_active_levels() -> set[int]:
    return set(
        GridLevel.objects.filter(status=LevelStatus.AWAITING_FILL).values_list(
            "level_index", flat=True
        )
    ) | set(
        Position.objects.filter(status=PositionStatus.OPEN).values_list("level_index", flat=True)
    )


# Adopted (manual-bag) positions live at level_index >= this; grid levels stay below it.
_ADOPTED_LEVEL_BASE = 1000


def sell_band_gaps(
    sell_prices: list[Decimal],
    step: Decimal,
    max_cluster_gap: Decimal,
    min_sell: Decimal,
) -> list[Decimal]:
    """Empty step-aligned levels inside the near-market take-profit cluster.

    The near cluster is the contiguous run of TPs from the lowest, split at the
    first gap wider than ``max_cluster_gap`` (this isolates the far-away manual
    bag). Returns the round levels between the cluster's low and high that carry no
    sell and sit at/above ``min_sell`` (the fee-break-even floor) — the holes to
    fill by buying at market and resting a take-profit there.
    """
    if step <= 0 or not sell_prices:
        return []
    ordered = sorted(sell_prices)
    cluster = [ordered[0]]
    for p in ordered[1:]:
        if p - cluster[-1] <= max_cluster_gap:
            cluster.append(p)
        else:
            break
    covered = set(cluster)
    hi = cluster[-1]
    gaps: list[Decimal] = []
    k = int((cluster[0] / step).to_integral_value(rounding=ROUND_CEILING))
    while True:
        level = Decimal(k) * step
        if level > hi:
            break
        if level >= min_sell and level not in covered:
            gaps.append(level)
        k += 1
    return gaps


def resting_buy_levels(
    price: Decimal, step: Decimal, count: int, held: set[Decimal]
) -> list[tuple[int, Decimal]]:
    """The ``count`` highest step-aligned prices below ``price`` that we don't
    already hold.

    Walks round levels down from just below the market, skipping levels already
    covered by an open position, until ``count`` resting-buy levels are collected.
    This keeps a constant number of live buy orders — when one fills, the next
    deeper unheld level takes its place. Levels at/below zero stop the walk.
    """
    if step <= 0 or price <= 0 or count <= 0:
        return []
    k_top = int(price / step)
    if Decimal(k_top) * step >= price:
        k_top -= 1
    levels: list[tuple[int, Decimal]] = []
    k = k_top
    while len(levels) < count:
        p = Decimal(k) * step
        if p <= 0:
            break
        if p not in held:
            levels.append((k, p))
        k -= 1
    return levels


def _grid_state(step: Decimal) -> tuple[dict[Decimal, tuple[int, str]], set[Decimal]]:
    """Snapshot for band maintenance: resting buys keyed by price, and the set of
    round prices currently *held* (an open grid position sits there)."""
    resting = {
        g.target_buy_price: (int(g.level_index), g.current_buy_order_id)
        for g in GridLevel.objects.filter(status=LevelStatus.AWAITING_FILL).exclude(
            current_buy_order_id=""
        )
    }
    held: set[Decimal] = set()
    # Only *grid* positions cover a buy level — a filled grid buy sells one step up,
    # so we don't re-buy that level until it clears. Sell-band seeds, re-adopted lots
    # and the manual bag are separate inventory committed to their own (higher) TPs,
    # so they must NOT block the buy grid.
    for entry in Position.objects.filter(
        status=PositionStatus.OPEN, level_index__lt=_ADOPTED_LEVEL_BASE
    ).values_list("entry_price", flat=True):
        k = int((entry / step).to_integral_value(rounding=ROUND_HALF_UP))
        held.add(Decimal(k) * step)
    return resting, held


def _open_sell_prices() -> list[Decimal]:
    return [
        p
        for p in Position.objects.filter(status=PositionStatus.OPEN).values_list(
            "tp_price", flat=True
        )
        if p is not None
    ]


def _idle_level(level_index: int) -> None:
    GridLevel.objects.filter(level_index=level_index).update(
        status=LevelStatus.IDLE, current_buy_order_id=""
    )


def _is_paused() -> bool:
    return bool(BotStatus.load().paused)
