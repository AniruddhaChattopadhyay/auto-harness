"""switch task queue to PGMQ

Replaces the hand-rolled task_queue table with the PGMQ Postgres extension.
Two queues are created: ``benchmark_run`` and ``optimizer_propose``.

Revision ID: 3b2c4d5e6f70
Revises: 2a1b3c4d5e6f
Create Date: 2026-04-26 21:00:00
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB

revision = "3b2c4d5e6f70"
down_revision = "2a1b3c4d5e6f"
branch_labels = None
depends_on = None


QUEUE_NAMES = ("benchmark_run", "optimizer_propose")


def upgrade() -> None:
    op.execute("CREATE EXTENSION IF NOT EXISTS pgmq")
    for q in QUEUE_NAMES:
        op.execute(sa.text("SELECT pgmq.create(:q)").bindparams(q=q))

    op.drop_index("task_queue_pending", table_name="task_queue")
    op.drop_table("task_queue")


def downgrade() -> None:
    op.create_table(
        "task_queue",
        sa.Column("id", sa.Text(), nullable=False),
        sa.Column("task_type", sa.Text(), nullable=False),
        sa.Column("payload", JSONB(), nullable=False),
        sa.Column("status", sa.Text(), nullable=False, server_default="pending"),
        sa.Column("attempts", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("max_attempts", sa.Integer(), nullable=False, server_default="3"),
        sa.Column(
            "scheduled_for",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.Column("claimed_by", sa.Text(), nullable=True),
        sa.Column("claimed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_error", sa.Text(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.PrimaryKeyConstraint("id", name="pk_task_queue"),
        sa.CheckConstraint(
            "task_type IN ('benchmark.run','optimizer.propose')",
            name="ck_task_queue_task_type",
        ),
        sa.CheckConstraint(
            "status IN ('pending','running','done','failed','dead_letter')",
            name="ck_task_queue_status",
        ),
    )
    op.create_index(
        "task_queue_pending",
        "task_queue",
        ["scheduled_for"],
        postgresql_where=sa.text("status = 'pending'"),
    )

    for q in QUEUE_NAMES:
        op.execute(sa.text("SELECT pgmq.drop_queue(:q)").bindparams(q=q))
    op.execute("DROP EXTENSION IF EXISTS pgmq")
