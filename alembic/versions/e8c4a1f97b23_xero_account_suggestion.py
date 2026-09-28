"""xero_account_suggestions, xero_account_assignments (BAGMAN accounting
platform, `xero/account-suggestion-producer` WO)

Revision ID: e8c4a1f97b23
Revises: c7a3f9e1b542
Create Date: 2026-09-28 00:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision: str = 'e8c4a1f97b23'
down_revision: Union[str, Sequence[str], None] = 'c7a3f9e1b542'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    op.create_table(
        'xero_account_suggestions',
        sa.Column('suggestion_id', sa.UUID(as_uuid=False), nullable=False),
        sa.Column('evidence_id', sa.UUID(as_uuid=False), nullable=False),
        sa.Column('entity_id', sa.UUID(as_uuid=False), nullable=False),
        sa.Column('tenant_id', sa.String(), nullable=False),
        sa.Column('suggested_account_id', sa.String(), nullable=False),
        sa.Column('confidence', sa.Float(), nullable=True),
        sa.Column('signals', postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column('ai_invocation_id', sa.UUID(as_uuid=False), nullable=False),
        sa.Column('status', sa.String(), nullable=False),
        sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(['evidence_id'], ['evidence_items.evidence_id']),
        sa.ForeignKeyConstraint(['entity_id'], ['governed_entities.entity_id']),
        sa.ForeignKeyConstraint(['ai_invocation_id'], ['ai_invocations.ai_invocation_id']),
        sa.PrimaryKeyConstraint('suggestion_id'),
    )
    # No uniqueness constraint on evidence_id beyond the primary key —
    # deliberate (services.xero.account_suggestion's own module
    # docstring: "one non-superseded suggestion attempt per fingerprint,
    # enforced via the idempotency check in the orchestrator, not a DB
    # constraint beyond a real PK"). This index backs
    # XeroAccountSuggestionRepository.get_by_evidence's query only.
    op.create_index(
        'ix_xero_account_suggestions_evidence', 'xero_account_suggestions', ['evidence_id'], unique=False
    )
    op.create_index(
        'ix_xero_account_suggestions_entity', 'xero_account_suggestions', ['entity_id'], unique=False
    )
    op.create_index(
        'ix_xero_account_suggestions_ai_invocation',
        'xero_account_suggestions',
        ['ai_invocation_id'],
        unique=False,
    )

    op.create_table(
        'xero_account_assignments',
        sa.Column('assignment_id', sa.UUID(as_uuid=False), nullable=False),
        sa.Column('evidence_id', sa.UUID(as_uuid=False), nullable=False),
        sa.Column('entity_id', sa.UUID(as_uuid=False), nullable=False),
        sa.Column('tenant_id', sa.String(), nullable=False),
        sa.Column('account_id', sa.String(), nullable=False),
        sa.Column('source', sa.String(), nullable=False),
        sa.Column('suggestion_id', sa.UUID(as_uuid=False), nullable=True),
        sa.Column('assigned_by_actor_type', sa.String(), nullable=False),
        sa.Column('assigned_by_actor_id', sa.String(), nullable=False),
        sa.Column('assigned_at', sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(['evidence_id'], ['evidence_items.evidence_id']),
        sa.ForeignKeyConstraint(['entity_id'], ['governed_entities.entity_id']),
        sa.ForeignKeyConstraint(['suggestion_id'], ['xero_account_suggestions.suggestion_id']),
        sa.PrimaryKeyConstraint('assignment_id'),
    )
    # Real, plain (non-partial — evidence_id is always present) unique
    # constraint: exactly one assignment may ever exist per evidence
    # item — the real database-level backstop for
    # services.xero.account_assignment's own write-once/ConflictError
    # discipline (mirrors XeroConnectionRow.uq_xero_connections_entity_id's
    # own "always-present column, plain unique constraint" reasoning).
    op.create_unique_constraint(
        'uq_xero_account_assignments_evidence_id', 'xero_account_assignments', ['evidence_id']
    )
    op.create_index(
        'ix_xero_account_assignments_entity', 'xero_account_assignments', ['entity_id'], unique=False
    )


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_index('ix_xero_account_assignments_entity', table_name='xero_account_assignments')
    op.drop_constraint(
        'uq_xero_account_assignments_evidence_id', 'xero_account_assignments', type_='unique'
    )
    op.drop_table('xero_account_assignments')

    op.drop_index('ix_xero_account_suggestions_ai_invocation', table_name='xero_account_suggestions')
    op.drop_index('ix_xero_account_suggestions_entity', table_name='xero_account_suggestions')
    op.drop_index('ix_xero_account_suggestions_evidence', table_name='xero_account_suggestions')
    op.drop_table('xero_account_suggestions')
