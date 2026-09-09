"""Tests for the instance-lease check the restart script polls."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from typer.testing import CliRunner

from cli.__main__ import app
from core.services import repository

runner = CliRunner()


def _heartbeat(monkeypatch: pytest.MonkeyPatch, beat: datetime | None) -> None:
    async def _last_heartbeat() -> datetime | None:
        return beat

    monkeypatch.setattr(repository, "last_heartbeat", _last_heartbeat)


def test_lease_is_held_while_a_heartbeat_is_fresh(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _heartbeat(monkeypatch, datetime.now(tz=UTC) - timedelta(seconds=10))
    result = runner.invoke(app, ["trader-lease"])
    assert result.exit_code == 1
    assert "lease held" in result.stdout


def test_lease_is_clear_once_the_heartbeat_goes_stale(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # the trader that owned this heartbeat was killed 5 minutes ago
    _heartbeat(monkeypatch, datetime.now(tz=UTC) - timedelta(seconds=300))
    result = runner.invoke(app, ["trader-lease"])
    assert result.exit_code == 0
    assert "lease clear" in result.stdout


def test_lease_is_clear_when_no_trader_ever_ran(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _heartbeat(monkeypatch, None)
    result = runner.invoke(app, ["trader-lease"])
    assert result.exit_code == 0
    assert "never" in result.stdout
