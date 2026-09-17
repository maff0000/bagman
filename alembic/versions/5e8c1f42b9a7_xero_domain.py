"""xero_connections, xero_accounts, xero_sync_runs, xero_oauth_states
(CD-6 Slice 2, PID §98.4, architect spec §2/§4/§17)

Revision ID: 5e8c1f42b9a7
Revises: 712c5a2aab92
Create Date: 2026-09-17 00:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision: str = '5e8c1f42b9a7'
down_revision: Union[str, Sequence[str], None] = '712c5a2aab92'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    op.create_table(
        'xero_connections',
        sa.Column('xero_connection_id', sa.UUID(as_uuid=False), nullable=False),
        sa.Column('entity_id', sa.UUID(as_uuid=False), nullable=False),
        sa.Column('provider', sa.String(), nullable=False),
        sa.Column('tenant_id', sa.String(), nullable=True),
        sa.Column('tenant_name', sa.String(), nullable=True),
        sa.Column('status', sa.String(), nullable=False),
        sa.Column('connected_at', sa.DateTime(timezone=True), nullable=True),
        sa.Column('last_successful_sync_at', sa.DateTime(timezone=True), nullable=True),
        sa.Column('last_attempted_sync_at', sa.DateTime(timezone=True), nullable=True),
        sa.Column('token_expires_at', sa.DateTime(timezone=True), nullable=True),
        sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
        sa.Column('updated_at', sa.DateTime(timezone=True), nullable=False),
        sa.Column('error_detail', sa.String(), nullable=True),
        sa.Column('metadata', postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.PrimaryKeyConstraint('xero_connection_id'),
    )
    op.create_unique_constraint('uq_xero_connections_entity_id', 'xero_connections', ['entity_id'])
    op.create_index('ix_xero_connections_status', 'xero_connections', ['status'], unique=False)
    # Partial unique index (architect spec §17's anti-duplicate-tenant-
    # mapping requirement) — unique only where tenant_id IS NOT NULL,
    # mirroring 9c2f4b1e7a05's own uq_needs_you_items_type_source_ref
    # pattern for the identical reason (a plain UniqueConstraint cannot
    # itself be conditioned on IS NOT NULL).
    op.create_index(
        'uq_xero_connections_tenant_id',
        'xero_connections',
        ['tenant_id'],
        unique=True,
        postgresql_where=sa.text('tenant_id IS NOT NULL'),
    )

    op.create_table(
        'xero_accounts',
        sa.Column('xero_account_row_id', sa.UUID(as_uuid=False), nullable=False),
        sa.Column('entity_id', sa.UUID(as_uuid=False), nullable=False),
        sa.Column('tenant_id', sa.String(), nullable=False),
        sa.Column('account_id', sa.String(), nullable=False),
        sa.Column('code', sa.String(), nullable=True),
        sa.Column('name', sa.String(), nullable=False),
        sa.Column('type', sa.String(), nullable=False),
        sa.Column('account_class', sa.String(), nullable=True),
        sa.Column('tax_type', sa.String(), nullable=True),
        sa.Column('status', sa.String(), nullable=True),
        sa.Column('show_in_expense_claims', sa.Boolean(), nullable=True),
        sa.Column('reporting_code', sa.String(), nullable=True),
        sa.Column('reporting_code_name', sa.String(), nullable=True),
        sa.Column('source_updated_date_utc', sa.DateTime(timezone=True), nullable=True),
        sa.Column('first_synced_at', sa.DateTime(timezone=True), nullable=False),
        sa.Column('last_synced_at', sa.DateTime(timezone=True), nullable=False),
        sa.Column('last_sync_run_id', sa.UUID(as_uuid=False), nullable=False),
        sa.PrimaryKeyConstraint('xero_account_row_id'),
    )
    op.create_unique_constraint(
        'uq_xero_accounts_tenant_account', 'xero_accounts', ['tenant_id', 'account_id']
    )
    op.create_index('ix_xero_accounts_entity', 'xero_accounts', ['entity_id'], unique=False)

    op.create_table(
        'xero_sync_runs',
        sa.Column('sync_run_id', sa.UUID(as_uuid=False), nullable=False),
        sa.Column('entity_id', sa.UUID(as_uuid=False), nullable=False),
        sa.Column('tenant_id', sa.String(), nullable=False),
        sa.Column('status', sa.String(), nullable=False),
        sa.Column('started_at', sa.DateTime(timezone=True), nullable=False),
        sa.Column('completed_at', sa.DateTime(timezone=True), nullable=True),
        sa.Column('accounts_seen_count', sa.Integer(), nullable=True),
        sa.Column('accounts_created_count', sa.Integer(), nullable=True),
        sa.Column('accounts_updated_count', sa.Integer(), nullable=True),
        sa.Column('error_code', sa.String(), nullable=True),
        sa.Column('error_detail', sa.String(), nullable=True),
        sa.PrimaryKeyConstraint('sync_run_id'),
    )
    op.create_index('ix_xero_sync_runs_entity', 'xero_sync_runs', ['entity_id'], unique=False)

    op.create_table(
        'xero_oauth_states',
        sa.Column('state', sa.String(), nullable=False),
        sa.Column('entity_id', sa.UUID(as_uuid=False), nullable=False),
        sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
        sa.Column('expires_at', sa.DateTime(timezone=True), nullable=False),
        sa.Column('consumed_at', sa.DateTime(timezone=True), nullable=True),
        sa.PrimaryKeyConstraint('state'),
    )


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_table('xero_oauth_states')
    op.drop_index('ix_xero_sync_runs_entity', table_name='xero_sync_runs')
    op.drop_table('xero_sync_runs')
    op.drop_index('ix_xero_accounts_entity', table_name='xero_accounts')
    op.drop_constraint('uq_xero_accounts_tenant_account', 'xero_accounts', type_='unique')
    op.drop_table('xero_accounts')
    op.drop_index('uq_xero_connections_tenant_id', table_name='xero_connections')
    op.drop_index('ix_xero_connections_status', table_name='xero_connections')
    op.drop_constraint('uq_xero_connections_entity_id', 'xero_connections', type_='unique')
    op.drop_table('xero_connections')
