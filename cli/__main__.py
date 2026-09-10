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
from core.services.runtime import INSTANCE_LEASE_S, another_instance_alive
from core.strategy.lattice import PercentGeometry, percent_lattice
from core.strategy.rounding import round_up_to_tick

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


async def _refuse_if_trader_live(clash: str) -> None:
    """Stop an operator command that would fight a running trader."""
    beat = await repository.last_heartbeat()
    now = datetime.now(tz=UTC)
    if not another_instance_alive(beat, now, INSTANCE_LEASE_S):
        return
    age = "unknown" if beat is None else f"{(now - beat).total_seconds():.0f}s"
    typer.echo(
        f"trader is live (heartbeat {age} ago) — stop it first,"
        f" otherwise both will {clash}",
        err=True,
    )
    raise typer.Exit(1)


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
    await _refuse_if_trader_live("move the same orders")
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


def _echo_rungs(geometry: PercentGeometry, price: Decimal) -> None:
    """Print the first buy rungs, with the gap and take-profit of each.

    Gaps run rung to rung from the rung under market, which is where the
    band starts; the take-profit is what a fill on that rung would rest.
    """
    lattice = geometry.lattice
    typer.echo("\n  buy rungs below market:")
    upper = lattice.snap_down(price)
    for _ in range(_PREVIEW_RUNGS):
        rung = lattice.below(upper)
        if rung <= 0:
            return
        gap = (upper - rung) / upper * 100
        tp = round_up_to_tick(geometry.tp_target(rung), lattice.tick)
        profit = (tp - rung) / tp * 100
        typer.echo(f"    {rung}  (gap {gap:.4f}%, TP {tp} = +{profit:.4f}%)")
        upper = rung


def _echo_milestones(geometry: PercentGeometry, price: Decimal) -> None:
    """Print how many rungs it takes to fall to each depth."""
    lattice = geometry.lattice
    top = lattice.index_of(price)
    typer.echo("\n  depth:")
    for share in _MILESTONES:
        index = lattice.index_of(price * share)
        typer.echo(
            f"    {(1 - share) * 100:>2.0f}% down: rung {top - index}"
            f" @ {lattice.price_at(index)}"
        )


def _ratios(step_raw: str, tp_raw: str) -> tuple[Decimal, Decimal]:
    """The step and profit fractions to preview, settings by default."""
    defaults = grid_settings()
    step = Decimal(step_raw) if step_raw else defaults.step_pct
    profit = Decimal(tp_raw) if tp_raw else defaults.profit_pct
    for name, value in (("step", step), ("profit", profit)):
        if not 0 < value < 1:
            typer.echo(f"{name} must be a fraction in (0, 1)", err=True)
            raise typer.Exit(1)
    return step, profit


async def _grid_geometry(
    *, apply_it: bool, step_raw: str, tp_raw: str
) -> None:
    """Preview the percent grid and optionally make it the live config."""
    step, profit = _ratios(step_raw, tp_raw)
    config = await repository.load_config()
    client = BybitClient.from_settings()
    symbol = str(config.symbol)
    instrument = await client.get_instrument(symbol)
    price = await client.get_last_price(symbol)
    geometry = PercentGeometry(
        lattice=percent_lattice(step, instrument.tick_size), tp_ratio=profit
    )

    typer.echo(f"=== PERCENT GRID · {symbol} ===")
    typer.echo(
        f"step {step} ({step * 100:.4f}% between buys) ·"
        f" profit {profit} ({profit * 100:.4f}% per trade)"
    )
    typer.echo(f"tick {instrument.tick_size} · price {price}")
    typer.echo(
        f"{geometry.lattice.index_of(price) + 1} rung(s) from market"
        f" down to {geometry.lattice.price_at(0)}"
    )
    _echo_rungs(geometry, price)
    _echo_milestones(geometry, price)

    typer.echo(
        f"\nlive config: mode={config.grid_mode} step={config.grid_step}"
        f" tp_step={config.tp_step}"
    )
    if not apply_it:
        typer.echo("\nDRY-RUN — nothing changed. Re-run with --apply.")
        return
    await repository.update_config(
        actor="cli",
        updates={
            "grid_mode": "percent",
            "grid_step": step,
            "tp_step": profit,
        },
    )
    typer.echo(
        "\napplied — both steps are fractions now."
        " Restart the trader to rebuild the grid."
    )


@app.command()
def grid_geometry(
    step: str = typer.Option(
        "",
        "--step",
        help="Fraction between buy rungs (default: GRID_STEP_PCT).",
    ),
    tp: str = typer.Option(
        "",
        "--tp",
        help="Profit fraction per trade (default: GRID_PROFIT_PCT).",
    ),
    apply_it: bool = typer.Option(
        False, "--apply", help="Write both fractions into the live config."
    ),
) -> None:
    """Show, and optionally apply, the percent grid's two fractions."""
    asyncio.run(_grid_geometry(apply_it=apply_it, step_raw=step, tp_raw=tp))


async def _lease_held() -> bool:
    """Whether a live trader's heartbeat still holds the instance lease."""
    beat = await repository.last_heartbeat()
    now = datetime.now(tz=UTC)
    held = another_instance_alive(beat, now, INSTANCE_LEASE_S)
    age = "never" if beat is None else f"{(now - beat).total_seconds():.0f}s"
    state = "held" if held else "clear"
    typer.echo(
        f"lease {state} (heartbeat {age} old, lease {INSTANCE_LEASE_S}s)"
    )
    return held


@app.command()
def trader_lease() -> None:
    """Exit 0 when a trader may start, 1 while the lease is still held.

    A killed trader leaves its own heartbeat behind, and the instance
    guard reads that as a live peer until the lease runs out.
    """
    if asyncio.run(_lease_held()):
        raise typer.Exit(1)


async def _adopt(*, commit: bool, entry_raw: str) -> None:
    """Report, and optionally open, lots over coin the book misses."""
    from core.services.adopt import SpareAdopter, plan_adoption
    from core.services.balances import spare_coin
    from core.services.events import NoOpEventBus
    from core.services.order_manager import OrderManager

    await _refuse_if_trader_live("place sells over the same coin")
    config = await repository.load_config()
    client = BybitClient.from_settings()
    symbol = str(config.symbol)
    instrument = await client.get_instrument(symbol)
    entry = (
        Decimal(entry_raw)
        if entry_raw
        else await client.get_last_price(symbol)
    )
    balances = await client.get_balances()
    positions = await repository.open_positions()
    spare = spare_coin(balances, positions, instrument.base_coin)
    plan = plan_adoption(
        spare=spare,
        entry_price=entry,
        order_qty_quote=config.order_qty_quote,
        instrument=instrument,
    )

    typer.echo("\n=== ADOPTION PLAN ===")
    typer.echo(
        f"{instrument.base_coin}: {spare} outside the book"
        f" ({len(positions)} open lot(s) hold the rest)"
    )
    if plan.lots <= 0:
        typer.echo("nothing to adopt — under one lot.")
        return
    om = OrderManager(
        client=client,
        instrument=instrument,
        config=config,
        bus=NoOpEventBus(),
    )
    adopter = SpareAdopter(om, NoOpEventBus())
    typer.echo(
        f"  {plan.lots} lot(s) x {plan.lot_qty} @ entry {entry}"
        f" = {plan.total_qty} (~${plan.total_qty * entry:.2f})"
    )
    typer.echo(f"  leftover left alone: {plan.leftover}")
    if not commit:
        typer.echo("\nDRY-RUN — nothing changed. Re-run with --commit.")
        return
    opened = await adopter.commit(plan, limit=plan.lots)
    typer.echo(f"\nAdopted {opened} lot(s).")


@app.command()
def adopt(
    commit: bool = typer.Option(
        False, help="Open the lots and rest their sells (default: dry-run)."
    ),
    entry: str = typer.Option(
        "", "--entry", help="Entry price to book (default: market)."
    ),
) -> None:
    """Put coin the book does not cover back to work as grid lots."""
    asyncio.run(_adopt(commit=commit, entry_raw=entry))


if __name__ == "__main__":
    app()
