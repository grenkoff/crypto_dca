"""Replay the live grid strategy over historical bars.

The engine drives the same pure functions the trader uses — grid level
selection, take-profit pricing and compensation planning — so a backtest
exercises the real strategy rather than a restatement of it. Only the
exchange is simulated: a resting order fills when a bar's range reaches
its price.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, date, datetime
from decimal import Decimal

from backtest.history import Bars
from core.exchange.types import Instrument
from core.services.order_manager import compute_buy_qty
from core.strategy.compensation import plan_compensation
from core.strategy.grid import resting_buy_levels
from core.strategy.pricing import compute_tp_price
from core.strategy.types import CompensationContext, OpenPosition

_SCALE = Decimal(100_000_000)


@dataclass(frozen=True)
class BacktestConfig:
    """Strategy parameters under test, mirroring ``StrategyConfig``."""

    grid_step: Decimal
    tp_step: Decimal
    order_qty_quote: Decimal
    maker_fee: Decimal
    max_open_orders: int
    start_usdt: Decimal
    min_profit_quote: Decimal = Decimal(0)


@dataclass(frozen=True)
class BacktestResult:
    """Outcome of one replay."""

    trades: int
    buys: int
    compensations: int
    realized: Decimal
    open_positions: int
    usdt: Decimal
    base_qty: Decimal
    equity: Decimal
    avg_deployed: Decimal
    last_price: Decimal
    stuck_cost: Decimal
    max_drawdown: Decimal
    starved_share: Decimal
    equity_curve: list[tuple[date, Decimal]] = field(default_factory=list)

    @property
    def profit_per_trade(self) -> Decimal:
        """Average realized profit of one round trip."""
        if self.trades == 0:
            return Decimal(0)
        return self.realized / self.trades


@dataclass
class _Lot:
    """A simulated open position."""

    id: int
    entry: Decimal
    qty: Decimal
    fees_in: Decimal
    tp: Decimal
    credit: Decimal = field(default=Decimal(0))

    def view(self) -> OpenPosition:
        """The strategy-facing view of this lot."""
        return OpenPosition(
            id=self.id,
            entry_price=self.entry,
            qty=self.qty,
            fees_in=self.fees_in,
            current_tp_price=self.tp,
            compensation_credit=self.credit,
        )


class _Book:
    """Mutable simulation state: cash, lots, resting buys, credit pool."""

    def __init__(self, cfg: BacktestConfig, instrument: Instrument) -> None:
        self.cfg = cfg
        self.instrument = instrument
        self.usdt = cfg.start_usdt
        self.base = Decimal(0)
        self.pool = Decimal(0)
        self.lots: list[_Lot] = []
        self.resting: set[Decimal] = set()
        self.trades = 0
        self.buys = 0
        self.compensations = 0
        self.realized = Decimal(0)
        self.deployed = Decimal(0)
        self.band_price: Decimal | None = None
        self._next_id = 1

    def stale_band(self, price: Decimal) -> bool:
        """Whether the resting band needs re-placing at ``price``.

        The live maintainer runs on a cycle, not on every tick; re-placing
        only once price has moved a full grid step matches that and keeps
        the replay honest about how often orders really move.
        """
        if self.band_price is None:
            return True
        return abs(price - self.band_price) >= self.cfg.grid_step

    def refresh(self, price: Decimal) -> None:
        """Re-place the resting buy band under ``price``."""
        self.band_price = price
        affordable = int(self.usdt / self.cfg.order_qty_quote)
        count = min(affordable, self.cfg.max_open_orders)
        if count <= 0:
            self.resting.clear()
            return
        held = {lot.entry for lot in self.lots}
        ceiling = self._buy_ceiling()
        targets = resting_buy_levels(
            price, self.cfg.grid_step, count, held, ceiling
        )
        self.resting = {level_price for _, level_price in targets}

    def _buy_ceiling(self) -> Decimal | None:
        if not self.lots:
            return None
        return (
            min(lot.tp for lot in self.lots)
            - self.cfg.tp_step
            - self.cfg.grid_step
        )

    def fill_buys(self, low: Decimal) -> None:
        """Fill every resting buy the bar reached."""
        for price in sorted(
            (p for p in self.resting if p >= low), reverse=True
        ):
            if self.usdt < self.cfg.order_qty_quote:
                break
            qty = compute_buy_qty(
                self.cfg.order_qty_quote, price, self.instrument
            )
            cost = qty * price
            if qty <= 0 or cost > self.usdt:
                continue
            fees_in = cost * self.cfg.maker_fee
            self.usdt -= cost
            self.deployed += cost
            self.base += qty * (Decimal(1) - self.cfg.maker_fee)
            self.resting.discard(price)
            self.lots.append(
                _Lot(
                    id=self._next_id,
                    entry=price,
                    qty=qty,
                    fees_in=fees_in,
                    tp=compute_tp_price(
                        entry_price=price,
                        qty=qty,
                        fees_in=fees_in,
                        tp_step=self.cfg.tp_step,
                        min_profit_quote=self.cfg.min_profit_quote,
                        maker_fee=self.cfg.maker_fee,
                        tick_size=self.instrument.tick_size,
                        min_order_amt=self.instrument.min_order_amt,
                    ),
                )
            )
            self._next_id += 1
            self.buys += 1

    def fill_tps(self, high: Decimal, price: Decimal) -> None:
        """Sell every lot whose take-profit the bar reached."""
        while self.lots:
            lot = min(self.lots, key=lambda item: item.tp)
            if lot.tp > high:
                return
            proceeds = lot.qty * lot.tp * (Decimal(1) - self.cfg.maker_fee)
            realized = proceeds - lot.entry * lot.qty - lot.fees_in
            self.usdt += proceeds
            self.base -= lot.qty
            self.deployed -= lot.entry * lot.qty
            self.realized += realized
            self.lots.remove(lot)
            self.trades += 1
            self.pool += realized
            self.compensate(price)

    def compensate(self, price: Decimal) -> None:
        """Apply one compensation move if the pool funds it."""
        if self.pool <= 0 or not self.lots:
            return
        decision = plan_compensation(
            [lot.view() for lot in self.lots],
            CompensationContext(
                pool=self.pool,
                maker_fee=self.cfg.maker_fee,
                current_price=price,
                tick_size=self.instrument.tick_size,
                grid_step=self.cfg.grid_step,
                tp_step=self.cfg.tp_step,
                nearest_buy_price=(
                    max(self.resting) if self.resting else Decimal(0)
                ),
                min_order_amt=self.instrument.min_order_amt,
            ),
        )
        if decision is None:
            return
        target = next(
            lot for lot in self.lots if lot.id == decision.target_position_id
        )
        target.tp = decision.new_tp_price
        target.credit = decision.new_credit
        self.pool -= decision.credit_drawn
        self.compensations += 1


def _bar_day(ts_ms: int) -> date:
    return datetime.fromtimestamp(ts_ms / 1000, tz=UTC).date()


def run_backtest(
    bars: Bars, cfg: BacktestConfig, instrument: Instrument
) -> BacktestResult:
    """Replay ``bars`` through the strategy and report the outcome."""
    if len(bars) == 0:
        raise ValueError("no bars to replay")
    book = _Book(cfg, instrument)
    deployed_sum = Decimal(0)
    starved = 0
    peak = cfg.start_usdt
    drawdown = Decimal(0)
    curve: list[tuple[date, Decimal]] = []
    day = _bar_day(int(bars.ts[0]))
    book.refresh(Decimal(int(bars.open[0])) / _SCALE)
    for index in range(len(bars)):
        opened = Decimal(int(bars.open[index])) / _SCALE
        high = Decimal(int(bars.high[index])) / _SCALE
        low = Decimal(int(bars.low[index])) / _SCALE
        close = Decimal(int(bars.close[index])) / _SCALE
        if opened - low <= high - opened:
            book.fill_buys(low)
            book.fill_tps(high, close)
        else:
            book.fill_tps(high, close)
            book.fill_buys(low)
        if book.stale_band(close):
            book.refresh(close)
        deployed_sum += book.deployed
        if book.usdt < cfg.order_qty_quote:
            starved += 1
        equity = book.usdt + book.base * close
        peak = max(peak, equity)
        drawdown = max(drawdown, peak - equity)
        bar_day = _bar_day(int(bars.ts[index]))
        if bar_day != day:
            curve.append((day, equity))
            day = bar_day
    last = Decimal(int(bars.close[-1])) / _SCALE
    final = book.usdt + book.base * last
    curve.append((day, final))
    return BacktestResult(
        trades=book.trades,
        buys=book.buys,
        compensations=book.compensations,
        realized=book.realized,
        open_positions=len(book.lots),
        usdt=book.usdt,
        base_qty=book.base,
        equity=final,
        avg_deployed=deployed_sum / len(bars),
        last_price=last,
        stuck_cost=sum((lot.entry * lot.qty for lot in book.lots), Decimal(0)),
        max_drawdown=drawdown,
        starved_share=Decimal(starved) / len(bars),
        equity_curve=curve,
    )
