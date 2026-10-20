"""SQLAlchemy table definitions for PID §118's mailbox-health incident
model and durable notification outbox (WO-BAGMAN-118-MAILBOX-HEALTH-
ALERTING §6.A.1). Mirrors ``persistence/postgres/needs_you_models.py``/
``persistence/postgres/evidence_classification_job_models.py``'s own
conventions exactly — one module, two tables, shares the same
declarative ``Base`` (imported from ``persistence.postgres.models``) so
both live in the same ``MetaData``/Alembic target.

* ``mailbox_health_incidents`` — ``ux_mailbox_health_incidents_open`` is
  a PARTIAL unique index on ``(mailbox_id, failure_class)``, unique only
  where ``status = 'OPEN'`` (PID §118.C) — the real, database-enforced
  guarantee against a duplicate open incident for the same mailbox+
  class, and the mechanical reason a resolved incident's own
  ``(mailbox_id, failure_class)`` pair is freed for a genuinely new
  ``incident_id`` on the next real failure (the episode mechanism).
* ``mailbox_health_notifications`` — ``uq_mailbox_health_notifications_incident_purpose``
  is a REAL (non-partial — ``purpose`` is always present) unique
  constraint on ``(incident_id, purpose)``: each purpose occurs at most
  once, ever, per incident (WO §6.A.1.A). ``claim_generation`` is the
  fencing token (PID §118.E.1) a reclaiming worker bumps and a
  completion write pins against — see
  ``persistence.postgres.mailbox_health_repository`` for the actual
  claim/fencing SQL.
"""
from __future__ import annotations

from datetime import datetime
from typing import Optional

from sqlalchemy import DateTime, ForeignKey, Index, Integer, String, UniqueConstraint, text
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from persistence.postgres.models import Base

#: Matches `persistence/postgres/models.py`'s own `_UUID` — a
#: native-UUID column typed to hand back plain Python `str`.
_UUID = UUID(as_uuid=False)


class MailboxHealthIncidentRow(Base):
    """Persisted form of
    `services.mailbox.health.incident.MailboxHealthIncident`."""

    __tablename__ = "mailbox_health_incidents"
    __table_args__ = (
        Index(
            "ux_mailbox_health_incidents_open",
            "mailbox_id",
            "failure_class",
            unique=True,
            postgresql_where=text("status = 'OPEN'"),
        ),
        Index("ix_mailbox_health_incidents_status", "status"),
    )

    incident_id: Mapped[str] = mapped_column(_UUID, primary_key=True)
    mailbox_id: Mapped[str] = mapped_column(_UUID, ForeignKey("mailbox_sources.mailbox_id"), nullable=False, index=True)
    failure_class: Mapped[str] = mapped_column(String, nullable=False)
    status: Mapped[str] = mapped_column(String, nullable=False)
    first_observed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    last_observed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    resolved_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    recovery_evidence: Mapped[Optional[dict]] = mapped_column(JSONB, nullable=True)
    metadata_: Mapped[dict] = mapped_column("metadata", JSONB, nullable=False, default=dict)


class MailboxHealthNotificationRow(Base):
    """Persisted form of
    `services.mailbox.health.notification.MailboxHealthNotification`."""

    __tablename__ = "mailbox_health_notifications"
    __table_args__ = (
        UniqueConstraint("incident_id", "purpose", name="uq_mailbox_health_notifications_incident_purpose"),
        Index("ix_mailbox_health_notifications_state", "state"),
        Index("ix_mailbox_health_notifications_claim_lookup", "state", "next_attempt_at"),
    )

    notification_id: Mapped[str] = mapped_column(_UUID, primary_key=True)
    incident_id: Mapped[str] = mapped_column(
        _UUID, ForeignKey("mailbox_health_incidents.incident_id"), nullable=False, index=True
    )
    purpose: Mapped[str] = mapped_column(String, nullable=False)
    state: Mapped[str] = mapped_column(String, nullable=False)
    attempt_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    max_attempts: Mapped[int] = mapped_column(Integer, nullable=False, default=3)
    next_attempt_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    claimed_by: Mapped[Optional[str]] = mapped_column(String, nullable=True)
    claimed_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    claim_generation: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    completed_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    provider_response_metadata: Mapped[Optional[dict]] = mapped_column(JSONB, nullable=True)
    cancellation_reason: Mapped[Optional[dict]] = mapped_column(JSONB, nullable=True)
