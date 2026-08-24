from __future__ import annotations

from datetime import date
from decimal import Decimal
from pathlib import Path

from backtest.report import ReportMeta, build_report
from backtest.sweep import (
    Cell,
    dump_cells,
    load_cells,
)


def _cell(grid: str, tp: str, realized: str, equity: str = "100") -> Cell:
    return Cell(
        grid_step=Decimal(grid),
        tp_step=Decimal(tp),
        realized=Decimal(realized),
        equity=Decimal(equity),
        trades=12,
        compensations=5,
        open_positions=3,
        avg_deployed=Decimal("80"),
        stuck_cost=Decimal("40"),
        max_drawdown=Decimal("7.5"),
        starved_share=Decimal("0.25"),
        profit_per_trade=Decimal("0.01"),
        apr=Decimal("33.3"),
        curve=[
            (date(2026, 1, 1), Decimal("100")),
            (date(2026, 1, 2), Decimal(equity)),
        ],
    )


def _meta() -> ReportMeta:
    return ReportMeta(
        symbol="KASUSDT",
        since=date(2026, 1, 1),
        until=date(2026, 1, 2),
        days=Decimal("1"),
        capital=Decimal("100"),
        bars=1000,
        first_price=Decimal("0.03"),
        last_price=Decimal("0.028"),
        baseline_grid=Decimal("0.00005"),
        baseline_tp=Decimal("0.0002"),
    )


def test_ratio_counts_grid_slots_inside_the_tp() -> None:
    assert _cell("0.00005", "0.0002", "1").ratio == 4
    assert _cell("0.00001", "0.0002", "1").ratio == 20


def test_report_marks_the_best_cell_and_lists_every_row() -> None:
    cells = [
        _cell("0.00005", "0.0001", "1.00", "95"),
        _cell("0.00005", "0.0002", "3.00", "99"),
        _cell("0.00010", "0.0001", "2.00", "97"),
        _cell("0.00010", "0.0002", "2.50", "98"),
    ]
    html = build_report(cells, _meta())
    assert html.count('class="cell') == 4 * 3
    assert html.count("cell top") == 3
    assert html.count("<tr><td>") == 4
    assert "3.00 USDT" in html


def test_report_states_the_window_and_the_live_setting() -> None:
    html = build_report([_cell("0.00005", "0.0002", "1")], _meta())
    assert "2026-01-01" in html
    assert "0.00005 / 0.00020" in html
    assert "KASUSDT" in html


def test_report_defines_dark_theme_under_both_scopes() -> None:
    html = build_report([_cell("0.00005", "0.0002", "1")], _meta())
    assert "prefers-color-scheme: dark" in html
    assert ':root[data-theme="dark"]' in html
    assert ':root:not([data-theme="light"])' in html


def test_report_survives_a_single_point_curve() -> None:
    cell = _cell("0.00005", "0.0002", "1")
    stub = Cell(**{**cell.__dict__, "curve": [(date(2026, 1, 1), Decimal(1))]})
    html = build_report([stub], _meta())
    assert "Grid parameter matrix" in html


def test_idle_pill_labels_the_state_not_just_the_colour() -> None:
    fed = build_report([_cell_with_idle("0.10")], _meta())
    starved = build_report([_cell_with_idle("0.95")], _meta())
    assert "fed" in fed and "pill ok" in fed
    assert "starved" in starved and "pill bad" in starved


def _cell_with_idle(share: str) -> Cell:
    cell = _cell("0.00005", "0.0002", "1")
    return Cell(**{**cell.__dict__, "starved_share": Decimal(share)})


def test_report_links_only_google_fonts() -> None:
    html = build_report([_cell("0.00005", "0.0002", "1")], _meta())
    assert "fonts.googleapis.com" in html
    assert "IBM Plex Mono" in html
    for marker in ("http://", "cdn.", "unpkg", "jsdelivr"):
        assert marker not in html


def test_heatmap_cells_carry_a_real_fill_and_ink_colour() -> None:
    cells = [
        _cell("0.00005", "0.0001", "1.00"),
        _cell("0.00005", "0.0002", "9.00"),
    ]
    html = build_report(cells, _meta())
    assert "--fill:#" in html
    assert "--ink:#" in html
    assert "--fill:(" not in html
    for chunk in html.split('style="--fill:')[1:]:
        decl = chunk.split('"')[0]
        fill, ink = decl.split(";")
        assert fill.startswith(("--fill:#", "#"))
        assert ink.startswith("--ink:#")


def test_heatmap_flips_ink_on_the_darkest_fills() -> None:
    cells = [_cell("0.00005", f"0.000{i}", str(i)) for i in range(1, 10)]
    html = build_report(cells, _meta())
    assert "--ink:#ffffff" in html
    assert "--ink:#0b0b0b" in html


def test_cells_survive_a_dump_load_round_trip(tmp_path: Path) -> None:
    cells = [
        _cell("0.00005", "0.0002", "3.5", "97"),
        _cell("0.00010", "0.0004", "1.25", "88"),
    ]
    path = tmp_path / "cells.json"
    dump_cells(path, cells, {"symbol": "KASUSDT"})
    back, saved = load_cells(path)
    assert saved["symbol"] == "KASUSDT"
    assert [c.grid_step for c in back] == [c.grid_step for c in cells]
    assert [c.realized for c in back] == [c.realized for c in cells]
    assert [c.curve for c in back] == [c.curve for c in cells]
    assert back[0].ratio == cells[0].ratio


def test_a_report_redrawn_from_saved_cells_matches_the_original(
    tmp_path: Path,
) -> None:
    cells = [_cell("0.00005", "0.0002", "3.5", "97")]
    path = tmp_path / "cells.json"
    dump_cells(path, cells)
    restored, _ = load_cells(path)
    assert build_report(restored, _meta()) == build_report(cells, _meta())
