"""mailbox microsoft sweep engine (CD-6 Slice 4)

Revision ID: b7e2f5a9c1d3
Revises: a3d7c1f9e246
Create Date: 2026-09-17 00:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision: str = 'b7e2f5a9c1d3'
down_revision: Union[str, Sequence[str], None] = 'a3d7c1f9e246'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    op.create_table(
        'mailbox_messages',
        sa.Column('mailbox_message_id', sa.UUID(as_uuid=False), nullable=False),
        sa.Column('mailbox_id', sa.UUID(as_uuid=False), nullable=False),
        sa.Column('provider_kind', sa.String(), nullable=False),
        sa.Column('immutable_provider_message_id', sa.String(), nullable=False),
        sa.Column('internet_message_id', sa.String(), nullable=True),
        sa.Column('observed_folder', sa.String(), nullable=False),
        sa.Column('subject', sa.String(), nullable=True),
        sa.Column('sender_address', sa.String(), nullable=True),
        sa.Column('sender_display_name', sa.String(), nullable=True),
        sa.Column('received_at', sa.DateTime(timezone=True), nullable=False),
        sa.Column('has_attachments', sa.Boolean(), nullable=False),
        sa.Column('evidence_id', sa.UUID(as_uuid=False), nullable=True),
        sa.Column('ingestion_status', sa.String(), nullable=False),
        sa.Column('first_seen_at', sa.DateTime(timezone=True), nullable=False),
        sa.Column('last_seen_at', sa.DateTime(timezone=True), nullable=False),
        sa.Column('metadata', postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.PrimaryKeyConstraint('mailbox_message_id'),
    )
    op.create_unique_constraint(
        'uq_mailbox_messages_mailbox_provider_id', 'mailbox_messages',
        ['mailbox_id', 'immutable_provider_message_id'],
    )
    op.create_index('ix_mailbox_messages_mailbox_id', 'mailbox_messages', ['mailbox_id'])

    op.create_table(
        'mailbox_sweep_runs',
        sa.Column('sweep_run_id', sa.UUID(as_uuid=False), nullable=False),
        sa.Column('mailbox_id', sa.UUID(as_uuid=False), nullable=False),
        sa.Column('trigger', sa.String(), nullable=False),
        sa.Column('status', sa.String(), nullable=False),
        sa.Column('started_at', sa.DateTime(timezone=True), nullable=False),
        sa.Column('completed_at', sa.DateTime(timezone=True), nullable=True),
        sa.Column('folders_attempted', postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column('messages_seen', sa.Integer(), nullable=False),
        sa.Column('messages_new', sa.Integer(), nullable=False),
        sa.Column('evidence_created', sa.Integer(), nullable=False),
        sa.Column('duplicates', sa.Integer(), nullable=False),
        sa.Column('quarantined', sa.Integer(), nullable=False),
        sa.Column('failures', sa.Integer(), nullable=False),
        sa.Column('error_code', sa.String(), nullable=True),
        sa.Column('error_detail', sa.String(), nullable=True),
        sa.PrimaryKeyConstraint('sweep_run_id'),
    )
    op.create_index('ix_mailbox_sweep_runs_mailbox_id', 'mailbox_sweep_runs', ['mailbox_id'])

    op.create_table(
        'mailbox_folder_cursors',
        sa.Column('mailbox_id', sa.UUID(as_uuid=False), nullable=False),
        sa.Column('provider_kind', sa.String(), nullable=False),
        sa.Column('folder', sa.String(), nullable=False),
        sa.Column('delta_link', sa.String(), nullable=True),
        sa.Column('bootstrap_timestamp', sa.DateTime(timezone=True), nullable=False),
        sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
        sa.Column('updated_at', sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint('mailbox_id', 'provider_kind', 'folder'),
    )

    op.create_table(
        'mailbox_sweep_locks',
        sa.Column('mailbox_id', sa.UUID(as_uuid=False), nullable=False),
        sa.Column('owner_token', sa.String(), nullable=False),
        sa.Column('acquired_at', sa.DateTime(timezone=True), nullable=False),
        sa.Column('expires_at', sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint('mailbox_id'),
    )

    op.create_table(
        'mailbox_microsoft_oauth_states',
        sa.Column('state', sa.String(), nullable=False),
        sa.Column('mailbox_id', sa.UUID(as_uuid=False), nullable=False),
        sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
        sa.Column('expires_at', sa.DateTime(timezone=True), nullable=False),
        sa.Column('consumed_at', sa.DateTime(timezone=True), nullable=True),
        sa.PrimaryKeyConstraint('state'),
    )


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_table('mailbox_microsoft_oauth_states')
    op.drop_table('mailbox_sweep_locks')
    op.drop_table('mailbox_folder_cursors')
    op.drop_index('ix_mailbox_sweep_runs_mailbox_id', table_name='mailbox_sweep_runs')
    op.drop_table('mailbox_sweep_runs')
    op.drop_index('ix_mailbox_messages_mailbox_id', table_name='mailbox_messages')
    op.drop_constraint('uq_mailbox_messages_mailbox_provider_id', 'mailbox_messages', type_='unique')
    op.drop_table('mailbox_messages')
