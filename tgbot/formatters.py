"""Pure text formatters for Telegram messages.

Kept side-effect free so they're easily snapshot-testable.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation
from typing import Any


@dataclass(frozen=True)
class StatusSnapshot:
    """Snapshot for the /status message."""

    paused: bool
    open_positions: int
    started_at: datetime | None
    last_heartbeat: datetime | None


@dataclass(frozen=True)
class BalanceSnapshot:
    """Snapshot for the /balance message."""

    balances: dict[str, Decimal]


@dataclass(frozen=True)
class PnlSnapshot:
    """Realized PnL over several windows for /pnl."""

    today: Decimal
    week: Decimal
    month: Decimal
    year: Decimal
    total: Decimal


@dataclass(frozen=True)
class DigestSnapshot:
    """Snapshot for the daily digest message."""

    when_astana: datetime
    closed_24h: int
    pnl_24h: Decimal
    pnl_week: Decimal
    pnl_total: Decimal
    compensations_24h: int
    open_positions: int
    deployed: Decimal
    free_usdt: Decimal
    price: Decimal | None


@dataclass(frozen=True)
class OrderRow:
    """One open-position row for the /orders message."""

    level_index: int
    entry_price: Decimal
    qty: Decimal
    tp_price: Decimal | None


@dataclass(frozen=True)
class OrdersSnapshot:
    """Snapshot for the /orders message."""

    open_positions: list[OrderRow]


def _humanize_age(since: datetime | None, now: datetime | None = None) -> str:
    if since is None:
        return "n/a"
    now = now or datetime.now(tz=UTC)
    delta = now - since
    seconds = int(delta.total_seconds())
    if seconds < 60:
        return f"{seconds}s"
    if seconds < 3600:
        return f"{seconds // 60}m"
    if seconds < 86400:
        return f"{seconds // 3600}h"
    return f"{seconds // 86400}d"


def build_status(snap: StatusSnapshot, now: datetime | None = None) -> str:
    """Render the /status message."""
    state = "⏸ paused" if snap.paused else "▶ running"
    uptime = _humanize_age(snap.started_at, now)
    heartbeat = _humanize_age(snap.last_heartbeat, now)
    return (
        f"*Status:* {state}\n"
        f"*Open positions:* {snap.open_positions}\n"
        f"*Uptime:* {uptime}\n"
        f"*Last heartbeat:* {heartbeat} ago"
    )


def build_balance(snap: BalanceSnapshot) -> str:
    """Render the /balance message."""
    if not snap.balances:
        return "_no balances_"
    lines = [
        f"`{coin}`: {amount}" for coin, amount in sorted(snap.balances.items())
    ]
    return "*Balances:*\n" + "\n".join(lines)


def build_pnl(snap: PnlSnapshot) -> str:
    """Render the /pnl message."""
    return (
        "*Realized PnL (USDT)*\n"
        f"Today `{_signed(snap.today, '0.0001')}` 💰 "
        f"Week `{_signed(snap.week, '0.0001')}` 💰 "
        f"Month `{_signed(snap.month, '0.0001')}` 💰 "
        f"Year `{_signed(snap.year, '0.0001')}` 💰 "
        f"Total `{_signed(snap.total, '0.0001')}`"
    )


def build_orders(snap: OrdersSnapshot) -> str:
    """Render the /orders message."""
    if not snap.open_positions:
        return "_no open positions_"
    rows = [
        f"L{row.level_index:>3}  entry `{row.entry_price}` "
        f"qty `{row.qty}` → TP `{row.tp_price}`"
        for row in snap.open_positions
    ]
    return "*Open positions:*\n" + "\n".join(rows)


def _q(amount: Decimal, places: str = "0.0001") -> Decimal:
    return amount.quantize(Decimal(places))


def _price5(value: Any) -> str:
    """Render a price string/Decimal with a fixed 5 decimals (e.g. 0.02890)."""
    try:
        return str(Decimal(str(value)).quantize(Decimal("0.00001")))
    except (InvalidOperation, TypeError, ValueError):
        return str(value)


def _signed(amount: Decimal, places: str = "0.0001") -> str:
    q = _q(amount, places)
    return f"+{q}" if q >= 0 else str(q)


def _dec(value: Any) -> Decimal:
    try:
        return Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError):
        return Decimal(0)


def _format_closed(payload: dict[str, Any]) -> str:
    """Render a position.closed event, flagging compensated closes.

    A compensated lot's own realized is small-negative by design; showing the
    pair (realized + credit) makes clear the paired result stays in profit.
    """
    realized = _dec(payload.get("realized"))
    credit = _dec(payload.get("compensation_credit"))
    price = _price5(payload.get("price"))
    if credit > 0:
        pair = realized + credit
        return (
            f"💊 `{price}` → `{_signed(realized)}` USDT "
            f"(компенс., пара `{_signed(pair)}`)"
        )
    emoji = "💰" if realized >= 0 else "🔴"
    return f"{emoji} `{price}` → `{_signed(realized)}` USDT"


def build_digest(snap: DigestSnapshot) -> str:
    """Render the daily digest message."""
    price = f"`{snap.price}`" if snap.price is not None else "_n/a_"
    return (
        f"📊 *Daily digest* — {snap.when_astana:%d %b %H:%M} Astana\n"
        f"*Closed (24h):* {snap.closed_24h} → `{_signed(snap.pnl_24h)}` USDT\n"
        f"*PnL week:* `{_signed(snap.pnl_week)}` · "
        f"*total:* `{_signed(snap.pnl_total)}`\n"
        f"*Compensations (24h):* {snap.compensations_24h}\n"
        f"*Open positions:* {snap.open_positions} · "
        f"deployed `{_q(snap.deployed, '0.0001')}` USDT\n"
        f"*Free USDT:* `{_q(snap.free_usdt, '0.0001')}` · *KAS:* {price}"
    )


def format_event(event: dict[str, Any]) -> str:
    """Render a single live event for the notifications channel."""
    etype = event.get("type", "?")
    payload = event.get("payload", {})
    if etype == "order.placed":
        return f"🔵 `{_price5(payload.get('price'))}`"
    if etype == "order.cancelled":
        return f"❌ `{_price5(payload.get('price'))}`"
    if etype == "position.opened":
        return (
            f"🟢 `{_price5(payload.get('entry_price'))}` → "
            f"TP `{_price5(payload.get('tp_price'))}`"
        )
    if etype == "position.closed":
        return _format_closed(payload)
    if etype == "compensation.applied":
        return f"💊 TP↓ `{_price5(payload.get('new_tp'))}`"
    if etype == "error":
        return f"❌ Error: {payload.get('message', '?')}"
    return f"📨 {etype}: `{payload}`"
