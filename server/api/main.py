"""
api/main.py — FastAPI application entry point.

Lifespan:
  startup  → init_engine() + ensure_bucket()
  shutdown → dispose_engine()

Routers mounted:
  /healthz      — health.py
  /tasks        — tasks.py
  /experiments  — experiments.py, jobs.py (nested under experiments)
"""

from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from typing import AsyncGenerator

import structlog
import structlog.stdlib
import structlog.dev
import logging

from fastapi import FastAPI

from core.db import dispose_engine, init_engine
from core.storage import ensure_bucket

from api.routes import experiments, health, jobs, tasks


# ── Structured logging setup ───────────────────────────────────────────────────

def _configure_logging() -> None:
    structlog.configure(
        processors=[
            structlog.stdlib.add_log_level,
            structlog.stdlib.add_logger_name,
            structlog.dev.ConsoleRenderer(),
        ],
        wrapper_class=structlog.stdlib.BoundLogger,
        context_class=dict,
        logger_factory=structlog.stdlib.LoggerFactory(),
        cache_logger_on_first_use=True,
    )
    logging.basicConfig(
        format="%(message)s",
        level=logging.INFO,
    )


# ── Lifespan ───────────────────────────────────────────────────────────────────

log = structlog.get_logger()


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncGenerator[None, None]:
    _configure_logging()
    log.info("startup: initialising DB engine")
    init_engine()
    log.info("startup: ensuring MinIO bucket exists")
    try:
        await asyncio.to_thread(ensure_bucket)
    except Exception as exc:
        log.warning("startup: MinIO bucket check failed (continuing)", error=str(exc))
    log.info("startup: complete")
    yield
    log.info("shutdown: disposing DB engine")
    await dispose_engine()
    log.info("shutdown: complete")


# ── App ────────────────────────────────────────────────────────────────────────

app = FastAPI(
    title="Agent Optimization Service",
    version="0.1.0",
    lifespan=lifespan,
)

# Mount all routers.
app.include_router(health.router)
app.include_router(tasks.router)
app.include_router(experiments.router)
app.include_router(jobs.router)
