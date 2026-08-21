"""Backtest CLI: fetch history, replay the grid, sweep parameters.

Run with ``python -m backtest <command>``.
"""

from __future__ import annotations

from datetime import UTC, date, datetime
from decimal import Decimal
from pathlib import Path

import typer

from backtest.engine import BacktestConfig, BacktestResult, run_backtest
from backtest.history import (
    Bars,
    build_bars,
    cache_path,
    fetch_month,
    load_bars,
    months_between,
    save_bars,
    utc_day,
)
from backtest.report import ReportMeta, build_report
from backtest.sweep import Cell, MatrixSpec, run_matrix
from core.exchange.types import Instrument

app = typer.Typer(add_completion=False, help="crypto_dca backtesting.")

_DATA = Path("data")
_DUMPS = _DATA / "ticks"
_CACHE = _DATA / "bars"
_DEFAULT_BUCKET = 1000
_LOT = Decimal("5")
_FEE = Decimal("0.000625")
_MAX_ORDERS = 50

_INSTRUMENT = Instrument(
    symbol="KASUSDT",
    base_coin="KAS",
    quote_coin="USDT",
    tick_size=Decimal("0.00001"),
    lot_size=Decimal("0.01"),
    min_order_qty=Decimal("0.01"),
    min_order_amt=Decimal("1"),
)


def _step(value: Decimal) -> str:
    return f"{value:.5f}"


def _decimals(raw: str) -> list[Decimal]:
    return [Decimal(part.strip()) for part in raw.split(",") if part.strip()]


def _bars(symbol: str, bucket_ms: int, refresh: bool) -> Bars:
    path = cache_path(_CACHE, symbol, bucket_ms)
    if refresh or not path.exists():
        typer.echo(f"aggregating dumps from {_DUMPS} …")
        bars = build_bars(symbol, _DUMPS, bucket_ms)
        if len(bars) == 0:
            typer.echo("no dumps found — run `fetch` first", err=True)
            raise typer.Exit(1)
        save_bars(path, bars)
        typer.echo(f"cached {len(bars):,} bars → {path}")
        return bars
    return load_bars(path)


def _window(bars: Bars, since: str | None, until: str | None) -> Bars:
    start = (
        utc_day(since)
        if since
        else datetime.fromtimestamp(int(bars.ts[0]) / 1000, tz=UTC)
    )
    end = (
        utc_day(until)
        if until
        else datetime.fromtimestamp(int(bars.ts[-1]) / 1000 + 1, tz=UTC)
    )
    window = bars.slice(start, end)
    if len(window) == 0:
        typer.echo("no bars in that window", err=True)
        raise typer.Exit(1)
    return window


def _config(
    grid_step: Decimal, tp_step: Decimal, capital: Decimal
) -> BacktestConfig:
    return BacktestConfig(
        grid_step=grid_step,
        tp_step=tp_step,
        order_qty_quote=_LOT,
        maker_fee=_FEE,
        max_open_orders=_MAX_ORDERS,
        start_usdt=capital,
    )


def _days(bars: Bars) -> Decimal:
    span = int(bars.ts[-1]) - int(bars.ts[0])
    return Decimal(max(span, 1)) / Decimal(86_400_000)


def _report(label: str, res: BacktestResult, days: Decimal) -> None:
    apr = Decimal(0)
    if res.avg_deployed > 1:
        apr = res.realized / res.avg_deployed * (Decimal(365) / days) * 100
    typer.echo(
        f"{label}  realized {res.realized:>9.2f}  "
        f"deployed {res.avg_deployed:>8.2f}  apr {apr:>6.1f}%  "
        f"trades {res.trades:>6}  comps {res.compensations:>6}  "
        f"open {res.open_positions:>4}  equity {res.equity:>9.2f}"
    )


@app.command()
def fetch(
    symbol: str = "KASUSDT",
    since: str = "2025-01",
    until: str = "",
) -> None:
    """Download monthly tick dumps that are not on disk yet."""
    start = datetime.strptime(since, "%Y-%m").date()
    end = datetime.strptime(until, "%Y-%m").date() if until else date.today()
    got = missing = 0
    for month in months_between(start, end):
        path = fetch_month(symbol, month, _DUMPS)
        if path is None:
            typer.echo(f"  {month}: not published yet")
            missing += 1
            continue
        size = path.stat().st_size / 1_048_576
        typer.echo(f"  {month}: {size:6.2f} MB")
        got += 1
    typer.echo(f"{got} month(s) on disk, {missing} not published")


@app.command()
def run(
    symbol: str = "KASUSDT",
    since: str = "",
    until: str = "",
    grid_step: str = "0.00005",
    tp_step: str = "0.0002",
    capital: str = "100",
    bucket_ms: int = _DEFAULT_BUCKET,
    refresh: bool = False,
) -> None:
    """Replay one parameter set over the cached history."""
    bars = _window(
        _bars(symbol, bucket_ms, refresh), since or None, until or None
    )
    days = _days(bars)
    typer.echo(
        f"{len(bars):,} bars · {days:.1f} days · "
        f"{Decimal(int(bars.close[0])) / Decimal(100_000_000):.5f} → "
        f"{Decimal(int(bars.close[-1])) / Decimal(100_000_000):.5f}"
    )
    res = run_backtest(
        bars,
        _config(Decimal(grid_step), Decimal(tp_step), Decimal(capital)),
        _INSTRUMENT,
    )
    _report(f"grid {grid_step} tp {tp_step}", res, days)


@app.command()
def sweep(
    symbol: str = "KASUSDT",
    since: str = "",
    until: str = "",
    grid_steps: str = "0.00005",
    tp_steps: str = "0.0001,0.0002,0.0003",
    capital: str = "100",
    bucket_ms: int = _DEFAULT_BUCKET,
    refresh: bool = False,
) -> None:
    """Replay a grid_step x tp_step matrix over the same history."""
    bars = _window(
        _bars(symbol, bucket_ms, refresh), since or None, until or None
    )
    days = _days(bars)
    typer.echo(f"{len(bars):,} bars · {days:.1f} days · capital {capital}")
    for grid in _decimals(grid_steps):
        for tp in _decimals(tp_steps):
            if tp < grid:
                continue
            res = run_backtest(
                bars,
                _config(grid, tp, Decimal(capital)),
                _INSTRUMENT,
            )
            _report(f"grid {grid} tp {tp}", res, days)


@app.command()
def matrix(
    symbol: str = "KASUSDT",
    since: str = "",
    until: str = "",
    grid_steps: str = "0.00001,0.00002,0.00003,0.00004,0.00005,"
    "0.00006,0.00007,0.00008,0.00009,0.00010",
    tp_steps: str = "0.00005,0.00010,0.00015,0.00020,0.00025,"
    "0.00030,0.00035,0.00040,0.00045,0.00050",
    capital: str = "100",
    out: str = "report.html",
    refresh: bool = False,
) -> None:
    """Replay the full parameter matrix and write an HTML report."""
    bars = _bars(symbol, _DEFAULT_BUCKET, refresh)
    window = _window(bars, since or None, until or None)
    first = datetime.fromtimestamp(int(window.ts[0]) / 1000, tz=UTC)
    last = datetime.fromtimestamp(int(window.ts[-1]) / 1000 + 1, tz=UTC)
    spec = MatrixSpec(
        grid_steps=_decimals(grid_steps),
        tp_steps=_decimals(tp_steps),
        capital=Decimal(capital),
        max_orders=_MAX_ORDERS,
    )
    total = len(spec.grid_steps) * len(spec.tp_steps)
    typer.echo(f"replaying {total} cells over {len(window):,} bars …")

    def progress(done: int, total_cells: int, cell: Cell) -> None:
        typer.echo(
            f"  [{done:>3}/{total_cells}] grid {_step(cell.grid_step)} "
            f"tp {_step(cell.tp_step)} → {cell.realized:.2f} USDT, "
            f"{cell.trades} trades"
        )

    cells = run_matrix(
        cache_path(_CACHE, symbol, _DEFAULT_BUCKET),
        first,
        last,
        spec,
        _INSTRUMENT,
        on_cell=progress,
    )
    meta = ReportMeta(
        symbol=symbol,
        since=first.date(),
        until=last.date(),
        days=_days(window),
        capital=Decimal(capital),
        bars=len(window),
        first_price=Decimal(int(window.close[0])) / Decimal(100_000_000),
        last_price=Decimal(int(window.close[-1])) / Decimal(100_000_000),
        baseline_grid=Decimal("0.00005"),
        baseline_tp=Decimal("0.0002"),
    )
    Path(out).write_text(build_report(cells, meta))
    best = max(cells, key=lambda c: c.realized)
    typer.echo(
        f"best: grid {best.grid_step} tp {best.tp_step} → "
        f"{best.realized:.2f} USDT realized, {best.trades} trades"
    )
    typer.echo(f"report → {out}")


if __name__ == "__main__":
    app()
