"""Sweep free (untracked) base coin back into managed positions.

Partial TP fills, WS-dropped fills, or manual moves leave base coin sitting free
in the wallet — bought, but not covered by any open position or resting sell.
This reconstructs the real entry prices of the held inventory (FIFO over the fill
history), takes the free amount as its cheapest lots, and re-adopts each lot as a
position whose take-profit rests one ``tp_step`` above its real entry (and never
below break-even, below market, or under the exchange's minimum notional).

The same logic runs from the ``readopt_free_balance`` management command (manual,
with a dry-run preview) and from the trader's reconcile loop (automatic).
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import ROUND_FLOOR, Decimal

import structlog
from asgiref.sync import sync_to_async
from django.db import transaction

from core.exchange.bybit import BybitClient
from core.exchange.types import Instrument, Side
from core.strategy.pricing import compute_tp_price
from core.strategy.reconstruction import Fill, fifo_residual, select_free_lots
from core.strategy.rounding import round_up_to_tick
from core.trading.models import Position, PositionStatus, StrategyConfig

log = structlog.get_logger()

# Re-adopted positions live above the manual bag (>=1000) at their own base.
READOPT_LEVEL_BASE = 2000
# Leave a sliver of base coin unsold to absorb rounding / fee dust.
DUST_KEEP = Decimal("0.999")


@dataclass
class Planned:
    level_index: int
    entry: Decimal
    qty: Decimal
    tp_price: Decimal


async def plan_free_readopt(
    *,
    client: BybitClient,
    config: StrategyConfig,
    instrument: Instrument,
    price: Decimal,
) -> list[Planned]:
    """Build re-adopt lots for the currently-free base coin. No side effects.

    Cheap early-out: if the free balance cannot fund even one minimum-notional
    sell, return ``[]`` without pulling the (heavier) execution history.
    """
    balances = await client.get_balances()
    bal = balances.get(instrument.base_coin)
    free = (bal.free if bal is not None else Decimal(0)) * DUST_KEEP
    if free * price < instrument.min_order_amt:
        return []

    execs = sorted(
        await client.get_executions(instrument.symbol, limit=200),
        key=lambda e: e.executed_at,
    )
    fills = [Fill(side=e.side.value, price=e.price, qty=e.qty) for e in execs]
    free_lots = select_free_lots(fifo_residual(fills), free)

    # Never rest a sell at/below market — PostOnly would reject it (and a taker
    # fill would go unrecorded). Floor every TP one tick above market.
    market_floor = round_up_to_tick(price + instrument.tick_size, instrument.tick_size)
    base_level = await sync_to_async(_next_level)()

    plan: list[Planned] = []
    for entry, raw_qty in free_lots:
        qty = _floor(raw_qty, instrument.lot_size)
        if qty < instrument.min_order_qty:
            continue
        # Do NOT pass min_order_amt here: lifting the TP to reach the minimum
        # notional would price a dust lot absurdly far above market (e.g. a 15-coin
        # lot needing a +900% price to clear $5). Instead compute the honest TP and
        # skip the lot if its notional is still sub-minimum — dust stays free.
        tp = compute_tp_price(
            entry_price=entry,
            qty=qty,
            fees_in=entry * qty * config.maker_fee,
            tp_step=config.tp_step,
            min_profit_quote=config.min_profit_quote,
            maker_fee=config.maker_fee,
            tick_size=instrument.tick_size,
        )
        tp = max(tp, market_floor)
        if qty * tp < instrument.min_order_amt:
            continue
        plan.append(Planned(level_index=base_level + len(plan), entry=entry, qty=qty, tp_price=tp))
    return plan


async def commit_readopt(
    *,
    client: BybitClient,
    symbol: str,
    config: StrategyConfig,
    plan: list[Planned],
) -> list[Planned]:
    """Place a protective sell for each planned lot and persist the position."""
    stamp = int(datetime.now(tz=UTC).timestamp() * 1000)
    placed: list[Planned] = []
    for p in plan:
        order_id = await client.place_limit(
            symbol,
            Side.SELL,
            p.qty,
            p.tp_price,
            order_link_id=f"readopt-{p.level_index}-{stamp}",
        )
        await sync_to_async(_write_position)(
            planned=p, tp_order_id=order_id, maker_fee=config.maker_fee
        )
        placed.append(p)
    return placed


def _floor(qty: Decimal, lot: Decimal) -> Decimal:
    return (qty / lot).to_integral_value(rounding=ROUND_FLOOR) * lot


def _next_level() -> int:
    last = (
        Position.objects.filter(level_index__gte=READOPT_LEVEL_BASE)
        .order_by("-level_index")
        .values_list("level_index", flat=True)
        .first()
    )
    return READOPT_LEVEL_BASE if last is None else int(last) + 1


def _write_position(*, planned: Planned, tp_order_id: str, maker_fee: Decimal) -> None:
    with transaction.atomic():
        Position.objects.create(
            level_index=planned.level_index,
            entry_price=planned.entry,
            qty=planned.qty,
            fees_in=planned.entry * planned.qty * maker_fee,
            tp_order_id=tp_order_id,
            tp_price=planned.tp_price,
            status=PositionStatus.OPEN,
            opened_at=datetime.now(tz=UTC),
        )
