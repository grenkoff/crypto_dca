"""Trader runtime: ties the client, WS stream and OrderManager into a
live loop (bootstrap, then run's event/reconcile loops, then shutdown).
"""

from __future__ import annotations

import asyncio
import signal
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import cast

import structlog
from asgiref.sync import sync_to_async

from core.config.settings import bybit_settings, trader_settings
from core.exchange.bybit import BybitClient
from core.exchange.dry_run import DryRunBybitClient
from core.exchange.types import Side
from core.exchange.ws import BybitPrivateStream, StreamEvent
from core.services.events import EventBus, NoOpEventBus
from core.services.grid_maintainer import GridMaintainer
from core.services.healer import Healer
from core.services.order_manager import OrderManager
from core.services.reconciliation import reconcile_once
from core.trading.models import (
    BotStatus,
    StrategyConfig,
)

log = structlog.get_logger()

RECONCILE_INTERVAL_S = 30
_INSTANCE_LEASE_S = RECONCILE_INTERVAL_S * 3


def another_instance_alive(
    last_heartbeat: datetime | None, now: datetime, lease_s: int
) -> bool:
    """Whether another trader is still running, judged by a fresh heartbeat.

    ``None`` (never started) or a stale heartbeat (older than the lease ⇒ the
    writer crashed) means no live peer — safe to start.
    """
    if last_heartbeat is None:
        return False
    return (now - last_heartbeat) < timedelta(seconds=lease_s)


class TraderRuntime:
    """Ties the client, WS stream and OrderManager into a live loop."""

    def __init__(self, *, bus: EventBus | None = None) -> None:
        self._bus: EventBus = bus or NoOpEventBus()
        self._stop = asyncio.Event()
        self._om: OrderManager | None = None
        self._client: BybitClient | None = None
        self._stream: BybitPrivateStream | None = None
        self._current_price = Decimal(0)
        self._grid: GridMaintainer | None = None
        self._healer: Healer | None = None

    async def bootstrap(self) -> None:
        """Load config, build the OrderManager, lay the initial grid."""
        await self._guard_single_instance()
        real_client = BybitClient.from_settings()
        if trader_settings().dry_run:
            log.warning("trader.dry_run_enabled")
            self._client = cast(BybitClient, DryRunBybitClient(real_client))
        else:
            self._client = real_client
        config = await sync_to_async(StrategyConfig.load)()
        instrument = await self._client.get_instrument(str(config.symbol))
        self._current_price = await self._client.get_last_price(
            str(config.symbol)
        )
        self._om = OrderManager(
            client=self._client,
            instrument=instrument,
            config=config,
            bus=self._bus,
        )
        self._grid = GridMaintainer(self._om, self._bus)
        self._healer = Healer(self._om)
        await self._mark_started()
        log.info(
            "trader.bootstrap",
            symbol=config.symbol,
            current_price=str(self._current_price),
            tick=str(instrument.tick_size),
        )
        await self._grid.rebuild_on_param_change()
        await self._grid.ensure(self._current_price)

    async def run(self) -> None:
        """Open the WS and run the event and reconcile loops."""
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
        """Stop the loops and close the WS."""
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
            except Exception as exc:
                log.exception(
                    "trader.event_error", error=str(exc), kind=event.kind
                )

    async def _handle_event(self, event: StreamEvent) -> None:
        assert (
            self._om is not None
            and self._client is not None
            and self._grid is not None
        )
        if event.kind != "execution":
            return
        from core.exchange.types import Execution as BybitExecution

        if not isinstance(event.payload, BybitExecution):
            return
        self._current_price = await self._client.get_last_price(
            self._om.symbol
        )
        if event.payload.side == Side.BUY:
            await self._om.handle_buy_fill(event.payload)
        else:
            await self._om.handle_sell_fill(event.payload, self._current_price)
        await self._grid.ensure(self._current_price)

    async def _reconcile_loop(self) -> None:
        assert (
            self._client is not None
            and self._om is not None
            and self._grid is not None
            and self._healer is not None
        )
        while not self._stop.is_set():
            try:
                self._current_price = await self._client.get_last_price(
                    self._om.symbol
                )
                await reconcile_once(self._client, self._om.symbol)
                await self._healer.heal(self._current_price)
                await self._grid.ensure(self._current_price)
            except Exception as exc:
                log.exception("reconcile.error", error=str(exc))
            try:
                await asyncio.wait_for(
                    self._stop.wait(), timeout=RECONCILE_INTERVAL_S
                )
            except TimeoutError:
                continue

    async def _wait_for_stop(self) -> None:
        await self._stop.wait()
        await self.shutdown()

    def _install_signal_handlers(self) -> None:
        loop = asyncio.get_running_loop()
        for sig in (signal.SIGINT, signal.SIGTERM):
            loop.add_signal_handler(sig, self._stop.set)

    async def _guard_single_instance(self) -> None:
        """Refuse to start if another trader is alive (fresh heartbeat).

        A fresh ``BotStatus`` heartbeat means a peer is running; abort
        rather than lay a second grid. ``TRADER_SKIP_INSTANCE_GUARD``
        bypasses the check.
        """
        if trader_settings().skip_instance_guard:
            return
        last = await sync_to_async(lambda: BotStatus.load().last_heartbeat)()
        if another_instance_alive(
            last, datetime.now(tz=UTC), _INSTANCE_LEASE_S
        ):
            log.error(
                "trader.instance_guard_blocked", last_heartbeat=str(last)
            )
            raise RuntimeError(
                "another trader appears to be running (fresh heartbeat) — "
                "refusing to start a second instance. Stop it first, or "
                "set TRADER_SKIP_INSTANCE_GUARD=1 if the host guarantees "
                "one instance."
            )

    async def _mark_started(self) -> None:
        def _persist() -> None:
            status = BotStatus.load()
            status.started_at = datetime.now(tz=UTC)
            status.last_error = ""
            status.save()

        await sync_to_async(_persist)()
