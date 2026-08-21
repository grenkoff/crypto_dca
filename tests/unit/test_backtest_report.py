from __future__ import annotations

from datetime import date
from decimal import Decimal

from backtest.report import ReportMeta, build_report
from backtest.sweep import Cell


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
