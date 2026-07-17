"""Daily-digest scheduler.

Polls every ``_POLL_SECONDS`` and fires the digest the first time the wall
clock passes the configured UTC trigger on a new day. ``digest_last_sent``
is stamped transactionally so a restart cannot double-send.
"""

from __future__ import annotations

import asyncio
import contextlib
from datetime import UTC, datetime

import structlog
from aiogram import Bot
from asgiref.sync import sync_to_async

from core.trading.models import NotificationSettings, TelegramUser
from tgbot.formatters import build_digest
from tgbot.queries import digest_snapshot

log = structlog.get_logger()

_POLL_SECONDS = 30


@sync_to_async
def _admin_chat_ids() -> list[int]:
    return list(
        TelegramUser.objects.filter(is_admin=True).values_list(
            "chat_id", flat=True
        )
    )


@sync_to_async
def _claim_due() -> bool:
    """Return True and stamp ``digest_last_sent`` iff the digest is due now."""
    s = NotificationSettings.load()
    if not s.digest_enabled:
        return False
    now = datetime.now(tz=UTC)
    scheduled = datetime.combine(now.date(), s.digest_time_utc, tzinfo=UTC)
    if now < scheduled or s.digest_last_sent == now.date():
        return False
    s.digest_last_sent = now.date()
    s.save(update_fields=["digest_last_sent", "updated_at"])
    return True


async def _send_digest(bot: Bot) -> None:
    snap = await digest_snapshot()
    text = build_digest(snap)
    for chat_id in await _admin_chat_ids():
        try:
            await bot.send_message(chat_id, text, parse_mode="Markdown")
        except Exception as exc:
            log.warning(
                "tgbot.digest_send_failed", chat_id=chat_id, error=str(exc)
            )


async def run_digest_scheduler(bot: Bot, stop: asyncio.Event) -> None:
    """Send the daily digest at the configured time until stopped."""
    log.info("tgbot.digest_scheduler_started")
    try:
        while not stop.is_set():
            try:
                if await _claim_due():
                    await _send_digest(bot)
            except Exception as exc:
                log.exception("tgbot.digest_failed", error=str(exc))
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(stop.wait(), timeout=_POLL_SECONDS)
    except asyncio.CancelledError:
        raise
