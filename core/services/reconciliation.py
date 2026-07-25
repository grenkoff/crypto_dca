"""Periodic reconciliation: detect drift between DB state and exchange truth.

MVP: log discrepancies only. Future iterations can auto-repair (e.g., re-place
missing buys, cancel orphan orders, update positions for missed WS events).
"""

from __future__ import annotations

import structlog

from core.exchange.bybit import BybitClient
from core.services import repository

log = structlog.get_logger()


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
    else:
        log.debug("reconcile.clean", **summary)

    await repository.heartbeat()
    return summary
