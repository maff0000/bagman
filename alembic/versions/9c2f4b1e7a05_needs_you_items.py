"""needs_you_items (CD-6 Slice 1, PID §98.5)

Revision ID: 9c2f4b1e7a05
Revises: f3a1c9d02b47
Create Date: 2026-09-16 00:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision: str = '9c2f4b1e7a05'
down_revision: Union[str, Sequence[str], None] = 'f3a1c9d02b47'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    op.create_table(
        'needs_you_items',
        sa.Column('item_id', sa.UUID(as_uuid=False), nullable=False),
        sa.Column('item_type', sa.String(), nullable=False),
        sa.Column('domain', sa.String(), nullable=False),
        sa.Column('source_object_reference', sa.UUID(as_uuid=False), nullable=True),
        sa.Column('question', sa.String(), nullable=False),
        sa.Column('allowed_action_type', sa.String(), nullable=False),
        sa.Column('priority', sa.String(), nullable=False),
        sa.Column('status', sa.String(), nullable=False),
        sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
        sa.Column('resolved_at', sa.DateTime(timezone=True), nullable=True),
        sa.Column('resolved_by_actor_type', sa.String(), nullable=True),
        sa.Column('resolved_by_actor_id', sa.String(), nullable=True),
        sa.Column('resolution', postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column('correlation_id', sa.UUID(as_uuid=False), nullable=False),
        sa.Column('metadata', postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.PrimaryKeyConstraint('item_id'),
    )
    op.create_index('ix_needs_you_items_status', 'needs_you_items', ['status'], unique=False)
    op.create_index('ix_needs_you_items_correlation', 'needs_you_items', ['correlation_id'], unique=False)
    # Partial unique index (CD-6 Slice 1 idempotent-creation guarantee —
    # see services/needs_you/needs_you.py's own module docstring): unique
    # only where source_object_reference IS NOT NULL, mirroring
    # d81cf00fc7de's own uq_intake_records_idempotency_key pattern.
    op.create_index(
        'uq_needs_you_items_type_source_ref',
        'needs_you_items',
        ['item_type', 'source_object_reference'],
        unique=True,
        postgresql_where=sa.text('source_object_reference IS NOT NULL'),
    )


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_index('uq_needs_you_items_type_source_ref', table_name='needs_you_items')
    op.drop_index('ix_needs_you_items_correlation', table_name='needs_you_items')
    op.drop_index('ix_needs_you_items_status', table_name='needs_you_items')
    op.drop_table('needs_you_items')
