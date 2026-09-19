"""mailbox_domain_rules: three-state policy learning + EXACT_ADDRESS match mode (CD-6 GUI-operations-foundation follow-on WO)

Revision ID: f7c3a58b1e9d
Revises: b2e6f9a1c374
Create Date: 2026-09-18 00:00:00.000005

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

# revision identifiers, used by Alembic.
revision: str = 'f7c3a58b1e9d'
down_revision: Union[str, Sequence[str], None] = 'b2e6f9a1c374'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

#: Judgment call (documented, matches this delivery's own final report):
#: production carries ZERO `mailbox_domain_rules` rows at the time of
#: this migration (nothing has ever been approved) — the app-level
#: policy rename (`ALLOWED`->`MUST_READ`, `IGNORED`->`BLACKLIST`, plus
#: the new `GRAYLIST` value) therefore needs NO data backfill/rewrite
#: here; `policy` remains a plain `String` column with no DB-level CHECK
#: constraint (mirrors this table's existing "validated at the service
#: layer, not the schema layer" discipline for `policy`/`match_mode`
#: already established by e5a2c917b6d4). This migration only adds the
#: genuinely NEW schema surface: the `sender_address` column
#: (EXACT_ADDRESS match mode's own identity key) and the two PARTIAL
#: unique indexes that replace the old single, non-partial
#: `uq_mailbox_domain_rules_mailbox_domain` constraint — see
#: `persistence/postgres/mailbox_domain_rule_models.py`'s own module
#: docstring for exactly why two partial indexes, not one.


def upgrade() -> None:
    """Upgrade schema."""
    op.add_column('mailbox_domain_rules', sa.Column('sender_address', sa.String(), nullable=True))

    op.drop_constraint('uq_mailbox_domain_rules_mailbox_domain', 'mailbox_domain_rules', type_='unique')

    op.create_index(
        'uq_mailbox_domain_rules_mailbox_domain_scope',
        'mailbox_domain_rules',
        ['mailbox_id', 'sender_domain'],
        unique=True,
        postgresql_where=sa.text("match_mode != 'EXACT_ADDRESS'"),
    )
    op.create_index(
        'uq_mailbox_domain_rules_mailbox_address',
        'mailbox_domain_rules',
        ['mailbox_id', 'sender_address'],
        unique=True,
        postgresql_where=sa.text("match_mode = 'EXACT_ADDRESS'"),
    )


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_index('uq_mailbox_domain_rules_mailbox_address', table_name='mailbox_domain_rules')
    op.drop_index('uq_mailbox_domain_rules_mailbox_domain_scope', table_name='mailbox_domain_rules')
    op.create_unique_constraint(
        'uq_mailbox_domain_rules_mailbox_domain', 'mailbox_domain_rules', ['mailbox_id', 'sender_domain']
    )
    op.drop_column('mailbox_domain_rules', 'sender_address')
