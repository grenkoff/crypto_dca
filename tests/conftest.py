from __future__ import annotations

from collections.abc import AsyncIterator, Iterator

import psycopg
import pytest
import pytest_asyncio
from sqlalchemy import create_engine, make_url
from sqlalchemy.ext.asyncio import (
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.pool import NullPool

import core.db.models  # noqa: F401  (register tables on Base.metadata)
from core.config.settings import database_settings
from core.db.base import Base
from core.db.session import configure_engine, new_session

_TEST_DB = "crypto_dca_test_sa"


def _urls() -> tuple[str, str, str, str]:
    base = make_url(database_settings().database_url)
    admin = base.set(drivername="postgresql", database="postgres")
    dsn = base.set(drivername="postgresql", database=_TEST_DB)
    async_url = base.set(drivername="postgresql+asyncpg", database=_TEST_DB)
    sync_url = base.set(drivername="postgresql+psycopg", database=_TEST_DB)
    return (
        admin.render_as_string(hide_password=False),
        dsn.render_as_string(hide_password=False),
        async_url.render_as_string(hide_password=False),
        sync_url.render_as_string(hide_password=False),
    )


_ADMIN_DSN, _TEST_DSN, _ASYNC_URL, _SYNC_URL = _urls()
_TABLES = ", ".join(
    f'"{t.name}"' for t in reversed(Base.metadata.sorted_tables)
)


@pytest.fixture(scope="session", autouse=True)
def _sa_database() -> Iterator[None]:
    """Create a disposable test DB with the SA schema for the session."""
    with psycopg.connect(_ADMIN_DSN, autocommit=True) as conn:
        conn.execute(f'DROP DATABASE IF EXISTS "{_TEST_DB}" WITH (FORCE)')
        conn.execute(f'CREATE DATABASE "{_TEST_DB}"')
    sync_engine = create_engine(_SYNC_URL)
    Base.metadata.create_all(sync_engine)
    sync_engine.dispose()
    try:
        yield
    finally:
        with psycopg.connect(_ADMIN_DSN, autocommit=True) as conn:
            conn.execute(f'DROP DATABASE IF EXISTS "{_TEST_DB}" WITH (FORCE)')


@pytest.fixture(autouse=True)
def db(request: pytest.FixtureRequest) -> Iterator[None]:
    """Bind the DAO to a truncated test DB for tests marked ``db``.

    ``NullPool`` closes each connection on session exit, so nothing lingers
    across pytest-asyncio's per-test event loops.
    """
    if request.node.get_closest_marker("db") is None:
        yield
        return
    with psycopg.connect(_TEST_DSN, autocommit=True) as conn:
        conn.execute(f"TRUNCATE {_TABLES} RESTART IDENTITY CASCADE")
    configure_engine(_ASYNC_URL, poolclass=NullPool)
    try:
        yield
    finally:
        configure_engine(None)


async def add_rows[T: object](*objs: T) -> tuple[T, ...]:
    """Insert SA model instances in one transaction; return them (with pks)."""
    async with new_session() as session, session.begin():
        session.add_all(objs)
    return objs


async def add_one[T: object](obj: T) -> T:
    """Insert one SA model instance and return it (with its pk populated)."""
    async with new_session() as session, session.begin():
        session.add(obj)
    return obj


@pytest_asyncio.fixture
async def db_session() -> AsyncIterator[AsyncSession]:
    engine = create_async_engine(_ASYNC_URL, poolclass=NullPool)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    try:
        async with factory() as session:
            yield session
            await session.rollback()
    finally:
        await engine.dispose()
