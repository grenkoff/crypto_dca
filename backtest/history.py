"""Historical spot trades from Bybit's public dumps, cached as bars.

Monthly tick dumps live at ``public.bybit.com/spot/<SYMBOL>/``; they are
downloaded once, then aggregated into sparse OHLC buckets (only intervals
that actually traded) and cached as compressed numpy archives.
"""

from __future__ import annotations

import gzip
import urllib.error
import urllib.request
from collections.abc import Iterator
from dataclasses import dataclass
from datetime import UTC, date, datetime
from pathlib import Path

import numpy as np
import numpy.typing as npt

_BASE_URL = "https://public.bybit.com/spot"
_SCALE = 100_000_000
_TIMEOUT = 120
_CHUNK = 1 << 20


@dataclass(frozen=True)
class Bars:
    """Sparse OHLC series: one bucket per interval that traded.

    Prices are integers in units of 1e-8 so the engine can rebuild exact
    ``Decimal`` values without float drift.
    """

    ts: npt.NDArray[np.int64]
    open: npt.NDArray[np.int64]
    high: npt.NDArray[np.int64]
    low: npt.NDArray[np.int64]
    close: npt.NDArray[np.int64]

    def __len__(self) -> int:
        return int(self.ts.size)

    def slice(self, start: datetime, end: datetime) -> Bars:
        """The bars falling within ``[start, end)``."""
        lo = int(start.timestamp() * 1000)
        hi = int(end.timestamp() * 1000)
        keep = (self.ts >= lo) & (self.ts < hi)
        return Bars(
            ts=self.ts[keep],
            open=self.open[keep],
            high=self.high[keep],
            low=self.low[keep],
            close=self.close[keep],
        )


def months_between(start: date, end: date) -> Iterator[str]:
    """Yield ``YYYY-MM`` labels covering ``start`` through ``end``."""
    year, month = start.year, start.month
    while (year, month) <= (end.year, end.month):
        yield f"{year:04d}-{month:02d}"
        month += 1
        if month > 12:
            year, month = year + 1, 1


def dump_url(symbol: str, month: str) -> str:
    """Public URL of one monthly tick dump."""
    return f"{_BASE_URL}/{symbol}/{symbol}-{month}.csv.gz"


def fetch_month(symbol: str, month: str, dest_dir: Path) -> Path | None:
    """Download one monthly dump unless already present.

    Returns the local path, or ``None`` when the month is not published
    yet (the current month appears only after it closes).
    """
    dest_dir.mkdir(parents=True, exist_ok=True)
    target = dest_dir / f"{symbol}-{month}.csv.gz"
    if target.exists():
        return target
    url = dump_url(symbol, month)
    if not url.startswith(f"{_BASE_URL}/"):
        raise ValueError(f"refusing to fetch off-site url: {url}")
    partial = target.with_suffix(".part")
    try:
        with (
            urllib.request.urlopen(url, timeout=_TIMEOUT) as response,
            partial.open("wb") as out,
        ):
            while chunk := response.read(_CHUNK):
                out.write(chunk)
    except urllib.error.HTTPError as exc:
        partial.unlink(missing_ok=True)
        if exc.code == 404:
            return None
        raise
    partial.replace(target)
    return target


def read_ticks(path: Path) -> tuple[npt.NDArray[np.int64], ...]:
    """Read a dump into (timestamp ms, price in 1e-8 units) arrays."""
    times: list[int] = []
    prices: list[int] = []
    with gzip.open(path, "rt") as handle:
        next(handle, None)
        for line in handle:
            parts = line.split(",")
            if len(parts) < 3:
                continue
            times.append(int(float(parts[1])))
            prices.append(round(float(parts[2]) * _SCALE))
    return np.array(times, dtype=np.int64), np.array(prices, dtype=np.int64)


def to_bars(
    times: npt.NDArray[np.int64],
    prices: npt.NDArray[np.int64],
    bucket_ms: int,
) -> Bars:
    """Aggregate ticks into sparse OHLC buckets of ``bucket_ms``."""
    if times.size == 0:
        empty = np.array([], dtype=np.int64)
        return Bars(empty, empty, empty, empty, empty)
    order = np.argsort(times, kind="stable")
    times, prices = times[order], prices[order]
    buckets = times // bucket_ms
    starts = np.flatnonzero(np.r_[True, buckets[1:] != buckets[:-1]])
    ends = np.r_[starts[1:], buckets.size] - 1
    return Bars(
        ts=buckets[starts] * bucket_ms,
        open=prices[starts],
        high=np.maximum.reduceat(prices, starts),
        low=np.minimum.reduceat(prices, starts),
        close=prices[ends],
    )


def cache_path(cache_dir: Path, symbol: str, bucket_ms: int) -> Path:
    """Where the aggregated bar cache for one symbol/bucket lives."""
    return cache_dir / f"{symbol}-{bucket_ms}ms.npz"


def save_bars(path: Path, bars: Bars) -> None:
    """Write bars to a compressed numpy archive."""
    path.parent.mkdir(parents=True, exist_ok=True)
    np.savez_compressed(
        path,
        ts=bars.ts,
        open=bars.open,
        high=bars.high,
        low=bars.low,
        close=bars.close,
    )


def load_bars(path: Path) -> Bars:
    """Read bars back from a compressed numpy archive."""
    with np.load(path) as data:
        return Bars(
            ts=data["ts"],
            open=data["open"],
            high=data["high"],
            low=data["low"],
            close=data["close"],
        )


def build_bars(symbol: str, dumps_dir: Path, bucket_ms: int) -> Bars:
    """Aggregate every downloaded dump for ``symbol`` into one series."""
    parts: list[Bars] = []
    for path in sorted(dumps_dir.glob(f"{symbol}-*.csv.gz")):
        times, prices = read_ticks(path)
        parts.append(to_bars(times, prices, bucket_ms))
    if not parts:
        empty = np.array([], dtype=np.int64)
        return Bars(empty, empty, empty, empty, empty)
    return Bars(
        ts=np.concatenate([p.ts for p in parts]),
        open=np.concatenate([p.open for p in parts]),
        high=np.concatenate([p.high for p in parts]),
        low=np.concatenate([p.low for p in parts]),
        close=np.concatenate([p.close for p in parts]),
    )


def utc_day(value: str) -> datetime:
    """Parse ``YYYY-MM-DD`` into a UTC midnight datetime."""
    return datetime.strptime(value, "%Y-%m-%d").replace(tzinfo=UTC)
