from __future__ import annotations

import pytest

from core.trading.models import TelegramUser
from tgbot.filters import _is_admin

pytestmark = pytest.mark.django_db(transaction=True)


async def test_admin_filter_recognises_admin() -> None:
    await TelegramUser.objects.acreate(chat_id=123, is_admin=True)
    assert await _is_admin(123) is True


async def test_admin_filter_rejects_non_admin() -> None:
    await TelegramUser.objects.acreate(chat_id=222, is_admin=False)
    assert await _is_admin(222) is False


async def test_admin_filter_rejects_unknown() -> None:
    assert await _is_admin(999) is False
