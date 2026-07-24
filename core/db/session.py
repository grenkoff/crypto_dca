"""Async SQLAlchemy engine and session factory.

Bound to ``DATABASE_URL`` via :func:`core.config.settings.database_settings`.
Django still owns the schema in Phase 0; this only reads it.
"""

from __future__ import annotations

from functools import lru_cache

from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from core.config.settings import database_settings

_ASYNC_PREFIX = "postgresql+asyncpg://"


def async_database_url() -> str:
    """Return the DATABASE_URL normalised to the asyncpg driver."""
    url = database_settings().database_url
    if not url:
        raise ValueError("DATABASE_URL is not set")
    for prefix in ("postgres://", "postgresql://", "postgresql+psycopg://"):
        if url.startswith(prefix):
            return _ASYNC_PREFIX + url[len(prefix) :]
    return url


@lru_cache(maxsize=1)
def engine() -> AsyncEngine:
    """Return the process-wide async engine (created lazily)."""
    return create_async_engine(async_database_url(), pool_pre_ping=True)


@lru_cache(maxsize=1)
def session_factory() -> async_sessionmaker[AsyncSession]:
    """Return the cached async session factory."""
    return async_sessionmaker(engine(), expire_on_commit=False)
