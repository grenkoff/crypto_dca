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
from decimal import ROUND_HALF_UP, Decimal
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
        n = int(cfg.max_open_orders)
        if step <= 0 or price <= 0 or n <= 0:
            return

        targets = band_levels(price, step, n)
        target_prices = {p for _, p in targets}

        resting, held = await sync_to_async(_grid_state)(step)
        # Prune resting buys that fell outside the band (market moved up).
        for p, (k, order_id) in list(resting.items()):
            if p in target_prices:
                continue
            try:
                await self._om.client.cancel_order(self._om.symbol, order_id)
            except Exception as exc:
                log.warning("grid.prune_failed", price=str(p), error=str(exc))
                continue
            await sync_to_async(_idle_level)(k)
            log.info("grid.pruned", price=str(p))
        # Fill any missing band levels (skip ones already resting or held).
        for k, p in targets:
            if p in resting or p in held:
                continue
            await self._om.place_buy_at_level(k, p)

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


def band_levels(price: Decimal, step: Decimal, count: int) -> list[tuple[int, Decimal]]:
    """The ``count`` highest step-aligned prices strictly below ``price``.

    Each level is ``(k, k*step)`` with ``k = price // step`` descending, giving a
    contiguous round ladder. Levels at/below zero are dropped.
    """
    if step <= 0 or price <= 0 or count <= 0:
        return []
    k_top = int(price / step)
    if Decimal(k_top) * step >= price:
        k_top -= 1
    levels: list[tuple[int, Decimal]] = []
    for j in range(count):
        k = k_top - j
        p = Decimal(k) * step
        if p <= 0:
            break
        levels.append((k, p))
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
    for entry in Position.objects.filter(
        status=PositionStatus.OPEN, level_index__lt=_ADOPTED_LEVEL_BASE
    ).values_list("entry_price", flat=True):
        k = int((entry / step).to_integral_value(rounding=ROUND_HALF_UP))
        held.add(Decimal(k) * step)
    return resting, held


def _idle_level(level_index: int) -> None:
    GridLevel.objects.filter(level_index=level_index).update(
        status=LevelStatus.IDLE, current_buy_order_id=""
    )


def _is_paused() -> bool:
    return bool(BotStatus.load().paused)
