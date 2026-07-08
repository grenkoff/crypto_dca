from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal

from tgbot.formatters import (
    BalanceSnapshot,
    OrderRow,
    OrdersSnapshot,
    PnlSnapshot,
    StatusSnapshot,
    build_balance,
    build_orders,
    build_pnl,
    build_status,
    format_event,
)


def test_build_status_running() -> None:
    now = datetime(2026, 5, 13, 12, 0, tzinfo=UTC)
    snap = StatusSnapshot(
        paused=False,
        open_positions=4,
        started_at=now - timedelta(hours=3, minutes=12),
        last_heartbeat=now - timedelta(seconds=15),
    )
    text = build_status(snap, now=now)
    assert "running" in text
    assert "Open positions:* 4" in text
    assert "Uptime:* 3h" in text
    assert "15s" in text


def test_build_status_paused_no_heartbeat() -> None:
    snap = StatusSnapshot(paused=True, open_positions=0, started_at=None, last_heartbeat=None)
    text = build_status(snap)
    assert "paused" in text
    assert "n/a" in text


def test_build_balance_empty() -> None:
    assert build_balance(BalanceSnapshot(balances={})) == "_no balances_"


def test_build_balance_sorted() -> None:
    snap = BalanceSnapshot(balances={"USDT": Decimal("100"), "BTC": Decimal("0.001")})
    text = build_balance(snap)
    # BTC sorts before USDT
    assert text.index("BTC") < text.index("USDT")


def test_build_pnl() -> None:
    text = build_pnl(PnlSnapshot(today=Decimal("1.50"), week=Decimal("8.00"), total=Decimal("42")))
    assert "Today `1.50`" in text
    assert "Total `42.00`" in text
    # single line for the three figures
    assert text.count("\n") == 1


def test_build_pnl_rounds_to_two_decimals() -> None:
    text = build_pnl(
        PnlSnapshot(
            today=Decimal("0.212272180125"),
            week=Decimal("1.038725796753"),
            total=Decimal("1.038725796753"),
        )
    )
    assert "Today `0.21`" in text
    assert "Week `1.04`" in text
    assert "Total `1.04`" in text


def test_build_orders_empty() -> None:
    assert build_orders(OrdersSnapshot(open_positions=[])) == "_no open positions_"


def test_build_orders_rows() -> None:
    snap = OrdersSnapshot(
        open_positions=[
            OrderRow(
                level_index=0,
                entry_price=Decimal("60000"),
                qty=Decimal("0.001"),
                tp_price=Decimal("60600"),
            ),
            OrderRow(
                level_index=1,
                entry_price=Decimal("59400"),
                qty=Decimal("0.001"),
                tp_price=Decimal("59994"),
            ),
        ]
    )
    text = build_orders(snap)
    assert "L  0" in text and "L  1" in text
    assert "60600" in text


def test_format_event_position_opened() -> None:
    text = format_event(
        {
            "type": "position.opened",
            "payload": {"level": 2, "entry_price": "60000", "tp_price": "60600"},
        }
    )
    assert "L2" in text
    assert "60600" in text


def test_format_event_compensation() -> None:
    text = format_event(
        {
            "type": "compensation.applied",
            "payload": {
                "target_position": 7,
                "source_position": 3,
                "new_tp": "59500",
                "profit": "0.50",
            },
        }
    )
    assert "#7" in text
    assert "59500" in text


def test_format_event_unknown_falls_back_to_raw() -> None:
    text = format_event({"type": "weird.thing", "payload": {"foo": "bar"}})
    assert "weird.thing" in text
