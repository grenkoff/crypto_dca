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
from core.exchange.types import Execution, Side
from core.exchange.ws import BybitPrivateStream, StreamEvent
from core.services import repository
from core.services.events import EventBus, NoOpEventBus
from core.services.order_manager import OrderManager
from core.services.reconciliation import reconcile_once
from core.strategy.grid import (
    buys_to_prune,
    generate_levels,
    resting_buy_levels,
)
from core.trading.models import (
    BotStatus,
    StrategyConfig,
)

log = structlog.get_logger()

RECONCILE_INTERVAL_S = 30
_NAKED_MIN_AGE_S = 120
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
        self._grid_lock = asyncio.Lock()

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
        await self._mark_started()
        log.info(
            "trader.bootstrap",
            symbol=config.symbol,
            current_price=str(self._current_price),
            tick=str(instrument.tick_size),
        )
        await self._rebuild_grid_on_param_change()
        await self._ensure_grid()

    async def _ensure_grid(self) -> None:
        async with self._grid_lock:
            await self._ensure_grid_impl()

    async def _ensure_grid_impl(self) -> None:
        """Maintain a contiguous band of buy orders below the current price.

        The band is the ``max_open_orders`` highest ``grid_step`` steps
        below market; gaps fill, out-of-band buys prune, held levels skip.
        """
        assert self._om is not None
        if await sync_to_async(repository.is_paused)():
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

        balances = await self._om.client.get_balances()
        quote = balances.get(self._om.instrument.quote_coin)
        total_quote = (
            (quote.free + quote.locked) if quote is not None else Decimal(0)
        )
        n = min(int(total_quote / per_order), int(cfg.max_open_orders))

        resting, held = await sync_to_async(repository.grid_state)(step)
        targets = resting_buy_levels(price, step, n, held)
        target_prices = {p for _, p in targets}
        prune = set(buys_to_prune(resting.keys(), target_prices))
        for p, (k, order_id) in list(resting.items()):
            if p not in prune:
                continue
            cancelled = False
            try:
                await self._om.client.cancel_order(self._om.symbol, order_id)
                cancelled = True
            except Exception as exc:
                if (
                    "170213" not in str(exc)
                    and "does not exist" not in str(exc).lower()
                ):
                    log.warning(
                        "grid.prune_failed", price=str(p), error=str(exc)
                    )
                    continue
            await sync_to_async(repository.idle_level)(k)
            log.info("grid.pruned", price=str(p))
            if cancelled:
                await self._bus.publish("order.cancelled", {"price": str(p)})
        for k, p in targets:
            if p in resting or p in held:
                continue
            try:
                await self._om.place_buy_at_level(k, p)
            except Exception as exc:
                log.warning(
                    "grid.place_skipped", price=str(p), error=str(exc)[:100]
                )

    async def _ensure_grid_percent(self) -> None:
        """Legacy percent-mode grid (relative levels off a moving anchor)."""
        assert self._om is not None
        config = self._om.config
        anchor = (
            config.top_anchor
            if config.top_anchor is not None
            else self._current_price
        )
        specs = generate_levels(
            top_anchor=anchor,
            mode=self._om.grid_mode,
            step=config.grid_step,
            count=config.max_open_orders,
            tick_size=self._om.instrument.tick_size,
        )
        existing = await sync_to_async(repository.existing_active_levels)()
        for spec in specs:
            if spec.level_index in existing:
                continue
            await self._om.place_buy_at_level(spec.level_index, spec.price)

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
        assert self._om is not None and self._client is not None
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
        await self._ensure_grid()

    async def _reconcile_loop(self) -> None:
        assert self._client is not None and self._om is not None
        while not self._stop.is_set():
            try:
                self._current_price = await self._client.get_last_price(
                    self._om.symbol
                )
                await reconcile_once(self._client, self._om.symbol)
                await self._recover_missed_fills()
                await self._heal_naked_positions()
                await self._heal_stale_buy_levels()
                await self._ensure_grid()
            except Exception as exc:
                log.exception("reconcile.error", error=str(exc))
            try:
                await asyncio.wait_for(
                    self._stop.wait(), timeout=RECONCILE_INTERVAL_S
                )
            except TimeoutError:
                continue

    async def _rebuild_grid_on_param_change(self) -> None:
        """Rebuild the buy grid when ``grid_step``/``order_qty_quote`` changed.

        Live orders are never resized, so on a geometry change we cancel every
        resting buy (TP sells untouched) and idle their levels to rebuild.
        """
        assert self._om is not None
        cfg = self._om.config
        if not await sync_to_async(repository.grid_params_changed)(
            cfg.grid_step, cfg.order_qty_quote
        ):
            return
        log.warning(
            "grid.params_changed_rebuild",
            grid_step=str(cfg.grid_step),
            order_qty=str(cfg.order_qty_quote),
        )
        for order in await self._om.client.get_open_orders(self._om.symbol):
            if order.side != Side.BUY:
                continue
            try:
                await self._om.client.cancel_order(
                    self._om.symbol, order.order_id
                )
            except Exception as exc:
                log.warning(
                    "grid.rebuild_cancel_failed",
                    order_id=order.order_id,
                    error=str(exc),
                )
        await sync_to_async(repository.reset_all_grid_levels)()
        await sync_to_async(repository.record_applied_grid_params)(
            cfg.grid_step, cfg.order_qty_quote
        )

    async def _heal_naked_positions(self) -> None:
        """Settle or reprotect open positions whose take-profit order vanished.

        For each aged open position with no live TP: replay a sell that
        filled unseen, else re-place a protective TP so it is never naked.
        """
        assert self._om is not None
        candidates = await sync_to_async(repository.naked_candidates)(
            _NAKED_MIN_AGE_S
        )
        if not candidates:
            return
        orders = await self._om.client.get_open_orders(self._om.symbol)
        live = {o.order_id for o in orders}
        for pos_id, tp_order_id in naked_positions(candidates, live):
            try:
                execs = await self._om.client.get_order_executions(
                    self._om.symbol, tp_order_id
                )
            except Exception as exc:
                log.warning(
                    "heal.naked_lookup_failed", id=pos_id, error=str(exc)[:100]
                )
                continue
            sells = [e for e in execs if e.side == Side.SELL]
            if sells:
                for execution in sells:
                    if await sync_to_async(repository.exec_logged)(
                        execution.exec_id
                    ):
                        continue
                    log.warning(
                        "heal.naked_settle", id=pos_id, order_id=tp_order_id
                    )
                    await self._om.handle_sell_fill(
                        execution, self._current_price
                    )
                continue
            pos = await sync_to_async(repository.get_open_position)(pos_id)
            if pos is None:
                continue
            log.warning(
                "heal.naked_reprotect", id=pos_id, order_id=tp_order_id
            )
            try:
                await self._om.reprotect(pos, self._current_price)
            except Exception as exc:
                msg = str(exc)
                if "170131" in msg or "insufficient" in msg.lower():
                    log.warning("heal.naked_settle_phantom", id=pos_id)
                    await self._om.settle_phantom(pos)
                else:
                    log.error(
                        "heal.reprotect_failed", id=pos_id, error=msg[:100]
                    )

    async def _heal_stale_buy_levels(self) -> None:
        """Unstick grid levels whose buy order left the exchange unseen.

        Cross-check awaiting levels vs live orders: replay a vanished order
        that filled, idle one that did not so the grid re-places it.
        """
        assert self._om is not None
        awaiting = await sync_to_async(repository.awaiting_buy_levels)()
        if not awaiting:
            return
        orders = await self._om.client.get_open_orders(self._om.symbol)
        open_ids = {o.order_id for o in orders}
        if all(oid in open_ids for _, oid in awaiting):
            return
        stale_ids = {oid for _, oid in awaiting if oid not in open_ids}
        fills_by_order: dict[str, Execution] = {}
        for execution in await self._om.client.get_executions(
            self._om.symbol, limit=100
        ):
            if execution.side == Side.BUY and execution.order_id in stale_ids:
                fills_by_order.setdefault(execution.order_id, execution)
        idle, replay = plan_level_heal(awaiting, open_ids, fills_by_order)
        for idx in idle:
            log.warning("grid.heal_idle_stale_level", level=idx)
            await sync_to_async(repository.idle_level)(idx)
        for idx, fill in replay:
            if await sync_to_async(repository.exec_logged)(fill.exec_id):
                await sync_to_async(repository.idle_level)(idx)
                continue
            log.warning(
                "grid.heal_replaying_buy", level=idx, order_id=fill.order_id
            )
            try:
                booked = await self._om.handle_buy_fill(fill)
            except Exception as exc:
                log.warning(
                    "grid.heal_replay_failed", level=idx, error=str(exc)[:120]
                )
                await sync_to_async(repository.idle_level)(idx)
                continue
            if booked is None:
                log.warning(
                    "grid.heal_replay_unbooked_idle",
                    level=idx,
                    order_id=fill.order_id,
                )
                await sync_to_async(repository.idle_level)(idx)

    async def _recover_missed_fills(self) -> None:
        """Replay TP fills the WS stream dropped (e.g. on a reconnect).

        An unlogged sell matching an open position's TP is fed back through the
        normal fill path (idempotent on ``exec_id``) to close it correctly.
        """
        assert self._om is not None
        tp_ids = await sync_to_async(repository.open_tp_order_ids)()
        if not tp_ids:
            return
        for execution in await self._om.client.get_executions(
            self._om.symbol, limit=100
        ):
            if execution.side != Side.SELL or execution.order_id not in tp_ids:
                continue
            if await sync_to_async(repository.exec_logged)(execution.exec_id):
                continue
            log.warning(
                "reconcile.replaying_missed_sell",
                exec_id=execution.exec_id,
                order_id=execution.order_id,
            )
            await self._om.handle_sell_fill(execution, self._current_price)

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


def naked_positions(
    candidates: list[tuple[int, str]], live_order_ids: set[str]
) -> list[tuple[int, str]]:
    """Of the (position_id, tp_order_id) candidates, those whose TP order is no
    longer live on the exchange — i.e. positions left without a resting
    protective sell."""
    return [(pid, oid) for pid, oid in candidates if oid not in live_order_ids]


def plan_level_heal(
    awaiting: list[tuple[int, str]],
    open_order_ids: set[str],
    fills_by_order: dict[str, Execution],
) -> tuple[list[int], list[tuple[int, Execution]]]:
    """Classify awaiting-fill levels whose buy order left the exchange.

    A vanished order with a matching fill is scheduled for replay; one with
    no fill is idled. Returns ``(idle, [(index, fill)])``.
    """
    idle: list[int] = []
    replay: list[tuple[int, Execution]] = []
    for idx, order_id in awaiting:
        if order_id in open_order_ids:
            continue
        fill = fills_by_order.get(order_id)
        if fill is None:
            idle.append(idx)
        else:
            replay.append((idx, fill))
    return idle, replay
