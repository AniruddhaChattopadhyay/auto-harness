"""initial schema

Revision ID: 1e1c51db2618
Revises:
Create Date: 2026-04-26 19:47:32.160268

Hand-written migration that matches the DDL in design.md §6.
Alembic autogenerate could not connect to Postgres (not running at
revision time), so the migration is written explicitly.  It is
equivalent to what autogenerate would produce plus the items it
typically misses: partial unique indexes, partial indexes, and CHECK
constraints with custom names.

Tables created (in dependency order):
  templates → agent_version → experiment → job → iteration → trial → task_queue
"""
from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import ARRAY, JSONB

# revision identifiers, used by Alembic.
revision: str = '1e1c51db2618'
down_revision: Union[str, None] = None
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # ── templates ──────────────────────────────────────────────────────────
    op.create_table(
        'templates',
        sa.Column('id', sa.Text(), nullable=False),
        sa.Column('name', sa.Text(), nullable=False),
        sa.Column('description', sa.Text(), nullable=True),
        sa.Column('instruction_template', sa.Text(), nullable=False),
        sa.Column('default_baseline_agent_uri', sa.Text(), nullable=False),
        sa.Column('default_task_ids', ARRAY(sa.Text()), nullable=True),
        sa.Column('default_max_concurrency', sa.Integer(), nullable=False, server_default='5'),
        sa.Column('default_optimizer_model', sa.Text(), nullable=True),
        sa.Column('default_optimizer_kind', sa.Text(), nullable=False, server_default='claude_agent_sdk'),
        sa.Column('default_sandbox_provider', sa.Text(), nullable=False, server_default='e2b'),
        sa.Column('created_at', sa.DateTime(timezone=True), nullable=False, server_default=sa.text('now()')),
        sa.Column('updated_at', sa.DateTime(timezone=True), nullable=False, server_default=sa.text('now()')),
        sa.PrimaryKeyConstraint('id', name='pk_templates'),
    )

    # ── agent_version ──────────────────────────────────────────────────────
    # experiment FK added after experiment table is created (below).
    op.create_table(
        'agent_version',
        sa.Column('id', sa.Text(), nullable=False),
        sa.Column('experiment_id', sa.Text(), nullable=False),
        sa.Column('version_number', sa.Integer(), nullable=False),
        sa.Column('parent_id', sa.Text(), nullable=True),
        sa.Column('source_uri', sa.Text(), nullable=False),
        sa.Column('source_hash', sa.Text(), nullable=False),
        sa.Column('created_at', sa.DateTime(timezone=True), nullable=False, server_default=sa.text('now()')),
        sa.PrimaryKeyConstraint('id', name='pk_agent_version'),
        sa.ForeignKeyConstraint(['parent_id'], ['agent_version.id'], name='fk_agent_version_parent'),
        sa.UniqueConstraint('experiment_id', 'version_number', name='uq_agent_version_exp_num'),
    )
    op.create_index('ix_agent_version_experiment_id', 'agent_version', ['experiment_id'])

    # ── experiment ─────────────────────────────────────────────────────────
    op.create_table(
        'experiment',
        sa.Column('id', sa.Text(), nullable=False),
        sa.Column('name', sa.Text(), nullable=False),
        sa.Column('description', sa.Text(), nullable=True),
        sa.Column('template_id', sa.Text(), nullable=False),
        sa.Column('task_ids', ARRAY(sa.Text()), nullable=True),
        sa.Column('optimizer_kind', sa.Text(), nullable=False, server_default='claude_agent_sdk'),
        sa.Column('optimizer_model', sa.Text(), nullable=False),
        sa.Column('sandbox_provider', sa.Text(), nullable=False, server_default='e2b'),
        sa.Column('max_concurrency', sa.Integer(), nullable=False, server_default='5'),
        sa.Column('workspace_uri', sa.Text(), nullable=False),
        sa.Column('baseline_agent_version_id', sa.Text(), nullable=True),
        sa.Column('best_agent_version_id', sa.Text(), nullable=True),
        sa.Column('best_score', sa.Numeric(5, 4), nullable=True),
        sa.Column('status', sa.Text(), nullable=False, server_default='created'),
        sa.Column('created_at', sa.DateTime(timezone=True), nullable=False, server_default=sa.text('now()')),
        sa.Column('updated_at', sa.DateTime(timezone=True), nullable=False, server_default=sa.text('now()')),
        sa.PrimaryKeyConstraint('id', name='pk_experiment'),
        sa.ForeignKeyConstraint(['template_id'], ['templates.id'], name='fk_experiment_template'),
        sa.ForeignKeyConstraint(['baseline_agent_version_id'], ['agent_version.id'], name='fk_experiment_baseline_av'),
        sa.ForeignKeyConstraint(['best_agent_version_id'], ['agent_version.id'], name='fk_experiment_best_av'),
        sa.CheckConstraint(
            "status IN ('created','baselining','ready','archived','failed')",
            name='ck_experiment_status',
        ),
    )

    # Now that experiment exists, add the FK from agent_version → experiment.
    op.create_foreign_key(
        'fk_agent_version_experiment',
        'agent_version', 'experiment',
        ['experiment_id'], ['id'],
        ondelete='CASCADE',
    )

    # ── job ────────────────────────────────────────────────────────────────
    op.create_table(
        'job',
        sa.Column('id', sa.Text(), nullable=False),
        sa.Column('experiment_id', sa.Text(), nullable=False),
        sa.Column('max_iterations', sa.Integer(), nullable=False),
        sa.Column('stopping_criteria', JSONB(), nullable=False, server_default=sa.text("'{}'::jsonb")),
        sa.Column('status', sa.Text(), nullable=False, server_default='queued'),
        sa.Column('started_at_iteration', sa.Integer(), nullable=True),
        sa.Column('ended_at_iteration', sa.Integer(), nullable=True),
        sa.Column('stop_reason', sa.Text(), nullable=True),
        sa.Column('error_message', sa.Text(), nullable=True),
        sa.Column('created_at', sa.DateTime(timezone=True), nullable=False, server_default=sa.text('now()')),
        sa.Column('started_at', sa.DateTime(timezone=True), nullable=True),
        sa.Column('finished_at', sa.DateTime(timezone=True), nullable=True),
        sa.PrimaryKeyConstraint('id', name='pk_job'),
        sa.ForeignKeyConstraint(['experiment_id'], ['experiment.id'], name='fk_job_experiment', ondelete='CASCADE'),
        sa.CheckConstraint(
            "status IN ('queued','running','done','failed','cancelled')",
            name='ck_job_status',
        ),
        sa.CheckConstraint(
            "stop_reason IN ('max_iterations','perfect_score','stagnation','wall_time','cancelled','error')"
            " OR stop_reason IS NULL",
            name='ck_job_stop_reason',
        ),
    )
    # Partial unique index: at most one active job per experiment (design.md §5.2).
    op.create_index(
        'one_active_job_per_experiment',
        'job',
        ['experiment_id'],
        unique=True,
        postgresql_where=sa.text("status IN ('queued','running')"),
    )
    op.create_index('ix_job_experiment_status', 'job', ['experiment_id', 'status'])

    # ── iteration ──────────────────────────────────────────────────────────
    op.create_table(
        'iteration',
        sa.Column('id', sa.Text(), nullable=False),
        sa.Column('experiment_id', sa.Text(), nullable=False),
        sa.Column('job_id', sa.Text(), nullable=False),
        sa.Column('iteration_number', sa.Integer(), nullable=False),
        sa.Column('parent_iteration_id', sa.Text(), nullable=True),
        sa.Column('agent_version_id', sa.Text(), nullable=False),
        sa.Column('parent_agent_version_id', sa.Text(), nullable=True),
        sa.Column('status', sa.Text(), nullable=False, server_default='running'),
        sa.Column('score', sa.Numeric(5, 4), nullable=True),
        sa.Column('outcome', sa.Text(), nullable=True),
        sa.Column('best_pointer_changed', sa.Boolean(), nullable=False, server_default='false'),
        sa.Column('optimizer_diagnosis', sa.Text(), nullable=True),
        sa.Column('expected_targets', ARRAY(sa.Text()), nullable=True),
        sa.Column('optimizer_instructions_uri', sa.Text(), nullable=True),
        sa.Column('optimizer_context_uri', sa.Text(), nullable=True),
        sa.Column('optimizer_transcript_uri', sa.Text(), nullable=True),
        sa.Column('optimizer_input_tokens', sa.Integer(), nullable=True),
        sa.Column('optimizer_output_tokens', sa.Integer(), nullable=True),
        sa.Column('optimizer_cost_usd', sa.Numeric(10, 6), nullable=True),
        sa.Column('error_message', sa.Text(), nullable=True),
        sa.Column('started_at', sa.DateTime(timezone=True), nullable=False, server_default=sa.text('now()')),
        sa.Column('finished_at', sa.DateTime(timezone=True), nullable=True),
        sa.PrimaryKeyConstraint('id', name='pk_iteration'),
        sa.ForeignKeyConstraint(['experiment_id'], ['experiment.id'], name='fk_iteration_experiment', ondelete='CASCADE'),
        sa.ForeignKeyConstraint(['job_id'], ['job.id'], name='fk_iteration_job', ondelete='CASCADE'),
        sa.ForeignKeyConstraint(['parent_iteration_id'], ['iteration.id'], name='fk_iteration_parent'),
        sa.ForeignKeyConstraint(['agent_version_id'], ['agent_version.id'], name='fk_iteration_agent_version'),
        sa.ForeignKeyConstraint(['parent_agent_version_id'], ['agent_version.id'], name='fk_iteration_parent_agent_version'),
        sa.UniqueConstraint('experiment_id', 'iteration_number', name='uq_iteration_exp_num'),
        sa.CheckConstraint(
            "status IN ('running','done','failed')",
            name='ck_iteration_status',
        ),
        sa.CheckConstraint(
            "outcome IN ('baseline','kept','reverted') OR outcome IS NULL",
            name='ck_iteration_outcome',
        ),
    )
    op.create_index('ix_iteration_job_id', 'iteration', ['job_id'])

    # ── trial ──────────────────────────────────────────────────────────────
    op.create_table(
        'trial',
        sa.Column('id', sa.Text(), nullable=False),
        sa.Column('iteration_id', sa.Text(), nullable=False),
        sa.Column('task_id', sa.Text(), nullable=False),
        sa.Column('status', sa.Text(), nullable=False, server_default='running'),
        sa.Column('reward', sa.Numeric(3, 2), nullable=True),
        sa.Column('wall_time_ms', sa.Integer(), nullable=True),
        sa.Column('command_count', sa.Integer(), nullable=True),
        sa.Column('sandbox_id', sa.Text(), nullable=True),
        sa.Column('trace_uri', sa.Text(), nullable=True),
        sa.Column('verifier_output_uri', sa.Text(), nullable=True),
        sa.Column('infra_error', sa.Text(), nullable=True),
        sa.Column('started_at', sa.DateTime(timezone=True), nullable=False, server_default=sa.text('now()')),
        sa.Column('finished_at', sa.DateTime(timezone=True), nullable=True),
        sa.PrimaryKeyConstraint('id', name='pk_trial'),
        sa.ForeignKeyConstraint(['iteration_id'], ['iteration.id'], name='fk_trial_iteration', ondelete='CASCADE'),
        sa.UniqueConstraint('iteration_id', 'task_id', name='uq_trial_iteration_task'),
        sa.CheckConstraint(
            "status IN ('running','done','infra_error')",
            name='ck_trial_status',
        ),
    )
    op.create_index('ix_trial_iteration_id', 'trial', ['iteration_id'])

    # ── task_queue ─────────────────────────────────────────────────────────
    op.create_table(
        'task_queue',
        sa.Column('id', sa.Text(), nullable=False),
        sa.Column('task_type', sa.Text(), nullable=False),
        sa.Column('payload', JSONB(), nullable=False),
        sa.Column('status', sa.Text(), nullable=False, server_default='pending'),
        sa.Column('attempts', sa.Integer(), nullable=False, server_default='0'),
        sa.Column('max_attempts', sa.Integer(), nullable=False, server_default='3'),
        sa.Column('scheduled_for', sa.DateTime(timezone=True), nullable=False, server_default=sa.text('now()')),
        sa.Column('claimed_by', sa.Text(), nullable=True),
        sa.Column('claimed_at', sa.DateTime(timezone=True), nullable=True),
        sa.Column('finished_at', sa.DateTime(timezone=True), nullable=True),
        sa.Column('last_error', sa.Text(), nullable=True),
        sa.Column('created_at', sa.DateTime(timezone=True), nullable=False, server_default=sa.text('now()')),
        sa.PrimaryKeyConstraint('id', name='pk_task_queue'),
        sa.CheckConstraint(
            "task_type IN ('benchmark.run','optimizer.propose')",
            name='ck_task_queue_task_type',
        ),
        sa.CheckConstraint(
            "status IN ('pending','running','done','failed','dead_letter')",
            name='ck_task_queue_status',
        ),
    )
    # Partial index: fast claim scan over pending rows only (design.md §6.6).
    op.create_index(
        'task_queue_pending',
        'task_queue',
        ['scheduled_for'],
        postgresql_where=sa.text("status = 'pending'"),
    )


def downgrade() -> None:
    # Drop in reverse dependency order.
    op.drop_table('task_queue')
    op.drop_table('trial')
    op.drop_table('iteration')
    op.drop_index('ix_job_experiment_status', table_name='job')
    op.drop_index('one_active_job_per_experiment', table_name='job')
    op.drop_table('job')
    op.drop_constraint('fk_agent_version_experiment', 'agent_version', type_='foreignkey')
    op.drop_table('experiment')
    op.drop_index('ix_agent_version_experiment_id', table_name='agent_version')
    op.drop_table('agent_version')
    op.drop_table('templates')
