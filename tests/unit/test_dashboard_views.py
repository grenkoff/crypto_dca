from __future__ import annotations

from decimal import Decimal

import pytest
from django.contrib.auth import get_user_model
from django.test import Client
from django.urls import reverse

from core.trading.models import StrategyConfig

pytestmark = pytest.mark.django_db


@pytest.fixture
def admin_client() -> Client:
    user_model = get_user_model()
    user_model.objects.create_superuser(username="admin", password="pw", email="a@b.com")
    client = Client()
    client.login(username="admin", password="pw")
    return client


def test_index_requires_login(client: Client) -> None:
    resp = client.get(reverse("dashboard:index"))
    assert resp.status_code == 302
    assert "/admin/login/" in resp["Location"]


def test_index_renders_for_admin(admin_client: Client) -> None:
    resp = admin_client.get(reverse("dashboard:index"))
    assert resp.status_code == 200
    assert b"Crypto DCA" in resp.content
    assert b"Open positions" in resp.content


def test_config_post_saves(admin_client: Client) -> None:
    cfg = StrategyConfig.load()
    resp = admin_client.post(
        reverse("dashboard:config"),
        {
            "symbol": "BTCUSDT",
            "grid_mode": "percent",
            "grid_step": "0.005",
            "order_qty_quote": "20",
            "top_anchor": "",
            "min_profit_quote": "0.05",
            "maker_fee": "0.001",
            "max_open_orders": "15",
        },
    )
    assert resp.status_code == 302
    cfg.refresh_from_db()
    assert cfg.order_qty_quote == Decimal("20")
    assert cfg.max_open_orders == 15
