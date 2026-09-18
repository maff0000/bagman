"""mailbox recursive folder discovery (CD-6 architect amendment)

Revision ID: a9c4e17f2b83
Revises: e5a2c917b6d4
Create Date: 2026-09-18 00:00:00.000001

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

# revision identifiers, used by Alembic.
revision: str = 'a9c4e17f2b83'
down_revision: Union[str, Sequence[str], None] = 'e5a2c917b6d4'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    # -- MailboxMessage: human-readable folder label alongside the
    # existing `observed_folder` (which now holds an OPEN, real Graph
    # folder id — see `services/mailbox/message.py`'s own module
    # docstring; this is purely additive, no column TYPE/constraint on
    # `observed_folder` itself changes, so the real 128 already-ingested
    # Infosecurs rows need no data migration at all — their existing
    # `observed_folder` values ("INBOX"/"JUNK") remain valid, readable
    # strings, simply no longer matched against a closed enum). --------
    op.add_column('mailbox_messages', sa.Column('observed_folder_display_name', sa.String(), nullable=True))

    # -- MailboxFolderCursor (`mailbox_folder_cursors`): NO schema
    # change at all. `folder` was already a plain `String` primary-key
    # component (never a Postgres ENUM type, never constrained to
    # INBOX/JUNK at the database layer) — see
    # `persistence/postgres/mailbox_microsoft_models.py::MailboxFolderCursorRow`.
    # The CD-6 folder-expansion amendment changes ONLY what VALUES the
    # application now writes into that column (real Graph folder ids
    # instead of the literal strings "INBOX"/"JUNK") — a pure
    # application-layer/judgment-call change, not a schema migration.
    # The OLD "INBOX"/"JUNK"-keyed cursor rows for the real, live
    # Infosecurs mailbox are deliberately left in place, unreferenced by
    # the new folder-ID-keyed lookups, as a historical audit trail (see
    # `services/mailbox/sweep.py`'s own module docstring, "Cursor
    # migration judgment call", for the full reasoning and why this is
    # safe: `MailboxMessageRepository`'s own (mailbox_id,
    # immutable_provider_message_id) idempotency prevents any duplicate
    # evidence even though Inbox/Junk Email's cursors restart from a
    # fresh bootstrap round under their new, real folder-id keys).


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_column('mailbox_messages', 'observed_folder_display_name')
