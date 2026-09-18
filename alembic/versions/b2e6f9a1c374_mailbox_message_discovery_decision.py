"""mailbox_messages persisted discovery decision (CD-6 architect amendment, second correction — distinguishes UNKNOWN-domain-candidate / UNKNOWN-domain-non-candidate / IGNORED-domain CHECKED_NOT_CANDIDATE cases)

Revision ID: b2e6f9a1c374
Revises: d4b8e6f1a729
Create Date: 2026-09-18 00:00:00.000004

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

# revision identifiers, used by Alembic.
revision: str = 'b2e6f9a1c374'
down_revision: Union[str, Sequence[str], None] = 'd4b8e6f1a729'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

#: Three new columns on `mailbox_messages` — see
#: `services/mailbox/message.py`'s own `MailboxMessage.discovery_candidate`/
#: `discovery_reason`/`discovery_checked_at` field docstrings for exactly
#: what each one means. All additive, nullable, with NO `server_default`
#: — `NULL` is the only safe default here: the real 128 already-ingested
#: Infosecurs messages never went through discovery-only handling at
#: all (they predate this addendum entirely, and were `INGESTED`
#: directly under the original Slice 4A 7-day scheme), so they must read
#: back an honest `NULL` ("unknown/not applicable"), never a
#: silently-invented `false` that would falsely claim "the heuristic ran
#: and said no" for a message that never ran it.


def upgrade() -> None:
    """Upgrade schema."""
    op.add_column('mailbox_messages', sa.Column('discovery_candidate', sa.Boolean(), nullable=True))
    op.add_column('mailbox_messages', sa.Column('discovery_reason', sa.String(), nullable=True))
    op.add_column('mailbox_messages', sa.Column('discovery_checked_at', sa.DateTime(timezone=True), nullable=True))


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_column('mailbox_messages', 'discovery_checked_at')
    op.drop_column('mailbox_messages', 'discovery_reason')
    op.drop_column('mailbox_messages', 'discovery_candidate')
