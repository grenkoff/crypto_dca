"""Recovery passes for state the WS stream missed or dropped."""

from __future__ import annotations

from decimal import Decimal

import structlog
from asgiref.sync import sync_to_async

from core.exchange.types import Execution, Side
from core.services import repository
from core.services.order_manager import OrderManager
from core.services.protector import Protector

log = structlog.get_logger()

_NAKED_MIN_AGE_S = 120


def naked_positions(
    candidates: list[tuple[int, str]], live_order_ids: set[str]
) -> list[tuple[int, str]]:
    """Of the (position_id, tp_order_id) candidates, those whose TP order is
    no longer live on the exchange — i.e. positions left without a resting
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


class Healer:
    """Recover naked positions, stale buy levels and dropped TP fills."""

    def __init__(self, om: OrderManager) -> None:
        self._om = om
        self._protector = Protector(
            client=om.client,
            instrument=om.instrument,
            config=om.config,
            bus=om.bus,
        )

    async def heal(self, price: Decimal) -> None:
        """Run every recovery pass in order for the current price."""
        await self.recover_missed_fills(price)
        await self.heal_naked_positions(price)
        await self.heal_stale_buy_levels(price)

    async def heal_naked_positions(self, price: Decimal) -> None:
        """Settle or reprotect open positions whose TP order vanished.

        For each aged open position with no live TP: replay a sell that
        filled unseen, else re-place a protective TP so it is never naked.
        """
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
                    await self._om.handle_sell_fill(execution, price)
                continue
            pos = await sync_to_async(repository.get_open_position)(pos_id)
            if pos is None:
                continue
            log.warning(
                "heal.naked_reprotect", id=pos_id, order_id=tp_order_id
            )
            try:
                await self._protector.reprotect(pos, price)
            except Exception as exc:
                msg = str(exc)
                if "170131" in msg or "insufficient" in msg.lower():
                    log.warning("heal.naked_settle_phantom", id=pos_id)
                    await self._protector.settle_phantom(pos)
                else:
                    log.error(
                        "heal.reprotect_failed", id=pos_id, error=msg[:100]
                    )

    async def heal_stale_buy_levels(self, price: Decimal) -> None:
        """Unstick grid levels whose buy order left the exchange unseen.

        Cross-check awaiting levels vs live orders: replay a vanished order
        that filled, idle one that did not so the grid re-places it.
        """
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

    async def recover_missed_fills(self, price: Decimal) -> None:
        """Replay TP fills the WS stream dropped (e.g. on a reconnect).

        An unlogged sell matching an open position's TP is fed back through
        the normal fill path (idempotent on ``exec_id``) to close it.
        """
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
            await self._om.handle_sell_fill(execution, price)
