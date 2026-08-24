from __future__ import annotations

from decimal import Decimal

import numpy as np
import pytest

from backtest.engine import BacktestConfig, run_backtest
from backtest.history import Bars, to_bars
from core.exchange.types import Instrument

_INSTRUMENT = Instrument(
    symbol="KASUSDT",
    base_coin="KAS",
    quote_coin="USDT",
    tick_size=Decimal("0.00001"),
    lot_size=Decimal("0.01"),
    min_order_qty=Decimal("0.01"),
    min_order_amt=Decimal("1"),
)


def _cfg(
    *,
    tp_step: Decimal = Decimal("0.0002"),
    start_usdt: Decimal = Decimal("100"),
    compensation_moves: int = 1,
) -> BacktestConfig:
    return BacktestConfig(
        grid_step=Decimal("0.00005"),
        tp_step=tp_step,
        order_qty_quote=Decimal("5"),
        maker_fee=Decimal("0.000625"),
        max_open_orders=50,
        start_usdt=start_usdt,
        compensation_moves=compensation_moves,
    )


def _bars(prices: list[str]) -> Bars:
    scale = 100_000_000
    times = np.arange(len(prices), dtype=np.int64) * 1000
    values = np.array(
        [round(float(p) * scale) for p in prices], dtype=np.int64
    )
    return to_bars(times, values, 1000)


def test_a_dip_opens_lots_and_a_rally_closes_them_in_profit() -> None:
    bars = _bars(["0.03000", "0.02900", "0.02800", "0.03100", "0.03200"])
    res = run_backtest(bars, _cfg(), _INSTRUMENT)
    assert res.buys > 0
    assert res.trades > 0
    assert res.realized > 0


def test_lots_stay_open_when_price_never_reaches_their_tp() -> None:
    bars = _bars(["0.03000", "0.02900", "0.02800", "0.02700"])
    res = run_backtest(bars, _cfg(), _INSTRUMENT)
    assert res.buys > 0
    assert res.trades == 0
    assert res.open_positions == res.buys


def test_no_capital_means_no_orders_at_all() -> None:
    bars = _bars(["0.03000", "0.02800", "0.03100"])
    res = run_backtest(bars, _cfg(start_usdt=Decimal("1")), _INSTRUMENT)
    assert res.buys == 0
    assert res.trades == 0
    assert res.equity == Decimal("1")


def test_spending_never_exceeds_the_starting_capital() -> None:
    bars = _bars(["0.03000", "0.02500", "0.02000", "0.01500"])
    res = run_backtest(bars, _cfg(start_usdt=Decimal("20")), _INSTRUMENT)
    assert res.usdt >= 0
    assert res.buys <= 4


def test_a_wider_tp_step_earns_more_per_trade() -> None:
    prices = ["0.03000", "0.02800", "0.03200", "0.02800", "0.03200"]
    narrow = run_backtest(
        _bars(prices), _cfg(tp_step=Decimal("0.0001")), _INSTRUMENT
    )
    wide = run_backtest(
        _bars(prices), _cfg(tp_step=Decimal("0.0005")), _INSTRUMENT
    )
    assert narrow.trades > 0
    assert wide.trades > 0
    assert wide.realized / wide.trades > narrow.realized / narrow.trades


def test_replaying_nothing_is_rejected() -> None:
    empty = np.array([], dtype=np.int64)
    with pytest.raises(ValueError, match="no bars to replay"):
        run_backtest(to_bars(empty, empty, 1000), _cfg(), _INSTRUMENT)


def test_equity_curve_has_one_point_per_day() -> None:
    day = 86_400_000
    times = np.array([0, day, 2 * day], dtype=np.int64)
    prices = np.array([3_000_000, 2_800_000, 3_100_000], dtype=np.int64)
    res = run_backtest(to_bars(times, prices, 1000), _cfg(), _INSTRUMENT)
    assert len(res.equity_curve) == 3
    assert res.equity_curve[0][0].isoformat() == "1970-01-01"


def test_starved_share_is_total_when_capital_never_suffices() -> None:
    bars = _bars(["0.03000", "0.02800", "0.03100"])
    res = run_backtest(bars, _cfg(start_usdt=Decimal("1")), _INSTRUMENT)
    assert res.starved_share == Decimal(1)


def test_drawdown_is_zero_when_equity_only_climbs() -> None:
    bars = _bars(["0.03000", "0.03100", "0.03200"])
    res = run_backtest(bars, _cfg(), _INSTRUMENT)
    assert res.max_drawdown == Decimal(0)


def test_profit_per_trade_is_zero_without_trades() -> None:
    bars = _bars(["0.03000", "0.02900"])
    res = run_backtest(bars, _cfg(), _INSTRUMENT)
    assert res.trades == 0
    assert res.profit_per_trade == Decimal(0)


def test_greedy_compensation_spends_the_pool_on_more_moves() -> None:
    prices = [
        "0.03000",
        "0.02800",
        "0.03200",
        "0.02800",
        "0.03200",
        "0.02800",
        "0.03200",
    ]
    single = run_backtest(_bars(prices), _cfg(), _INSTRUMENT)
    greedy = run_backtest(
        _bars(prices), _cfg(compensation_moves=50), _INSTRUMENT
    )
    assert greedy.compensations >= single.compensations


def test_one_move_per_close_is_the_default() -> None:
    assert _cfg().compensation_moves == 1
