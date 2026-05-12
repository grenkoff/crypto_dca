"""Aiogram command handlers."""

from __future__ import annotations

import structlog
from aiogram import F, Router
from aiogram.filters import Command
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message

from core.config.settings import bybit_settings
from core.exchange.bybit import BybitClient
from tgbot.filters import AdminUserFilter
from tgbot.formatters import build_balance, build_orders, build_pnl, build_status
from tgbot.queries import (
    balance_snapshot,
    orders_snapshot,
    pnl_snapshot,
    set_paused,
    status_snapshot,
)

log = structlog.get_logger()

router = Router(name="tgbot.commands")
router.message.filter(AdminUserFilter())
router.callback_query.filter(AdminUserFilter())


@router.message(Command("start", "help"))
async def cmd_start(message: Message) -> None:
    await message.answer(
        "Crypto DCA bot.\nCommands: /status /balance /pnl /orders /pause /resume /cancelall",
        parse_mode="Markdown",
    )


@router.message(Command("status"))
async def cmd_status(message: Message) -> None:
    snap = await status_snapshot()
    await message.answer(build_status(snap), parse_mode="Markdown")


@router.message(Command("balance"))
async def cmd_balance(message: Message) -> None:
    snap = await balance_snapshot()
    await message.answer(build_balance(snap), parse_mode="Markdown")


@router.message(Command("pnl"))
async def cmd_pnl(message: Message) -> None:
    snap = await pnl_snapshot()
    await message.answer(build_pnl(snap), parse_mode="Markdown")


@router.message(Command("orders"))
async def cmd_orders(message: Message) -> None:
    snap = await orders_snapshot()
    await message.answer(build_orders(snap), parse_mode="Markdown")


@router.message(Command("pause"))
async def cmd_pause(message: Message) -> None:
    await set_paused(True)
    await message.answer("⏸ paused — existing orders remain, no new ones will be placed")


@router.message(Command("resume"))
async def cmd_resume(message: Message) -> None:
    await set_paused(False)
    await message.answer("▶ resumed")


@router.message(Command("cancelall"))
async def cmd_cancelall(message: Message) -> None:
    kb = InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="✅ Confirm cancel all", callback_data="cancelall:confirm"
                ),
                InlineKeyboardButton(text="❌ Abort", callback_data="cancelall:abort"),
            ]
        ]
    )
    await message.answer(
        "⚠️ This cancels *all* open orders and pauses the bot. Continue?",
        parse_mode="Markdown",
        reply_markup=kb,
    )


@router.callback_query(F.data == "cancelall:abort")
async def cb_cancelall_abort(call: CallbackQuery) -> None:
    if isinstance(call.message, Message):
        await call.message.edit_text("Aborted.")
    await call.answer()


@router.callback_query(F.data == "cancelall:confirm")
async def cb_cancelall_confirm(call: CallbackQuery) -> None:
    from core.trading.models import StrategyConfig

    settings = bybit_settings()
    client = BybitClient.from_credentials(
        settings.api_key, settings.api_secret, testnet=settings.testnet
    )
    cfg = await StrategyConfig.objects.aget(pk=1)
    try:
        await client.cancel_all(str(cfg.symbol))
    except Exception as exc:  # pragma: no cover - depends on live API
        log.exception("tgbot.cancelall_failed", error=str(exc))
        if isinstance(call.message, Message):
            await call.message.edit_text(f"❌ Cancel failed: {exc}")
        await call.answer()
        return
    await set_paused(True)
    if isinstance(call.message, Message):
        await call.message.edit_text("✅ All orders cancelled. Bot paused.")
    await call.answer()
