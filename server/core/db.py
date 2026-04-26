"""
core/db.py — async SQLAlchemy engine + session factory.

Design notes:
- Engine is created lazily via init_engine() so the module can be imported
  without a running Postgres instance (e.g. during alembic autogenerate).
- get_session() is an async context manager suitable for use both as a
  FastAPI dependency (yield style) and as a plain async-with block in workers.
"""

from __future__ import annotations

from contextlib import asynccontextmanager
from typing import AsyncGenerator

from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from core.config import get_settings

_engine: AsyncEngine | None = None
async_session_factory: async_sessionmaker[AsyncSession] | None = None


def _get_engine() -> AsyncEngine:
    global _engine, async_session_factory
    if _engine is None:
        raise RuntimeError(
            "DB engine not initialised. Call init_engine() during application startup."
        )
    return _engine


def init_engine(database_url: str | None = None) -> AsyncEngine:
    """
    Create (or recreate) the async engine and session factory.
    Call once during lifespan startup.
    """
    global _engine, async_session_factory

    url = database_url or get_settings().database_url
    if not url.startswith("postgresql+asyncpg://"):
        # Transparently upgrade plain postgresql:// → asyncpg driver.
        url = url.replace("postgresql://", "postgresql+asyncpg://", 1)

    _engine = create_async_engine(
        url,
        echo=False,
        pool_pre_ping=True,
        pool_size=5,
        max_overflow=10,
    )
    async_session_factory = async_sessionmaker(
        _engine,
        class_=AsyncSession,
        expire_on_commit=False,
    )
    return _engine


async def dispose_engine() -> None:
    """Drain the connection pool. Call during lifespan shutdown."""
    global _engine
    if _engine is not None:
        await _engine.dispose()
        _engine = None


@asynccontextmanager
async def get_session() -> AsyncGenerator[AsyncSession, None]:
    """
    Async context manager that yields a database session.

    Usage as FastAPI dependency::

        async def route(session: AsyncSession = Depends(get_session)):
            ...

    Usage in worker code::

        async with get_session() as session:
            ...

    The session is committed on clean exit and rolled back on exception.
    """
    factory = async_session_factory
    if factory is None:
        # Lazily initialise if init_engine() was never called (e.g. in tests).
        init_engine()
        factory = async_session_factory

    async with factory() as session:  # type: ignore[union-attr]
        try:
            yield session
            await session.commit()
        except Exception:
            await session.rollback()
            raise
