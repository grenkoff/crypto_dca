from __future__ import annotations

from collections.abc import AsyncIterator, Iterator

import psycopg
import pytest
import pytest_asyncio
from sqlalchemy import make_url
from sqlalchemy.ext.asyncio import (
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from core.config.settings import database_settings

_TEST_DB = "crypto_dca_test_sa"


def _admin_and_test_urls() -> tuple[str, str]:
    base = make_url(database_settings().database_url)
    admin = base.set(drivername="postgresql", database="postgres")
    test = base.set(drivername="postgresql+asyncpg", database=_TEST_DB)
    return admin.render_as_string(hide_password=False), test.render_as_string(
        hide_password=False
    )


@pytest.fixture(scope="session")
def sa_test_db() -> Iterator[str]:
    admin_dsn, test_url = _admin_and_test_urls()
    with psycopg.connect(admin_dsn, autocommit=True) as conn:
        conn.execute(f'DROP DATABASE IF EXISTS "{_TEST_DB}" WITH (FORCE)')
        conn.execute(f'CREATE DATABASE "{_TEST_DB}"')
    try:
        yield test_url
    finally:
        with psycopg.connect(admin_dsn, autocommit=True) as conn:
            conn.execute(f'DROP DATABASE IF EXISTS "{_TEST_DB}" WITH (FORCE)')


@pytest_asyncio.fixture
async def db_session(sa_test_db: str) -> AsyncIterator[AsyncSession]:
    engine = create_async_engine(sa_test_db)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    try:
        async with factory() as session:
            yield session
            await session.rollback()
    finally:
        await engine.dispose()
