"""
migrations/env.py — Alembic environment for async SQLAlchemy + SQLModel.

Wired to SQLModel.metadata from core.models so that autogenerate picks up
all table definitions without any manual include lists.
"""

from __future__ import annotations

import asyncio
from logging.config import fileConfig

from alembic import context
from sqlalchemy import pool
from sqlalchemy.engine import Connection
from sqlalchemy.ext.asyncio import async_engine_from_config

# Import ALL models so SQLModel.metadata is populated before autogenerate runs.
import core.models  # noqa: F401  — side-effect: registers table metadata
from sqlmodel import SQLModel

# ── Alembic config object ──────────────────────────────────────────────────────

config = context.config

# Interpret the config file for Python logging (if present).
if config.config_file_name is not None:
    fileConfig(config.config_file_name)

# This is the metadata autogenerate will diff against.
target_metadata = SQLModel.metadata

# ── Helpers ────────────────────────────────────────────────────────────────────


def run_migrations_offline() -> None:
    """
    Run migrations in 'offline' mode.

    Generates SQL without a live DB connection — useful for review / CI.
    """
    url = config.get_main_option("sqlalchemy.url")
    context.configure(
        url=url,
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
        compare_type=True,
    )
    with context.begin_transaction():
        context.run_migrations()


def do_run_migrations(connection: Connection) -> None:
    context.configure(
        connection=connection,
        target_metadata=target_metadata,
        compare_type=True,
    )
    with context.begin_transaction():
        context.run_migrations()


async def run_async_migrations() -> None:
    """Run migrations in 'online' mode using an async engine."""
    # Pull the database URL from alembic.ini [alembic] section or env override.
    import os
    from core.config import get_settings

    db_url = (
        config.get_main_option("sqlalchemy.url")
        or get_settings().database_url
    )

    configuration = config.get_section(config.config_ini_section, {})
    configuration["sqlalchemy.url"] = db_url

    connectable = async_engine_from_config(
        configuration,
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
    )

    async with connectable.connect() as connection:
        await connection.run_sync(do_run_migrations)

    await connectable.dispose()


def run_migrations_online() -> None:
    """Entry point for online migrations (called by alembic)."""
    asyncio.run(run_async_migrations())


# ── Dispatch ───────────────────────────────────────────────────────────────────

if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
