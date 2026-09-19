"""SQLAlchemy table definition for BAGMAN's durable `NeedsYouItem`
schema (CD-6 Slice 1, PID §98.5).

Kept as its own module, exactly the same layering decision
``persistence/postgres/intake_models.py`` documents for
``intake_records``: ``needs_you_items`` is not a CD-2 canonical domain,
so it does not belong in ``persistence/postgres/models.py``'s own
closed CD-2 list. Shares the same declarative ``Base`` (imported from
``persistence.postgres.models``) so both modules' tables live in one
``MetaData``/Alembic target — there is still only one PostgreSQL
schema.

Design notes (mirrors ``persistence/postgres/intake_models.py``'s own
conventions)
--------------------------------------------------------------------------
* ``item_id`` is a native PostgreSQL ``UUID`` (``as_uuid=False``),
  exactly like every other canonical ``*_id`` column elsewhere in this
  package.
* ``source_object_reference`` is deliberately a plain nullable
  ``UUID`` column, NOT a foreign key to any specific table —
  ``NeedsYouItem`` is intentionally cross-domain/polymorphic (an
  ``evidence_id`` today; a future ``EmailMessage``/``ProcessingRule``
  id once those domains exist), exactly the same "polymorphic target,
  not a foreign key" doctrine ``persistence/postgres/models.py`` already
  documents for ``provenance.subject_id``/``audit_events.subject_id``.
* ``uq_needs_you_items_type_source_ref`` is a PARTIAL unique index on
  ``(item_type, source_object_reference)`` — unique only where
  ``source_object_reference IS NOT NULL`` — the real, database-level
  enforcement of this delivery's idempotent-creation guarantee (see
  ``services.needs_you.needs_you``'s module docstring, "Idempotency
  design decision"): a replayed producer call for the same
  ``(item_type, source_object_reference)`` pair must resolve to the
  SAME row, never create a second one. Exactly the same
  ``postgresql_where``-qualified partial-unique-index technique
  ``IntakeRecordRow.uq_intake_records_idempotency_key`` already uses,
  for the identical reason (plain ``UniqueConstraint`` cannot itself be
  conditioned on ``IS NOT NULL``).
* ``resolution`` is nullable ``JSONB`` (unknown until the item is
  resolved/dismissed) — set together with ``resolved_at``/
  ``resolved_by_actor_type``/``resolved_by_actor_id`` by
  ``PostgresNeedsYouRepository.resolve_needs_you_item``.
* ``correlation_id`` is NOT a foreign key — like
  ``audit_events.correlation_id``/``intake_records.correlation_id``, it
  identifies a workflow instance, not a canonical domain object.
"""
from __future__ import annotations

from datetime import datetime
from typing import Optional

from sqlalchemy import DateTime, Index, String, text
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from persistence.postgres.models import Base

#: Matches `persistence/postgres/models.py`'s own `_UUID` — a
#: native-UUID column typed to hand back plain Python `str`.
_UUID = UUID(as_uuid=False)


class NeedsYouItemRow(Base):
    """Persisted form of
    `services.needs_you.needs_you.NeedsYouItem` (CD-6 Slice 1)."""

    __tablename__ = "needs_you_items"
    __table_args__ = (
        Index(
            "uq_needs_you_items_type_source_ref",
            "item_type",
            "source_object_reference",
            unique=True,
            postgresql_where=text("source_object_reference IS NOT NULL"),
        ),
        Index("ix_needs_you_items_status", "status"),
        Index("ix_needs_you_items_correlation", "correlation_id"),
    )

    item_id: Mapped[str] = mapped_column(_UUID, primary_key=True)
    item_type: Mapped[str] = mapped_column(String, nullable=False)
    domain: Mapped[str] = mapped_column(String, nullable=False)
    # Polymorphic target — deliberately not a foreign key; see module
    # docstring.
    source_object_reference: Mapped[Optional[str]] = mapped_column(_UUID, nullable=True)
    question: Mapped[str] = mapped_column(String, nullable=False)
    allowed_action_type: Mapped[str] = mapped_column(String, nullable=False)
    priority: Mapped[str] = mapped_column(String, nullable=False)
    status: Mapped[str] = mapped_column(String, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    resolved_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    resolved_by_actor_type: Mapped[Optional[str]] = mapped_column(String, nullable=True)
    resolved_by_actor_id: Mapped[Optional[str]] = mapped_column(String, nullable=True)
    resolution: Mapped[Optional[dict]] = mapped_column(JSONB, nullable=True)
    correlation_id: Mapped[str] = mapped_column(_UUID, nullable=False)
    metadata_: Mapped[dict] = mapped_column("metadata", JSONB, nullable=False, default=dict)
