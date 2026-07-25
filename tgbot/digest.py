"""Daily-digest scheduler.

Polls every ``_POLL_SECONDS`` and fires the digest the first time the wall
clock passes the configured UTC trigger on a new day. ``digest_last_sent``
is stamped transactionally so a restart cannot double-send.
"""

from __future__ import annotations

import asyncio
import contextlib

import structlog
from aiogram import Bot

from core.services import repository
from tgbot.formatters import build_digest
from tgbot.queries import digest_snapshot

log = structlog.get_logger()

_POLL_SECONDS = 30


async def _send_digest(bot: Bot) -> None:
    snap = await digest_snapshot()
    text = build_digest(snap)
    for chat_id in await repository.admin_chat_ids():
        try:
            await bot.send_message(chat_id, text, parse_mode="Markdown")
        except Exception as exc:
            log.warning(
                "tgbot.digest_send_failed", chat_id=chat_id, error=str(exc)
            )


async def run_digest_scheduler(bot: Bot, stop: asyncio.Event) -> None:
    """Send the daily digest at the configured time until stopped."""
    log.info("tgbot.digest_scheduler_started")
    while not stop.is_set():
        try:
            if await repository.claim_digest_due():
                await _send_digest(bot)
        except Exception as exc:
            log.exception("tgbot.digest_failed", error=str(exc))
        with contextlib.suppress(TimeoutError):
            await asyncio.wait_for(stop.wait(), timeout=_POLL_SECONDS)
