"""Allow-list filter: only TelegramUser.is_admin chat_ids can use the bot."""

from __future__ import annotations

from aiogram.filters import BaseFilter
from aiogram.types import Message

from core.services import repository


class AdminUserFilter(BaseFilter):
    """aiogram filter passing only admin Telegram users."""

    async def __call__(self, message: Message) -> bool:
        """Return True if the message sender is a bot admin."""
        if message.from_user is None:
            return False
        return await repository.is_admin(message.from_user.id)
