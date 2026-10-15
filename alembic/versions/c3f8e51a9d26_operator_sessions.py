"""operator_sessions (WO-BAGMAN-117 §6.A.7, PID §117.C)

Revision ID: c3f8e51a9d26
Revises: ae936a444eae
Create Date: 2026-10-14 00:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

# revision identifiers, used by Alembic.
revision: str = 'c3f8e51a9d26'
down_revision: Union[str, Sequence[str], None] = 'ae936a444eae'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    # token_digest (SHA-256 hex digest, 64 chars) is the primary key —
    # the raw session token itself is NEVER a column on this table
    # (PID §117.C). No existing table is altered by this migration.
    op.create_table(
        'operator_sessions',
        sa.Column('token_digest', sa.String(length=64), nullable=False),
        sa.Column('csrf_token', sa.String(), nullable=False),
        sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
        sa.Column('expires_at', sa.DateTime(timezone=True), nullable=False),
        sa.Column('revoked_at', sa.DateTime(timezone=True), nullable=True),
        sa.PrimaryKeyConstraint('token_digest'),
    )
    # Partial index supporting revoke_all_sessions' bulk "every
    # currently-active session" update (PID §117.F password-reset
    # revocation) — mirrors f7c3a58b1e9d's own partial-index precedent.
    op.create_index(
        'ix_operator_sessions_active',
        'operator_sessions',
        ['revoked_at'],
        unique=False,
        postgresql_where=sa.text('revoked_at IS NULL'),
    )


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_index('ix_operator_sessions_active', table_name='operator_sessions')
    op.drop_table('operator_sessions')
