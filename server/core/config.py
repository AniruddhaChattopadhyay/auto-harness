"""
core/config.py — typed env-var loader via pydantic-settings.

All env vars are declared with sensible defaults that match .env.example.
The DATABASE_URL is required (no default) so the process fails fast if unset.
"""

from __future__ import annotations

from functools import lru_cache
from typing import Optional

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
    )

    # ── Provider API keys ───────────────────────────────────────────────────
    openai_api_key: Optional[str] = None
    anthropic_api_key: Optional[str] = None
    e2b_api_key: Optional[str] = None

    # ── Default models / impl ───────────────────────────────────────────────
    benchmark_model: str = "gpt-5.4"
    optimizer_model: str = "claude-sonnet-4-6"
    optimizer_kind: str = "claude_agent_sdk"

    # ── Postgres ────────────────────────────────────────────────────────────
    database_url: str  # required — no default; must be postgresql+asyncpg://…

    # ── MinIO ───────────────────────────────────────────────────────────────
    minio_endpoint: str = "localhost:9000"
    minio_access_key: str = "minioadmin"
    minio_secret_key: str = "minioadmin"
    minio_bucket: str = "harness"
    minio_use_ssl: bool = False

    # ── Worker tuning ───────────────────────────────────────────────────────
    worker_benchmark_concurrency: int = 2
    worker_optimizer_concurrency: int = 2
    worker_poll_interval_seconds: int = 2
    worker_task_timeout_seconds: int = 1800

    # ── Logging ─────────────────────────────────────────────────────────────
    log_level: str = "INFO"


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Cached singleton — same object for the lifetime of the process."""
    return Settings()
