"""Smoke tests for the preflight management command."""

from __future__ import annotations

from io import StringIO

import pytest
from django.core.management import call_command

pytestmark = pytest.mark.django_db


def test_preflight_fails_with_no_credentials(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("BYBIT_API_KEY", "")
    monkeypatch.setenv("BYBIT_API_SECRET", "")
    monkeypatch.setenv("REDIS_URL", "")
    # Bust the cached settings
    from core.config import settings as s

    s.bybit_settings.cache_clear()
    s.redis_settings.cache_clear()

    out = StringIO()
    with pytest.raises(SystemExit) as exc:
        call_command("preflight", stdout=out)
    assert exc.value.code == 1
    output = out.getvalue()
    assert "bybit credentials" in output
    assert "✗" in output
    assert "BYBIT_API_KEY" in output


def test_preflight_warns_when_strategy_config_defaults_present(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """With default sane config + no creds, we still expect creds-fail; the config check should pass."""
    monkeypatch.setenv("BYBIT_API_KEY", "")
    monkeypatch.setenv("BYBIT_API_SECRET", "")
    monkeypatch.setenv("REDIS_URL", "")
    from core.config import settings as s

    s.bybit_settings.cache_clear()
    s.redis_settings.cache_clear()

    out = StringIO()
    with pytest.raises(SystemExit):
        call_command("preflight", stdout=out)
    output = out.getvalue()
    assert "strategy config" in output
    # default StrategyConfig should not fail the sanity check
    assert "✓ strategy config" in output
