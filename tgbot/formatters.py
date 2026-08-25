"""Pure text formatters for Telegram messages.

Kept side-effect free so they're easily snapshot-testable.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation
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
    """Realized PnL over several rolling windows for /pnl."""

    today: Decimal
    last_24h: Decimal
    last_7d: Decimal
    last_30d: Decimal
    last_365d: Decimal
    all_time: Decimal


@dataclass(frozen=True)
class DigestSnapshot:
    """Snapshot for the daily digest message."""

    when_utc: datetime
    closed_24h: int
    pnl_24h: Decimal
    pnl_week: Decimal
    pnl_total: Decimal
    compensations_24h: int
    open_positions: int
    deployed: Decimal
    free_usdt: Decimal
    price: Decimal | None
    tp_projection: Sequence[Decimal]


@dataclass(frozen=True)
class AprSnapshot:
    """Inputs and result for the /apr annual-return estimate."""

    realized: Decimal
    days: Decimal
    avg_deployed: Decimal
    free: Decimal
    profit_per_day: Decimal
    apr: Decimal | None


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
        "*Realized PnL, USDT*\n"
        f"today `{_signed(snap.today)}`\n"
        f"last 24 hours `{_signed(snap.last_24h)}`\n"
        f"last 7 days `{_signed(snap.last_7d)}`\n"
        f"last 30 days `{_signed(snap.last_30d)}`\n"
        f"last 365 days `{_signed(snap.last_365d)}`\n"
        f"all time `{_signed(snap.all_time)}`"
    )


def build_unlock(locked_now: Decimal, days: Decimal | None) -> str:
    """Render the locked-USDT amount and days-to-unlock on one line."""
    locked = _q(locked_now, "0.01")
    if days is None:
        return f"Locked `{locked}` USDT · unlock `n/a`"
    d = days.quantize(Decimal("1"), rounding=ROUND_HALF_UP)
    return f"Locked `{locked}` USDT · unlock ~`{d}` days"


_APR_GENERAL = (
    r"\mathrm{APR} = \frac{\mathrm{realized}\times 365}"
    r"{\mathrm{days}\times(\overline{\mathrm{locked}}+\mathrm{free})}"
    r"\times 100\%"
)


def apr_formulas(snap: AprSnapshot) -> tuple[str, str] | None:
    """LaTeX (general, substituted) for the APR estimate, or None.

    ``locked`` is barred to mark the average deployed capital over the span.
    """
    if snap.apr is None:
        return None
    realized = _q(snap.realized, "0.0001")
    days = _q(snap.days, "0.1")
    avg_deployed = _q(snap.avg_deployed, "0.01")
    free = _q(snap.free, "0.01")
    apr = _q(snap.apr, "0.1")
    num = f"{realized}" + r"\times 365"
    den = f"{days}" + r"\times(" + f"{avg_deployed}+{free}" + ")"
    substituted = (
        r"\mathrm{APR} = \frac{" + num + "}{" + den + r"}\times 100\% "
        r"\approx " + f"{apr}" + r"\%\,\mathrm{/yr}"
    )
    return _APR_GENERAL, substituted


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


_SPARK = "▁▂▃▄▅▆▇█"


def _sparkline(values: Sequence[Decimal]) -> str:
    """Render values as a unicode bar strip; flat when they barely move."""
    if not values:
        return ""
    low, high = min(values), max(values)
    span = high - low
    if span <= 0:
        return _SPARK[0] * len(values)
    top = len(_SPARK) - 1
    return "".join(_SPARK[int((v - low) / span * top)] for v in values)


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
    """Render a close together with the compensations it paid for.

    A compensated lot's own realized is small-negative by design; showing
    the pair (realized + credit) makes clear the paired result stays in
    profit. Take-profit moves funded by this close are listed underneath,
    so one close is one message however many moves it bought.
    """
    realized = _dec(payload.get("realized"))
    credit = _dec(payload.get("compensation_credit"))
    price = _price5(payload.get("price"))
    if credit > 0:
        pair = realized + credit
        head = (
            f"💊 `{price}` → `{_signed(realized)}` USDT "
            f"(compensated, pair `{_signed(pair)}`)"
        )
    else:
        emoji = "💰" if realized >= 0 else "🔴"
        head = f"{emoji} `{price}` → `{_signed(realized)}` USDT"
    moves = payload.get("compensations") or []
    if not isinstance(moves, list) or not moves:
        return head
    lines = [head]
    lines += [_format_move(move) for move in moves if isinstance(move, dict)]
    return "\n".join(lines)


def _format_move(move: dict[str, Any]) -> str:
    """One take-profit move inside a close message."""
    new_tp = _price5(move.get("new_tp"))
    old = move.get("old_tp")
    if old:
        return f"   ↓ TP `{_price5(old)}` → `{new_tp}`"
    return f"   ↓ TP `{new_tp}`"


def build_digest(snap: DigestSnapshot) -> str:
    """Render the daily digest message."""
    price = f"`{snap.price}`" if snap.price is not None else "_n/a_"
    return (
        f"📊 *Daily digest* — {snap.when_utc:%d %b %H:%M} UTC\n"
        f"*Closed (24h):* {snap.closed_24h} → `{_signed(snap.pnl_24h)}` USDT\n"
        f"*PnL week:* `{_signed(snap.pnl_week)}` · "
        f"*total:* `{_signed(snap.pnl_total)}`\n"
        f"*Compensations (24h):* {snap.compensations_24h}\n"
        f"*Open positions:* {snap.open_positions} · "
        f"deployed `{_q(snap.deployed, '0.0001')}` USDT\n"
        f"*Free USDT:* `{_q(snap.free_usdt, '0.0001')}` · *KAS:* {price}\n"
        f"{_build_projection(snap.tp_projection)}"
    )


def _build_projection(series: Sequence[Decimal]) -> str:
    """Render the all-TPs-filled projection and its recent trend."""
    if not series:
        return "*If all TPs fill:* _n/a_"
    now = series[-1]
    line = f"*If all TPs fill:* `{_q(now, '0.01')}` USDT"
    if len(series) < 2:
        return line
    first = series[0]
    return (
        f"{line}\n*{len(series)}d:* `{_q(first, '0.01')}` → "
        f"`{_q(now, '0.01')}` (`{_signed(now - first, '0.01')}`) "
        f"{_sparkline(series)}"
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
    if etype == "error":
        return f"❌ Error: {payload.get('message', '?')}"
    return f"📨 {etype}: `{payload}`"
