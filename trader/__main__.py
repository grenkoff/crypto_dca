from __future__ import annotations

import asyncio
import signal

import structlog

log = structlog.get_logger()


async def run() -> None:
    log.info("trader.started")
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, stop.set)
    await stop.wait()
    log.info("trader.stopped")


def main() -> None:
    asyncio.run(run())


if __name__ == "__main__":
    main()
