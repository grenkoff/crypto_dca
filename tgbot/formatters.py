"""Pure text formatters for Telegram messages.

Kept side-effect free so they're easily snapshot-testable.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import datetime
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation
from typing import Any


@dataclass(frozen=True)
class PnlSnapshot:
    """Banked profit over several rolling windows for /pnl."""

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


def build_pnl(snap: PnlSnapshot) -> str:
    """Render the /pnl message."""
    return (
        "*Banked profit, USDT*\n"
        f"today `{_signed(snap.today)}`\n"
        f"last 24 hours `{_signed(snap.last_24h)}`\n"
        f"last 7 days `{_signed(snap.last_7d)}`\n"
        f"last 30 days `{_signed(snap.last_30d)}`\n"
        f"last 365 days `{_signed(snap.last_365d)}`\n"
        f"all time `{_signed(snap.all_time)}`"
    )


def build_equity(total: Decimal | None) -> str:
    """The account's whole worth, as the exchange dashboard shows it."""
    if total is None:
        return ""
    return f"Balance `{_q(total, '0.01')}` USDT"


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
    lines = [head]
    moves = payload.get("compensations") or []
    if isinstance(moves, list):
        lines += [
            _format_move(move) for move in moves if isinstance(move, dict)
        ]
    pool_line = _format_pool(payload)
    if pool_line:
        lines.append(pool_line)
    return "\n".join(lines)


def _format_pool(payload: dict[str, Any]) -> str:
    """The share of profit that funds compensation, and what is left."""
    share = payload.get("share")
    pool = payload.get("pool")
    if not share or not pool:
        return ""
    return f"   Pool `{_dec(share) * 100:.0f}%` · `{_q(_dec(pool), '0.0001')}`"


def _format_move(move: dict[str, Any]) -> str:
    """One take-profit move — or one retirement — in a close message."""
    if move.get("kind") == "exit":
        return _format_exit(move)
    new_tp = _price5(move.get("new_tp"))
    old = move.get("old_tp")
    if old:
        return f"   ↓ TP `{_price5(old)}` → `{new_tp}`"
    return f"   ↓ TP `{new_tp}`"


def _format_drained(payload: dict[str, Any]) -> str:
    """Compensations bought by the pool with no close to trigger them.

    The pool is spent on the reconcile tick, and what makes a tick able
    to spend it is usually the fill just before: its take-profit joins
    the wall and opens a slot under it. So the header names that fill,
    the way the message announcing it did.
    """
    lines = [_drained_header(payload.get("after"))]
    moves = payload.get("compensations") or []
    if isinstance(moves, list):
        lines += [
            _format_move(move) for move in moves if isinstance(move, dict)
        ]
    pool = payload.get("pool")
    if pool:
        lines.append(f"   left `{_q(_dec(pool), '0.0001')}`")
    return "\n".join(lines)


def _drained_header(after: Any) -> str:
    """The title line, naming the fill this drain followed."""
    if not isinstance(after, dict):
        return "Pool spent"
    entry = _price5(after.get("entry"))
    tp = _price5(after.get("tp"))
    return f"Pool spent after 🟢 `{entry}` → TP `{tp}`"


def _format_exit(move: dict[str, Any]) -> str:
    """A stranded lot sold at market, paid for out of the pool."""
    lots = len(
        [part for part in (move.get("positions") or "").split(",") if part]
    )
    tail = f" x{lots}" if lots > 1 else ""
    drawn = _q(_dec(move.get("drawn")), "0.0001")
    return (
        f"   ✂️ TP `{_price5(move.get('old_tp'))}`{tail} sold at "
        f"`{_price5(move.get('price'))}` (`-{drawn}`)"
    )


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


def _format_adopted(payload: dict[str, Any]) -> str:
    """Coin that was outside the book, put back to work as lots."""
    lots = payload.get("lots", "?")
    qty = _dec(payload.get("qty"))
    entry = payload.get("entry")
    return (
        f"🧹 Adopted `{lots}` lot(s) · `{_q(qty, '0.01')}` "
        f"@ `{_price5(entry)}`"
    )


def _format_placed(payload: dict[str, Any]) -> str:
    """A resting buy laid on the grid."""
    return f"🔵 `{_price5(payload.get('price'))}`"


def _format_cancelled(payload: dict[str, Any]) -> str:
    """A resting buy pulled off the grid."""
    return f"❌ `{_price5(payload.get('price'))}`"


def _format_opened(payload: dict[str, Any]) -> str:
    """A buy that filled, with the take-profit now resting over it."""
    return (
        f"🟢 `{_price5(payload.get('entry_price'))}` → "
        f"TP `{_price5(payload.get('tp_price'))}`"
    )


def _format_error(payload: dict[str, Any]) -> str:
    """Something the trader wants a human to see."""
    return f"❌ Error: {payload.get('message', '?')}"


_RENDERERS: dict[str, Callable[[dict[str, Any]], str]] = {
    "order.placed": _format_placed,
    "order.cancelled": _format_cancelled,
    "position.opened": _format_opened,
    "position.closed": _format_closed,
    "pool.drained": _format_drained,
    "coin.adopted": _format_adopted,
    "error": _format_error,
}


def format_event(event: dict[str, Any]) -> str:
    """Render a single live event for the notifications channel."""
    etype = event.get("type", "?")
    payload = event.get("payload", {})
    render = _RENDERERS.get(etype)
    if render is None:
        return f"📨 {etype}: `{payload}`"
    return render(payload)
