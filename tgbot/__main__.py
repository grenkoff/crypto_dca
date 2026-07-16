from __future__ import annotations

import asyncio
import contextlib
import os
import signal

import django
import structlog

log = structlog.get_logger()


async def run() -> None:
    from core.config.logging import configure_logging

    configure_logging()
    os.environ.setdefault("DJANGO_SETTINGS_MODULE", "web.settings")
    django.setup()

    from aiogram import Bot, Dispatcher

    from core.config.settings import redis_settings, telegram_settings
    from core.services.redis_bus import RedisEventBus
    from tgbot.digest import run_digest_scheduler
    from tgbot.handlers import router
    from tgbot.notifications import run_subscriber

    settings = telegram_settings()
    if not settings.bot_token:
        raise RuntimeError("TELEGRAM_BOT_TOKEN is required")

    bot = Bot(token=settings.bot_token)
    dp = Dispatcher()
    dp.include_router(router)

    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, stop.set)

    redis_url = redis_settings().redis_url
    bus = RedisEventBus(redis_url) if redis_url else None

    log.info("tgbot.starting", has_redis=bus is not None)

    polling_task = asyncio.create_task(
        dp.start_polling(bot, handle_signals=False)
    )
    digest_task = asyncio.create_task(run_digest_scheduler(bot, stop))
    subscriber_task: asyncio.Task[None] | None = None
    if bus is not None:
        subscriber_task = asyncio.create_task(run_subscriber(bus, bot, stop))

    await stop.wait()
    log.info("tgbot.shutting_down")

    await dp.stop_polling()
    polling_task.cancel()
    digest_task.cancel()
    if subscriber_task is not None:
        subscriber_task.cancel()
    for task in (polling_task, digest_task, subscriber_task):
        if task is None:
            continue
        with contextlib.suppress(asyncio.CancelledError):
            await task

    await bot.session.close()
    if bus is not None:
        await bus.close()


def main() -> None:
    asyncio.run(run())


if __name__ == "__main__":
    main()
