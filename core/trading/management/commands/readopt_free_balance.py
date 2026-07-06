"""Re-adopt untracked (free) base coin at its REAL entry price.

Base coin can end up free and unmanaged — a grid buy that filled while the trader
was down, or a fill-handling bug. This reconstructs the actual purchase prices of
the currently-held inventory by FIFO-matching the fill history, takes the lowest-
priced lots up to the free balance, and re-adopts each as a position whose
take-profit sits one ``tp_step`` above its **real** entry (never below break-even).

Dry-run by default; pass ``--commit`` to place the sells and write the positions.

    uv run python manage.py readopt_free_balance            # preview
    uv run python manage.py readopt_free_balance --commit   # execute
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

from asgiref.sync import sync_to_async
from django.core.management.base import BaseCommand, CommandParser
from django.db import transaction

from core.config.settings import bybit_settings
from core.exchange.bybit import BybitClient
from core.exchange.types import Side
from core.strategy.pricing import compute_tp_price
from core.strategy.reconstruction import Fill, fifo_residual, select_free_lots
from core.strategy.rounding import round_up_to_tick
from core.trading.models import Position, PositionStatus, StrategyConfig

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


class Command(BaseCommand):
    help = "Re-adopt free base coin at its real (FIFO-reconstructed) entry, TP = entry + tp_step."

    def add_arguments(self, parser: CommandParser) -> None:
        parser.add_argument("--commit", action="store_true", help="Place orders and write rows.")

    def handle(self, *args: Any, **options: Any) -> None:
        asyncio.run(self._run(commit=options["commit"]))

    async def _run(self, *, commit: bool) -> None:
        creds = bybit_settings()
        if not creds.api_key or not creds.api_secret:
            self.stdout.write(self.style.ERROR("BYBIT_API_KEY / SECRET not set."))
            raise SystemExit(1)
        client = BybitClient.from_credentials(
            creds.api_key, creds.api_secret, testnet=creds.testnet
        )

        config = await sync_to_async(StrategyConfig.load)()
        symbol = str(config.symbol)
        instrument = await client.get_instrument(symbol)
        balances = await client.get_balances()
        free_base = (
            balances[instrument.base_coin].free if instrument.base_coin in balances else Decimal(0)
        ) * DUST_KEEP
        price = await client.get_last_price(symbol)
        # Never rest a sell at/below market — PostOnly would reject it, and if it did
        # fill it would be an unrecorded taker. Floor every TP one tick above market.
        market_floor = round_up_to_tick(price + instrument.tick_size, instrument.tick_size)
        execs = await client.get_executions(symbol, limit=200)
        execs = sorted(execs, key=lambda e: e.executed_at)
        fills = [Fill(side=e.side.value, price=e.price, qty=e.qty) for e in execs]

        residual = fifo_residual(fills)
        free_lots = select_free_lots(residual, free_base)

        self.stdout.write(f"symbol={symbol} free={free_base} tp_step={config.tp_step}")
        self.stdout.write(
            "reconstructed residual lots (real entries): "
            + ", ".join(f"{p}:{q}" for p, q in sorted(residual))
        )

        base_level = await sync_to_async(_next_level)()
        plan: list[Planned] = []
        for i, (entry, qty) in enumerate(free_lots):
            qty = _floor(qty, instrument.lot_size)
            if qty < instrument.min_order_qty:
                continue
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
                self.stdout.write(f"  skip dust lot {qty}@{entry} (< min notional)")
                continue
            plan.append(Planned(level_index=base_level + i, entry=entry, qty=qty, tp_price=tp))

        self.stdout.write(
            self.style.MIGRATE_HEADING("\n=== RE-ADOPT PLAN (TP = real entry + tp_step) ===")
        )
        for p in plan:
            self.stdout.write(
                f"  L{p.level_index}  {p.qty} @ entry {p.entry} -> TP {p.tp_price} "
                f"(=${p.qty * p.tp_price:.2f})"
            )
        self.stdout.write(f"total: {len(plan)} position(s), {sum(p.qty for p in plan)} base coin")

        if not commit:
            self.stdout.write(
                self.style.WARNING("\nDRY-RUN — nothing placed. Re-run with --commit.")
            )
            return

        stamp = int(datetime.now(tz=UTC).timestamp() * 1000)
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
            self.stdout.write(f"placed L{p.level_index}: {order_id}")
        self.stdout.write(self.style.SUCCESS(f"\nRe-adopted {len(plan)} position(s)."))


def _floor(qty: Decimal, lot: Decimal) -> Decimal:
    return (qty / lot).to_integral_value(rounding="ROUND_FLOOR") * lot


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
