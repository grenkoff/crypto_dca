"""Periodic reconciliation: detect drift between DB and exchange truth.

Drift is logged, and orders the bot placed but never recorded are put
right: a placement can reach the exchange while its database write does
not, leaving an order nobody owns. Such an order carries our own link
id, so it can be handed back to its position or cancelled as a duplicate.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import structlog

from core.exchange.bybit import BybitClient
from core.exchange.types import Order, Side
from core.services import repository
from core.services.order_common import level_from_link

log = structlog.get_logger()

_ADOPT_GRACE_SECONDS = 120


async def reconcile_once(client: BybitClient, symbol: str) -> dict[str, int]:
    """Single pass: compare exchange open orders with DB grid + positions."""
    exchange_orders = await client.get_open_orders(symbol)
    exchange_ids = {o.order_id for o in exchange_orders}

    db_buy_orders = await repository.active_buy_order_ids()
    db_tp_orders = await repository.open_tp_order_ids()

    missing_buys = db_buy_orders - exchange_ids
    missing_tps = db_tp_orders - exchange_ids
    orphan_orders = exchange_ids - db_buy_orders - db_tp_orders

    summary = {
        "exchange_open": len(exchange_orders),
        "db_buys": len(db_buy_orders),
        "db_tps": len(db_tp_orders),
        "missing_buys": len(missing_buys),
        "missing_tps": len(missing_tps),
        "orphans": len(orphan_orders),
    }
    if missing_buys or missing_tps or orphan_orders:
        log.warning("reconcile.drift", **summary)
        await _recover_orphans(
            client,
            symbol,
            [o for o in exchange_orders if o.order_id in orphan_orders],
        )
    else:
        log.debug("reconcile.clean", **summary)

    await repository.heartbeat()
    return summary


async def _recover_orphans(
    client: BybitClient, symbol: str, orphans: list[Order]
) -> None:
    """Hand each unowned order back to its position, or cancel it."""
    fresh = datetime.now(tz=UTC) - timedelta(seconds=_ADOPT_GRACE_SECONDS)
    for order in orphans:
        if order.created_at > fresh:
            continue
        level = level_from_link(order.order_link_id)
        if level is None:
            log.warning(
                "reconcile.foreign_order",
                order_id=order.order_id,
                link_id=order.order_link_id,
            )
            continue
        await _recover_one(client, symbol, order, level)


async def _recover_one(
    client: BybitClient, symbol: str, order: Order, level: int
) -> None:
    """Adopt one stray order if its lot is unprotected, else cancel it."""
    position = (
        await repository.open_position_at_level(level)
        if order.side == Side.SELL
        else None
    )
    if position is not None and not position.tp_order_id:
        await repository.adopt_tp_order(int(position.id), order.order_id)
        log.info(
            "reconcile.tp_adopted",
            position=int(position.id),
            order_id=order.order_id,
        )
        return
    try:
        await client.cancel_order(symbol, order.order_id)
    except Exception as exc:
        log.warning(
            "reconcile.orphan_cancel_failed",
            order_id=order.order_id,
            error=str(exc)[:120],
        )
        return
    log.info(
        "reconcile.orphan_cancelled",
        order_id=order.order_id,
        link_id=order.order_link_id,
        price=str(order.price),
    )
