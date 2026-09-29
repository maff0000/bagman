"""evidence_classification_jobs.classification_outcome (BAGMAN
accounting platform, `evidence/automatic-classification-activation` WO
— architect delta, 2026-09-29 review)

Adds the durable `classification_outcome` column
`services.evidence.classification_job.EvidenceClassificationJob` now
carries — the exact `classify_evidence` outcome (one of
`services.evidence.classification_orchestrator.CLASSIFY_EVIDENCE_OUTCOMES`)
a job's one dispatch attempt produced, so an `AI_IN_PROGRESS`/
`AI_INVOCATION_FAILED`/`AI_PRIOR_FAILURE` outcome can never be silently
recorded as if real classification succeeded. See
`services.evidence.classification_job`'s own module docstring
("Correct durable job outcome semantics") and
`scripts/process_evidence_classification_jobs.py`'s own outcome-mapping
doctrine for the full reasoning. Nullable — `None` for any job that has
never reached a terminal `classify_evidence`-driven outcome (still
`PENDING`/`CLAIMED`, or `FAILED_RETRYABLE`/`FAILED_TERMINAL` from a
genuinely RAISED infrastructure exception that never reached
`classify_evidence`'s own outcome vocabulary at all).

Revision ID: ae936a444eae
Revises: a7f34c9e2d18
Create Date: 2026-09-29 00:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

# revision identifiers, used by Alembic.
revision: str = 'ae936a444eae'
down_revision: Union[str, Sequence[str], None] = 'a7f34c9e2d18'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    op.add_column(
        'evidence_classification_jobs',
        sa.Column('classification_outcome', sa.String(), nullable=True),
    )


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_column('evidence_classification_jobs', 'classification_outcome')
