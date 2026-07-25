"""Subscribe to Redis events and forward as Telegram messages to admin
chats."""

from __future__ import annotations

import asyncio
from collections.abc import Iterable

import structlog
from aiogram import Bot

from core.services import repository
from core.services.redis_bus import RedisEventBus
from tgbot.formatters import format_event
from tgbot.notify_settings import event_enabled

log = structlog.get_logger()


async def run_subscriber(
    bus: RedisEventBus, bot: Bot, stop: asyncio.Event
) -> None:
    """Relay bus events to admin chats until the stop event is set."""
    log.info("tgbot.subscriber_started")
    try:
        async for event in bus.subscribe():
            if stop.is_set():
                break
            if not await event_enabled(event.get("type", "")):
                continue
            chat_ids = await repository.admin_chat_ids()
            text = format_event(event)
            await _broadcast(bot, chat_ids, text)
    except asyncio.CancelledError:
        raise
    except Exception as exc:
        log.exception("tgbot.subscriber_crashed", error=str(exc))


async def _broadcast(bot: Bot, chats: Iterable[int], text: str) -> None:
    for chat_id in chats:
        try:
            await bot.send_message(chat_id, text, parse_mode="Markdown")
        except Exception as exc:
            log.warning("tgbot.send_failed", chat_id=chat_id, error=str(exc))
