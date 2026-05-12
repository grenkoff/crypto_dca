from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal

import pytest
from django.contrib.auth import get_user_model
from django.urls import reverse
from rest_framework.test import APIClient

from core.trading.models import BotStatus, Position, PositionStatus

pytestmark = pytest.mark.django_db


@pytest.fixture
def auth_client() -> APIClient:
    user_model = get_user_model()
    user_model.objects.create_user(username="api", password="pw")
    client = APIClient()
    client.force_authenticate(user=user_model.objects.get(username="api"))
    return client


def test_status_requires_auth() -> None:
    client = APIClient()
    resp = client.get(reverse("api:status"))
    assert resp.status_code == 403


def test_status_returns_shape(auth_client: APIClient) -> None:
    bot = BotStatus.load()
    bot.paused = False
    bot.save()
    resp = auth_client.get(reverse("api:status"))
    assert resp.status_code == 200
    assert resp.data["paused"] is False
    assert "open_positions" in resp.data


def test_positions_endpoint_filters(auth_client: APIClient) -> None:
    Position.objects.create(
        level_index=0,
        entry_price=Decimal("60000"),
        qty=Decimal("0.001"),
        opened_at=datetime.now(tz=UTC),
        status=PositionStatus.OPEN,
    )
    Position.objects.create(
        level_index=1,
        entry_price=Decimal("59000"),
        qty=Decimal("0.001"),
        opened_at=datetime.now(tz=UTC),
        closed_at=datetime.now(tz=UTC),
        realized_pnl=Decimal("0.10"),
        status=PositionStatus.CLOSED,
    )
    resp = auth_client.get(reverse("api:positions") + "?status=open")
    assert resp.status_code == 200
    assert len(resp.data) == 1
    assert resp.data[0]["level_index"] == 0

    resp = auth_client.get(reverse("api:positions") + "?status=closed")
    assert len(resp.data) == 1
    assert resp.data[0]["realized_pnl"] == "0.100000000000"


def test_pnl_endpoint(auth_client: APIClient) -> None:
    Position.objects.create(
        level_index=0,
        entry_price=Decimal("60000"),
        qty=Decimal("0.001"),
        opened_at=datetime.now(tz=UTC),
        closed_at=datetime.now(tz=UTC),
        realized_pnl=Decimal("0.25"),
        status=PositionStatus.CLOSED,
    )
    resp = auth_client.get(reverse("api:pnl"))
    assert resp.status_code == 200
    assert Decimal(resp.data["total"]) == Decimal("0.25")


def test_pause_resume_endpoints(auth_client: APIClient) -> None:
    resp = auth_client.post(reverse("api:pause"))
    assert resp.status_code == 200
    assert BotStatus.load().paused is True
    resp = auth_client.post(reverse("api:resume"))
    assert resp.status_code == 200
    assert BotStatus.load().paused is False
