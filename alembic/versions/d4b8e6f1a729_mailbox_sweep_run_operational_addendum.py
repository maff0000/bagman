"""mailbox_sweep_runs operational addendum aggregate reporting columns (CD-6 architect operational addendum, ahead of the first real large historical sweep)

Revision ID: d4b8e6f1a729
Revises: c48f2a7e9d31
Create Date: 2026-09-18 00:00:00.000003

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

# revision identifiers, used by Alembic.
revision: str = 'd4b8e6f1a729'
down_revision: Union[str, Sequence[str], None] = 'c48f2a7e9d31'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

#: The seven new aggregate-reporting columns this addendum adds to
#: `mailbox_sweep_runs` — see `services/mailbox/sweep_run.py`'s own
#: `MailboxSweepRun` dataclass docstring/fields for what each one
#: means. All additive, `NOT NULL DEFAULT 0` — an existing row (any
#: sweep run recorded before this addendum) reads back a real, honest
#: `0` for each, never a silently-invented non-zero value.
_NEW_COLUMNS = (
    'unique_sender_domains',
    'allowed_domain_messages',
    'ignored_domain_messages',
    'unknown_domain_messages',
    'likely_financial_candidates',
    'messages_with_attachments',
    'graph_throttle_retries',
)


def upgrade() -> None:
    """Upgrade schema."""
    for column_name in _NEW_COLUMNS:
        op.add_column(
            'mailbox_sweep_runs',
            sa.Column(column_name, sa.Integer(), nullable=False, server_default='0'),
        )


def downgrade() -> None:
    """Downgrade schema."""
    for column_name in _NEW_COLUMNS:
        op.drop_column('mailbox_sweep_runs', column_name)
