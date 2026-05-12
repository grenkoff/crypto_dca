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
from decimal import Decimal

import structlog
from asgiref.sync import sync_to_async

from core.config.settings import bybit_settings
from core.exchange.bybit import BybitClient
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

    async def bootstrap(self) -> None:
        settings = bybit_settings()
        self._client = BybitClient.from_credentials(
            settings.api_key, settings.api_secret, testnet=settings.testnet
        )
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
        await self._ensure_grid_placed()

    async def _ensure_grid_placed(self) -> None:
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
            paused = await sync_to_async(_is_paused)()
            if paused:
                log.info("grid.skip_paused", level=spec.level_index)
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
        # Refresh last price for compensation math
        self._current_price = await self._client.get_last_price(self._om.symbol)
        if event.payload.side == Side.BUY:
            level_index = await self._om.handle_buy_fill(event.payload)
            if level_index is not None:
                await self._maybe_extend_grid()
        else:
            level_index = await self._om.handle_sell_fill(event.payload, self._current_price)
            if level_index is not None:
                # Reactivate the just-vacated grid slot with a fresh buy
                await self._place_buy_at_existing_level(level_index)

    async def _maybe_extend_grid(self) -> None:
        """After a buy fills, place a buy at the next deeper level (if under the cap)."""
        assert self._om is not None
        config = self._om.config
        active = await sync_to_async(_count_active_buys)()
        if active >= config.max_open_orders:
            return
        next_index = await sync_to_async(_next_deeper_level_index)()
        anchor = config.top_anchor if config.top_anchor is not None else self._current_price
        specs = generate_levels(
            top_anchor=anchor,
            mode=self._om.grid_mode,
            step=config.grid_step,
            count=next_index + 1,
            tick_size=self._om.instrument.tick_size,
        )
        if next_index >= len(specs):
            log.info("grid.exhausted", next_index=next_index)
            return
        spec = specs[next_index]
        await self._om.place_buy_at_level(spec.level_index, spec.price)

    async def _place_buy_at_existing_level(self, level_index: int) -> None:
        assert self._om is not None
        level = await GridLevel.objects.aget(level_index=level_index)
        await self._om.place_buy_at_level(level_index, level.target_buy_price)

    async def _reconcile_loop(self) -> None:
        assert self._client is not None and self._om is not None
        while not self._stop.is_set():
            try:
                await reconcile_once(self._client, self._om.symbol)
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


def _count_active_buys() -> int:
    return GridLevel.objects.filter(status=LevelStatus.AWAITING_FILL).count()


def _next_deeper_level_index() -> int:
    last = GridLevel.objects.order_by("-level_index").values_list("level_index", flat=True).first()
    return 0 if last is None else int(last) + 1


def _is_paused() -> bool:
    return bool(BotStatus.load().paused)
