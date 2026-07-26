from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

import pytest
from fastapi import WebSocketDisconnect
from fastapi.testclient import TestClient
from httpx import ASGITransport, AsyncClient

from core.db.models import Position, PositionStatus, StrategyConfig
from tests.conftest import add_rows
from webui.app import app

pytestmark = pytest.mark.db


def _client() -> AsyncClient:
    return AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    )


async def _seed() -> None:
    await add_rows(
        StrategyConfig(id=1, symbol="KASUSDT"),
        Position(
            level_index=5,
            entry_price=Decimal("0.02"),
            qty=Decimal("100"),
            tp_order_id="tp-5",
            tp_price=Decimal("0.03"),
            status=PositionStatus.OPEN,
            opened_at=datetime(2026, 7, 1, tzinfo=UTC),
        ),
    )


async def test_healthz() -> None:
    async with _client() as client:
        resp = await client.get("/healthz")
    assert resp.status_code == 200
    assert resp.json() == {"status": "ok"}


async def test_api_dashboard_reports_pnl_and_orders() -> None:
    await _seed()
    async with _client() as client:
        resp = await client.get("/api/dashboard")
    assert resp.status_code == 200
    body = resp.json()
    assert body["symbol"] == "KASUSDT"
    assert body["open_positions"] == 1
    assert Decimal(body["deployed"]) == Decimal("2")
    assert set(body["pnl"]) == {
        "today",
        "last_24h",
        "last_7d",
        "last_30d",
        "all_time",
    }
    assert body["orders"][0]["level_index"] == 5
    assert body["orders"][0]["tp_price"] == "0.03"


async def test_dashboard_html_renders() -> None:
    await _seed()
    async with _client() as client:
        resp = await client.get("/")
    assert resp.status_code == 200
    assert "KASUSDT" in resp.text
    assert "running" in resp.text
    assert "Live events" in resp.text


async def test_fragment_renders_snapshot_only() -> None:
    await _seed()
    async with _client() as client:
        resp = await client.get("/fragment")
    assert resp.status_code == 200
    assert "Take-profit" in resp.text
    assert "<html" not in resp.text.lower()


async def _fake_events() -> AsyncIterator[dict[str, Any]]:
    yield {"type": "position.opened", "payload": {"level": 5}, "ts": 1.0}
    yield {"type": "order.placed", "payload": {}, "ts": 2.0}


def test_ws_streams_events(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("webui.app._subscribe_events", _fake_events)
    with TestClient(app).websocket_connect("/ws") as ws:
        first = ws.receive_json()
        second = ws.receive_json()
    assert first["type"] == "position.opened"
    assert second["type"] == "order.placed"


def test_ws_closes_without_redis(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("REDIS_URL", "")
    from core.config import settings

    settings.redis_settings.cache_clear()
    with (
        TestClient(app).websocket_connect("/ws") as ws,
        pytest.raises(WebSocketDisconnect),
    ):
        ws.receive_text()
