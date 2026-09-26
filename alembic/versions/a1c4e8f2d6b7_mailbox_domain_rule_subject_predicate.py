"""mailbox_domain_rules: deterministic subject-aware mailbox domain policy — EXACT_DOMAIN_SUBJECT match mode (CD-6 GUI-operations-foundation follow-on WO)

Revision ID: a1c4e8f2d6b7
Revises: 9d3f5c7a1b46
Create Date: 2026-09-23 00:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

# revision identifiers, used by Alembic.
revision: str = 'a1c4e8f2d6b7'
down_revision: Union[str, Sequence[str], None] = '9d3f5c7a1b46'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

#: A genuinely new match_mode, `EXACT_DOMAIN_SUBJECT` (deterministic
#: subject-aware mailbox domain policy) — see
#: `services/mailbox/domain_rule.py`'s own module docstring for the full
#: semantics, and `persistence/postgres/mailbox_domain_rule_models.py`'s
#: own module docstring for the exact three-index scheme this migration
#: produces. Production carries exactly 19 `mailbox_domain_rules` rows
#: at the time of this migration — all `EXACT`/`INCLUDE_SUBDOMAINS`/
#: `EXACT_ADDRESS`, zero `EXACT_DOMAIN_SUBJECT` — so this migration does
#: NOT need to touch any existing row's DATA at all, only schema/
#: indexes. `match_mode`/`policy`/`subject_predicate_type` remain plain
#: `String` columns with no DB-level CHECK constraint (mirrors this
#: table's existing "validated at the service layer, not the schema
#: layer" discipline, established by e5a2c917b6d4 and carried forward by
#: f7c3a58b1e9d).
#:
#: The critical index correction (why the OLD domain-scope index cannot
#: simply be left as-is): `uq_mailbox_domain_rules_mailbox_domain_scope`
#: was scoped `WHERE match_mode != 'EXACT_ADDRESS'` — which would
#: WRONGLY also cover the new `EXACT_DOMAIN_SUBJECT` match_mode, making
#: it impossible to store more than one `EXACT_DOMAIN_SUBJECT` row per
#: (mailbox_id, sender_domain), and colliding with any coexisting
#: domain-level fallback rule at the same domain. This migration DROPS
#: that index and RECREATES it scoped to ONLY
#: `match_mode IN ('EXACT', 'INCLUDE_SUBDOMAINS')` — the genuinely
#: domain-level identity space — then ADDS a new, separate partial
#: unique index, `uq_mailbox_domain_rules_mailbox_subject`, on
#: (mailbox_id, sender_domain, subject_predicate_type,
#: subject_predicate_value) scoped `WHERE match_mode =
#: 'EXACT_DOMAIN_SUBJECT'`. `uq_mailbox_domain_rules_mailbox_address`
#: (the EXACT_ADDRESS identity space) is left completely untouched.


def upgrade() -> None:
    """Upgrade schema."""
    op.add_column('mailbox_domain_rules', sa.Column('subject_predicate_type', sa.String(), nullable=True))
    op.add_column('mailbox_domain_rules', sa.Column('subject_predicate_value', sa.String(), nullable=True))

    op.drop_index('uq_mailbox_domain_rules_mailbox_domain_scope', table_name='mailbox_domain_rules')

    op.create_index(
        'uq_mailbox_domain_rules_mailbox_domain_scope',
        'mailbox_domain_rules',
        ['mailbox_id', 'sender_domain'],
        unique=True,
        postgresql_where=sa.text("match_mode IN ('EXACT', 'INCLUDE_SUBDOMAINS')"),
    )
    op.create_index(
        'uq_mailbox_domain_rules_mailbox_subject',
        'mailbox_domain_rules',
        ['mailbox_id', 'sender_domain', 'subject_predicate_type', 'subject_predicate_value'],
        unique=True,
        postgresql_where=sa.text("match_mode = 'EXACT_DOMAIN_SUBJECT'"),
    )


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_index('uq_mailbox_domain_rules_mailbox_subject', table_name='mailbox_domain_rules')
    op.drop_index('uq_mailbox_domain_rules_mailbox_domain_scope', table_name='mailbox_domain_rules')
    op.create_index(
        'uq_mailbox_domain_rules_mailbox_domain_scope',
        'mailbox_domain_rules',
        ['mailbox_id', 'sender_domain'],
        unique=True,
        postgresql_where=sa.text("match_mode != 'EXACT_ADDRESS'"),
    )
    op.drop_column('mailbox_domain_rules', 'subject_predicate_value')
    op.drop_column('mailbox_domain_rules', 'subject_predicate_type')
