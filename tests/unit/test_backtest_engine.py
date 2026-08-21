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
) -> BacktestConfig:
    return BacktestConfig(
        grid_step=Decimal("0.00005"),
        tp_step=tp_step,
        order_qty_quote=Decimal("5"),
        maker_fee=Decimal("0.000625"),
        max_open_orders=50,
        start_usdt=start_usdt,
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
