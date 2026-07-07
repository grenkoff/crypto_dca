"""Re-adopt untracked (free) base coin at its REAL entry price.

Base coin can end up free and unmanaged — a grid buy that filled while the trader
was down, or a partial TP fill. This reconstructs the actual purchase prices of
the currently-held inventory by FIFO-matching the fill history, takes the lowest-
priced lots up to the free balance, and re-adopts each as a position whose take-
profit sits one ``tp_step`` above its **real** entry (never below break-even).

The same sweep runs automatically inside the trader's reconcile loop; this command
is the manual/preview entry point onto the shared logic in ``core.services.readopt``.

Dry-run by default; pass ``--commit`` to place the sells and write the positions.

    uv run python manage.py readopt_free_balance            # preview
    uv run python manage.py readopt_free_balance --commit   # execute
"""

from __future__ import annotations

import asyncio
from typing import Any

from asgiref.sync import sync_to_async
from django.core.management.base import BaseCommand, CommandParser

from core.config.settings import bybit_settings
from core.exchange.bybit import BybitClient
from core.services.readopt import commit_readopt, plan_free_readopt
from core.trading.models import StrategyConfig


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
        price = await client.get_last_price(symbol)

        plan = await plan_free_readopt(
            client=client, config=config, instrument=instrument, price=price
        )

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

        placed = await commit_readopt(client=client, symbol=symbol, config=config, plan=plan)
        for p in placed:
            self.stdout.write(f"placed L{p.level_index}: {p.qty} @ {p.tp_price}")
        self.stdout.write(self.style.SUCCESS(f"\nRe-adopted {len(placed)} position(s)."))
