"""The commands Telegram lists behind its own Menu button."""

from __future__ import annotations

from aiogram.types import BotCommand

_MENU: tuple[tuple[str, str], ...] = (
    ("start", "Check the bot is alive"),
    ("pnl", "Banked profit and the chart"),
    ("book", "Ladder of resting orders"),
    ("apr", "Estimated annual return"),
    ("notify", "Toggle notifications"),
)


def bot_commands() -> list[BotCommand]:
    """The command list Telegram shows behind its Menu button.

    Only the handful worth a tap; the rest still work when typed.
    """
    return [
        BotCommand(command=command, description=description)
        for command, description in _MENU
    ]
