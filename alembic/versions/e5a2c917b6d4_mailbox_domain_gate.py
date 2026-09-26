"""mailbox domain-policy gate + entity email bootstrap boundary (CD-6 architect amendment)

Revision ID: e5a2c917b6d4
Revises: b7e2f5a9c1d3
Create Date: 2026-09-18 00:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision: str = 'e5a2c917b6d4'
down_revision: Union[str, Sequence[str], None] = 'b7e2f5a9c1d3'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    # -- GovernedEntity: real accounting-period configuration (additive,
    # nullable — existing rows get NULL, never an invented default). --
    op.add_column('governed_entities', sa.Column('fiscal_year_start_month_day', sa.String(), nullable=True))
    op.add_column(
        'governed_entities', sa.Column('email_bootstrap_floor_at', sa.DateTime(timezone=True), nullable=True)
    )

    # -- MailboxMessage: Stage-A discovery fields (additive). ----------
    op.add_column('mailbox_messages', sa.Column('sender_domain', sa.String(), nullable=True))
    op.add_column(
        'mailbox_messages',
        sa.Column(
            'attachment_metadata',
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
            server_default='[]',
        ),
    )
    op.add_column(
        'mailbox_messages',
        sa.Column('auth_signals', postgresql.JSONB(astext_type=sa.Text()), nullable=False, server_default='{}'),
    )

    # -- MailboxDomainRule: mailbox-specific Stage-B domain gate. -------
    op.create_table(
        'mailbox_domain_rules',
        sa.Column('rule_id', sa.UUID(as_uuid=False), nullable=False),
        sa.Column('mailbox_id', sa.UUID(as_uuid=False), nullable=False),
        sa.Column('sender_domain', sa.String(), nullable=False),
        sa.Column('match_mode', sa.String(), nullable=False),
        sa.Column('policy', sa.String(), nullable=False),
        sa.Column('destination_entity_id', sa.UUID(as_uuid=False), nullable=True),
        sa.Column('destination_mode', sa.String(), nullable=True),
        sa.Column('source', sa.String(), nullable=False),
        sa.Column('processor_hint', sa.String(), nullable=True),
        sa.Column('approved_at', sa.DateTime(timezone=True), nullable=True),
        sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
        sa.Column('updated_at', sa.DateTime(timezone=True), nullable=False),
        sa.Column('last_seen_at', sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint('rule_id'),
    )
    op.create_unique_constraint(
        'uq_mailbox_domain_rules_mailbox_domain', 'mailbox_domain_rules', ['mailbox_id', 'sender_domain']
    )


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_constraint('uq_mailbox_domain_rules_mailbox_domain', 'mailbox_domain_rules', type_='unique')
    op.drop_table('mailbox_domain_rules')
    op.drop_column('mailbox_messages', 'auth_signals')
    op.drop_column('mailbox_messages', 'attachment_metadata')
    op.drop_column('mailbox_messages', 'sender_domain')
    op.drop_column('governed_entities', 'email_bootstrap_floor_at')
    op.drop_column('governed_entities', 'fiscal_year_start_month_day')
