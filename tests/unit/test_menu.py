from __future__ import annotations

from tgbot.menu import bot_commands


def test_menu_lists_the_five_commands_worth_a_tap() -> None:
    assert [entry.command for entry in bot_commands()] == [
        "start",
        "pnl",
        "book",
        "apr",
        "notify",
    ]


def test_every_menu_entry_carries_help_text() -> None:
    assert all(entry.description for entry in bot_commands())
