"""evidence_classification_jobs (BAGMAN accounting platform,
`evidence/automatic-classification-activation` WO)

The durable job-queue table backing
`services.evidence.classification_job.EvidenceClassificationJobRepository`
— the missing automatic trigger for
`services.evidence.classification_orchestrator.classify_evidence`. See
that module's own docstring for the full idempotency/lifecycle
doctrine this table backs.

Revision ID: a7f34c9e2d18
Revises: f1a2b3c4d5e6
Create Date: 2026-09-28 00:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

# revision identifiers, used by Alembic.
revision: str = 'a7f34c9e2d18'
down_revision: Union[str, Sequence[str], None] = 'f1a2b3c4d5e6'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    op.create_table(
        'evidence_classification_jobs',
        sa.Column('job_id', sa.UUID(as_uuid=False), nullable=False),
        sa.Column('evidence_id', sa.UUID(as_uuid=False), nullable=False),
        sa.Column('status', sa.String(), nullable=False),
        sa.Column('actor_type', sa.String(), nullable=False),
        sa.Column('actor_id', sa.String(), nullable=False),
        sa.Column('correlation_id', sa.String(), nullable=False),
        sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
        sa.Column('updated_at', sa.DateTime(timezone=True), nullable=False),
        sa.Column('attempt_count', sa.Integer(), nullable=False),
        sa.Column('max_attempts', sa.Integer(), nullable=False),
        sa.Column('claimed_by', sa.String(), nullable=True),
        sa.Column('claimed_at', sa.DateTime(timezone=True), nullable=True),
        sa.Column('last_error', sa.String(), nullable=True),
        sa.ForeignKeyConstraint(['evidence_id'], ['evidence_items.evidence_id']),
        sa.PrimaryKeyConstraint('job_id'),
    )
    # Real, plain (non-partial — evidence_id is always present) unique
    # constraint: at most one EvidenceClassificationJob may ever exist
    # per evidence item — the real database-level backstop for
    # services.evidence.classification_job's own evidence_id-keyed
    # idempotency doctrine, mirroring
    # xero_account_suggestions.uq_xero_account_suggestions_evidence_id
    # (migration f1a2b3c4d5e6, this migration's own down_revision) and
    # xero_account_assignments.uq_xero_account_assignments_evidence_id
    # exactly.
    op.create_unique_constraint(
        'uq_evidence_classification_jobs_evidence_id', 'evidence_classification_jobs', ['evidence_id']
    )
    op.create_index(
        'ix_evidence_classification_jobs_status', 'evidence_classification_jobs', ['status'], unique=False
    )


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_index('ix_evidence_classification_jobs_status', table_name='evidence_classification_jobs')
    op.drop_constraint(
        'uq_evidence_classification_jobs_evidence_id', 'evidence_classification_jobs', type_='unique'
    )
    op.drop_table('evidence_classification_jobs')
