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
    paused: bool
    open_positions: int
    started_at: datetime | None
    last_heartbeat: datetime | None


@dataclass(frozen=True)
class BalanceSnapshot:
    balances: dict[str, Decimal]  # coin -> free


@dataclass(frozen=True)
class PnlSnapshot:
    today: Decimal
    week: Decimal
    total: Decimal


@dataclass(frozen=True)
class DigestSnapshot:
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
    level_index: int
    entry_price: Decimal
    qty: Decimal
    tp_price: Decimal | None


@dataclass(frozen=True)
class OrdersSnapshot:
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
    if not snap.balances:
        return "_no balances_"
    lines = [f"`{coin}`: {amount}" for coin, amount in sorted(snap.balances.items())]
    return "*Balances:*\n" + "\n".join(lines)


def build_pnl(snap: PnlSnapshot) -> str:
    return (
        "*Realized PnL (USDT)*\n"
        f"Today `{_q(snap.today, '0.01')}` · "
        f"Week `{_q(snap.week, '0.01')}` · "
        f"Total `{_q(snap.total, '0.01')}`"
    )


def build_orders(snap: OrdersSnapshot) -> str:
    if not snap.open_positions:
        return "_no open positions_"
    rows = [
        f"L{row.level_index:>3}  entry `{row.entry_price}` qty `{row.qty}` → TP `{row.tp_price}`"
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


def build_digest(snap: DigestSnapshot) -> str:
    price = f"`{snap.price}`" if snap.price is not None else "_n/a_"
    return (
        f"📊 *Daily digest* — {snap.when_astana:%d %b %H:%M} Astana\n"
        f"*Closed (24h):* {snap.closed_24h} → `{_signed(snap.pnl_24h)}` USDT\n"
        f"*PnL week:* `{_signed(snap.pnl_week)}` · *total:* `{_signed(snap.pnl_total)}`\n"
        f"*Compensations (24h):* {snap.compensations_24h}\n"
        f"*Open positions:* {snap.open_positions} · deployed `{_q(snap.deployed, '0.01')}` USDT\n"
        f"*Free USDT:* `{_q(snap.free_usdt, '0.01')}` · *KAS:* {price}"
    )


def format_event(event: dict[str, Any]) -> str:
    etype = event.get("type", "?")
    payload = event.get("payload", {})
    if etype == "order.placed":
        return (
            f"📌 Buy placed: L{payload.get('level')} @ `{payload.get('price')}`\n"
            f"`{payload.get('order_id', '')}`"
        )
    if etype == "position.opened":
        return (
            f"🟢 `{_price5(payload.get('entry_price'))}` → TP `{_price5(payload.get('tp_price'))}`"
        )
    if etype == "position.closed":
        realized = payload.get("realized", "0")
        emoji = (
            "💰"
            if str(realized).lstrip("-").replace(".", "").isdigit()
            and not str(realized).startswith("-")
            else "🔴"
        )
        return f"{emoji} Position closed: L{payload.get('level')} → `{realized}` USDT"
    if etype == "compensation.applied":
        return (
            f"🩹 Compensation: pos #{payload.get('target_position')} "
            f"new TP `{payload.get('new_tp')}` "
            f"(profit `{payload.get('profit')}`)"
        )
    if etype == "error":
        return f"❌ Error: {payload.get('message', '?')}"
    return f"📨 {etype}: `{payload}`"
