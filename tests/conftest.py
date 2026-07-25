from __future__ import annotations

from collections.abc import AsyncIterator, Iterator

import psycopg
import pytest
import pytest_asyncio
from sqlalchemy import Engine, create_engine, make_url
from sqlalchemy.ext.asyncio import (
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.pool import NullPool

from core.config.settings import database_settings
from core.db.session import configure_engine

_TEST_DB = "crypto_dca_test_sa"


@pytest.fixture(autouse=True)
def _dao_on_django_test_db(
    request: pytest.FixtureRequest,
) -> Iterator[None]:
    """Point the DAO's async engine at Django's per-test database.

    The DAO (``core.services.repository``) now runs on SQLAlchemy, while
    tests still build fixtures via the Django ORM. Both must hit the same
    Postgres test DB. ``NullPool`` closes each connection on session exit,
    so nothing lingers across pytest-asyncio's per-test event loops.
    """
    marker = request.node.get_closest_marker("django_db")
    if marker is None:
        yield
        return
    request.getfixturevalue(
        "transactional_db" if marker.kwargs.get("transaction") else "db"
    )
    from django.db import connection

    url = make_url(database_settings().database_url).set(
        drivername="postgresql+asyncpg",
        database=connection.settings_dict["NAME"],
    )
    configure_engine(
        url.render_as_string(hide_password=False), poolclass=NullPool
    )
    try:
        yield
    finally:
        configure_engine(None)


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


@pytest.fixture
def sa_sync_engine() -> Iterator[Engine]:
    from django.db import connection

    base = make_url(database_settings().database_url)
    url = base.set(
        drivername="postgresql+psycopg",
        database=connection.settings_dict["NAME"],
    )
    engine = create_engine(url.render_as_string(hide_password=False))
    try:
        yield engine
    finally:
        engine.dispose()
