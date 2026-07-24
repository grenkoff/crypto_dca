from __future__ import annotations

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from core.db.session import async_database_url


def test_async_url_uses_asyncpg_driver() -> None:
    assert async_database_url().startswith("postgresql+asyncpg://")


async def test_db_session_executes(db_session: AsyncSession) -> None:
    result = await db_session.execute(text("select 1"))
    assert result.scalar() == 1
