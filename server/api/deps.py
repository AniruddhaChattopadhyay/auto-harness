"""
api/deps.py — FastAPI dependency providers.

Provides:
- SessionDep: async DB session (commit on success, rollback on exception)
- SettingsDep: cached Settings singleton
- not_found(): raise 404 with a uniform JSON body
"""

from __future__ import annotations

from typing import Annotated, AsyncGenerator

from fastapi import Depends, HTTPException
from sqlalchemy.ext.asyncio import AsyncSession

from core.config import Settings, get_settings
from core.db import get_session


# ── Session dependency ─────────────────────────────────────────────────────────

async def _yield_session() -> AsyncGenerator[AsyncSession, None]:
    """
    FastAPI dependency that wraps core.db.get_session().

    Commits on clean exit; rolls back if an exception propagates.
    The contextmanager form in core.db already handles commit/rollback,
    so we just enter it as an async context manager.
    """
    async with get_session() as session:
        yield session


SessionDep = Annotated[AsyncSession, Depends(_yield_session)]


# ── Settings dependency ────────────────────────────────────────────────────────

SettingsDep = Annotated[Settings, Depends(get_settings)]


# ── Error helpers ──────────────────────────────────────────────────────────────

def not_found(model: str, id: str) -> HTTPException:
    """Return a 404 HTTPException with a uniform body."""
    raise HTTPException(
        status_code=404,
        detail=f"{model} '{id}' not found.",
    )
