"""Trader daemon entrypoint: bootstrap and run the live TraderRuntime."""

from __future__ import annotations

import asyncio

import structlog

log = structlog.get_logger()


async def run() -> None:
    """Configure logging and Django, build the bus, run the trader."""
    from core.config.bootstrap import bootstrap_django

    bootstrap_django()
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
    """Console entrypoint: run the async trader to completion."""
    asyncio.run(run())


if __name__ == "__main__":
    main()
