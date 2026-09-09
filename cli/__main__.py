"""Operator CLIs (Typer): preflight, consolidate, add-admin.

Django-free replacements for the old management commands. Run with
``python -m cli <command>``.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from decimal import Decimal

import typer

from cli.preflight import FAIL, WARN, run_checks
from core.config.settings import bybit_settings, grid_settings
from core.exchange.bybit import BybitClient
from core.services import repository
from core.services.consolidate import (
    commit_consolidation,
    load_open_positions,
    plan_consolidation,
)
from core.services.order_common import config_geometry
from core.strategy.lattice import Lattice, percent_lattice

app = typer.Typer(add_completion=False, help="crypto_dca operator commands.")


@app.command()
def preflight() -> None:
    """Validate credentials, balance, instrument, Redis, and config."""
    checks = asyncio.run(run_checks())
    for c in checks:
        line = f"{c.status} {c.name}"
        if c.detail:
            line += f": {c.detail}"
        typer.echo(line)
    hard = [c for c in checks if c.status == FAIL]
    if hard:
        typer.echo("")
        typer.echo(f"{len(hard)} hard failure(s)", err=True)
        raise typer.Exit(1)
    warnings = [c for c in checks if c.status == WARN]
    typer.echo("")
    if warnings:
        typer.echo(f"{len(warnings)} warning(s) — review before trading")
    else:
        typer.echo("All checks passed.")


@app.command()
def add_admin(
    chat_id: int = typer.Argument(..., help="Telegram chat_id to allow"),
    label: str = typer.Option("", help="Free-text label"),
) -> None:
    """Add (or upgrade to admin) an allowed Telegram user."""
    created = asyncio.run(repository.upsert_admin(chat_id, label))
    verb = "Created" if created else "Updated"
    typer.echo(f"{verb} admin: {label or chat_id} (admin)")


@app.command()
def consolidate(
    commit: bool = typer.Option(
        False, help="Cancel/replace and rewrite (default: dry-run)."
    ),
) -> None:
    """Merge duplicate same-price open positions into one lot."""
    asyncio.run(_consolidate(commit=commit))


async def _consolidate(*, commit: bool) -> None:
    creds = bybit_settings()
    if not creds.api_key or not creds.api_secret:
        typer.echo("BYBIT_API_KEY / SECRET not set.", err=True)
        raise typer.Exit(1)
    client = BybitClient.from_settings()

    config = await repository.load_config()
    symbol = str(config.symbol)
    instrument = await client.get_instrument(symbol)
    price = await client.get_last_price(symbol)

    positions = await load_open_positions()
    plan = plan_consolidation(
        positions=positions,
        geometry=config_geometry(config, instrument.tick_size),
        min_profit_quote=config.min_profit_quote,
        maker_fee=config.maker_fee,
        tick_size=instrument.tick_size,
        min_order_amt=instrument.min_order_amt,
        market_price=price,
    )

    typer.echo("\n=== CONSOLIDATION PLAN ===")
    if not plan:
        typer.echo("no duplicate-price positions — nothing to consolidate.")
        return
    for g in plan:
        typer.echo(
            f"  @ {g.price_key}: keep #{g.survivor_id}, "
            f"absorb {g.absorbed_ids} -> {g.combined_qty} "
            f"@ entry {g.weighted_entry} -> TP {g.new_tp_price} "
            f"(=${g.combined_qty * g.new_tp_price:.2f}), "
            f"cancel {len(g.cancel_order_ids)} sell(s)"
        )
    absorbed = sum(len(g.absorbed_ids) for g in plan)
    typer.echo(f"total: {len(plan)} group(s), {absorbed} position(s) absorbed")

    if not commit:
        typer.echo("\nDRY-RUN — nothing changed. Re-run with --commit.")
        return

    done = await commit_consolidation(
        client=client, symbol=symbol, config=config, plan=plan
    )
    for g in done:
        typer.echo(
            f"merged @ {g.price_key}: #{g.survivor_id} now {g.combined_qty}"
        )
    typer.echo(f"\nConsolidated {len(done)} group(s).")


async def _compensate(commit: bool) -> None:
    """Report, and optionally make, the moves the pool can fund."""
    from core.config.bootstrap import bootstrap
    from core.services.balances import BalanceCache
    from core.services.compensator import Compensator
    from core.services.events import NoOpEventBus

    bootstrap()
    pool = await repository.pending_credit()
    source = await repository.last_closed_position_id()
    if pool <= 0 or source is None:
        typer.echo(f"pool {pool}, nothing to spend")
        return
    _paused, _open, _started, beat = await repository.status_data()
    if beat is not None:
        age = (datetime.now(tz=UTC) - beat).total_seconds()
        if age < 90:
            typer.echo(
                f"trader is live (heartbeat {age:.0f}s ago) — stop it"
                " first, otherwise both will move the same orders",
                err=True,
            )
            raise typer.Exit(1)
    config = await repository.get_config()
    client = BybitClient.from_settings()
    symbol = str(config.symbol)
    instrument = await client.get_instrument(symbol)
    price = await client.get_last_price(symbol)
    typer.echo(f"pool {pool} · price {price} · source position {source}")
    if not commit:
        typer.echo("dry run — pass --commit to move the take-profits")
        return
    compensator = Compensator(
        client=client,
        instrument=instrument,
        config=config,
        geometry=config_geometry(config, instrument.tick_size),
        bus=NoOpEventBus(),
        balances=BalanceCache(client),
    )
    moves = await compensator.drain_pool(price, source)
    for move in moves:
        typer.echo(
            f"  {move['old_tp']} -> {move['new_tp']} (drew {move['drawn']})"
        )
    left = await repository.pending_credit()
    typer.echo(f"{len(moves)} move(s), pool now {left}")


@app.command()
def compensate(
    commit: bool = typer.Option(
        False, help="Move the take-profits (default: dry-run)."
    ),
) -> None:
    """Spend the banked compensation pool without waiting for a close."""
    asyncio.run(_compensate(commit))


_PREVIEW_RUNGS = 8
_MILESTONES = (
    Decimal("0.90"),
    Decimal("0.75"),
    Decimal("0.50"),
    Decimal("0.25"),
    Decimal("0.10"),
    Decimal("0.05"),
)


def _echo_rungs(lattice: Lattice, price: Decimal) -> None:
    """Print the first buy rungs, with the gap each one leaves.

    Measured rung to rung from the rung under market, which is where the
    band starts: the gap is the profit a fill there takes.
    """
    typer.echo("\n  rungs below market:")
    upper = lattice.snap_down(price)
    for _ in range(_PREVIEW_RUNGS):
        rung = lattice.below(upper)
        if rung <= 0:
            return
        gap = (upper - rung) / upper * 100
        typer.echo(
            f"    {rung}  ({rung / price * 100:.3f}% of market,"
            f" gap {gap:.4f}%)"
        )
        upper = rung


def _echo_milestones(lattice: Lattice, price: Decimal) -> None:
    """Print how many rungs it takes to fall to each depth."""
    top = lattice.index_of(price)
    typer.echo("\n  depth:")
    for share in _MILESTONES:
        index = lattice.index_of(price * share)
        typer.echo(
            f"    {(1 - share) * 100:>2.0f}% down: rung {top - index}"
            f" @ {lattice.price_at(index)}"
        )


async def _grid_profit(*, apply_it: bool, raw: str) -> None:
    """Preview the percent ladder and optionally make it the live config."""
    ratio = Decimal(raw) if raw else grid_settings().profit_pct
    if not 0 < ratio < 1:
        typer.echo("profit must be a fraction in (0, 1)", err=True)
        raise typer.Exit(1)
    config = await repository.load_config()
    client = BybitClient.from_settings()
    symbol = str(config.symbol)
    instrument = await client.get_instrument(symbol)
    price = await client.get_last_price(symbol)
    lattice = percent_lattice(ratio, instrument.tick_size)

    typer.echo(f"=== PERCENT GRID · {symbol} ===")
    typer.echo(
        f"profit {ratio} ({ratio * 100:.4f}% per rung) ·"
        f" tick {instrument.tick_size} · price {price}"
    )
    typer.echo(
        f"{lattice.index_of(price) + 1} rung(s) from market"
        f" down to {lattice.price_at(0)}"
    )
    _echo_rungs(lattice, price)
    _echo_milestones(lattice, price)

    typer.echo(
        f"\nlive config: mode={config.grid_mode} step={config.grid_step}"
        f" tp_step={config.tp_step}"
    )
    if not apply_it:
        typer.echo("\nDRY-RUN — nothing changed. Re-run with --apply.")
        return
    await repository.update_config(
        actor="cli",
        updates={"grid_mode": "percent", "grid_step": ratio},
    )
    typer.echo(
        "\napplied — take-profits now rest one rung above entry and"
        " tp_step is unused. Restart the trader to rebuild the grid."
    )


@app.command()
def grid_profit(
    set_to: str = typer.Option(
        "",
        "--set",
        help="Profit fraction per rung (default: GRID_PROFIT_PCT).",
    ),
    apply_it: bool = typer.Option(
        False, "--apply", help="Write the ratio into the live config."
    ),
) -> None:
    """Show, and optionally apply, the percent grid's profit step."""
    asyncio.run(_grid_profit(apply_it=apply_it, raw=set_to))


if __name__ == "__main__":
    app()
