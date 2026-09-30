"""evidence_classification_reconciliation_cursors (BAGMAN accounting
platform, `evidence/automatic-classification-activation` WO —
post-merge preflight review correction, item B)

The durable, per-activation-boundary scan cursor backing
`services.evidence.classification_reconciliation_cursor
.EvidenceClassificationReconciliationCursorRepository` — closes the
reconciliation starvation bug documented on that module's own
docstring (a fixed-window, no-persisted-state scan could permanently
never reach an orphan sorted behind more than `max_rows_scanned`
already-resolved rows). See that module's own docstring for the full
reasoning, and `services.evidence.classification_job
.reconcile_missing_classification_jobs`'s own docstring for how this
table's rows are actually used.

Revision ID: 143b86b2ab44
Revises: ae936a444eae
Create Date: 2026-09-30 00:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision: str = '143b86b2ab44'
down_revision: Union[str, Sequence[str], None] = 'ae936a444eae'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    op.create_table(
        'evidence_classification_reconciliation_cursors',
        sa.Column('cursor_key', sa.String(), nullable=False),
        sa.Column('last_created_at', sa.DateTime(timezone=True), nullable=False),
        # JSONB list of evidence_id strings — the set of every id
        # actually inspected at exactly `last_created_at` (post-merge
        # audit correction; replaces a single-scalar `last_evidence_id`
        # column, never shipped, whose ordering-based `<=` tie-break was
        # unsound across processes — see
        # services.evidence.classification_reconciliation_cursor's own
        # docstring).
        sa.Column('last_evidence_ids', postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
        sa.Column('updated_at', sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint('cursor_key'),
    )


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_table('evidence_classification_reconciliation_cursors')
