from __future__ import annotations

import asyncio
import os

import django
import structlog

log = structlog.get_logger()


async def run() -> None:
    os.environ.setdefault("DJANGO_SETTINGS_MODULE", "web.settings")
    django.setup()
    from core.services.runtime import TraderRuntime

    runtime = TraderRuntime()
    log.info("trader.starting")
    await runtime.bootstrap()
    try:
        await runtime.run()
    finally:
        await runtime.shutdown()


def main() -> None:
    asyncio.run(run())


if __name__ == "__main__":
    main()
