"""Adopt manually-held spot inventory into the bot as OPEN positions.

Two sources are folded into the strategy so pairwise compensation can work them
toward break-even out of future realized profit:

1. **Existing sell orders** already resting on the exchange become positions
   linked to those orders (no new orders are placed).
2. **Naked base balance** (coins held with no sell order) is chunked and gets
   fresh take-profit sell orders priced at the guaranteed-profit minimum.

Because the true cost basis of a manual bag is unknown, an ``--entry`` estimate
is supplied. The per-pair realized result of every compensated close equals
``(entry_estimate - true_cost) * qty``, so **over-estimating entry guarantees a
non-negative real outcome**. Pick ``--entry`` at or above the real average.

Dry-run by default (prints the plan, touches nothing). Pass ``--commit`` to
place the naked take-profit orders and write the Position rows.

    uv run python manage.py adopt_positions --entry 0.052            # preview
    uv run python manage.py adopt_positions --entry 0.052 --commit   # execute
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
from core.exchange.dry_run import DryRunBybitClient
from core.exchange.types import Instrument, Order, OrderStatus, Side
from core.strategy.rounding import round_down_to_tick, round_up_to_tick
from core.trading.models import Position, PositionStatus, StrategyConfig

# Adopted positions live outside the bot's own grid index range (0..max_open_orders)
# so they never collide with generated grid levels.
ADOPTED_LEVEL_BASE = 1000

# Leave a sliver of base coin unsold to absorb rounding / fee dust.
DUST_KEEP = Decimal("0.99")


@dataclass
class PlannedPosition:
    kind: str  # "existing" | "naked"
    qty: Decimal
    entry: Decimal
    fees_in: Decimal
    tp_price: Decimal
    tp_order_id: str  # empty for naked until placed
    level_index: int


def _breakeven_tp(entry: Decimal, qty: Decimal, fees_in: Decimal, maker_fee: Decimal) -> Decimal:
    """Lowest sell price whose net PnL is >= 0 against ``entry`` (before min-profit)."""
    return (entry * qty + fees_in) / (qty * (Decimal(1) - maker_fee))


class Command(BaseCommand):
    help = "Adopt manually-held spot inventory (open sells + naked balance) as bot positions."

    def add_arguments(self, parser: CommandParser) -> None:
        parser.add_argument(
            "--entry",
            type=Decimal,
            default=Decimal("0.052"),
            help="Estimated average entry price of the manual bag (over-estimate to stay safe).",
        )
        parser.add_argument(
            "--commit",
            action="store_true",
            help="Actually place naked take-profit orders and write positions (default: dry-run).",
        )
        parser.add_argument(
            "--skip-naked",
            action="store_true",
            help="Only adopt existing sell orders; leave naked balance untouched.",
        )
        parser.add_argument(
            "--force",
            action="store_true",
            help="Proceed even if OPEN positions already exist (re-adoption guard).",
        )

    def handle(self, *args: Any, **options: Any) -> None:
        asyncio.run(self._run(options))

    async def _run(self, opts: dict[str, Any]) -> None:
        entry: Decimal = opts["entry"]
        commit: bool = opts["commit"]

        existing_open = await sync_to_async(_open_position_count)()
        if existing_open and not opts["force"]:
            self.stdout.write(
                self.style.ERROR(
                    f"{existing_open} OPEN position(s) already exist. "
                    "Re-running would duplicate them. Pass --force only if you know why."
                )
            )
            raise SystemExit(1)

        creds = bybit_settings()
        if not creds.api_key or not creds.api_secret:
            self.stdout.write(self.style.ERROR("BYBIT_API_KEY / SECRET not set."))
            raise SystemExit(1)

        real = BybitClient.from_credentials(creds.api_key, creds.api_secret, testnet=creds.testnet)
        client: BybitClient = real if commit else DryRunBybitClient(real)  # type: ignore[assignment]

        config = await sync_to_async(StrategyConfig.load)()
        symbol = str(config.symbol)
        maker_fee: Decimal = config.maker_fee
        instrument = await real.get_instrument(symbol)
        price = await real.get_last_price(symbol)
        orders = await real.get_open_orders(symbol)
        balances = await real.get_balances()

        # Only resting limit sells hold inventory; skip conditional/untriggered orders.
        resting = {OrderStatus.NEW, OrderStatus.PARTIALLY_FILLED}
        sells = [o for o in orders if o.side == Side.SELL and o.status in resting]
        free_base = (
            balances[instrument.base_coin].free if instrument.base_coin in balances else Decimal(0)
        )

        self.stdout.write(
            f"symbol={symbol} price={price} entry_estimate={entry} maker_fee={maker_fee}"
        )
        self.stdout.write(
            f"instrument: tick={instrument.tick_size} lot={instrument.lot_size} "
            f"min_qty={instrument.min_order_qty} min_amt={instrument.min_order_amt}"
        )
        self._sanity(entry, price, sells)

        plan: list[PlannedPosition] = []
        level = ADOPTED_LEVEL_BASE

        # 1. Existing sell orders -> positions bound to those orders.
        for o in sells:
            fees_in = entry * o.qty * maker_fee
            plan.append(
                PlannedPosition(
                    kind="existing",
                    qty=o.qty,
                    entry=entry,
                    fees_in=fees_in,
                    tp_price=o.price,
                    tp_order_id=o.order_id,
                    level_index=level,
                )
            )
            level += 1

        # 2. Naked base balance -> chunked positions with fresh TP sells.
        naked_chunks: list[Decimal] = []
        if not opts["skip_naked"]:
            naked_chunks = _chunk_naked(
                free_base=free_base * DUST_KEEP,
                target_notional=config.order_qty_quote,
                entry=entry,
                instrument=instrument,
            )
            for qty in naked_chunks:
                fees_in = entry * qty * maker_fee
                raw_tp = _breakeven_tp(entry, qty, fees_in, maker_fee) + (
                    config.min_profit_quote / qty
                )
                tp_price = round_up_to_tick(raw_tp, instrument.tick_size)
                plan.append(
                    PlannedPosition(
                        kind="naked",
                        qty=qty,
                        entry=entry,
                        fees_in=fees_in,
                        tp_price=tp_price,
                        tp_order_id="",
                        level_index=level,
                    )
                )
                level += 1

        self._print_plan(plan, instrument, price, free_base)

        if not commit:
            self.stdout.write(
                self.style.WARNING(
                    "\nDRY-RUN: nothing placed or written. Re-run with --commit to execute."
                )
            )
            return

        await self._commit(plan, client, symbol, instrument)

    def _sanity(self, entry: Decimal, price: Decimal, sells: list[Order]) -> None:
        if entry <= price:
            self.stdout.write(
                self.style.ERROR(
                    f"entry_estimate {entry} <= current price {price}: the bag is not underwater "
                    "at this estimate; adoption math is meaningless. Aborting."
                )
            )
            raise SystemExit(1)
        below = [o for o in sells if o.price < entry]
        if len(below) < len(sells):
            above = len(sells) - len(below)
            self.stdout.write(
                self.style.WARNING(
                    f"{above} existing sell(s) priced ABOVE entry_estimate {entry} — those close "
                    "in real profit already; fine, just noting."
                )
            )

    def _print_plan(
        self,
        plan: list[PlannedPosition],
        instrument: Instrument,
        price: Decimal,
        free_base: Decimal,
    ) -> None:
        existing = [p for p in plan if p.kind == "existing"]
        naked = [p for p in plan if p.kind == "naked"]
        self.stdout.write("")
        self.stdout.write(self.style.MIGRATE_HEADING("=== ADOPTION PLAN ==="))
        self.stdout.write(
            f"\nExisting sell orders -> {len(existing)} position(s) "
            "(no new orders, just DB records):"
        )
        for p in existing:
            self.stdout.write(
                f"  L{p.level_index}  qty={p.qty}  TP@{p.tp_price} (order {p.tp_order_id[:12]})"
            )
        self.stdout.write(
            f"\nNaked balance -> {len(naked)} new TP sell order(s) at break-even+min_profit:"
        )
        for p in naked:
            self.stdout.write(f"  L{p.level_index}  qty={p.qty}  new TP@{p.tp_price}")

        adopted_qty = sum(p.qty for p in plan)
        naked_qty = sum(p.qty for p in naked)
        self.stdout.write("")
        self.stdout.write(
            f"Totals: {len(plan)} positions, {adopted_qty} {instrument.base_coin} adopted "
            f"({naked_qty} of it into {len(naked)} fresh sells)."
        )
        self.stdout.write(
            f"Free {instrument.base_coin} available: {free_base}; "
            f"after placing naked sells ~{free_base - naked_qty} left as dust."
        )

    async def _commit(
        self,
        plan: list[PlannedPosition],
        client: BybitClient,
        symbol: str,
        instrument: Instrument,
    ) -> None:
        # Place naked TP sells first; capture their order ids before writing rows.
        for p in plan:
            if p.kind == "naked":
                p.tp_order_id = await client.place_limit(
                    symbol,
                    Side.SELL,
                    p.qty,
                    p.tp_price,
                    order_link_id=f"adopt-naked-{p.level_index}",
                )
                self.stdout.write(f"placed naked sell L{p.level_index}: {p.tp_order_id}")

        await sync_to_async(_write_positions)(plan)
        self.stdout.write(self.style.SUCCESS(f"\nAdopted {len(plan)} positions. Done."))


def _chunk_naked(
    *,
    free_base: Decimal,
    target_notional: Decimal,
    entry: Decimal,
    instrument: Instrument,
) -> list[Decimal]:
    """Split free base balance into lot-aligned chunks of ~target_notional each."""
    chunk = round_down_to_tick(target_notional / entry, instrument.lot_size)
    if chunk < instrument.min_order_qty or chunk * entry < instrument.min_order_amt:
        # Bump chunk until it clears both minimums.
        chunk = max(
            instrument.min_order_qty,
            round_up_to_tick(instrument.min_order_amt / entry, instrument.lot_size),
        )
    chunks: list[Decimal] = []
    remaining = round_down_to_tick(free_base, instrument.lot_size)
    while remaining >= chunk:
        chunks.append(chunk)
        remaining -= chunk
    # Fold a valid remainder into a final chunk.
    if remaining >= instrument.min_order_qty and remaining * entry >= instrument.min_order_amt:
        chunks.append(remaining)
    return chunks


def _open_position_count() -> int:
    return Position.objects.filter(status=PositionStatus.OPEN).count()


def _write_positions(plan: list[PlannedPosition]) -> None:
    now = datetime.now(tz=UTC)
    with transaction.atomic():
        for p in plan:
            Position.objects.create(
                level_index=p.level_index,
                entry_price=p.entry,
                qty=p.qty,
                fees_in=p.fees_in,
                tp_order_id=p.tp_order_id,
                tp_price=p.tp_price,
                status=PositionStatus.OPEN,
                opened_at=now,
            )
