from __future__ import annotations

import asyncio
import os

import django
import structlog

log = structlog.get_logger()


async def run() -> None:
    from core.config.logging import configure_logging

    configure_logging()
    os.environ.setdefault("DJANGO_SETTINGS_MODULE", "web.settings")
    django.setup()
    from core.config.settings import redis_settings
    from core.services.events import EventBus, NoOpEventBus
    from core.services.redis_bus import RedisEventBus
    from core.services.runtime import TraderRuntime

    url = redis_settings().redis_url
    bus: EventBus = RedisEventBus(url) if url else NoOpEventBus()

    runtime = TraderRuntime(bus=bus)
    log.info("trader.starting", bus=type(bus).__name__)
    await runtime.bootstrap()
    try:
        await runtime.run()
    finally:
        await runtime.shutdown()
        if isinstance(bus, RedisEventBus):
            await bus.close()


def main() -> None:
    asyncio.run(run())


if __name__ == "__main__":
    main()
