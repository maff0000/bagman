"""mailbox_health_incidents and mailbox_health_notifications (PID §118,
WO-BAGMAN-118-MAILBOX-HEALTH-ALERTING §6.A.1)

Revision ID: e2736f7219d6
Revises: c3f8e51a9d26
Create Date: 2026-10-20 00:00:00.000000

Both new tables in one migration, per the governing Work Order's own
instruction (§6.A item 1). No existing table is altered.

Structural invariants this migration is independently audited against
(WO §12 acceptance criteria 2/13):

* ``ux_mailbox_health_incidents_open`` — a PARTIAL unique index on
  ``(mailbox_id, failure_class)`` scoped to ``status = 'OPEN'`` — the
  real, PostgreSQL-enforced guarantee against duplicate open incidents
  (PID §118.C). Mirrors ``uq_needs_you_items_type_source_ref``'s own
  ``postgresql_where``-qualified partial-unique-index technique.
* ``uq_mailbox_health_notifications_incident_purpose`` — a REAL (non-
  partial — ``purpose`` is always present) unique constraint on
  ``(incident_id, purpose)``: each purpose (``INITIAL``/``REMINDER_24H``/
  ``RECOVERY``) occurs at most once, ever, per incident (WO §6.A.1.A —
  exactly one reminder total per incident, never a recurring nag).
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision: str = 'e2736f7219d6'
down_revision: Union[str, Sequence[str], None] = 'c3f8e51a9d26'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    op.create_table(
        'mailbox_health_incidents',
        sa.Column('incident_id', sa.UUID(as_uuid=False), nullable=False),
        sa.Column('mailbox_id', sa.UUID(as_uuid=False), nullable=False),
        sa.Column('failure_class', sa.String(), nullable=False),
        sa.Column('status', sa.String(), nullable=False),
        sa.Column('first_observed_at', sa.DateTime(timezone=True), nullable=False),
        sa.Column('last_observed_at', sa.DateTime(timezone=True), nullable=False),
        sa.Column('resolved_at', sa.DateTime(timezone=True), nullable=True),
        sa.Column('recovery_evidence', postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column('metadata', postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.ForeignKeyConstraint(('mailbox_id',), ('mailbox_sources.mailbox_id',)),
        sa.PrimaryKeyConstraint('incident_id'),
    )
    op.create_index(
        'ix_mailbox_health_incidents_mailbox_id', 'mailbox_health_incidents', ['mailbox_id'], unique=False
    )
    op.create_index(
        'ix_mailbox_health_incidents_status', 'mailbox_health_incidents', ['status'], unique=False
    )
    # Partial unique index (PID §118.C) — unique only where status =
    # 'OPEN'; a resolved incident's own (mailbox_id, failure_class) pair
    # is thereby freed, and the database's own conflict target (never
    # application-level locking) is what makes two simultaneous
    # evaluators racing for the same new failure resolve to exactly one
    # row.
    op.create_index(
        'ux_mailbox_health_incidents_open',
        'mailbox_health_incidents',
        ['mailbox_id', 'failure_class'],
        unique=True,
        postgresql_where=sa.text("status = 'OPEN'"),
    )

    op.create_table(
        'mailbox_health_notifications',
        sa.Column('notification_id', sa.UUID(as_uuid=False), nullable=False),
        sa.Column('incident_id', sa.UUID(as_uuid=False), nullable=False),
        sa.Column('purpose', sa.String(), nullable=False),
        sa.Column('state', sa.String(), nullable=False),
        sa.Column('attempt_count', sa.Integer(), nullable=False, server_default='0'),
        sa.Column('max_attempts', sa.Integer(), nullable=False, server_default='3'),
        sa.Column('next_attempt_at', sa.DateTime(timezone=True), nullable=False),
        sa.Column('claimed_by', sa.String(), nullable=True),
        sa.Column('claimed_at', sa.DateTime(timezone=True), nullable=True),
        # Fencing token (PID §118.E.1) — incremented on every successful
        # claim; the completion-write's own WHERE clause pins against
        # this, never wall-clock timing alone.
        sa.Column('claim_generation', sa.Integer(), nullable=False, server_default='0'),
        sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
        sa.Column('completed_at', sa.DateTime(timezone=True), nullable=True),
        sa.Column('provider_response_metadata', postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        # WO §6.A.1.C mandatory correction — pre-dispatch suppression's
        # own durable record of WHY a notification never reached
        # Pushover.
        sa.Column('cancellation_reason', postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.ForeignKeyConstraint(('incident_id',), ('mailbox_health_incidents.incident_id',)),
        sa.PrimaryKeyConstraint('notification_id'),
    )
    op.create_index(
        'ix_mailbox_health_notifications_incident_id', 'mailbox_health_notifications', ['incident_id'], unique=False
    )
    op.create_index(
        'ix_mailbox_health_notifications_state', 'mailbox_health_notifications', ['state'], unique=False
    )
    # Claim predicate index — supports the due-PENDING/RETRYABLE_FAILURE
    # and expired-IN_PROGRESS claim queries (WO §6.A.1.B) without a full
    # table scan.
    op.create_index(
        'ix_mailbox_health_notifications_claim_lookup',
        'mailbox_health_notifications',
        ['state', 'next_attempt_at'],
        unique=False,
    )
    # Real (non-partial — purpose is always present) unique constraint
    # (WO §6.A.1.A): each purpose occurs at most once, ever, per
    # incident.
    op.create_unique_constraint(
        'uq_mailbox_health_notifications_incident_purpose',
        'mailbox_health_notifications',
        ['incident_id', 'purpose'],
    )


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_constraint(
        'uq_mailbox_health_notifications_incident_purpose', 'mailbox_health_notifications', type_='unique'
    )
    op.drop_index('ix_mailbox_health_notifications_claim_lookup', table_name='mailbox_health_notifications')
    op.drop_index('ix_mailbox_health_notifications_state', table_name='mailbox_health_notifications')
    op.drop_index('ix_mailbox_health_notifications_incident_id', table_name='mailbox_health_notifications')
    op.drop_table('mailbox_health_notifications')

    op.drop_index('ux_mailbox_health_incidents_open', table_name='mailbox_health_incidents')
    op.drop_index('ix_mailbox_health_incidents_status', table_name='mailbox_health_incidents')
    op.drop_index('ix_mailbox_health_incidents_mailbox_id', table_name='mailbox_health_incidents')
    op.drop_table('mailbox_health_incidents')
