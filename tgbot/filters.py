"""Allow-list filter: only TelegramUser.is_admin chat_ids can use the bot."""

from __future__ import annotations

from aiogram.filters import BaseFilter
from aiogram.types import Message
from asgiref.sync import sync_to_async

from core.trading.models import TelegramUser


class AdminUserFilter(BaseFilter):
    """aiogram filter passing only admin Telegram users."""

    async def __call__(self, message: Message) -> bool:
        """Return True if the message sender is a bot admin."""
        if message.from_user is None:
            return False
        return await _is_admin(message.from_user.id)


@sync_to_async
def _is_admin(chat_id: int) -> bool:
    return TelegramUser.objects.filter(chat_id=chat_id, is_admin=True).exists()
