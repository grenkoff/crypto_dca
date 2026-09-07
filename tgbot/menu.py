"""The button menu that mirrors the bot's commands.

One definition drives three things: the labels on the reply keyboard,
the filters that let a tapped label reach the same handler as the typed
command, and the list Telegram shows in its own command menu.
"""

from __future__ import annotations

from typing import Any

from aiogram import F
from aiogram.filters import Command
from aiogram.filters.logic import or_f
from aiogram.types import (
    BotCommand,
    KeyboardButton,
    ReplyKeyboardMarkup,
)

_MENU: tuple[tuple[str, str, str], ...] = (
    ("status", "📈 Status", "Grid and bot state"),
    ("balance", "💰 Balance", "Wallet balances"),
    ("pnl", "📊 PnL", "Banked profit and the chart"),
    ("book", "📖 Book", "Ladder of resting orders"),
    ("orders", "📋 Orders", "Open positions"),
    ("apr", "📐 APR", "Estimated annual return"),
    ("notify", "🔔 Notify", "Toggle notifications"),
    ("digesttime", "🕒 Digest", "Set the daily digest time"),
    ("help", "❓ Help", "Show the commands"),
)

_LABELS = {command: label for command, label, _ in _MENU}
_COLUMNS = 3


def label(command: str) -> str:
    """The button text that stands for ``command``."""
    return _LABELS[command]


def on(*commands: str) -> Any:
    """Filter matching the typed command or its tapped button.

    ``or_f`` is the composition aiogram provides; the ``|`` operator
    does not work here, because ``Command`` is not a magic filter and
    the result silently degrades into one that raises when resolved.
    """
    tapped = [F.text == _LABELS[n] for n in commands if n in _LABELS]
    return or_f(Command(*commands), *tapped)


def keyboard() -> ReplyKeyboardMarkup:
    """The persistent keyboard carrying every command."""
    buttons = [KeyboardButton(text=label) for _, label, _ in _MENU]
    rows = [
        buttons[start : start + _COLUMNS]
        for start in range(0, len(buttons), _COLUMNS)
    ]
    return ReplyKeyboardMarkup(
        keyboard=rows, resize_keyboard=True, is_persistent=True
    )


def bot_commands() -> list[BotCommand]:
    """The command list Telegram shows in its own menu."""
    return [
        BotCommand(command=command, description=description)
        for command, _, description in _MENU
    ]
