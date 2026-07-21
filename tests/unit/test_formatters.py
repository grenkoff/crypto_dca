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
    build_unlock,
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
    snap = StatusSnapshot(
        paused=True, open_positions=0, started_at=None, last_heartbeat=None
    )
    text = build_status(snap)
    assert "paused" in text
    assert "n/a" in text


def test_build_balance_empty() -> None:
    assert build_balance(BalanceSnapshot(balances={})) == "_no balances_"


def test_build_balance_sorted() -> None:
    snap = BalanceSnapshot(
        balances={"USDT": Decimal("100"), "BTC": Decimal("0.001")}
    )
    text = build_balance(snap)
    # BTC sorts before USDT
    assert text.index("BTC") < text.index("USDT")


def test_build_pnl() -> None:
    text = build_pnl(
        PnlSnapshot(
            today=Decimal("0.75"),
            last_24h=Decimal("1.50"),
            last_7d=Decimal("8.00"),
            last_30d=Decimal("20.00"),
            last_365d=Decimal("40.00"),
            all_time=Decimal("42"),
        )
    )
    assert "today `+0.7500`" in text
    assert "last 24 hours `+1.5000`" in text
    assert "last 7 days `+8.0000`" in text
    assert "last 30 days `+20.0000`" in text
    assert "last 365 days `+40.0000`" in text
    assert "all time `+42.0000`" in text
    assert "💰" not in text
    # title plus six window lines
    assert text.count("\n") == 6


def test_build_unlock_shows_locked_and_days() -> None:
    text = build_unlock(Decimal("410.905"), Decimal("136.7"), Decimal("2.345"))
    assert "Locked in open trades: `410.90` USDT" in text
    assert "~`137` days" in text
    assert "`2.34`/day comp" in text


def test_build_unlock_na_when_days_unknown() -> None:
    text = build_unlock(Decimal("100"), None, Decimal("0"))
    assert "Locked in open trades: `100.00` USDT" in text
    assert "Unlock all: `n/a`" in text


def test_build_pnl_rounds_to_four_decimals() -> None:
    text = build_pnl(
        PnlSnapshot(
            today=Decimal("0.212272180125"),
            last_24h=Decimal("0.212272180125"),
            last_7d=Decimal("1.038725796753"),
            last_30d=Decimal("1.038725796753"),
            last_365d=Decimal("1.038725796753"),
            all_time=Decimal("1.038725796753"),
        )
    )
    assert "today `+0.2123`" in text
    assert "last 24 hours `+0.2123`" in text
    assert "last 7 days `+1.0387`" in text
    assert "all time `+1.0387`" in text


def test_build_orders_empty() -> None:
    assert (
        build_orders(OrdersSnapshot(open_positions=[]))
        == "_no open positions_"
    )


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


def test_format_event_order_placed() -> None:
    text = format_event(
        {
            "type": "order.placed",
            "payload": {
                "level": 291,
                "price": "0.0291",
                "order_id": "2254818261395047680",
            },
        }
    )
    assert text == "🔵 `0.02910`"


def test_format_event_order_cancelled() -> None:
    text = format_event(
        {"type": "order.cancelled", "payload": {"price": "0.02775"}}
    )
    assert text == "❌ `0.02775`"


def test_format_event_position_opened() -> None:
    text = format_event(
        {
            "type": "position.opened",
            "payload": {
                "level": 289,
                "entry_price": "0.0289",
                "tp_price": "0.029",
            },
        }
    )
    assert text.startswith("🟢")  # green circle
    assert "\n" not in text  # single line
    assert "Position opened" not in text  # label dropped
    assert "L289" not in text  # level dropped
    assert "entry" not in text  # label dropped
    assert text == "🟢 `0.02890` → TP `0.02900`"


def test_format_event_position_closed_profit() -> None:
    text = format_event(
        {
            "type": "position.closed",
            "payload": {
                "level": 293,
                "price": "0.029",
                "realized": "0.010804278125",
            },
        }
    )
    assert text == "💰 `0.02900` → `+0.0108` USDT"


def test_format_event_position_closed_loss() -> None:
    text = format_event(
        {
            "type": "position.closed",
            "payload": {
                "level": 291,
                "price": "0.0289",
                "realized": "-0.00625031625",
            },
        }
    )
    assert text == "🔴 `0.02890` → `-0.0063` USDT"


def test_format_event_position_closed_compensated() -> None:
    text = format_event(
        {
            "type": "position.closed",
            "payload": {
                "level": 712,
                "price": "0.0277",
                "realized": "-0.006250158750",
                "compensation_credit": "0.006250158850",
            },
        }
    )
    assert text == (
        "💊 `0.02770` → `-0.0063` USDT (compensated, pair `+0.0000`)"
    )


def test_format_event_compensation() -> None:
    text = format_event(
        {
            "type": "compensation.applied",
            "payload": {
                "target_position": 179,
                "source_position": 3,
                "old_tp": "0.0295",
                "new_tp": "0.0294",
                "profit": "0.0108603075",
            },
        }
    )
    assert text == "💊 TP `0.02950` ↓ `0.02940`"


def test_format_event_compensation_without_old_tp() -> None:
    text = format_event(
        {
            "type": "compensation.applied",
            "payload": {
                "target_position": 179,
                "source_position": 3,
                "new_tp": "0.0294",
                "profit": "0.0108603075",
            },
        }
    )
    assert text == "💊 TP↓ `0.02940`"


def test_format_event_unknown_falls_back_to_raw() -> None:
    text = format_event({"type": "weird.thing", "payload": {"foo": "bar"}})
    assert "weird.thing" in text
