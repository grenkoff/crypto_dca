"""Validate everything that must be right before the trader starts.

Run on the trader/web shell before the first live deploy. Exits non-zero
on any hard failure; soft warnings only print.
"""

from __future__ import annotations

import asyncio
from decimal import Decimal
from typing import Any, cast

import redis.asyncio as redis_async
from django.core.management.base import BaseCommand

from core.config.settings import bybit_settings, redis_settings
from core.exchange.bybit import BybitClient
from core.trading.models import StrategyConfig

OK = "✓"
WARN = "⚠"
FAIL = "✗"


class Check:
    """A single named preflight check with a status and detail."""

    def __init__(self, name: str) -> None:
        self.name = name
        self.status = OK
        self.detail = ""

    def ok(self, detail: str = "") -> None:
        """Mark the check as passed."""
        self.status = OK
        self.detail = detail

    def warn(self, detail: str) -> None:
        """Mark the check as a warning."""
        self.status = WARN
        self.detail = detail

    def fail(self, detail: str) -> None:
        """Mark the check as failed."""
        self.status = FAIL
        self.detail = detail


class Command(BaseCommand):
    """Validate credentials, balances, instrument, Redis, and config."""

    help = (
        "Validate Bybit credentials, balance, instrument, Redis, and "
        "StrategyConfig sanity."
    )

    def handle(self, *args: Any, **options: Any) -> None:
        """Run all checks and print the results."""
        checks = asyncio.run(_run_all_checks())
        for c in checks:
            line = f"{c.status} {c.name}"
            if c.detail:
                line += f": {c.detail}"
            self.stdout.write(line)
        hard_failures = [c for c in checks if c.status == FAIL]
        if hard_failures:
            self.stdout.write("")
            self.stdout.write(
                self.style.ERROR(f"{len(hard_failures)} hard failure(s)")
            )
            raise SystemExit(1)
        warnings = [c for c in checks if c.status == WARN]
        if warnings:
            self.stdout.write("")
            self.stdout.write(
                self.style.WARNING(
                    f"{len(warnings)} warning(s) — review before trading"
                )
            )
        else:
            self.stdout.write("")
            self.stdout.write(self.style.SUCCESS("All checks passed."))


async def _run_all_checks() -> list[Check]:
    return [
        await _check_strategy_config(),
        *(await _check_bybit_and_balance()),
        await _check_redis(),
    ]


async def _check_strategy_config() -> Check:
    c = Check("strategy config")
    cfg = await asyncio.to_thread(StrategyConfig.load)
    issues: list[str] = []
    if cfg.grid_step <= 0:
        issues.append("grid_step ≤ 0")
    if cfg.grid_mode == "percent" and cfg.grid_step >= 1:
        issues.append("percent step ≥ 1")
    if cfg.order_qty_quote <= 0:
        issues.append("order_qty_quote ≤ 0")
    if cfg.max_open_orders <= 0:
        issues.append("max_open_orders ≤ 0")
    if cfg.maker_fee >= Decimal("0.01"):
        issues.append(f"maker_fee={cfg.maker_fee} unusually high")
    if issues:
        c.fail("; ".join(issues))
    else:
        c.ok(
            f"symbol={cfg.symbol} mode={cfg.grid_mode} step={cfg.grid_step} "
            f"qty={cfg.order_qty_quote} max={cfg.max_open_orders}"
        )
    return c


async def _check_bybit_and_balance() -> list[Check]:
    creds = bybit_settings()
    cfg = await asyncio.to_thread(StrategyConfig.load)

    creds_check = Check("bybit credentials")
    inst_check = Check("instrument fetch")
    balance_check = Check("balance vs grid depth")

    if not creds.api_key or not creds.api_secret:
        creds_check.fail("BYBIT_API_KEY or BYBIT_API_SECRET not set")
        inst_check.fail("skipped — no credentials")
        balance_check.fail("skipped — no credentials")
        return [creds_check, inst_check, balance_check]

    client = BybitClient.from_settings()
    try:
        balances = await client.get_balances()
    except Exception as exc:
        creds_check.fail(f"get_balances raised: {exc}")
        inst_check.fail("skipped — credentials failing")
        balance_check.fail("skipped — credentials failing")
        return [creds_check, inst_check, balance_check]
    creds_check.ok(f"testnet={creds.testnet}")

    try:
        instrument = await client.get_instrument(str(cfg.symbol))
        inst_check.ok(
            f"tick={instrument.tick_size} lot={instrument.lot_size} "
            f"min_amt={instrument.min_order_amt}"
        )
    except Exception as exc:
        inst_check.fail(f"get_instrument({cfg.symbol}) raised: {exc}")
        balance_check.fail("skipped — no instrument")
        return [creds_check, inst_check, balance_check]

    quote_coin = instrument.quote_coin
    quote_balance = balances.get(quote_coin)
    if quote_balance is None:
        balance_check.warn(f"no {quote_coin} balance in account")
        return [creds_check, inst_check, balance_check]

    required = cfg.order_qty_quote * cfg.max_open_orders
    available = quote_balance.free
    if available < cfg.order_qty_quote:
        balance_check.fail(
            f"only {available} {quote_coin} free, "
            f"need ≥{cfg.order_qty_quote} for one order"
        )
    elif available < required:
        balance_check.warn(
            f"{available} {quote_coin} free covers "
            f"~{int(available / cfg.order_qty_quote)} "
            f"of {cfg.max_open_orders} planned levels"
        )
    else:
        balance_check.ok(f"{available} {quote_coin} ≥ {required} required")

    return [creds_check, inst_check, balance_check]


async def _check_redis() -> Check:
    c = Check("redis")
    url = redis_settings().redis_url
    if not url:
        c.warn("REDIS_URL not set — telegram notifications will be a no-op")
        return c
    try:
        client = redis_async.Redis.from_url(url, decode_responses=True)
        pong = await cast(Any, client).ping()
        await client.aclose()
    except Exception as exc:
        c.fail(f"ping failed: {exc}")
        return c
    c.ok(f"ping={pong}")
    return c
