from __future__ import annotations

import pytest

from core.db.models import TelegramUser
from core.services import repository
from tests.conftest import add_rows

pytestmark = pytest.mark.db


async def test_admin_filter_recognises_admin() -> None:
    await add_rows(TelegramUser(chat_id=123, is_admin=True))
    assert await repository.is_admin(123) is True


async def test_admin_filter_rejects_non_admin() -> None:
    await add_rows(TelegramUser(chat_id=222, is_admin=False))
    assert await repository.is_admin(222) is False


async def test_admin_filter_rejects_unknown() -> None:
    assert await repository.is_admin(999) is False
