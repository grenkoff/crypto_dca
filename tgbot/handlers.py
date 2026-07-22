"""Aiogram command handlers."""

from __future__ import annotations

import asyncio
from datetime import time

from aiogram import F, Router
from aiogram.filters import Command, CommandObject
from aiogram.types import (
    BufferedInputFile,
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
)

from core.trading.models import NotificationSettings
from tgbot.charts import render_pnl_chart
from tgbot.filters import AdminUserFilter
from tgbot.formatters import (
    build_balance,
    build_orders,
    build_pnl,
    build_status,
    build_unlock,
)
from tgbot.notify_settings import (
    TOGGLE_LABELS,
    load_settings,
    set_digest_time_utc,
    toggle_field,
)
from tgbot.queries import (
    balance_snapshot,
    daily_close_line,
    orders_snapshot,
    pnl_curve_data,
    pnl_snapshot,
    status_snapshot,
    unlock_estimate,
)

router = Router(name="tgbot.commands")
router.message.filter(AdminUserFilter())
router.callback_query.filter(AdminUserFilter())


@router.message(Command("start", "help"))
async def cmd_start(message: Message) -> None:
    """Reply with the command list."""
    await message.answer(
        "Crypto DCA bot.\n"
        "Commands: /status /balance /pnl /orders /notify /digesttime",
        parse_mode="Markdown",
    )


def _notify_keyboard(s: NotificationSettings) -> InlineKeyboardMarkup:
    rows = [
        [
            InlineKeyboardButton(
                text=f"{'✅' if getattr(s, field) else '❌'} {label}",
                callback_data=f"notify:toggle:{field}",
            )
        ]
        for field, label in TOGGLE_LABELS
    ]
    return InlineKeyboardMarkup(inline_keyboard=rows)


def _notify_text(s: NotificationSettings) -> str:
    return (
        "*Notifications* — tap to toggle\n"
        f"Digest time: `{s.digest_time_utc:%H:%M}` UTC  "
        "(change with /digesttime HH:MM)"
    )


@router.message(Command("notify"))
async def cmd_notify(message: Message) -> None:
    """Show the notification toggle keyboard."""
    s = await load_settings()
    await message.answer(
        _notify_text(s),
        parse_mode="Markdown",
        reply_markup=_notify_keyboard(s),
    )


@router.callback_query(F.data.startswith("notify:toggle:"))
async def cb_notify_toggle(call: CallbackQuery) -> None:
    """Toggle a notification setting from the inline keyboard."""
    field = str(call.data).rsplit(":", 1)[-1]
    try:
        await toggle_field(field)
    except ValueError:
        await call.answer("unknown toggle")
        return
    s = await load_settings()
    if isinstance(call.message, Message):
        await call.message.edit_reply_markup(reply_markup=_notify_keyboard(s))
    await call.answer("updated")


@router.message(Command("digesttime"))
async def cmd_digesttime(message: Message, command: CommandObject) -> None:
    """Set the daily digest time (UTC)."""
    arg = (command.args or "").strip()
    try:
        hh, mm = (int(x) for x in arg.split(":", 1))
        digest_time = time(hh, mm)
    except (ValueError, TypeError):
        await message.answer(
            "Usage: `/digesttime HH:MM` (UTC)", parse_mode="Markdown"
        )
        return
    await set_digest_time_utc(digest_time)
    await message.answer(
        f"📊 Digest time set to `{digest_time:%H:%M}` UTC",
        parse_mode="Markdown",
    )


@router.message(Command("status"))
async def cmd_status(message: Message) -> None:
    """Reply with the bot status."""
    snap = await status_snapshot()
    await message.answer(build_status(snap), parse_mode="Markdown")


@router.message(Command("balance"))
async def cmd_balance(message: Message) -> None:
    """Reply with wallet balances."""
    snap = await balance_snapshot()
    await message.answer(build_balance(snap), parse_mode="Markdown")


@router.message(Command("pnl"))
async def cmd_pnl(message: Message) -> None:
    """Reply with realized PnL and a funds-and-profit chart."""
    snap = await pnl_snapshot()
    days, base_capital, locked, dates = await pnl_curve_data()
    unlock_days, _ = await unlock_estimate()
    caption = (
        build_pnl(snap) + "\n\n" + build_unlock(base_capital, unlock_days)
    )
    if not days:
        await message.answer(caption, parse_mode="Markdown")
        return
    price = await daily_close_line(dates)
    png = await asyncio.to_thread(
        render_pnl_chart, days, base_capital, locked, price
    )
    await message.answer_photo(
        BufferedInputFile(png, filename="pnl.png"),
        caption=caption,
        parse_mode="Markdown",
    )


@router.message(Command("orders"))
async def cmd_orders(message: Message) -> None:
    """Reply with open positions."""
    snap = await orders_snapshot()
    await message.answer(build_orders(snap), parse_mode="Markdown")
