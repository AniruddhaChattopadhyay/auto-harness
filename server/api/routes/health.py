"""
api/routes/health.py — liveness endpoint.
"""

from __future__ import annotations

import structlog
from fastapi import APIRouter
from sqlalchemy import text

from api.deps import SessionDep

log = structlog.get_logger()

router = APIRouter(tags=["health"])


@router.get("/healthz")
async def healthz(session: SessionDep) -> dict:
    """Return OK and perform a lightweight DB ping."""
    await session.execute(text("SELECT 1"))
    return {"status": "ok"}
