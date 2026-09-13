"""SQLAlchemy table definition for BAGMAN's durable `IntakeRecord`
schema (CD-4 WI-1, PID §8).

Kept as its own module rather than added to `persistence/postgres/models.py`:
that module's own docstring enumerates "one table per CD-2 canonical
domain" as a closed, itemised list (`governed_entities`, `sources`,
`external_references`, `evidence_items`, `provenance`, `audit_events`)
scoped explicitly to CD-2 — `intake_records` is a CD-4 addition, not a
CD-2 domain, so it gets its own module rather than silently extending
that list. Shares the same declarative `Base` (imported from
`persistence.postgres.models`) so both modules' tables live in one
`MetaData`/Alembic target — there is still only one PostgreSQL schema.

Design notes (mirrors `persistence/postgres/models.py`'s own conventions)
--------------------------------------------------------------------------
* `intake_id` is a native PostgreSQL `UUID` (`as_uuid=False`), exactly
  like every other canonical `*_id` column elsewhere in this package.
* `source_id` IS a foreign key to `sources.source_id` — an intake
  attempt's source is a real, resolved reference (PID §9), unlike the
  hint fields below.
* `entity_hint` is deliberately a plain nullable string column, NOT a
  foreign key to `governed_entities` — same "hint, not ownership
  assertion" doctrine as `SourceRow.governed_entity_hint` (PID §9/§10).
* `evidence_id` IS a foreign key to `evidence_items.evidence_id`, but
  nullable — it is set only once `ACCEPTED` -> `REGISTERED` (PID §6);
  every other terminal state leaves it `NULL`.
* `content_hash` is nullable `JSONB` (unknown until WI-2's hashing
  runs), unlike `EvidenceItemRow.content_hash`, which is NOT NULL
  because an `EvidenceItem` is never created before its hash is known.
* `idempotency_key` is a nullable `String` with a PARTIAL unique index
  — unique only where non-null. PostgreSQL's plain `UniqueConstraint`
  already treats multiple `NULL`s as distinct (never colliding with
  each other), which is the exact semantics wanted for "a manual
  upload may not always supply a key" (PID §25) — but a `UniqueConstraint`
  cannot itself be conditioned on `IS NOT NULL`, so this uses a
  `postgresql_where`-qualified `Index` instead, exactly as PostgreSQL's
  own documented pattern for a partial unique index. This is the real,
  database-enforced backing for `services.evidence.intake.intake`'s
  idempotency-conflict doctrine (see that module's docstring) — the
  single source of truth for "has this key been used before", not
  application-level logic alone.
* `correlation_id` is NOT a foreign key — like `audit_events.correlation_id`,
  it identifies a workflow instance, not a canonical domain object.
"""
from __future__ import annotations

from datetime import datetime
from typing import Optional

from sqlalchemy import BigInteger, DateTime, ForeignKey, Index, String, text
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from persistence.postgres.models import Base

#: Matches `persistence/postgres/models.py`'s own `_UUID` — a
#: native-UUID column typed to hand back plain Python `str`.
_UUID = UUID(as_uuid=False)


class IntakeRecordRow(Base):
    """Persisted form of
    `services.evidence.intake.intake.IntakeRecord` (CD-4 WI-1)."""

    __tablename__ = "intake_records"
    __table_args__ = (
        Index(
            "uq_intake_records_idempotency_key",
            "idempotency_key",
            unique=True,
            postgresql_where=text("idempotency_key IS NOT NULL"),
        ),
        Index("ix_intake_records_correlation", "correlation_id"),
        Index("ix_intake_records_source", "source_id"),
    )

    intake_id: Mapped[str] = mapped_column(_UUID, primary_key=True)
    source_id: Mapped[str] = mapped_column(_UUID, ForeignKey("sources.source_id"), nullable=False)
    # HINT ONLY (PID §9/§10) — deliberately never a foreign key; see
    # the module docstring.
    entity_hint: Mapped[Optional[str]] = mapped_column(String, nullable=True)
    status: Mapped[str] = mapped_column(String, nullable=False)
    received_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    completed_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    original_filename: Mapped[Optional[str]] = mapped_column(String, nullable=True)
    reported_mime_type: Mapped[Optional[str]] = mapped_column(String, nullable=True)
    detected_mime_type: Mapped[Optional[str]] = mapped_column(String, nullable=True)
    size_bytes: Mapped[Optional[int]] = mapped_column(BigInteger, nullable=True)
    content_hash: Mapped[Optional[dict]] = mapped_column(JSONB, nullable=True)
    # Nullable: set only once ACCEPTED -> REGISTERED (PID §6); this FK
    # must never become NOT NULL.
    evidence_id: Mapped[Optional[str]] = mapped_column(
        _UUID, ForeignKey("evidence_items.evidence_id"), nullable=True
    )
    failure_code: Mapped[Optional[str]] = mapped_column(String, nullable=True)
    quarantine_reason: Mapped[Optional[str]] = mapped_column(String, nullable=True)
    correlation_id: Mapped[str] = mapped_column(_UUID, nullable=False)
    idempotency_key: Mapped[Optional[str]] = mapped_column(String, nullable=True)
    metadata_: Mapped[dict] = mapped_column("metadata", JSONB, nullable=False, default=dict)
