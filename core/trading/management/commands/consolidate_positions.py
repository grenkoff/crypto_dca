"""Merge duplicate open positions sitting at the same price into one lot.

Historical races left some prices carrying two or more open positions, each
with its own resting sell — the very stacking the one-buy-per-level rule now
prevents. This collapses each such group into a single position (cost-weighted
entry, one take-profit over the combined quantity), cancelling the group's
sells and placing one merged sell in their place.

Dry-run by default; pass ``--commit`` to cancel/replace orders and rewrite
rows. Run with the trader STOPPED so its heal loop does not race the cancels.

uv run python manage.py consolidate_positions            # preview uv run
python manage.py consolidate_positions --commit    # execute
"""

from __future__ import annotations

import asyncio
from typing import Any

from asgiref.sync import sync_to_async
from django.core.management.base import BaseCommand, CommandParser

from core.config.settings import bybit_settings
from core.exchange.bybit import BybitClient
from core.services.consolidate import (
    commit_consolidation,
    load_open_positions,
    plan_consolidation,
)
from core.trading.models import StrategyConfig


class Command(BaseCommand):
    help = (
        "Collapse duplicate open positions at the same price into a "
        "single lot + sell."
    )

    def add_arguments(self, parser: CommandParser) -> None:
        parser.add_argument(
            "--commit", action="store_true", help="Cancel/replace and rewrite."
        )

    def handle(self, *args: Any, **options: Any) -> None:
        asyncio.run(self._run(commit=options["commit"]))

    async def _run(self, *, commit: bool) -> None:
        creds = bybit_settings()
        if not creds.api_key or not creds.api_secret:
            self.stdout.write(
                self.style.ERROR("BYBIT_API_KEY / SECRET not set.")
            )
            raise SystemExit(1)
        client = BybitClient.from_credentials(
            creds.api_key, creds.api_secret, testnet=creds.testnet
        )

        config = await sync_to_async(StrategyConfig.load)()
        symbol = str(config.symbol)
        instrument = await client.get_instrument(symbol)
        price = await client.get_last_price(symbol)

        positions = await load_open_positions()
        plan = plan_consolidation(
            positions=positions,
            step=config.grid_step,
            tp_step=config.tp_step,
            min_profit_quote=config.min_profit_quote,
            maker_fee=config.maker_fee,
            tick_size=instrument.tick_size,
            min_order_amt=instrument.min_order_amt,
            market_price=price,
        )

        self.stdout.write(
            self.style.MIGRATE_HEADING("\n=== CONSOLIDATION PLAN ===")
        )
        if not plan:
            self.stdout.write(
                "no duplicate-price positions — nothing to consolidate."
            )
            return
        for g in plan:
            self.stdout.write(
                f"  @ {g.price_key}: keep #{g.survivor_id}, "
                f"absorb {g.absorbed_ids} -> {g.combined_qty} "
                f"@ entry {g.weighted_entry} -> TP {g.new_tp_price} "
                f"(=${g.combined_qty * g.new_tp_price:.2f}), "
                f"cancel {len(g.cancel_order_ids)} sell(s)"
            )
        absorbed = sum(len(g.absorbed_ids) for g in plan)
        self.stdout.write(
            f"total: {len(plan)} group(s), {absorbed} position(s) absorbed"
        )

        if not commit:
            self.stdout.write(
                self.style.WARNING(
                    "\nDRY-RUN — nothing changed. Re-run with --commit."
                )
            )
            return

        done = await commit_consolidation(
            client=client, symbol=symbol, config=config, plan=plan
        )
        for g in done:
            self.stdout.write(
                f"merged @ {g.price_key}: "
                f"#{g.survivor_id} now {g.combined_qty}"
            )
        self.stdout.write(
            self.style.SUCCESS(f"\nConsolidated {len(done)} group(s).")
        )
