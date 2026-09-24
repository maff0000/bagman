"""evidence_classifications + evidence_classification_rules (CD-6 Slice 5 WI-1)

Revision ID: b4d8f1a92c65
Revises: a1c4e8f2d6b7
Create Date: 2026-09-24 00:00:00.000000

Purely additive: two brand-new tables (``evidence_classification_rules``
then ``evidence_classifications``, in that order — the latter carries a
real foreign key to the former's ``rule_id``, so it must exist first)
plus their indexes. Zero modification to any existing table (in
particular ``evidence_items`` is untouched — no column, no index change
of any kind), zero data migration, zero row insertion. See
``services/evidence/classification.py`` and
``services/evidence/classification_rule.py``'s own module docstrings for
the full domain doctrine, and
``persistence/postgres/evidence_classification_models.py`` /
``persistence/postgres/evidence_classification_rule_models.py``'s own
module docstrings for the exact index scheme this migration produces.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision: str = 'b4d8f1a92c65'
down_revision: Union[str, Sequence[str], None] = 'a1c4e8f2d6b7'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    # --- evidence_classification_rules (created first: evidence_classifications.rule_id FKs into it) ---
    op.create_table(
        'evidence_classification_rules',
        sa.Column('rule_id', sa.UUID(as_uuid=False), nullable=False),
        sa.Column('sender_scope_type', sa.String(), nullable=False),
        sa.Column('sender_scope_value', sa.String(), nullable=False),
        sa.Column('subject_predicate_type', sa.String(), nullable=False),
        sa.Column('subject_predicate_value', sa.String(), nullable=False),
        sa.Column('document_type', sa.String(), nullable=False),
        sa.Column('status', sa.String(), nullable=False),
        sa.Column('source', sa.String(), nullable=False),
        sa.Column('supersedes_rule_id', sa.UUID(as_uuid=False), nullable=True),
        sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
        sa.Column('approved_at', sa.DateTime(timezone=True), nullable=False),
        sa.Column('retired_at', sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(['supersedes_rule_id'], ['evidence_classification_rules.rule_id']),
        sa.PrimaryKeyConstraint('rule_id'),
    )
    op.create_index(
        'ix_evidence_classification_rules_sender_scope',
        'evidence_classification_rules',
        ['sender_scope_type', 'sender_scope_value'],
        unique=False,
    )
    # Partial unique index: at most one ACTIVE rule may exist at a given
    # (sender_scope_type, sender_scope_value, subject_predicate_type,
    # subject_predicate_value) identity. Any number of RETIRED rows may
    # share the same identity over time.
    op.create_index(
        'uq_evidence_classification_rules_active_identity',
        'evidence_classification_rules',
        ['sender_scope_type', 'sender_scope_value', 'subject_predicate_type', 'subject_predicate_value'],
        unique=True,
        postgresql_where=sa.text("status = 'ACTIVE'"),
    )

    # --- evidence_classifications ---
    op.create_table(
        'evidence_classifications',
        sa.Column('classification_id', sa.UUID(as_uuid=False), nullable=False),
        sa.Column('evidence_id', sa.UUID(as_uuid=False), nullable=False),
        sa.Column('classification_type', sa.String(), nullable=False),
        sa.Column('document_type', sa.String(), nullable=False),
        sa.Column('status', sa.String(), nullable=False),
        sa.Column('source', sa.String(), nullable=False),
        sa.Column('confidence', sa.Float(), nullable=True),
        sa.Column('rule_id', sa.UUID(as_uuid=False), nullable=True),
        sa.Column('ai_invocation_id', sa.UUID(as_uuid=False), nullable=True),
        sa.Column('operator_action_id', sa.String(), nullable=True),
        sa.Column('reason_codes', postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column('supersedes_classification_id', sa.UUID(as_uuid=False), nullable=True),
        sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(['evidence_id'], ['evidence_items.evidence_id']),
        sa.ForeignKeyConstraint(['rule_id'], ['evidence_classification_rules.rule_id']),
        sa.ForeignKeyConstraint(['ai_invocation_id'], ['ai_invocations.ai_invocation_id']),
        sa.ForeignKeyConstraint(['supersedes_classification_id'], ['evidence_classifications.classification_id']),
        sa.PrimaryKeyConstraint('classification_id'),
    )
    op.create_index(
        'ix_evidence_classifications_evidence_type_created',
        'evidence_classifications',
        ['evidence_id', 'classification_type', 'created_at'],
        unique=False,
    )
    # Branch prevention: at most one row may supersede any given
    # classification_id.
    op.create_index(
        'uq_evidence_classifications_supersedes',
        'evidence_classifications',
        ['supersedes_classification_id'],
        unique=True,
        postgresql_where=sa.text('supersedes_classification_id IS NOT NULL'),
    )
    # Producer idempotency — three separate partial unique indexes, one
    # per source (see persistence/postgres/evidence_classification_models.py's
    # own module docstring for why one combined constraint cannot
    # correctly express this).
    op.create_index(
        'uq_evidence_classifications_rule_producer',
        'evidence_classifications',
        ['evidence_id', 'classification_type', 'rule_id'],
        unique=True,
        postgresql_where=sa.text("source = 'DETERMINISTIC_RULE'"),
    )
    op.create_index(
        'uq_evidence_classifications_ai_producer',
        'evidence_classifications',
        ['evidence_id', 'classification_type', 'ai_invocation_id'],
        unique=True,
        postgresql_where=sa.text("source = 'AI_PROPOSAL'"),
    )
    op.create_index(
        'uq_evidence_classifications_operator_producer',
        'evidence_classifications',
        ['evidence_id', 'classification_type', 'operator_action_id'],
        unique=True,
        postgresql_where=sa.text("source = 'OPERATOR_ASSIGNED'"),
    )


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_index('uq_evidence_classifications_operator_producer', table_name='evidence_classifications')
    op.drop_index('uq_evidence_classifications_ai_producer', table_name='evidence_classifications')
    op.drop_index('uq_evidence_classifications_rule_producer', table_name='evidence_classifications')
    op.drop_index('uq_evidence_classifications_supersedes', table_name='evidence_classifications')
    op.drop_index('ix_evidence_classifications_evidence_type_created', table_name='evidence_classifications')
    op.drop_table('evidence_classifications')

    op.drop_index('uq_evidence_classification_rules_active_identity', table_name='evidence_classification_rules')
    op.drop_index('ix_evidence_classification_rules_sender_scope', table_name='evidence_classification_rules')
    op.drop_table('evidence_classification_rules')
