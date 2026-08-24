"""Render a matrix of backtest cells as a self-contained HTML report.

Charts are native HTML/SVG rather than embedded images, so the report
stays light, responsive and readable in either colour theme.
"""

from __future__ import annotations

import html
import json
from dataclasses import dataclass
from datetime import date
from decimal import Decimal

from backtest.sweep import Cell

_SEQ = [
    "#cde2fb",
    "#b7d3f6",
    "#9ec5f4",
    "#86b6ef",
    "#6da7ec",
    "#5598e7",
    "#3987e5",
    "#2a78d6",
    "#256abf",
    "#1c5cab",
    "#184f95",
    "#104281",
]
_SERIES = ["#2a78d6", "#eb6834", "#1baf7a"]
_INK_DARK = "#0b0b0b"
_INK_LIGHT = "#ffffff"
_INK_FLIP = 7
_IDLE_WARN = Decimal("0.6")
_IDLE_BAD = Decimal("0.85")


@dataclass(frozen=True)
class ReportMeta:
    """Context describing what was replayed."""

    symbol: str
    since: date
    until: date
    days: Decimal
    capital: Decimal
    bars: int
    first_price: Decimal
    last_price: Decimal
    baseline_grid: Decimal
    baseline_tp: Decimal


def _fmt(value: Decimal, places: int = 2) -> str:
    return f"{value:.{places}f}"


def _step(value: Decimal) -> str:
    return f"{value:.5f}"


def _ramp(value: Decimal, low: Decimal, high: Decimal) -> tuple[str, str]:
    """Fill colour for a magnitude, plus ink that stays legible on it."""
    if high <= low:
        return _SEQ[0], _INK_DARK
    share = (value - low) / (high - low)
    index = max(0, min(int(share * (len(_SEQ) - 1)), len(_SEQ) - 1))
    ink = _INK_DARK if index < _INK_FLIP else _INK_LIGHT
    return _SEQ[index], ink


def _heatmap(
    cells: list[Cell],
    grids: list[Decimal],
    tps: list[Decimal],
    pick: str,
    places: int,
) -> str:
    """One grid_step x tp_step heatmap, coloured by ``pick``."""
    lookup = {(c.grid_step, c.tp_step): c for c in cells}
    values = [getattr(c, pick) for c in cells]
    low, high = min(values), max(values)
    best = max(cells, key=lambda c: getattr(c, pick))
    head = "".join(f"<th>{_step(tp)}</th>" for tp in tps)
    rows = []
    for grid in grids:
        tds = []
        for tp in tps:
            cell = lookup.get((grid, tp))
            if cell is None:
                tds.append('<td class="empty"></td>')
                continue
            value = getattr(cell, pick)
            mark = " top" if cell is best else ""
            fill, ink = _ramp(value, low, high)
            tds.append(
                f'<td class="cell{mark}" '
                f'style="--fill:{fill};--ink:{ink}" tabindex="0" '
                f'data-tip="grid {_step(grid)} · tp {_step(tp)} · '
                f"{cell.ratio}x slots&#10;realized {_fmt(cell.realized)} · "
                f"equity {_fmt(cell.equity)}&#10;trades {cell.trades} · "
                f'idle {_fmt(cell.starved_share * 100, 0)}%">'
                f"{_fmt(value, places)}</td>"
            )
        rows.append(f"<tr><th>{_step(grid)}</th>{''.join(tds)}</tr>")
    return (
        '<div class="scroll"><table class="heat">'
        f'<thead><tr><th class="corner">grid \\ tp</th>{head}</tr></thead>'
        f"<tbody>{''.join(rows)}</tbody></table></div>"
        f'<p class="legend"><span class="swatch low"></span>'
        f'{_fmt(low, places)}<span class="ramp"></span>'
        f'{_fmt(high, places)}<span class="swatch high"></span></p>'
    )


def _polyline(
    curve: list[tuple[date, Decimal]],
    lo: Decimal,
    hi: Decimal,
    width: int,
    height: int,
) -> str:
    if len(curve) < 2:
        return ""
    span = hi - lo if hi > lo else Decimal(1)
    last = len(curve) - 1
    points = []
    for index, (_, value) in enumerate(curve):
        x = Decimal(index) / Decimal(last) * width
        y = height - (value - lo) / span * height
        points.append(f"{x:.1f},{y:.1f}")
    return " ".join(points)


def _equity_chart(cells: list[Cell], meta: ReportMeta) -> str:
    """Equity over time for the best cells against the live setting."""
    ranked = sorted(cells, key=lambda c: c.equity, reverse=True)
    baseline = next(
        (
            c
            for c in cells
            if c.grid_step == meta.baseline_grid
            and c.tp_step == meta.baseline_tp
        ),
        None,
    )
    chosen = ranked[:2]
    if baseline is not None and baseline not in chosen:
        chosen.append(baseline)
    series = [c for c in chosen if len(c.curve) > 1]
    if not series:
        return ""
    values = [v for c in series for _, v in c.curve]
    lo, hi = min(values), max(values)
    width, height = 720, 260
    paths = []
    labels = []
    for slot, cell in enumerate(series):
        colour = _SERIES[slot % len(_SERIES)]
        points = _polyline(cell.curve, lo, hi, width, height)
        tag = "live setting" if cell is baseline else "best"
        paths.append(
            f'<polyline points="{points}" fill="none" '
            f'stroke="{colour}" stroke-width="2" '
            'stroke-linejoin="round" stroke-linecap="round" />'
        )
        labels.append(
            f'<li><span class="dot" style="--dot:{colour}"></span>'
            f"grid {_step(cell.grid_step)} · tp {_step(cell.tp_step)} "
            f'<span class="muted">({tag}, ends '
            f"{_fmt(cell.equity)})</span></li>"
        )
    return (
        f'<div class="scroll"><svg viewBox="0 0 {width} {height}" '
        f'width="100%" height="{height}" role="img" '
        'aria-label="Equity over time">'
        f'<line x1="0" y1="{height}" x2="{width}" y2="{height}" '
        'class="axis" />'
        f"{''.join(paths)}</svg></div>"
        f'<ul class="legend-list">{"".join(labels)}</ul>'
        f'<p class="muted">Vertical span {_fmt(lo)} to {_fmt(hi)} USDT '
        f"from {meta.capital} start.</p>"
    )


def _idle_pill(share: Decimal) -> str:
    """Idle share as a labelled state chip, not colour alone."""
    if share >= _IDLE_BAD:
        state, mark = "bad", "starved"
    elif share >= _IDLE_WARN:
        state, mark = "warn", "tight"
    else:
        state, mark = "ok", "fed"
    return (
        f'<span class="pill {state}">{_fmt(share * 100, 0)}% '
        f"<em>{mark}</em></span>"
    )


def _row(cell: Cell) -> str:
    return (
        "<tr>"
        f"<td>{_step(cell.grid_step)}</td>"
        f"<td>{_step(cell.tp_step)}</td>"
        f"<td>{cell.ratio}x</td>"
        f"<td>{_fmt(cell.realized)}</td>"
        f"<td>{_fmt(cell.equity)}</td>"
        f"<td>{_fmt(cell.apr, 1)}%</td>"
        f"<td>{cell.trades}</td>"
        f"<td>{_fmt(cell.profit_per_trade, 4)}</td>"
        f"<td>{_fmt(cell.avg_deployed)}</td>"
        f"<td>{_fmt(cell.max_drawdown)}</td>"
        f"<td>{_idle_pill(cell.starved_share)}</td>"
        "</tr>"
    )


def _rows(cells: list[Cell]) -> str:
    ranked = sorted(cells, key=lambda c: c.realized, reverse=True)
    return "".join(_row(cell) for cell in ranked)


def build_report(cells: list[Cell], meta: ReportMeta) -> str:
    """Render the whole report as one HTML document body."""
    grids = sorted({c.grid_step for c in cells})
    tps = sorted({c.tp_step for c in cells})
    best = max(cells, key=lambda c: c.realized)
    best_equity = max(cells, key=lambda c: c.equity)
    drift = (meta.last_price / meta.first_price - 1) * 100
    payload = html.escape(
        json.dumps(
            {
                "symbol": meta.symbol,
                "cells": len(cells),
                "since": meta.since.isoformat(),
                "until": meta.until.isoformat(),
            }
        )
    )
    return _TEMPLATE.format(
        symbol=meta.symbol,
        since=meta.since,
        until=meta.until,
        days=_fmt(meta.days, 0),
        bars=f"{meta.bars:,}",
        capital=_fmt(meta.capital, 0),
        first_price=_step(meta.first_price),
        last_price=_step(meta.last_price),
        drift=_fmt(drift, 1),
        cells=len(cells),
        best_grid=_step(best.grid_step),
        best_tp=_step(best.tp_step),
        best_realized=_fmt(best.realized),
        best_apr=_fmt(best.apr, 1),
        best_trades=best.trades,
        eq_grid=_step(best_equity.grid_step),
        eq_tp=_step(best_equity.tp_step),
        eq_value=_fmt(best_equity.equity),
        base_grid=_step(meta.baseline_grid),
        base_tp=_step(meta.baseline_tp),
        heat_realized=_heatmap(cells, grids, tps, "realized", 2),
        heat_equity=_heatmap(cells, grids, tps, "equity", 1),
        heat_trades=_heatmap(cells, grids, tps, "trades", 0),
        equity_chart=_equity_chart(cells, meta),
        rows=_rows(cells),
        payload=payload,
    )


_TEMPLATE = """<title>Grid Parameter Matrix</title>
<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
<link rel="stylesheet" href="https://fonts.googleapis.com/css2?family=IBM+Plex+Sans:wght@400;500;600&family=IBM+Plex+Mono:wght@400;500&display=swap">
<style>
:root {{
  color-scheme: light;
  --bg: #fcfcfb; --panel: #ffffff; --ink: #0b0b0b; --muted: #52514e;
  --line: #dfe3e8; --accent: #2a78d6; --ramp-lo: #cde2fb; --ramp-hi: #104281;
  --good: #0ca30c; --warn: #fab219; --bad: #d03b3b;
  --sans: "IBM Plex Sans", ui-sans-serif, system-ui, sans-serif;
  --mono: "IBM Plex Mono", ui-monospace, "SFMono-Regular", monospace;
}}
@media (prefers-color-scheme: dark) {{
  :root:not([data-theme="light"]) {{
    color-scheme: dark;
    --bg: #1a1a19; --panel: #23262a; --ink: #ffffff; --muted: #c3c2b7;
    --line: #383d43; --accent: #3987e5;
  }}
}}
:root[data-theme="dark"] {{
  color-scheme: dark;
  --bg: #1a1a19; --panel: #23262a; --ink: #ffffff; --muted: #c3c2b7;
  --line: #383d43; --accent: #3987e5;
}}
body {{
  margin: 0; padding: 2rem 1.25rem 4rem; background: var(--bg);
  color: var(--ink); font: 15px/1.55 var(--sans);
}}
main {{ max-width: 1080px; margin: 0 auto; }}
h1 {{ font-size: 1.7rem; margin: 0 0 .3rem; letter-spacing: -.02em;
  font-weight: 600; text-wrap: balance; }}
h2 {{ font-size: 1.15rem; margin: 2.6rem 0 .5rem; }}
p {{ margin: .5rem 0; }}
.muted {{ color: var(--muted); }}
.sub {{ color: var(--muted); margin-bottom: 1.6rem; }}
.tiles {{
  display: grid; gap: .75rem; grid-template-columns: repeat(auto-fit,
  minmax(200px, 1fr)); margin: 1.5rem 0;
}}
.tile {{
  background: var(--panel); border: 1px solid var(--line);
  border-radius: 12px; padding: .9rem 1rem;
}}
.tile .k {{ color: var(--muted); font-size: .72rem; text-transform: uppercase;
  letter-spacing: .07em; font-weight: 500; }}
.tile .v {{ font-size: 1.5rem; font-weight: 600; margin-top: .2rem;
  font-family: var(--mono); letter-spacing: -.02em; }}
.tile .n {{ color: var(--muted); font-size: .85rem; }}
.scroll {{ overflow-x: auto; -webkit-overflow-scrolling: touch; }}
table {{ border-collapse: collapse; font-variant-numeric: tabular-nums;
  font-family: var(--mono); }}
.heat {{ font-size: .8rem; }}
.heat th {{
  color: var(--muted); font-weight: 500; padding: .3rem .45rem;
  white-space: nowrap; font-size: .75rem;
}}
.heat .corner {{ text-align: left; }}
.heat td {{
  padding: .45rem .5rem; text-align: right; border: 2px solid var(--bg);
  border-radius: 4px; background: var(--fill); color: var(--ink);
  min-width: 62px; cursor: default; position: relative;
}}
.heat td.top {{ outline: 2px solid var(--ink); outline-offset: -2px; }}
.heat td:focus-visible {{ outline: 3px solid var(--accent);
  outline-offset: 1px; }}
.pill {{ display: inline-flex; gap: .35rem; align-items: baseline;
  padding: .1rem .45rem; border-radius: 999px; font-size: .75rem;
  border: 1px solid var(--state); color: var(--ink); }}
.pill em {{ font-style: normal; color: var(--state); font-family: var(--sans);
  font-size: .7rem; text-transform: uppercase; letter-spacing: .05em; }}
.pill.ok {{ --state: var(--good); }}
.pill.warn {{ --state: var(--warn); }}
.pill.bad {{ --state: var(--bad); }}
.heat td:hover::after, .heat td:focus::after {{
  content: attr(data-tip); position: absolute; left: 50%; bottom: 100%;
  transform: translateX(-50%); background: var(--ink); color: var(--bg);
  padding: .45rem .6rem; border-radius: 8px; white-space: pre;
  font-size: .75rem; z-index: 5; pointer-events: none;
}}
.legend {{ display: flex; align-items: center; gap: .5rem; font-size: .8rem;
  color: var(--muted); }}
.ramp {{
  width: 130px; height: 10px; border-radius: 5px;
  background: linear-gradient(90deg, var(--ramp-lo), var(--ramp-hi));
}}
.swatch {{ width: 12px; height: 12px; border-radius: 3px; }}
.swatch.low {{ background: var(--ramp-lo); }}
.swatch.high {{ background: var(--ramp-hi); }}
.axis {{ stroke: var(--line); stroke-width: 1; }}
.legend-list {{ list-style: none; padding: 0; margin: .6rem 0;
  display: flex; flex-wrap: wrap; gap: 1.1rem; font-size: .85rem; }}
.dot {{ display: inline-block; width: 10px; height: 10px; border-radius: 50%;
  background: var(--dot); margin-right: .4rem; }}
.full {{ width: 100%; font-size: .82rem; }}
.full th {{
  text-align: right; color: var(--muted); font-weight: 500;
  border-bottom: 1px solid var(--line); padding: .4rem .5rem;
  position: sticky; top: 0; background: var(--bg);
}}
.full td {{ text-align: right; padding: .35rem .5rem;
  border-bottom: 1px solid var(--line); }}
.full tbody tr:hover {{ background: var(--panel); }}
</style>
<main>
<h1>Grid parameter matrix — {symbol}</h1>
<p class="sub">{cells} replays over {since} → {until} ({days} days,
{bars} second bars). Price {first_price} → {last_price}
({drift}%). Starting capital {capital} USDT, lot 5 USDT,
maker fee 0.0625%.</p>

<div class="tiles">
  <div class="tile"><div class="k">Best realized</div>
    <div class="v">{best_realized} USDT</div>
    <div class="n">grid {best_grid} · tp {best_tp} · {best_trades} trades</div>
  </div>
  <div class="tile"><div class="k">Return on deployed</div>
    <div class="v">{best_apr}%</div><div class="n">annualised, best cell</div>
  </div>
  <div class="tile"><div class="k">Best ending equity</div>
    <div class="v">{eq_value} USDT</div>
    <div class="n">grid {eq_grid} · tp {eq_tp}</div>
  </div>
  <div class="tile"><div class="k">Live setting</div>
    <div class="v">{base_grid} / {base_tp}</div>
    <div class="n">grid_step / tp_step in production</div>
  </div>
</div>

<h2>Realized profit, USDT</h2>
<p class="muted">Rows are grid_step, columns tp_step. Darker is more
profit; the outlined cell is the best. Hover any cell for its detail.</p>
{heat_realized}

<h2>Ending equity, USDT</h2>
<p class="muted">Cash plus inventory at the last price — what the account
is actually worth once the run ends.</p>
{heat_equity}

<h2>Round trips completed</h2>
<p class="muted">How busy each configuration is. Profit needs turnover,
but turnover alone pays the exchange, not you.</p>
{heat_trades}

<h2>Equity over time</h2>
{equity_chart}

<h2>Every cell</h2>
<p class="muted">Sorted by realized profit. <em>Deployed</em> is the average
capital tied up in open lots; <em>idle</em> is the share of time the grid had
no cash left to place a buy.</p>
<div class="scroll"><table class="full">
<thead><tr>
<th>grid</th><th>tp</th><th>slots</th><th>realized</th><th>equity</th>
<th>apr</th><th>trades</th><th>per trade</th><th>deployed</th>
<th>max dd</th><th>idle</th>
</tr></thead>
<tbody>{rows}</tbody>
</table></div>
<script type="application/json" id="meta">{payload}</script>
</main>
"""
