from __future__ import annotations

import asyncio
import os

import structlog
from aiogram import Bot, Dispatcher
from aiogram.filters import CommandStart
from aiogram.types import Message

log = structlog.get_logger()


async def run() -> None:
    token = os.environ["TELEGRAM_BOT_TOKEN"]
    bot = Bot(token=token)
    dp = Dispatcher()

    @dp.message(CommandStart())
    async def on_start(message: Message) -> None:
        await message.answer("hello")

    log.info("tgbot.started")
    await dp.start_polling(bot)


def main() -> None:
    asyncio.run(run())


if __name__ == "__main__":
    main()
