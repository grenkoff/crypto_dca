from __future__ import annotations

from datetime import UTC, datetime

import pytest
from aiogram.types import Chat, Message

from tgbot.menu import bot_commands, keyboard, label, on

_COMMANDS = (
    "status",
    "balance",
    "pnl",
    "book",
    "orders",
    "apr",
    "notify",
    "digesttime",
    "help",
)


def test_every_command_has_a_button() -> None:
    texts = {button.text for row in keyboard().keyboard for button in row}
    assert texts == {label(name) for name in _COMMANDS}


def test_the_keyboard_stays_open_and_sized_to_the_screen() -> None:
    board = keyboard()
    assert board.is_persistent is True
    assert board.resize_keyboard is True
    assert all(len(row) <= 3 for row in board.keyboard)


def test_telegram_menu_lists_the_same_commands_with_help_text() -> None:
    listed = bot_commands()
    assert [entry.command for entry in listed] == list(_COMMANDS)
    assert all(entry.description for entry in listed)


def _message(text: str) -> Message:
    return Message.model_construct(
        message_id=1,
        date=datetime.now(UTC),
        chat=Chat(id=1, type="private"),
        text=text,
    )


async def test_a_tapped_button_reaches_the_same_handler_as_the_command() -> (
    None
):
    matched = on("status")
    assert await matched(_message(label("status")), bot=None)
    assert await matched(_message("/status"), bot=None)
    assert not await matched(_message("something else"), bot=None)


async def test_every_button_resolves_to_its_own_command() -> None:
    # `|` silently degrades Command into a magic filter that raises, so
    # each pairing is checked rather than trusted
    for name in _COMMANDS:
        assert await on(name)(_message(label(name)), bot=None)
        assert await on(name)(_message(f"/{name}"), bot=None)


def test_token_is_deliberately_left_off_the_keyboard() -> None:
    # tapping it would rotate the dashboard token by accident
    with pytest.raises(KeyError):
        label("token")
