"""Bridge from pybit's threaded WebSocket callbacks into asyncio."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from dataclasses import dataclass
from typing import Any, Literal

import structlog

from core.exchange.bybit import _parse_execution, _parse_order
from core.exchange.types import Execution, Order

log = structlog.get_logger()

EventKind = Literal["execution", "order"]


@dataclass(frozen=True)
class StreamEvent:
    kind: EventKind
    payload: Execution | Order


class BybitPrivateStream:
    """Subscribes to private execution + order topics and exposes them as an
    async iterator.

    pybit's WebSocket invokes callbacks on its own daemon thread. We marshal
    each message back onto the asyncio loop via call_soon_threadsafe.
    """

    def __init__(
        self,
        api_key: str,
        api_secret: str,
        *,
        testnet: bool,
        queue_size: int = 1024,
    ) -> None:
        self._api_key = api_key
        self._api_secret = api_secret
        self._testnet = testnet
        self._queue: asyncio.Queue[StreamEvent] = asyncio.Queue(
            maxsize=queue_size
        )
        self._loop: asyncio.AbstractEventLoop | None = None
        self._ws: Any | None = None

    async def start(self) -> None:
        from pybit.unified_trading import WebSocket

        self._loop = asyncio.get_running_loop()
        self._ws = WebSocket(
            testnet=self._testnet,
            channel_type="private",
            api_key=self._api_key,
            api_secret=self._api_secret,
        )
        self._ws.execution_stream(callback=self._on_execution)
        self._ws.order_stream(callback=self._on_order)
        log.info("bybit_ws.started", testnet=self._testnet)

    async def stop(self) -> None:
        if self._ws is not None:
            try:
                self._ws.exit()
            except Exception as exc:  # pragma: no cover — best-effort shutdown
                log.warning("bybit_ws.shutdown_error", error=str(exc))
            self._ws = None

    async def events(self) -> AsyncIterator[StreamEvent]:
        while True:
            yield await self._queue.get()

    def _on_execution(self, message: dict[str, Any]) -> None:
        for item in message.get("data", []):
            try:
                event = StreamEvent(
                    kind="execution", payload=_parse_execution(item)
                )
            except Exception as exc:
                log.warning(
                    "bybit_ws.parse_execution_failed",
                    error=str(exc),
                    item=item,
                )
                continue
            self._enqueue(event)

    def _on_order(self, message: dict[str, Any]) -> None:
        for item in message.get("data", []):
            try:
                event = StreamEvent(kind="order", payload=_parse_order(item))
            except Exception as exc:
                log.warning(
                    "bybit_ws.parse_order_failed", error=str(exc), item=item
                )
                continue
            self._enqueue(event)

    def _enqueue(self, event: StreamEvent) -> None:
        loop = self._loop
        if loop is None:
            return
        loop.call_soon_threadsafe(self._put_nowait, event)

    def _put_nowait(self, event: StreamEvent) -> None:
        try:
            self._queue.put_nowait(event)
        except asyncio.QueueFull:
            log.error("bybit_ws.queue_full_dropping", kind=event.kind)
