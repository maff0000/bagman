"""intake_records (CD-4 WI-1, PID §8)

Revision ID: d81cf00fc7de
Revises: 13a9ed820cfd
Create Date: 2026-09-13 00:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision: str = 'd81cf00fc7de'
down_revision: Union[str, Sequence[str], None] = '13a9ed820cfd'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    op.create_table(
        'intake_records',
        sa.Column('intake_id', sa.UUID(as_uuid=False), nullable=False),
        sa.Column('source_id', sa.UUID(as_uuid=False), nullable=False),
        sa.Column('entity_hint', sa.String(), nullable=True),
        sa.Column('status', sa.String(), nullable=False),
        sa.Column('received_at', sa.DateTime(timezone=True), nullable=False),
        sa.Column('completed_at', sa.DateTime(timezone=True), nullable=True),
        sa.Column('original_filename', sa.String(), nullable=True),
        sa.Column('reported_mime_type', sa.String(), nullable=True),
        sa.Column('detected_mime_type', sa.String(), nullable=True),
        sa.Column('size_bytes', sa.BigInteger(), nullable=True),
        sa.Column('content_hash', postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column('evidence_id', sa.UUID(as_uuid=False), nullable=True),
        sa.Column('failure_code', sa.String(), nullable=True),
        sa.Column('quarantine_reason', sa.String(), nullable=True),
        sa.Column('correlation_id', sa.UUID(as_uuid=False), nullable=False),
        sa.Column('idempotency_key', sa.String(), nullable=True),
        sa.Column('metadata', postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.ForeignKeyConstraint(['source_id'], ['sources.source_id'], ),
        sa.ForeignKeyConstraint(['evidence_id'], ['evidence_items.evidence_id'], ),
        sa.PrimaryKeyConstraint('intake_id'),
    )
    op.create_index('ix_intake_records_correlation', 'intake_records', ['correlation_id'], unique=False)
    op.create_index('ix_intake_records_source', 'intake_records', ['source_id'], unique=False)
    # Partial unique index (PID §25/§53): unique only where
    # idempotency_key IS NOT NULL — many records may have no key at
    # all, but any two that DO have one must not collide.
    op.create_index(
        'uq_intake_records_idempotency_key',
        'intake_records',
        ['idempotency_key'],
        unique=True,
        postgresql_where=sa.text('idempotency_key IS NOT NULL'),
    )


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_index('uq_intake_records_idempotency_key', table_name='intake_records')
    op.drop_index('ix_intake_records_source', table_name='intake_records')
    op.drop_index('ix_intake_records_correlation', table_name='intake_records')
    op.drop_table('intake_records')
