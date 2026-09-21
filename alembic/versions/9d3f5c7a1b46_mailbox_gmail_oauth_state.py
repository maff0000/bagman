"""mailbox_gmail_oauth_states: Postgres-backed Gmail OAuth CSRF-state persistence (CD-6 GUI-operations-foundation follow-on WO)

Revision ID: 9d3f5c7a1b46
Revises: f7c3a58b1e9d
Create Date: 2026-09-21 00:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

# revision identifiers, used by Alembic.
revision: str = '9d3f5c7a1b46'
down_revision: Union[str, Sequence[str], None] = 'f7c3a58b1e9d'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

#: Same column/constraint shape as `mailbox_microsoft_oauth_states`
#: (b7e2f5a9c1d3) — primary key directly on `state`, `mailbox_id` as a
#: non-nullable UUID, `created_at`/`expires_at` non-nullable,
#: `consumed_at` nullable. This migration touches no other table — the
#: Gmail adapter's OAuth-state store was `InMemoryGmailOAuthStateRepository`
#: even in production until now (see
#: `persistence/postgres/mailbox_gmail_repository.py`'s own module
#: docstring for the defect this closes).


def upgrade() -> None:
    """Upgrade schema."""
    op.create_table(
        'mailbox_gmail_oauth_states',
        sa.Column('state', sa.String(), nullable=False),
        sa.Column('mailbox_id', sa.UUID(as_uuid=False), nullable=False),
        sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
        sa.Column('expires_at', sa.DateTime(timezone=True), nullable=False),
        sa.Column('consumed_at', sa.DateTime(timezone=True), nullable=True),
        sa.PrimaryKeyConstraint('state'),
    )


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_table('mailbox_gmail_oauth_states')
