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
from collections.abc import Iterable
from datetime import UTC, datetime, timedelta
from decimal import ROUND_HALF_UP, Decimal
from typing import cast

import structlog
from asgiref.sync import sync_to_async

from core.config.settings import bybit_settings, trader_settings
from core.exchange.bybit import BybitClient
from core.exchange.dry_run import DryRunBybitClient
from core.exchange.types import Execution, Side
from core.exchange.ws import BybitPrivateStream, StreamEvent
from core.services.events import EventBus, NoOpEventBus
from core.services.order_manager import OrderManager
from core.services.readopt import commit_readopt, plan_free_readopt
from core.services.reconciliation import reconcile_once
from core.strategy.grid import generate_levels
from core.trading.models import (
    BotStatus,
    ExecutionLog,
    GridLevel,
    LevelStatus,
    Position,
    PositionStatus,
    StrategyConfig,
)

log = structlog.get_logger()

RECONCILE_INTERVAL_S = 30
# Only heal a position whose TP order is missing if it's older than this — so a
# freshly-opened position's just-placed TP is never raced and double-protected.
_NAKED_MIN_AGE_S = 120


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
        await self._rebuild_grid_on_param_change()
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
        # Prune ONLY buys stranded below the grid (price rose away from them) so their
        # capital can redeploy near the market. A resting buy at or above the band
        # bottom is either in-band or one a falling market is dropping toward — it must
        # be left to FILL, never cancelled and re-chased lower (that would forfeit the
        # very dip it was placed to catch).
        prune = set(buys_to_prune(resting.keys(), target_prices))
        for p, (k, order_id) in list(resting.items()):
            if p not in prune:
                continue
            cancelled = False
            try:
                await self._om.client.cancel_order(self._om.symbol, order_id)
                cancelled = True
            except Exception as exc:
                # "order does not exist" ⇒ it already filled/cancelled; idle the stale
                # level anyway so it doesn't linger as phantom drift.
                if "170213" not in str(exc) and "does not exist" not in str(exc).lower():
                    log.warning("grid.prune_failed", price=str(p), error=str(exc))
                    continue
            await sync_to_async(_idle_level)(k)
            log.info("grid.pruned", price=str(p))
            if cancelled:  # announce only a real cancel (a vanished order likely filled)
                await self._bus.publish("order.cancelled", {"price": str(p)})
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
                await self._recover_missed_fills()  # replay fills dropped by WS hiccups
                await self._heal_naked_positions()  # settle/reprotect positions with a lost TP
                await self._heal_stale_buy_levels()  # unstick levels whose buy vanished
                await self._sweep_free_inventory()  # re-adopt free base coin (partial fills)
                await self._ensure_grid()  # self-heal the band even without fills
            except Exception as exc:
                log.exception("reconcile.error", error=str(exc))
            try:
                await asyncio.wait_for(self._stop.wait(), timeout=RECONCILE_INTERVAL_S)
            except TimeoutError:
                continue

    async def _sweep_free_inventory(self) -> None:
        """Re-adopt base coin left free by partial TP fills into managed positions.

        A partially-filled sell leaves the unsold remainder untracked in the wallet;
        left alone it accrues as dead inventory. This reconstructs its real entry and
        rests a fresh take-profit above market — the automatic form of the
        ``readopt_free_balance`` command. Serialised with grid maintenance so the two
        never place orders against a stale balance snapshot.
        """
        assert self._om is not None
        async with self._grid_lock:
            plan = await plan_free_readopt(
                client=self._om.client,
                config=self._om.config,
                instrument=self._om.instrument,
                price=self._current_price,
            )
            if not plan:
                return
            placed = await commit_readopt(
                client=self._om.client,
                symbol=self._om.symbol,
                config=self._om.config,
                plan=plan,
            )
        for p in placed:
            log.info(
                "readopt.adopted",
                level=p.level_index,
                qty=str(p.qty),
                entry=str(p.entry),
                tp=str(p.tp_price),
            )
            await self._bus.publish(
                "position.opened",
                {
                    "level": p.level_index,
                    "entry_price": str(p.entry),
                    "tp_price": str(p.tp_price),
                },
            )

    async def _rebuild_grid_on_param_change(self) -> None:
        """Tear the buy grid down and rebuild it when grid geometry changed.

        Live orders are never resized in place, so after ``grid_step`` or
        ``order_qty_quote`` changes the band would otherwise carry a mix of old- and
        new-sized buys. When the config diverges from the last-applied geometry we
        cancel **every resting buy** (SELL take-profits are left untouched) and idle
        their levels; the following ``_ensure_grid`` then lays a fresh, uniform grid.
        """
        assert self._om is not None
        cfg = self._om.config
        if not await sync_to_async(_grid_params_changed)(cfg.grid_step, cfg.order_qty_quote):
            return
        log.warning(
            "grid.params_changed_rebuild",
            grid_step=str(cfg.grid_step),
            order_qty=str(cfg.order_qty_quote),
        )
        for order in await self._om.client.get_open_orders(self._om.symbol):
            if order.side != Side.BUY:
                continue  # take-profits (SELL) keep working as before
            try:
                await self._om.client.cancel_order(self._om.symbol, order.order_id)
            except Exception as exc:  # pragma: no cover - depends on live API
                log.warning("grid.rebuild_cancel_failed", order_id=order.order_id, error=str(exc))
        await sync_to_async(_reset_all_grid_levels)()
        await sync_to_async(_record_applied_grid_params)(cfg.grid_step, cfg.order_qty_quote)

    async def _heal_naked_positions(self) -> None:
        """Settle or reprotect open positions whose take-profit order left the exchange.

        A position's protective sell can vanish — an interrupted compensation cancel,
        or a fill dropped beyond the recent-execution window (leaving it phantom-open).
        For each open position older than the guard window (so a freshly-placed TP is
        never raced) whose TP order is no longer live, we look up that order's own
        executions: if it SOLD, replay the fill to close it properly (idempotent on
        exec_id); if it did not, re-place a protective TP so the coin is never naked.
        """
        assert self._om is not None
        candidates = await sync_to_async(_naked_candidates)(_NAKED_MIN_AGE_S)
        if not candidates:
            return
        orders = await self._om.client.get_open_orders(self._om.symbol)
        live = {o.order_id for o in orders}
        for pos_id, tp_order_id in naked_positions(candidates, live):
            try:
                execs = await self._om.client.get_order_executions(self._om.symbol, tp_order_id)
            except Exception as exc:  # pragma: no cover - depends on live API
                log.warning("heal.naked_lookup_failed", id=pos_id, error=str(exc)[:100])
                continue
            sells = [e for e in execs if e.side == Side.SELL]
            if sells:  # the TP filled unseen — replay to close with the real PnL
                for execution in sells:
                    if await sync_to_async(_exec_logged)(execution.exec_id):
                        continue
                    log.warning("heal.naked_settle", id=pos_id, order_id=tp_order_id)
                    await self._om.handle_sell_fill(execution, self._current_price)
                continue
            # No fill: the TP was cancelled/lost but the coin is still held — reprotect.
            pos = await sync_to_async(_get_open_position)(pos_id)
            if pos is None:
                continue
            log.warning("heal.naked_reprotect", id=pos_id, order_id=tp_order_id)
            try:
                await self._om.reprotect(pos, self._current_price)
            except Exception as exc:  # pragma: no cover - depends on live API
                msg = str(exc)
                if "170131" in msg or "insufficient" in msg.lower():
                    # Can't place the sell — the coin is gone: the TP filled under a
                    # superseded order id we couldn't trace. Settle the phantom-open
                    # position (book it at its TP) instead of retrying forever.
                    log.warning("heal.naked_settle_phantom", id=pos_id)
                    await self._om.settle_phantom(pos)
                else:
                    log.error("heal.reprotect_failed", id=pos_id, error=msg[:100])

    async def _heal_stale_buy_levels(self) -> None:
        """Unstick grid levels whose buy order left the exchange unseen.

        A level marked ``awaiting_fill`` still points at its buy order, so the grid
        treats the level as covered and never re-places it. If that order was
        cancelled during a restart or filled while the WS was down, the level is a
        permanent hole (``reconcile.drift`` keeps flagging ``missing_buys``). Here we
        cross-check every awaiting level against the live open orders: a vanished
        order that actually FILLED is replayed as a buy (booking the position); one
        with no fill is idled so the grid re-places it next pass.
        """
        assert self._om is not None
        awaiting = await sync_to_async(_awaiting_buy_levels)()
        if not awaiting:
            return
        orders = await self._om.client.get_open_orders(self._om.symbol)
        open_ids = {o.order_id for o in orders}
        if all(oid in open_ids for _, oid in awaiting):
            return  # every level still resting — nothing vanished
        stale_ids = {oid for _, oid in awaiting if oid not in open_ids}
        fills_by_order: dict[str, Execution] = {}
        for execution in await self._om.client.get_executions(self._om.symbol, limit=100):
            if execution.side == Side.BUY and execution.order_id in stale_ids:
                fills_by_order.setdefault(execution.order_id, execution)
        idle, replay = plan_level_heal(awaiting, open_ids, fills_by_order)
        for idx in idle:
            log.warning("grid.heal_idle_stale_level", level=idx)
            await sync_to_async(_idle_level)(idx)
        for idx, fill in replay:
            if await sync_to_async(_exec_logged)(fill.exec_id):
                # already booked into a position — just clear the stale marker
                await sync_to_async(_idle_level)(idx)
                continue
            log.warning("grid.heal_replaying_buy", level=idx, order_id=fill.order_id)
            await self._om.handle_buy_fill(fill)

    async def _recover_missed_fills(self) -> None:
        """Replay TP fills the WS stream dropped (e.g. on a connection reset).

        A sell execution matching an open position's TP order that we never logged
        is fed back through the normal fill path — idempotent on ``exec_id``, so it
        closes the phantom-open position with the correct PnL (and compensation).
        """
        assert self._om is not None
        tp_ids = await sync_to_async(_open_tp_order_ids)()
        if not tp_ids:
            return
        for execution in await self._om.client.get_executions(self._om.symbol, limit=100):
            if execution.side != Side.SELL or execution.order_id not in tp_ids:
                continue
            if await sync_to_async(_exec_logged)(execution.exec_id):
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


def resting_buy_levels(
    price: Decimal, step: Decimal, count: int, held: set[Decimal]
) -> list[tuple[int, Decimal]]:
    """The ``count`` highest step-aligned prices below ``price`` that we don't
    already hold.

    Walks round levels down from just below the market, skipping levels already
    covered by an open position, until ``count`` resting-buy levels are collected.
    This keeps a constant number of live buy orders — when one fills, the next
    deeper unheld level takes its place. Levels at/below zero stop the walk.

    The topmost buy keeps a **full ``step`` of clearance** below the market: it sits
    at least one step under the price, so a level only earns a buy once the market
    has risen a full step above it (price 0.02978 → top 0.02970; the 0.02975 level
    gets a buy only at price 0.02980).
    """
    if step <= 0 or price <= 0 or count <= 0:
        return []
    k_floor = int(price / step)
    if Decimal(k_floor) * step > price:  # guard against any rounding overshoot
        k_floor -= 1
    k_top = k_floor - 1  # one full step of clearance below the market
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


def naked_positions(
    candidates: list[tuple[int, str]], live_order_ids: set[str]
) -> list[tuple[int, str]]:
    """Of the (position_id, tp_order_id) candidates, those whose TP order is no longer
    live on the exchange — i.e. positions left without a resting protective sell."""
    return [(pid, oid) for pid, oid in candidates if oid not in live_order_ids]


def _naked_candidates(min_age_seconds: int) -> list[tuple[int, str]]:
    cutoff = datetime.now(tz=UTC) - timedelta(seconds=min_age_seconds)
    return [
        (int(pid), str(oid))
        for pid, oid in Position.objects.filter(status=PositionStatus.OPEN, opened_at__lt=cutoff)
        .exclude(tp_order_id="")
        .values_list("id", "tp_order_id")
    ]


def _get_open_position(pos_id: int) -> Position | None:
    return Position.objects.filter(id=pos_id, status=PositionStatus.OPEN).first()


def buys_to_prune(resting_prices: Iterable[Decimal], target_prices: set[Decimal]) -> list[Decimal]:
    """Resting buy prices to cancel: only those stranded strictly below the grid.

    The band bottom is the deepest target level. Buys below it sit uselessly deep
    (the market rose away from them) — cancel to redeploy near price. Buys at or
    above the band bottom are in-band, or above it where a falling market will fill
    them, so they are kept. With no targets (no capital) nothing is pruned.
    """
    if not target_prices:
        return []
    bottom = min(target_prices)
    return [p for p in resting_prices if p < bottom]


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


def _idle_level(level_index: int) -> None:
    GridLevel.objects.filter(level_index=level_index).update(
        status=LevelStatus.IDLE, current_buy_order_id=""
    )


def _grid_params_changed(grid_step: Decimal, order_qty: Decimal) -> bool:
    """Whether grid geometry differs from what the buy grid was last built with.

    On the very first run the applied values are unset — we adopt the current geometry
    without forcing a rebuild (the existing grid already matches the live config).
    """
    bot = BotStatus.load()
    if bot.applied_grid_step is None or bot.applied_order_qty is None:
        bot.applied_grid_step = grid_step
        bot.applied_order_qty = order_qty
        bot.save(update_fields=["applied_grid_step", "applied_order_qty"])
        return False
    return bot.applied_grid_step != grid_step or bot.applied_order_qty != order_qty


def _reset_all_grid_levels() -> None:
    GridLevel.objects.filter(status=LevelStatus.AWAITING_FILL).update(
        status=LevelStatus.IDLE, current_buy_order_id=""
    )


def _record_applied_grid_params(grid_step: Decimal, order_qty: Decimal) -> None:
    bot = BotStatus.load()
    bot.applied_grid_step = grid_step
    bot.applied_order_qty = order_qty
    bot.save(update_fields=["applied_grid_step", "applied_order_qty"])


def _awaiting_buy_levels() -> list[tuple[int, str]]:
    """(level_index, order_id) for every grid level still expecting a buy fill."""
    return [
        (int(idx), oid)
        for idx, oid in GridLevel.objects.filter(status=LevelStatus.AWAITING_FILL)
        .exclude(current_buy_order_id="")
        .values_list("level_index", "current_buy_order_id")
    ]


def plan_level_heal(
    awaiting: list[tuple[int, str]],
    open_order_ids: set[str],
    fills_by_order: dict[str, Execution],
) -> tuple[list[int], list[tuple[int, Execution]]]:
    """Classify awaiting-fill levels whose buy order is no longer on the exchange.

    A level still on the exchange is healthy and skipped. A vanished order that has
    a matching fill is scheduled for **replay** (it filled unseen → book the
    position); one with no fill was cancelled/lost and is scheduled to be **idled**
    so the grid re-places it. Returns ``(idle_indices, [(level_index, fill)])``.
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


def _open_tp_order_ids() -> set[str]:
    return set(
        Position.objects.filter(status=PositionStatus.OPEN)
        .exclude(tp_order_id="")
        .values_list("tp_order_id", flat=True)
    )


def _exec_logged(exec_id: str) -> bool:
    return ExecutionLog.objects.filter(exec_id=exec_id).exists()


def _is_paused() -> bool:
    return bool(BotStatus.load().paused)
