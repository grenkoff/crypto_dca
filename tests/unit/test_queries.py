from __future__ import annotations

from tgbot.queries import Bar as QueryBar
from tgbot.queries import rescale_ohlc

Bar = QueryBar | None


def test_rescale_ohlc_aligns_first_close() -> None:
    btc: list[Bar] = [
        (100.0, 110.0, 90.0, 100.0, 2000.0),
        (100.0, 130.0, 95.0, 120.0, 2000.0),
    ]
    ref: list[Bar] = [
        (0.02, 0.021, 0.019, 0.025, 1000.0),
        (0.025, 0.026, 0.024, 0.026, 1000.0),
    ]
    out = rescale_ohlc(btc, ref)
    assert out[0] is not None and out[1] is not None
    # first BTC close scaled onto the first KAS close
    assert abs(out[0][3] - 0.025) < 1e-12
    # a +20% BTC move keeps its shape after scaling
    assert abs(out[1][3] - 0.030) < 1e-12


def test_rescale_ohlc_aligns_at_first_shared_day() -> None:
    btc: list[Bar] = [None, (100.0, 110.0, 90.0, 100.0, 2000.0)]
    ref: list[Bar] = [None, (0.02, 0.021, 0.019, 0.05, 1000.0)]
    out = rescale_ohlc(btc, ref)
    assert out[0] is None
    assert out[1] is not None and abs(out[1][3] - 0.05) < 1e-12


def test_rescale_ohlc_no_shared_day_returns_unchanged() -> None:
    btc: list[Bar] = [(100.0, 110.0, 90.0, 100.0, 2000.0), None]
    ref: list[Bar] = [None, (0.02, 0.021, 0.019, 0.05, 1000.0)]
    assert rescale_ohlc(btc, ref) == btc
