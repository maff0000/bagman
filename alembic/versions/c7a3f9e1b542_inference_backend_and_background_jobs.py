"""inference_backend on ai_invocations + background_jobs table (CD-6 §103
Inference Architecture Ruling)

Revision ID: c7a3f9e1b542
Revises: b4d8f1a92c65
Create Date: 2026-09-26 00:00:00.000000

Two additive changes, both scoped to this ruling's bounded CD-5
implementation work order (`PID.md` §103.4):

1. Adds `inference_backend` to the existing `ai_invocations` table —
   `sa.Column("inference_backend", sa.String(), nullable=False,
   server_default="MAC_LOCAL")`. `server_default` (not merely a
   Python-side dataclass default) matches historical reality: every
   existing row really was served by the Mac mini — there was no other
   backend before this ruling. Downgrade drops the column.
2. Creates the new `background_jobs` table (see
   `persistence/postgres/background_job_models.py`'s own module
   docstring for the full index/concurrency rationale) — the durable
   job mechanism PID §103.4 item 1 requires for Trinity backlog/
   overflow processing. Purely additive: no existing table's rows are
   touched by this half of the migration at all.

Zero data migration beyond the `server_default` backfill implied by
item 1 above; zero row insertion.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision: str = 'c7a3f9e1b542'
down_revision: Union[str, Sequence[str], None] = 'b4d8f1a92c65'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    # --- 1. ai_invocations.inference_backend ---
    op.add_column(
        'ai_invocations',
        sa.Column('inference_backend', sa.String(), nullable=False, server_default='MAC_LOCAL'),
    )

    # --- 2. background_jobs ---
    op.create_table(
        'background_jobs',
        sa.Column('job_id', sa.UUID(as_uuid=False), nullable=False),
        sa.Column('idempotency_key', sa.String(), nullable=False),
        sa.Column('task_id', sa.String(), nullable=False),
        sa.Column('task_version', sa.Integer(), nullable=False),
        sa.Column('input_references', postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column('evidence_content', sa.Text(), nullable=False),
        sa.Column('inference_backend', sa.String(), nullable=False),
        sa.Column('capability_alias', sa.String(), nullable=False),
        sa.Column('status', sa.String(), nullable=False),
        sa.Column('attempt_count', sa.Integer(), nullable=False, server_default='0'),
        sa.Column('max_attempts', sa.Integer(), nullable=False, server_default='3'),
        sa.Column('claimed_by', sa.String(), nullable=True),
        sa.Column('claimed_at', sa.DateTime(timezone=True), nullable=True),
        sa.Column('ai_invocation_id', sa.UUID(as_uuid=False), nullable=True),
        sa.Column('last_error', sa.Text(), nullable=True),
        sa.Column('actor_type', sa.String(), nullable=False),
        sa.Column('actor_id', sa.String(), nullable=False),
        sa.Column('correlation_id', sa.UUID(as_uuid=False), nullable=False),
        sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
        sa.Column('updated_at', sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint('job_id'),
    )
    op.create_index(
        'uq_background_jobs_idempotency_key', 'background_jobs', ['idempotency_key'], unique=True,
    )
    op.create_index(
        'ix_background_jobs_task', 'background_jobs', ['task_id', 'task_version'], unique=False,
    )
    op.create_index(
        'ix_background_jobs_claimable', 'background_jobs', ['status', 'created_at'], unique=False,
    )
    op.create_index(
        'ix_background_jobs_correlation', 'background_jobs', ['correlation_id'], unique=False,
    )


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_index('ix_background_jobs_correlation', table_name='background_jobs')
    op.drop_index('ix_background_jobs_claimable', table_name='background_jobs')
    op.drop_index('ix_background_jobs_task', table_name='background_jobs')
    op.drop_index('uq_background_jobs_idempotency_key', table_name='background_jobs')
    op.drop_table('background_jobs')

    op.drop_column('ai_invocations', 'inference_backend')
