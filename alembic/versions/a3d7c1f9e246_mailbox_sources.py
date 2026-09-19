"""mailbox_sources (CD-6 Slice 3: Mailbox Management)

Revision ID: a3d7c1f9e246
Revises: 5e8c1f42b9a7
Create Date: 2026-09-17 00:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision: str = 'a3d7c1f9e246'
down_revision: Union[str, Sequence[str], None] = '5e8c1f42b9a7'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    op.create_table(
        'mailbox_sources',
        sa.Column('mailbox_id', sa.UUID(as_uuid=False), nullable=False),
        sa.Column('display_name', sa.String(), nullable=False),
        sa.Column('email_address', sa.String(), nullable=False),
        sa.Column('provider_kind', sa.String(), nullable=False),
        sa.Column('default_entity_id', sa.UUID(as_uuid=False), nullable=True),
        sa.Column('enabled', sa.Boolean(), nullable=False),
        sa.Column('status', sa.String(), nullable=False),
        sa.Column('connection_state', sa.String(), nullable=False),
        sa.Column('last_connection_check_at', sa.DateTime(timezone=True), nullable=True),
        sa.Column('last_successful_sweep_at', sa.DateTime(timezone=True), nullable=True),
        sa.Column('last_error_code', sa.String(), nullable=True),
        sa.Column('last_error_detail', sa.String(), nullable=True),
        sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
        sa.Column('updated_at', sa.DateTime(timezone=True), nullable=False),
        sa.Column('metadata', postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.PrimaryKeyConstraint('mailbox_id'),
    )
    # GLOBAL uniqueness (a plain, non-partial constraint — email_address
    # is always present on every row) — see
    # services/mailbox/mailbox.py's own module docstring "Uniqueness"
    # section for why this is never freed by retirement, unlike
    # xero_connections.tenant_id's own partial-unique-index treatment.
    op.create_unique_constraint(
        'uq_mailbox_sources_email_address', 'mailbox_sources', ['email_address']
    )


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_constraint('uq_mailbox_sources_email_address', 'mailbox_sources', type_='unique')
    op.drop_table('mailbox_sources')
