from __future__ import annotations

from datetime import UTC, date, datetime
from pathlib import Path

import numpy as np

from backtest.history import (
    Bars,
    dump_url,
    load_bars,
    months_between,
    save_bars,
    to_bars,
    utc_day,
)


def test_months_between_spans_the_year_boundary() -> None:
    got = list(months_between(date(2025, 11, 5), date(2026, 2, 20)))
    assert got == ["2025-11", "2025-12", "2026-01", "2026-02"]


def test_months_between_single_month() -> None:
    assert list(months_between(date(2026, 7, 1), date(2026, 7, 31))) == [
        "2026-07"
    ]


def test_dump_url_points_at_the_public_spot_dumps() -> None:
    url = dump_url("KASUSDT", "2026-07")
    assert url == (
        "https://public.bybit.com/spot/KASUSDT/KASUSDT-2026-07.csv.gz"
    )


def test_to_bars_groups_ticks_into_sparse_buckets() -> None:
    times = np.array([0, 400, 900, 5_100, 5_900], dtype=np.int64)
    prices = np.array([100, 130, 110, 200, 190], dtype=np.int64)
    bars = to_bars(times, prices, 1000)
    assert len(bars) == 2
    assert list(bars.ts) == [0, 5000]
    assert list(bars.open) == [100, 200]
    assert list(bars.high) == [130, 200]
    assert list(bars.low) == [100, 190]
    assert list(bars.close) == [110, 190]


def test_to_bars_sorts_unordered_ticks() -> None:
    times = np.array([900, 100], dtype=np.int64)
    prices = np.array([120, 100], dtype=np.int64)
    bars = to_bars(times, prices, 1000)
    assert list(bars.open) == [100]
    assert list(bars.close) == [120]


def test_to_bars_of_nothing_is_empty() -> None:
    empty = np.array([], dtype=np.int64)
    assert len(to_bars(empty, empty, 1000)) == 0


def test_slice_keeps_only_the_requested_window() -> None:
    bars = to_bars(
        np.array([0, 60_000, 120_000], dtype=np.int64),
        np.array([10, 20, 30], dtype=np.int64),
        1000,
    )
    window = bars.slice(
        datetime.fromtimestamp(60, tz=UTC), datetime.fromtimestamp(120, tz=UTC)
    )
    assert list(window.close) == [20]


def test_bars_survive_a_save_load_round_trip(tmp_path: Path) -> None:
    bars = to_bars(
        np.array([0, 1000], dtype=np.int64),
        np.array([10, 20], dtype=np.int64),
        1000,
    )
    path = tmp_path / "bars.npz"
    save_bars(path, bars)
    back: Bars = load_bars(path)
    assert list(back.ts) == list(bars.ts)
    assert list(back.close) == list(bars.close)


def test_utc_day_parses_to_midnight_utc() -> None:
    assert utc_day("2026-08-21") == datetime(2026, 8, 21, tzinfo=UTC)
