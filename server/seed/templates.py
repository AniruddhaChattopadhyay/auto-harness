"""
seed/templates.py — One-shot seed script for the `templates` table.

Run from the server/ directory:
    uv run python -m seed.templates

What this does:
  1. UPSERTs the `terminal_bench` template row into Postgres (idempotent via
     ON CONFLICT (id) DO UPDATE).
  2. Uploads seed/data/terminal_bench/baseline_agent.py to MinIO at key
     benchmarks/terminal_bench/baseline_agent.py (overwrite is fine).
  3. Seeds a placeholder learnings.md at
     benchmarks/terminal_bench/learnings.md — the empty starting state that
     gets copied to experiments/{id}/learnings.md at experiment-create time.

See design.md §5.16 and §6.7 for the schema and resolution-order spec.
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

import structlog
from sqlalchemy.dialects.postgresql import insert as pg_insert

from core.db import get_session, init_engine
from core.models import Template
from core.storage import ensure_bucket, put_object

log = structlog.get_logger(__name__)

# ── Paths to seed source files ─────────────────────────────────────────────────

_DATA_DIR = Path(__file__).parent / "data" / "terminal_bench"
_BASELINE_AGENT_PATH = _DATA_DIR / "baseline_agent.py"
_INSTRUCTION_TEMPLATE_PATH = _DATA_DIR / "instruction_template.md"

# ── MinIO keys (shared "benchmarks/" prefix — outside per-experiment workspace) ─

_BASELINE_AGENT_URI = "benchmarks/terminal_bench/baseline_agent.py"
_LEARNINGS_URI = "benchmarks/terminal_bench/learnings.md"

# ── Template row values ────────────────────────────────────────────────────────

_TEMPLATE_ID = "terminal_bench"
_TEMPLATE_NAME = "TerminalBench 2.0"
_TEMPLATE_DESCRIPTION = (
    "Real-world terminal tasks (sysadmin, coding, security) — "
    "89 tasks total in upstream; we curate ~5–10 representative + fast ones."
)

# 8 total tasks: 5 train (visible to optimizer) + 3 test (held out — never shown to optimizer).
# Train set proven against gpt-5.4 (4/5 baseline) and gpt-4o-mini (1/5). Test set picked
# fresh from terminal-bench@2.0 to be diverse + bounded.
# TODO: expand to 10–15 total before final delivery.
_DEFAULT_TASK_IDS = [
    # Train (5)
    "fix-git",
    "configure-git-webserver",
    "git-leak-recovery",
    "nginx-request-logging",
    "count-dataset-tokens",
    # Test / holdout (3) — also part of the benchmark pool, but optimizer never sees their traces
    "regex-log",
    "headless-terminal",
    "password-recovery",
]
_DEFAULT_TEST_TASK_IDS = [
    "regex-log",
    "headless-terminal",
    "password-recovery",
]

_PLACEHOLDER_LEARNINGS = (
    "# Learnings\n\n"
    "_(empty — populated by the optimizer agent across iterations)_\n"
)


# ── DB step ───────────────────────────────────────────────────────────────────


async def _upsert_template(instruction_template: str) -> None:
    """UPSERT the terminal_bench row. Idempotent via ON CONFLICT (id) DO UPDATE."""
    log.info("upserting template row", template_id=_TEMPLATE_ID)

    values = {
        "id": _TEMPLATE_ID,
        "name": _TEMPLATE_NAME,
        "description": _TEMPLATE_DESCRIPTION,
        "instruction_template": instruction_template,
        "default_baseline_agent_uri": _BASELINE_AGENT_URI,
        "default_task_ids": _DEFAULT_TASK_IDS,
        "default_test_task_ids": _DEFAULT_TEST_TASK_IDS,
        "default_max_concurrency": 5,
        "default_optimizer_model": "claude-sonnet-4-6",
        "default_optimizer_kind": "claude_agent_sdk",
        "default_sandbox_provider": "e2b",
    }

    stmt = (
        pg_insert(Template)
        .values(**values)
        .on_conflict_do_update(
            index_elements=["id"],
            set_={
                "name": values["name"],
                "description": values["description"],
                "instruction_template": values["instruction_template"],
                "default_baseline_agent_uri": values["default_baseline_agent_uri"],
                "default_task_ids": values["default_task_ids"],
                "default_test_task_ids": values["default_test_task_ids"],
                "default_max_concurrency": values["default_max_concurrency"],
                "default_optimizer_model": values["default_optimizer_model"],
                "default_optimizer_kind": values["default_optimizer_kind"],
                "default_sandbox_provider": values["default_sandbox_provider"],
            },
        )
    )

    async with get_session() as session:
        await session.execute(stmt)

    log.info("template row upserted", template_id=_TEMPLATE_ID)


# ── MinIO steps ───────────────────────────────────────────────────────────────


def _upload_baseline_agent(agent_bytes: bytes) -> None:
    """Upload baseline_agent.py to MinIO. Overwrite is idempotent."""
    log.info("uploading baseline agent", uri=_BASELINE_AGENT_URI)
    put_object(_BASELINE_AGENT_URI, agent_bytes, content_type="text/x-python")
    log.info("baseline agent uploaded", uri=_BASELINE_AGENT_URI)


def _upload_placeholder_learnings() -> None:
    """Seed an empty learnings.md. Overwrite is idempotent."""
    log.info("uploading placeholder learnings.md", uri=_LEARNINGS_URI)
    put_object(_LEARNINGS_URI, _PLACEHOLDER_LEARNINGS, content_type="text/markdown")
    log.info("placeholder learnings.md uploaded", uri=_LEARNINGS_URI)


# ── Main ──────────────────────────────────────────────────────────────────────


async def main() -> None:
    structlog.configure(
        wrapper_class=structlog.make_filtering_bound_logger(20),  # INFO = 20
    )
    log.info("seed/templates.py starting")

    # -- Read source files from disk ----------------------------------------
    log.info("reading seed source files", data_dir=str(_DATA_DIR))

    if not _INSTRUCTION_TEMPLATE_PATH.exists():
        log.error("instruction_template.md not found", path=str(_INSTRUCTION_TEMPLATE_PATH))
        sys.exit(1)
    if not _BASELINE_AGENT_PATH.exists():
        log.error("baseline_agent.py not found", path=str(_BASELINE_AGENT_PATH))
        sys.exit(1)

    instruction_template = _INSTRUCTION_TEMPLATE_PATH.read_text(encoding="utf-8")
    agent_bytes = _BASELINE_AGENT_PATH.read_bytes()

    log.info(
        "source files read",
        instruction_template_chars=len(instruction_template),
        baseline_agent_bytes=len(agent_bytes),
    )

    # -- Initialise DB engine -----------------------------------------------
    log.info("initialising DB engine")
    init_engine()

    # -- UPSERT template row ------------------------------------------------
    await _upsert_template(instruction_template)

    # -- MinIO: ensure bucket exists then upload ----------------------------
    log.info("ensuring MinIO bucket exists")
    ensure_bucket()

    _upload_baseline_agent(agent_bytes)
    _upload_placeholder_learnings()

    log.info("seed/templates.py completed successfully")


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except Exception:
        log.exception("seed/templates.py failed")
        sys.exit(1)
