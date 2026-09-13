"""ai_invocations (CD-5 WI-1, PID §27/§73)

Revision ID: f3a1c9d02b47
Revises: d81cf00fc7de
Create Date: 2026-09-13 00:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision: str = 'f3a1c9d02b47'
down_revision: Union[str, Sequence[str], None] = 'd81cf00fc7de'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

# Must stay in lockstep with
# persistence/postgres/ai_invocation_models.py's
# `_PRIMARY_INPUT_REFERENCE_SQL_EXPRESSION` and
# ai.invocation.PRIMARY_INPUT_REFERENCE_KEYS's precedence order.
_PRIMARY_INPUT_REFERENCE_SQL_EXPRESSION = (
    "COALESCE(input_references->>'evidence_id', "
    "input_references->>'intake_id', "
    "input_references->>'entity_id')"
)


def upgrade() -> None:
    """Upgrade schema."""
    op.create_table(
        'ai_invocations',
        sa.Column('ai_invocation_id', sa.UUID(as_uuid=False), nullable=False),
        sa.Column('task_id', sa.String(), nullable=False),
        sa.Column('task_version', sa.Integer(), nullable=False),
        sa.Column('role', sa.String(), nullable=False),
        sa.Column('provider', sa.String(), nullable=False),
        sa.Column('capability_alias', sa.String(), nullable=True),
        sa.Column('provider_model', sa.String(), nullable=True),
        sa.Column('started_at', sa.DateTime(timezone=True), nullable=False),
        sa.Column('completed_at', sa.DateTime(timezone=True), nullable=True),
        sa.Column('status', sa.String(), nullable=False),
        sa.Column('correlation_id', sa.UUID(as_uuid=False), nullable=False),
        sa.Column('actor_type', sa.String(), nullable=False),
        sa.Column('actor_id', sa.String(), nullable=False),
        sa.Column('input_references', postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        # STORED GENERATED column — the real, database-enforced half of
        # the one-active-invocation concurrency guard (PID §73). See
        # persistence/postgres/ai_invocation_models.py's module
        # docstring for the full mechanism and its documented edge case.
        sa.Column(
            'primary_input_reference',
            sa.String(),
            sa.Computed(_PRIMARY_INPUT_REFERENCE_SQL_EXPRESSION, persisted=True),
            nullable=True,
        ),
        sa.Column('prompt_contract_version', sa.String(), nullable=True),
        sa.Column('output', postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column('confidence', sa.Float(), nullable=True),
        sa.Column('validation_result', postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column('error_code', sa.String(), nullable=True),
        sa.Column('usage_metadata', postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column('latency_ms', sa.Integer(), nullable=True),
        sa.PrimaryKeyConstraint('ai_invocation_id'),
    )
    op.create_index('ix_ai_invocations_correlation', 'ai_invocations', ['correlation_id'], unique=False)
    op.create_index('ix_ai_invocations_task', 'ai_invocations', ['task_id', 'task_version'], unique=False)
    # Partial unique index (PID §73): unique on
    # (task_id, task_version, primary_input_reference) only where
    # status is one of the two NON-terminal states — any number of
    # terminal rows may share a subject (PID §74's retry doctrine), but
    # only one non-terminal row may exist for it at a time.
    op.create_index(
        'uq_ai_invocations_active_subject',
        'ai_invocations',
        ['task_id', 'task_version', 'primary_input_reference'],
        unique=True,
        postgresql_where=sa.text("status IN ('REQUESTED', 'RUNNING')"),
    )


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_index('uq_ai_invocations_active_subject', table_name='ai_invocations')
    op.drop_index('ix_ai_invocations_task', table_name='ai_invocations')
    op.drop_index('ix_ai_invocations_correlation', table_name='ai_invocations')
    op.drop_table('ai_invocations')
