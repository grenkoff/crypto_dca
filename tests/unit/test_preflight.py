"""Smoke tests for the preflight CLI."""

from __future__ import annotations

import pytest
from typer.testing import CliRunner

from cli.__main__ import app

pytestmark = pytest.mark.django_db(transaction=True)

runner = CliRunner()


def _no_creds(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("BYBIT_API_KEY", "")
    monkeypatch.setenv("BYBIT_API_SECRET", "")
    monkeypatch.setenv("REDIS_URL", "")
    from core.config import settings as s

    s.bybit_settings.cache_clear()
    s.redis_settings.cache_clear()


def test_preflight_fails_with_no_credentials(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _no_creds(monkeypatch)
    result = runner.invoke(app, ["preflight"])
    assert result.exit_code == 1
    assert "bybit credentials" in result.output
    assert "✗" in result.output
    assert "BYBIT_API_KEY" in result.output


def test_preflight_warns_when_strategy_config_defaults_present(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """With default sane config + no creds, we still expect creds-fail; the
    config check should pass."""
    _no_creds(monkeypatch)
    result = runner.invoke(app, ["preflight"])
    assert "strategy config" in result.output
    # default StrategyConfig should not fail the sanity check
    assert "✓ strategy config" in result.output
