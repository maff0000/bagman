"""xero_account_suggestions: real uniqueness constraint on evidence_id
(BAGMAN accounting platform, `xero/account-suggestion-producer` WO
post-fix follow-up)

Architect finding: `xero_account_suggestions` was created PK-only, with
no constraint preventing two rows for the same `evidence_id`. A real,
disposable-PostgreSQL concurrency test
(`tests/persistence/test_xero_account_suggestion_concurrency.py`)
forced two genuinely concurrent `produce_account_suggestion` callers
for the same `evidence_id` through the exact post-invocation/
pre-persist window and reproduced two committed rows deterministically
— the suspected race was real, not merely theoretical.

The intended invariant (module docstring,
`services/xero/account_suggestion.py`, and the orchestrator's own
`get_by_evidence`-before-any-AI-call business-level idempotency check)
has always been "at most one suggestion per evidence item" — this
migration makes that invariant real at the database level, mirroring
`xero_account_assignments`'s own already-correct
`uq_xero_account_assignments_evidence_id` pattern exactly.
`persistence/postgres/xero_account_suggestion_repository.py::
PostgresXeroAccountSuggestionRepository.create_suggestion` was updated
in the same change to translate the resulting `IntegrityError` into an
idempotent "return the existing row" outcome (never a raised
`ConflictError` — unlike an assignment, a suggestion has no
"conflicting decision" semantic to guard; the losing concurrent caller
simply learns about the row the winning caller already created).

Revision ID: f1a2b3c4d5e6
Revises: e8c4a1f97b23
Create Date: 2026-09-28 00:00:00.000000

"""
from typing import Sequence, Union

from alembic import op

# revision identifiers, used by Alembic.
revision: str = 'f1a2b3c4d5e6'
down_revision: Union[str, Sequence[str], None] = 'e8c4a1f97b23'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    op.create_unique_constraint(
        'uq_xero_account_suggestions_evidence_id', 'xero_account_suggestions', ['evidence_id']
    )


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_constraint(
        'uq_xero_account_suggestions_evidence_id', 'xero_account_suggestions', type_='unique'
    )
