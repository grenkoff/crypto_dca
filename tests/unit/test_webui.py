from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

import pytest
from fastapi import WebSocketDisconnect
from fastapi.testclient import TestClient
from httpx import ASGITransport, AsyncClient

from core.db.models import (
    Position,
    PositionStatus,
    StrategyConfig,
    TelegramUser,
)
from core.services import repository
from core.services.tokens import hash_token, new_token
from tests.conftest import add_rows
from webui.app import app

pytestmark = pytest.mark.db

_TOKEN = "secret-control-token"
_AUTH = {"Authorization": f"Bearer {_TOKEN}"}


def _client() -> AsyncClient:
    return AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    )


async def _admin(token: str = _TOKEN) -> None:
    await add_rows(
        TelegramUser(chat_id=1, is_admin=True, control_token=hash_token(token))
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


async def test_control_requires_token() -> None:
    async with _client() as client:
        resp = await client.post("/control/pause")
    assert resp.status_code == 401


async def test_control_rejects_bad_token() -> None:
    await _admin()
    async with _client() as client:
        resp = await client.post(
            "/control/pause", headers={"Authorization": "Bearer wrong"}
        )
    assert resp.status_code == 401
    assert await repository.is_paused() is False


async def test_pause_resume_and_audit() -> None:
    await _admin()
    async with _client() as client:
        paused = await client.post("/control/pause", headers=_AUTH)
        assert paused.status_code == 200
        assert paused.json()["paused"] is True
        assert await repository.is_paused() is True
        resumed = await client.post("/control/resume", headers=_AUTH)
        assert resumed.json()["paused"] is False
        assert await repository.is_paused() is False
        audit = await client.get("/api/audit")
    actions = [row["action"] for row in audit.json()]
    assert "pause" in actions
    assert "resume" in actions
    assert audit.json()[0]["actor"]  # actor recorded


async def test_config_update_valid_and_invalid() -> None:
    await _seed()
    await _admin()
    async with _client() as client:
        ok = await client.post(
            "/control/config",
            headers=_AUTH,
            json={"grid_step": "0.01", "max_open_orders": 15},
        )
        assert ok.status_code == 200
        bad = await client.post(
            "/control/config", headers=_AUTH, json={"grid_step": "0"}
        )
        assert bad.status_code == 400
        unknown = await client.post("/control/config", headers=_AUTH, json={})
        assert unknown.status_code == 400
    cfg = await repository.get_config()
    assert cfg.max_open_orders == 15
    assert cfg.grid_step == Decimal("0.01")


async def test_issue_token_requires_admin() -> None:
    await add_rows(TelegramUser(chat_id=9, is_admin=False))
    assert (
        await repository.issue_control_token(chat_id=9, token_hash="x" * 64)
        is False
    )
    assert (
        await repository.issue_control_token(chat_id=404, token_hash="x" * 64)
        is False
    )


def test_hash_token_is_stable_and_opaque() -> None:
    token = new_token()
    assert hash_token(token) == hash_token(token)
    assert len(hash_token(token)) == 64
    assert hash_token(token) != token
