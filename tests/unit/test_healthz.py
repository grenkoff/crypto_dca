from __future__ import annotations

from django.test import Client


def test_healthz_no_auth_required() -> None:
    resp = Client().get("/healthz")
    assert resp.status_code == 200
    assert resp.json() == {"status": "ok"}
