from __future__ import annotations

from datetime import UTC, datetime, time
from decimal import Decimal

import pytest

from core.trading.models import NotificationSettings
from tgbot.digest import _claim_due
from tgbot.formatters import DigestSnapshot, build_digest

pytestmark = pytest.mark.django_db(transaction=True)


async def _set(**kwargs: object) -> None:
    from asgiref.sync import sync_to_async

    @sync_to_async
    def _save() -> None:
        s = NotificationSettings.load()
        for k, v in kwargs.items():
            setattr(s, k, v)
        s.save()

    await _save()


async def test_claim_due_fires_once_per_day() -> None:
    # trigger well in the past today, never sent -> fires, then is deduped
    await _set(
        digest_enabled=True, digest_time_utc=time(0, 0), digest_last_sent=None
    )
    assert await _claim_due() is True
    assert await _claim_due() is False  # already stamped for today


async def test_claim_due_skips_when_disabled() -> None:
    await _set(
        digest_enabled=False, digest_time_utc=time(0, 0), digest_last_sent=None
    )
    assert await _claim_due() is False


async def test_claim_due_skips_before_trigger() -> None:
    # trigger one minute before end of day -> almost never reached yet today
    await _set(
        digest_enabled=True,
        digest_time_utc=time(23, 59),
        digest_last_sent=None,
    )
    now = datetime.now(tz=UTC)
    expected = now.replace(hour=23, minute=59, second=0, microsecond=0) <= now
    assert await _claim_due() is expected


def test_build_digest_signs_and_labels() -> None:
    snap = DigestSnapshot(
        when_utc=datetime(2026, 7, 7, 0, 0),
        closed_24h=24,
        pnl_24h=Decimal("0.21"),
        pnl_week=Decimal("1.40"),
        pnl_total=Decimal("-3.10"),
        compensations_24h=6,
        open_positions=52,
        deployed=Decimal("268.33"),
        free_usdt=Decimal("3.10"),
        price=Decimal("0.0308"),
    )
    text = build_digest(snap)
    assert "Daily digest" in text
    assert "Closed (24h):* 24" in text
    assert "+0.21" in text
    assert "-3.10" in text  # negative total keeps its sign
    assert "UTC" in text
    assert "0.0308" in text


def test_build_digest_handles_missing_price() -> None:
    snap = DigestSnapshot(
        when_utc=datetime(2026, 7, 7, 0, 0),
        closed_24h=0,
        pnl_24h=Decimal(0),
        pnl_week=Decimal(0),
        pnl_total=Decimal(0),
        compensations_24h=0,
        open_positions=0,
        deployed=Decimal(0),
        free_usdt=Decimal(0),
        price=None,
    )
    text = build_digest(snap)
    assert "n/a" in text
