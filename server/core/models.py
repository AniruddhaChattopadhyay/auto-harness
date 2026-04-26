"""
core/models.py — SQLModel table definitions.

All seven tables from design.md §6 are defined here.
Importing this module populates SQLModel.metadata so that alembic can
autogenerate migrations from it.

ID columns use ULID (python-ulid) for lexicographic sortability.
Timestamps are UTC-aware (timezone=True).
JSONB and ARRAY columns use raw SQLAlchemy column types since SQLModel
does not expose Postgres-specific types natively.
"""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from typing import Optional

import sqlalchemy as sa
from sqlalchemy import CheckConstraint, Index, UniqueConstraint
from sqlalchemy.dialects.postgresql import ARRAY, JSONB
from sqlalchemy.sql import expression
import ulid as ulid_lib

from sqlmodel import Column, Field, SQLModel

# ── ID factory ─────────────────────────────────────────────────────────────────


def _new_ulid() -> str:
    # Fix from smoke test: python-ulid 3.x API is ulid.ULID(), not ulid.new()
    return str(ulid_lib.ULID())


# ── Table: templates ───────────────────────────────────────────────────────────


class Template(SQLModel, table=True):
    """
    Config table seeded once via seed/templates.py.
    The API never writes to this table.
    See design.md §6.7 and §5.16.
    """

    __tablename__ = "templates"
    __table_args__ = (
        # Timestamps default handled in Python; no extra DB constraints needed.
    )

    id: str = Field(primary_key=True)
    name: str = Field(nullable=False)
    description: Optional[str] = Field(default=None, nullable=True)

    # The outer-agent INSTRUCTIONS.md template text.
    instruction_template: str = Field(nullable=False)

    # MinIO key pointing at the starting agent .py for new experiments.
    default_baseline_agent_uri: str = Field(nullable=False)

    # Per-benchmark defaults flowed into experiment on creation.
    default_task_ids: Optional[list[str]] = Field(
        default=None,
        sa_column=Column(ARRAY(sa.String), nullable=True),
    )
    # Subset of default_task_ids whose traces are HIDDEN from the optimizer (held-out test set).
    # Per design §5.10 / reference repo's anti-cheating: optimizer can only see train traces.
    default_test_task_ids: Optional[list[str]] = Field(
        default=None,
        sa_column=Column(ARRAY(sa.String), nullable=True),
    )
    default_max_concurrency: int = Field(default=5, nullable=False)
    default_optimizer_model: Optional[str] = Field(default=None, nullable=True)
    default_optimizer_kind: str = Field(default="claude_agent_sdk", nullable=False)
    default_sandbox_provider: str = Field(default="e2b", nullable=False)

    created_at: datetime = Field(
        default_factory=datetime.utcnow,
        sa_column=Column(
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
    )
    updated_at: datetime = Field(
        default_factory=datetime.utcnow,
        sa_column=Column(
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
    )


# ── Table: agent_version ───────────────────────────────────────────────────────


class AgentVersion(SQLModel, table=True):
    """
    A thin row pointing at a .py file in MinIO.
    The file is the source of truth for the agent's behaviour (design.md §5.1).
    """

    __tablename__ = "agent_version"
    __table_args__ = (
        UniqueConstraint("experiment_id", "version_number", name="uq_agent_version_exp_num"),
    )

    id: str = Field(
        default_factory=_new_ulid,
        primary_key=True,
    )
    experiment_id: str = Field(
        nullable=False,
        foreign_key="experiment.id",
        index=True,
    )
    version_number: int = Field(nullable=False)  # 0 = baseline
    parent_id: Optional[str] = Field(
        default=None,
        foreign_key="agent_version.id",
        nullable=True,
    )

    source_uri: str = Field(nullable=False)   # MinIO key for the .py file
    source_hash: str = Field(nullable=False)  # sha256(file bytes) — dedup signal

    created_at: datetime = Field(
        default_factory=datetime.utcnow,
        sa_column=Column(
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
    )


# ── Table: experiment ──────────────────────────────────────────────────────────


class Experiment(SQLModel, table=True):
    """
    Long-lived container that owns the baseline + rolling optimisation state.
    Multiple jobs run sequentially under one experiment.
    See design.md §6.1 and §3.
    """

    __tablename__ = "experiment"
    __table_args__ = (
        CheckConstraint(
            "status IN ('created','baselining','ready','archived','failed')",
            name="ck_experiment_status",
        ),
    )

    id: str = Field(
        default_factory=_new_ulid,
        primary_key=True,
    )
    name: str = Field(nullable=False)
    description: Optional[str] = Field(default=None, nullable=True)

    # Template row that provided defaults and the instruction template.
    template_id: str = Field(
        nullable=False,
        foreign_key="templates.id",
    )

    # Locked at creation; filled from templates row when caller doesn't override.
    task_ids: Optional[list[str]] = Field(
        default=None,
        sa_column=Column(ARRAY(sa.String), nullable=True),
    )
    # Subset of task_ids whose traces are HIDDEN from the optimizer (held-out test set).
    test_task_ids: Optional[list[str]] = Field(
        default=None,
        sa_column=Column(ARRAY(sa.String), nullable=True),
    )
    optimizer_kind: str = Field(default="claude_agent_sdk", nullable=False)
    optimizer_model: str = Field(nullable=False)
    sandbox_provider: str = Field(default="e2b", nullable=False)
    max_concurrency: int = Field(default=5, nullable=False)

    # MinIO workspace prefix: "experiments/{id}/"
    workspace_uri: str = Field(nullable=False)

    # Rolling state — mutated by workers as iterations complete.
    baseline_agent_version_id: Optional[str] = Field(
        default=None,
        foreign_key="agent_version.id",
        nullable=True,
    )
    best_agent_version_id: Optional[str] = Field(
        default=None,
        foreign_key="agent_version.id",
        nullable=True,
    )
    best_score: Optional[Decimal] = Field(
        default=None,
        sa_column=Column(sa.Numeric(5, 4), nullable=True),
    )

    status: str = Field(default="created", nullable=False)

    created_at: datetime = Field(
        default_factory=datetime.utcnow,
        sa_column=Column(
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
    )
    updated_at: datetime = Field(
        default_factory=datetime.utcnow,
        sa_column=Column(
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
    )


# ── Table: job ─────────────────────────────────────────────────────────────────


class Job(SQLModel, table=True):
    """
    A session of N iterations within an experiment.
    At most one active (queued/running) job per experiment enforced by a
    partial unique index (design.md §5.2, §6.3).
    """

    __tablename__ = "job"
    __table_args__ = (
        CheckConstraint(
            "status IN ('queued','running','done','failed','cancelled')",
            name="ck_job_status",
        ),
        CheckConstraint(
            "stop_reason IN ('max_iterations','perfect_score','stagnation','wall_time','cancelled','error')"
            " OR stop_reason IS NULL",
            name="ck_job_stop_reason",
        ),
        # Partial unique index: at most one active job per experiment.
        Index(
            "one_active_job_per_experiment",
            "experiment_id",
            unique=True,
            postgresql_where=sa.text("status IN ('queued','running')"),
        ),
        # Additional index for common query pattern.
        Index("ix_job_experiment_status", "experiment_id", "status"),
    )

    id: str = Field(
        default_factory=_new_ulid,
        primary_key=True,
    )
    experiment_id: str = Field(
        nullable=False,
        foreign_key="experiment.id",
    )

    max_iterations: int = Field(nullable=False)
    stopping_criteria: dict = Field(
        default_factory=dict,
        sa_column=Column(
            JSONB,
            nullable=False,
            server_default=sa.text("'{}'::jsonb"),
        ),
    )

    status: str = Field(default="queued", nullable=False)

    # Denormalised iteration window for cheap reads.
    started_at_iteration: Optional[int] = Field(default=None, nullable=True)
    ended_at_iteration: Optional[int] = Field(default=None, nullable=True)

    stop_reason: Optional[str] = Field(default=None, nullable=True)
    error_message: Optional[str] = Field(default=None, nullable=True)

    created_at: datetime = Field(
        default_factory=datetime.utcnow,
        sa_column=Column(
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
    )
    started_at: Optional[datetime] = Field(
        default=None,
        sa_column=Column(sa.DateTime(timezone=True), nullable=True),
    )
    finished_at: Optional[datetime] = Field(
        default=None,
        sa_column=Column(sa.DateTime(timezone=True), nullable=True),
    )


# ── Table: iteration ───────────────────────────────────────────────────────────


class Iteration(SQLModel, table=True):
    """
    Core record: one row per attempted optimisation cycle (including baseline).
    Pointers to MinIO for large artefacts; queryable scalars on the row.
    See design.md §6.4.
    """

    __tablename__ = "iteration"
    __table_args__ = (
        CheckConstraint(
            "status IN ('running','done','failed')",
            name="ck_iteration_status",
        ),
        CheckConstraint(
            "outcome IN ('baseline','kept','reverted') OR outcome IS NULL",
            name="ck_iteration_outcome",
        ),
        UniqueConstraint("experiment_id", "iteration_number", name="uq_iteration_exp_num"),
        # Index for listing iterations by job.
        Index("ix_iteration_job_id", "job_id"),
    )

    id: str = Field(
        default_factory=_new_ulid,
        primary_key=True,
    )
    experiment_id: str = Field(
        nullable=False,
        foreign_key="experiment.id",
    )
    job_id: str = Field(
        nullable=False,
        foreign_key="job.id",
    )
    iteration_number: int = Field(nullable=False)  # monotonic per experiment; 0 = baseline
    parent_iteration_id: Optional[str] = Field(
        default=None,
        foreign_key="iteration.id",
        nullable=True,
    )

    # The agent that ran in the benchmark run for this iteration.
    agent_version_id: str = Field(
        nullable=False,
        foreign_key="agent_version.id",
    )
    # What the optimizer started from; null for baseline.
    parent_agent_version_id: Optional[str] = Field(
        default=None,
        foreign_key="agent_version.id",
        nullable=True,
    )

    status: str = Field(default="running", nullable=False)
    score: Optional[Decimal] = Field(
        default=None,
        sa_column=Column(sa.Numeric(5, 4), nullable=True),
    )
    outcome: Optional[str] = Field(default=None, nullable=True)
    best_pointer_changed: bool = Field(
        default=False,
        sa_column=Column(
            sa.Boolean,
            nullable=False,
            server_default=expression.false(),
        ),
    )

    # Optimizer artefacts — null for baseline (iteration 0).
    optimizer_diagnosis: Optional[str] = Field(default=None, nullable=True)
    expected_targets: Optional[list[str]] = Field(
        default=None,
        sa_column=Column(ARRAY(sa.String), nullable=True),
    )
    optimizer_instructions_uri: Optional[str] = Field(default=None, nullable=True)
    optimizer_context_uri: Optional[str] = Field(default=None, nullable=True)
    optimizer_transcript_uri: Optional[str] = Field(default=None, nullable=True)
    optimizer_input_tokens: Optional[int] = Field(default=None, nullable=True)
    optimizer_output_tokens: Optional[int] = Field(default=None, nullable=True)
    optimizer_cost_usd: Optional[Decimal] = Field(
        default=None,
        sa_column=Column(sa.Numeric(10, 6), nullable=True),
    )

    error_message: Optional[str] = Field(default=None, nullable=True)

    started_at: datetime = Field(
        default_factory=datetime.utcnow,
        sa_column=Column(
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
    )
    finished_at: Optional[datetime] = Field(
        default=None,
        sa_column=Column(sa.DateTime(timezone=True), nullable=True),
    )


# ── Table: trial ───────────────────────────────────────────────────────────────


class Trial(SQLModel, table=True):
    """
    One row per (iteration × task). Captures inner-agent execution outcome.
    See design.md §6.5.
    """

    __tablename__ = "trial"
    __table_args__ = (
        CheckConstraint(
            "status IN ('running','done','infra_error')",
            name="ck_trial_status",
        ),
        UniqueConstraint("iteration_id", "task_id", name="uq_trial_iteration_task"),
    )

    id: str = Field(
        default_factory=_new_ulid,
        primary_key=True,
    )
    iteration_id: str = Field(
        nullable=False,
        foreign_key="iteration.id",
        index=True,
    )
    task_id: str = Field(nullable=False)  # TerminalBench slug

    status: str = Field(default="running", nullable=False)
    reward: Optional[Decimal] = Field(
        default=None,
        sa_column=Column(sa.Numeric(3, 2), nullable=True),
    )
    wall_time_ms: Optional[int] = Field(default=None, nullable=True)
    command_count: Optional[int] = Field(default=None, nullable=True)
    sandbox_id: Optional[str] = Field(default=None, nullable=True)

    trace_uri: Optional[str] = Field(default=None, nullable=True)
    verifier_output_uri: Optional[str] = Field(default=None, nullable=True)

    infra_error: Optional[str] = Field(default=None, nullable=True)

    started_at: datetime = Field(
        default_factory=datetime.utcnow,
        sa_column=Column(
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
    )
    finished_at: Optional[datetime] = Field(
        default=None,
        sa_column=Column(sa.DateTime(timezone=True), nullable=True),
    )


# Task queue lives in the PGMQ extension (see core/queue.py and the
# pgmq_queues migration); no SQLModel is needed here.


# ── Public re-exports ──────────────────────────────────────────────────────────
# Import SQLModel.metadata to expose it at module level; used by alembic env.py.
from sqlmodel import SQLModel as _SQLModel  # noqa: E402 (re-exported)

__all__ = [
    "Template",
    "AgentVersion",
    "Experiment",
    "Job",
    "Iteration",
    "Trial",
    "SQLModel",
]

# Re-export so callers can do: from core.models import SQLModel
SQLModel = _SQLModel
