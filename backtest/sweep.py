"""Run a grid_step x tp_step matrix in parallel and rank the outcomes.

Each cell is an independent replay, so the matrix fans out across
processes; workers load the bar cache once and slice it per run.
"""

from __future__ import annotations

from concurrent.futures import ProcessPoolExecutor
from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal
from pathlib import Path

from backtest.engine import BacktestConfig, run_backtest
from backtest.history import Bars, load_bars
from core.exchange.types import Instrument

_MS_PER_DAY = Decimal(86_400_000)

_BARS: Bars | None = None


@dataclass(frozen=True)
class Cell:
    """One matrix cell: the parameters and what they produced."""

    grid_step: Decimal
    tp_step: Decimal
    realized: Decimal
    equity: Decimal
    trades: int
    compensations: int
    open_positions: int
    avg_deployed: Decimal
    stuck_cost: Decimal
    max_drawdown: Decimal
    starved_share: Decimal
    profit_per_trade: Decimal
    apr: Decimal
    curve: list[tuple[date, Decimal]]

    @property
    def ratio(self) -> int:
        """How many grid slots the take-profit spans."""
        return int(self.tp_step / self.grid_step)


@dataclass(frozen=True)
class MatrixSpec:
    """The parameter grid to explore and the account it runs on."""

    grid_steps: list[Decimal]
    tp_steps: list[Decimal]
    capital: Decimal
    max_orders: int


def _init(cache: str, since: str, until: str) -> None:
    global _BARS
    bars = load_bars(Path(cache))
    _BARS = bars.slice(
        datetime.fromisoformat(since), datetime.fromisoformat(until)
    )


def _span_days(bars: Bars) -> Decimal:
    span = int(bars.ts[-1]) - int(bars.ts[0])
    return max(Decimal(span), Decimal(1)) / _MS_PER_DAY


def _run_cell(job: tuple[str, str, str, int, Instrument]) -> Cell:
    grid_raw, tp_raw, capital_raw, max_orders, instrument = job
    assert _BARS is not None
    grid_step, tp_step = Decimal(grid_raw), Decimal(tp_raw)
    result = run_backtest(
        _BARS,
        BacktestConfig(
            grid_step=grid_step,
            tp_step=tp_step,
            order_qty_quote=Decimal("5"),
            maker_fee=Decimal("0.000625"),
            max_open_orders=max_orders,
            start_usdt=Decimal(capital_raw),
        ),
        instrument,
    )
    days = _span_days(_BARS)
    apr = Decimal(0)
    if result.avg_deployed > 1:
        apr = (
            result.realized / result.avg_deployed * (Decimal(365) / days) * 100
        )
    return Cell(
        grid_step=grid_step,
        tp_step=tp_step,
        realized=result.realized,
        equity=result.equity,
        trades=result.trades,
        compensations=result.compensations,
        open_positions=result.open_positions,
        avg_deployed=result.avg_deployed,
        stuck_cost=result.stuck_cost,
        max_drawdown=result.max_drawdown,
        starved_share=result.starved_share,
        profit_per_trade=result.profit_per_trade,
        apr=apr,
        curve=result.equity_curve,
    )


def run_matrix(
    cache: Path,
    since: datetime,
    until: datetime,
    spec: MatrixSpec,
    instrument: Instrument,
    workers: int | None = None,
) -> list[Cell]:
    """Replay every grid_step/tp_step pair and return the cells."""
    jobs = [
        (str(grid), str(tp), str(spec.capital), spec.max_orders, instrument)
        for grid in spec.grid_steps
        for tp in spec.tp_steps
    ]
    with ProcessPoolExecutor(
        max_workers=workers,
        initializer=_init,
        initargs=(str(cache), since.isoformat(), until.isoformat()),
    ) as pool:
        return list(pool.map(_run_cell, jobs, chunksize=1))
