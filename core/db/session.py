"""Async SQLAlchemy engine and session factory.

Bound to ``DATABASE_URL`` via :func:`core.config.settings.database_settings`.
The DAO (``core.services.repository``) opens sessions through
:func:`new_session`. Tests rebind the process engine to Django's test
database via :func:`configure_engine`.
"""

from __future__ import annotations

from typing import Any

from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from core.config.settings import database_settings

_ASYNC_PREFIX = "postgresql+asyncpg://"

_engine: AsyncEngine | None = None
_factory: async_sessionmaker[AsyncSession] | None = None
_url_override: str | None = None
_engine_kwargs: dict[str, Any] = {"pool_pre_ping": True}


def async_database_url() -> str:
    """Return the DATABASE_URL normalised to the asyncpg driver."""
    if _url_override is not None:
        return _url_override
    url = database_settings().database_url
    if not url:
        raise ValueError("DATABASE_URL is not set")
    for prefix in ("postgres://", "postgresql://", "postgresql+psycopg://"):
        if url.startswith(prefix):
            return _ASYNC_PREFIX + url[len(prefix) :]
    return url


def engine() -> AsyncEngine:
    """Return the process-wide async engine (created lazily)."""
    global _engine
    if _engine is None:
        _engine = create_async_engine(async_database_url(), **_engine_kwargs)
    return _engine


def session_factory() -> async_sessionmaker[AsyncSession]:
    """Return the cached async session factory."""
    global _factory
    if _factory is None:
        _factory = async_sessionmaker(engine(), expire_on_commit=False)
    return _factory


def new_session() -> AsyncSession:
    """Open a fresh :class:`AsyncSession` from the process factory."""
    return session_factory()()


def configure_engine(url: str | None, **engine_kwargs: Any) -> None:
    """Rebind the process engine to ``url`` (or clear the override).

    A testing hook: point the DAO at a disposable database, optionally
    with custom engine kwargs (e.g. ``poolclass=NullPool``). The previous
    engine reference is dropped; the next :func:`engine` call rebuilds it.
    """
    global _engine, _factory, _url_override, _engine_kwargs
    _url_override = url
    _engine_kwargs = engine_kwargs or {"pool_pre_ping": True}
    _engine = None
    _factory = None
