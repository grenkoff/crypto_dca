from __future__ import annotations

import pytest

from tgbot.notify_settings import (
    EVENT_TOGGLE,
    TOGGLE_LABELS,
    event_enabled,
    toggle_field,
)


def test_every_event_toggle_field_is_a_real_toggle() -> None:
    fields = {f for f, _ in TOGGLE_LABELS}
    for field in EVENT_TOGGLE.values():
        assert field in fields


pytestmark = pytest.mark.db


async def test_toggle_field_flips_and_persists() -> None:
    before = await event_enabled("position.closed")
    new = await toggle_field("notify_closed")
    assert new is (not before)
    assert await event_enabled("position.closed") is new


async def test_toggle_field_rejects_unknown() -> None:
    with pytest.raises(ValueError):
        await toggle_field("notify_bogus")


async def test_unknown_event_type_never_suppressed() -> None:
    assert await event_enabled("something.new") is True
