"""add test_task_ids columns for train/test holdout

Revision ID: 2a1b3c4d5e6f
Revises: 1e1c51db2618
Create Date: 2026-04-26 20:30:00
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "2a1b3c4d5e6f"
down_revision = "1e1c51db2618"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "templates",
        sa.Column("default_test_task_ids", sa.ARRAY(sa.String()), nullable=True),
    )
    op.add_column(
        "experiment",
        sa.Column("test_task_ids", sa.ARRAY(sa.String()), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("experiment", "test_task_ids")
    op.drop_column("templates", "default_test_task_ids")
