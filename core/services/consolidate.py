"""Merge duplicate open positions at the same price into one lot.

Each group becomes a single position (cost-weighted entry, one TP over the
combined qty). The planner is pure; ``commit_consolidation`` does the
exchange work. Run with the trader stopped.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime
from decimal import ROUND_HALF_UP, Decimal

import structlog
from asgiref.sync import sync_to_async
from django.db import transaction

from core.exchange.bybit import BybitClient
from core.exchange.types import Side
from core.strategy.pricing import compute_tp_price
from core.strategy.rounding import next_tick_above, round_down_to_tick
from core.trading.models import Position, PositionStatus, StrategyConfig

log = structlog.get_logger()


_MANUAL_BAG_MIN = 1000
_MANUAL_BAG_MAX = 2000


@dataclass(frozen=True)
class PosRow:
    """Minimal open-position view the planner needs (keeps it
    pure/testable)."""

    id: int
    level_index: int
    entry: Decimal
    qty: Decimal
    filled_qty: Decimal
    fees_in: Decimal
    tp_order_id: str
    opened_at: datetime


@dataclass
class MergeGroup:
    """A group of same-price positions to merge into one lot."""

    price_key: Decimal
    survivor_id: int
    absorbed_ids: list[int]
    combined_qty: Decimal
    weighted_entry: Decimal
    combined_fees_in: Decimal
    new_tp_price: Decimal
    cancel_order_ids: list[str] = field(default_factory=list)


def plan_consolidation(
    *,
    positions: list[PosRow],
    step: Decimal,
    tp_step: Decimal,
    min_profit_quote: Decimal,
    maker_fee: Decimal,
    tick_size: Decimal,
    min_order_amt: Decimal,
    market_price: Decimal,
) -> list[MergeGroup]:
    """One MergeGroup per price carrying more than one fully-held position.

    Partially-filled positions are excluded. The oldest lot survives; the rest
    are absorbed. The merged sell rests above the recomputed TP and market.
    """
    if step <= 0:
        raise ValueError("step must be positive")
    market_floor = next_tick_above(market_price, tick_size)

    groups: dict[Decimal, list[PosRow]] = {}
    for p in positions:
        if p.filled_qty > 0:
            continue
        if _MANUAL_BAG_MIN <= p.level_index < _MANUAL_BAG_MAX:
            continue
        k = int((p.entry / step).to_integral_value(rounding=ROUND_HALF_UP))
        groups.setdefault(Decimal(k) * step, []).append(p)

    plan: list[MergeGroup] = []
    for price_key, rows in sorted(groups.items()):
        if len(rows) < 2:
            continue
        rows = sorted(rows, key=lambda r: r.opened_at)
        survivor = rows[0]
        combined_qty = sum((r.qty for r in rows), Decimal(0))
        combined_cost = sum((r.entry * r.qty for r in rows), Decimal(0))
        combined_fees_in = sum((r.fees_in for r in rows), Decimal(0))
        weighted_entry = round_down_to_tick(
            combined_cost / combined_qty, tick_size
        )
        tp = compute_tp_price(
            entry_price=weighted_entry,
            qty=combined_qty,
            fees_in=combined_fees_in,
            tp_step=tp_step,
            min_profit_quote=min_profit_quote,
            maker_fee=maker_fee,
            tick_size=tick_size,
            min_order_amt=min_order_amt,
        )
        plan.append(
            MergeGroup(
                price_key=price_key,
                survivor_id=survivor.id,
                absorbed_ids=[r.id for r in rows[1:]],
                combined_qty=combined_qty,
                weighted_entry=weighted_entry,
                combined_fees_in=combined_fees_in,
                new_tp_price=max(tp, market_floor),
                cancel_order_ids=[
                    r.tp_order_id for r in rows if r.tp_order_id
                ],
            )
        )
    return plan


async def load_open_positions() -> list[PosRow]:
    """Load all open positions as ``PosRow`` value objects."""

    @sync_to_async
    def _load() -> list[PosRow]:
        return [
            PosRow(
                id=int(p.id),
                level_index=int(p.level_index),
                entry=p.entry_price,
                qty=p.qty,
                filled_qty=p.filled_qty,
                fees_in=p.fees_in,
                tp_order_id=p.tp_order_id,
                opened_at=p.opened_at,
            )
            for p in Position.objects.filter(status=PositionStatus.OPEN)
        ]

    return await _load()


async def commit_consolidation(
    *,
    client: BybitClient,
    symbol: str,
    config: StrategyConfig,
    plan: list[MergeGroup],
) -> list[MergeGroup]:
    """Cancel each group's sells, place one merged sell, rewrite the DB.

    Run with the trader stopped so its heal loop does not race the cancels.
    """
    stamp = int(datetime.now(tz=UTC).timestamp() * 1000)
    done: list[MergeGroup] = []
    for g in plan:
        for order_id in g.cancel_order_ids:
            try:
                await client.cancel_order(symbol, order_id)
            except Exception as exc:
                log.warning(
                    "consolidate.cancel_failed",
                    order_id=order_id,
                    error=str(exc)[:100],
                )
        new_tp_order_id = await client.place_limit(
            symbol,
            Side.SELL,
            g.combined_qty,
            g.new_tp_price,
            order_link_id=f"consolidate-{g.survivor_id}-{stamp}",
        )
        await sync_to_async(_apply_merge)(
            group=g, new_tp_order_id=new_tp_order_id
        )
        log.info(
            "consolidate.merged",
            price=str(g.price_key),
            survivor=g.survivor_id,
            absorbed=g.absorbed_ids,
            qty=str(g.combined_qty),
            tp=str(g.new_tp_price),
        )
        done.append(g)
    return done


def _apply_merge(*, group: MergeGroup, new_tp_order_id: str) -> None:
    with transaction.atomic():
        survivor = Position.objects.select_for_update().get(
            id=group.survivor_id
        )
        survivor.qty = group.combined_qty
        survivor.entry_price = group.weighted_entry
        survivor.fees_in = group.combined_fees_in
        survivor.tp_order_id = new_tp_order_id
        survivor.tp_price = group.new_tp_price
        survivor.save(
            update_fields=[
                "qty",
                "entry_price",
                "fees_in",
                "tp_order_id",
                "tp_price",
            ]
        )
        Position.objects.filter(id__in=group.absorbed_ids).delete()
