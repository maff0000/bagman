"""SQLAlchemy table definitions for BAGMAN's durable PostgreSQL schema
(CD-3 WI-1, PID §10-11).

One table per CD-2 canonical domain, mirroring each domain's
``contracts/<domain>/bagman.<domain>.v1.schema.json`` fields exactly
(the PID's own suggested table names — PID §10 explicitly allows
different names if justified; these are used as-is with no
justification needed to depart from them):

* ``governed_entities``   <- ``core.entity.GovernedEntity``
* ``sources``              <- ``core.source.Source``
* ``external_references``  <- ``core.external_reference.ExternalReference``
* ``evidence_items``       <- ``services.evidence.evidence.EvidenceItem``
* ``provenance``           <- ``core.provenance.Provenance``
* ``audit_events``         <- ``core.audit.AuditEvent``

This module defines ONLY table shape (SQLAlchemy ORM classes) — no
domain behaviour, no validation, no canonical error handling. Those
concerns belong to ``persistence/postgres/*_repository.py`` and,
beneath them, ``core``/``services.evidence`` (PID §53: canonical
domain models must never depend on PostgreSQL; the reverse is fine and
is exactly what this module is).

Design notes
------------
* Every canonical ``*_id`` column is a native PostgreSQL ``UUID``
  (``as_uuid=False`` so SQLAlchemy hands back plain Python ``str``,
  matching every canonical domain model's own ``*_id: str`` type) —
  real uniqueness/foreign-key/index benefits from the native type,
  while callers at the repository boundary never need to know the
  storage representation.
* Every timestamp column is ``DateTime(timezone=True)`` (PostgreSQL
  ``TIMESTAMP WITH TIME ZONE``) — never naive (PID §28).
* Free-form/metadata fields are ``JSONB``.
* The Python attribute for the free-form ``metadata`` field is named
  ``metadata_`` (mapped to the actual column name ``"metadata"``)
  because ``metadata`` is reserved on every SQLAlchemy declarative
  model (``Base.metadata`` is the schema's ``MetaData`` object).
* ``sources.governed_entity_hint`` is deliberately a plain nullable
  string column, NOT a foreign key to ``governed_entities`` — PID §9
  is explicit that a source's entity hint is a hint, never an
  ownership assertion; a hard FK here would silently upgrade it into
  one.
* ``evidence_items.entity_id`` IS a foreign key to
  ``governed_entities.entity_id`` (unlike the hint above, this field
  really is a resolved entity association once set) but is nullable —
  unresolved entity ownership is legitimate and first-class (PID
  §4/§37); it must never be NOT NULL.
* ``external_references`` carries the real unique constraint on
  (``provider``, ``source_id``, ``resource_type``, ``external_id``) —
  the actual persisted form of the CD-2 idempotency guarantee (PID
  §23). This is enforced by the database itself, not application logic
  alone.
* ``provenance.subject_id`` / ``audit_events.subject_id`` are NOT
  foreign keys: ``subject_type`` is open/polymorphic (any canonical
  BAGMAN object type, e.g. ``EvidenceItem``, ``Classification``), so
  there is no single table a plain FK could target.
  ``external_references.canonical_object_id`` is polymorphic for the
  same reason.
* No update/delete path exists anywhere in this module for
  ``audit_events`` — it is enforced entirely by
  ``persistence.postgres.audit_repository.PostgresAuditRepository``
  simply never defining an update/delete method (PID §21); no
  database-level ``REVOKE``/role management is introduced here (out of
  scope for this work item).
"""
from __future__ import annotations

from datetime import datetime
from typing import Optional

from sqlalchemy import BigInteger, DateTime, ForeignKey, Index, String, UniqueConstraint
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

#: A native-UUID column typed to hand back plain Python ``str`` at the
#: ORM boundary, matching every canonical domain model's ``*_id: str``.
_UUID = UUID(as_uuid=False)


class Base(DeclarativeBase):
    """Shared declarative base for every BAGMAN PostgreSQL-backed
    table. Lives only under ``persistence/postgres/`` — never imported
    by ``core/`` or ``services/`` (PID §53)."""


class GovernedEntityRow(Base):
    """Persisted form of ``core.entity.GovernedEntity``."""

    __tablename__ = "governed_entities"

    entity_id: Mapped[str] = mapped_column(_UUID, primary_key=True)
    entity_type: Mapped[str] = mapped_column(String, nullable=False)
    canonical_name: Mapped[str] = mapped_column(String, nullable=False)
    display_name: Mapped[str] = mapped_column(String, nullable=False)
    status: Mapped[str] = mapped_column(String, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    # CD-6 architect amendment (email historical-ingestion boundary) —
    # additive, nullable columns; existing rows get NULL (a real,
    # honest "not yet configured" state — see core.entity.GovernedEntity's
    # own docstring), never a silently-invented default.
    fiscal_year_start_month_day: Mapped[Optional[str]] = mapped_column(String, nullable=True)
    # Renamed from `email_bootstrap_floor_at` (CD-6 architect amendment,
    # second correction) — this is an OPTIONAL CLAMP/OVERRIDE only, never
    # itself the historical-bootstrap answer; see
    # `core.entity.GovernedEntity.historical_floor_override_at`'s own
    # docstring for the full doctrine this rename encodes.
    historical_floor_override_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    metadata_: Mapped[dict] = mapped_column("metadata", JSONB, nullable=False, default=dict)


class SourceRow(Base):
    """Persisted form of ``core.source.Source``."""

    __tablename__ = "sources"

    source_id: Mapped[str] = mapped_column(_UUID, primary_key=True)
    source_type: Mapped[str] = mapped_column(String, nullable=False)
    provider: Mapped[str] = mapped_column(String, nullable=False)
    status: Mapped[str] = mapped_column(String, nullable=False)
    external_source_ref: Mapped[Optional[str]] = mapped_column(String, nullable=True)
    # HINT ONLY (PID §9) — deliberately never a foreign key; see the
    # module docstring.
    governed_entity_hint: Mapped[Optional[str]] = mapped_column(String, nullable=True)
    metadata_: Mapped[dict] = mapped_column("metadata", JSONB, nullable=False, default=dict)


class ExternalReferenceRow(Base):
    """Persisted form of ``core.external_reference.ExternalReference``.

    ``uq_external_references_tuple`` is the actual database-level
    enforcement of the CD-2 idempotency composite-uniqueness guarantee
    (PID §10/§23/§34) — the source of truth, not merely an
    application-level check.
    """

    __tablename__ = "external_references"
    __table_args__ = (
        UniqueConstraint(
            "provider",
            "source_id",
            "resource_type",
            "external_id",
            name="uq_external_references_tuple",
        ),
    )

    external_reference_id: Mapped[str] = mapped_column(_UUID, primary_key=True)
    provider: Mapped[str] = mapped_column(String, nullable=False)
    resource_type: Mapped[str] = mapped_column(String, nullable=False)
    external_id: Mapped[str] = mapped_column(String, nullable=False)
    canonical_object_type: Mapped[str] = mapped_column(String, nullable=False)
    # Polymorphic target — not a foreign key (see module docstring).
    canonical_object_id: Mapped[str] = mapped_column(_UUID, nullable=False)
    source_id: Mapped[str] = mapped_column(
        _UUID, ForeignKey("sources.source_id"), nullable=False
    )
    first_observed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    metadata_: Mapped[dict] = mapped_column("metadata", JSONB, nullable=False, default=dict)


class EvidenceItemRow(Base):
    """Persisted form of ``services.evidence.evidence.EvidenceItem``."""

    __tablename__ = "evidence_items"

    evidence_id: Mapped[str] = mapped_column(_UUID, primary_key=True)
    # Nullable: unresolved entity ownership is legitimate and
    # first-class (PID §4/§37) — this FK must never become NOT NULL.
    entity_id: Mapped[Optional[str]] = mapped_column(
        _UUID, ForeignKey("governed_entities.entity_id"), nullable=True
    )
    evidence_type: Mapped[str] = mapped_column(String, nullable=False)
    source_id: Mapped[str] = mapped_column(
        _UUID, ForeignKey("sources.source_id"), nullable=False
    )
    observed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    received_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    content_hash: Mapped[dict] = mapped_column(JSONB, nullable=False)
    mime_type: Mapped[str] = mapped_column(String, nullable=False)
    size_bytes: Mapped[int] = mapped_column(BigInteger, nullable=False)
    status: Mapped[str] = mapped_column(String, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    original_name: Mapped[Optional[str]] = mapped_column(String, nullable=True)
    storage_reference: Mapped[Optional[str]] = mapped_column(String, nullable=True)
    metadata_: Mapped[dict] = mapped_column("metadata", JSONB, nullable=False, default=dict)


class ProvenanceRow(Base):
    """Persisted form of ``core.provenance.Provenance``.

    Append-only in practice: ``PostgresProvenanceRepository`` (like its
    in-memory counterpart) exposes only ``record_provenance`` plus
    reads — no update/delete method exists.
    """

    __tablename__ = "provenance"
    __table_args__ = (Index("ix_provenance_subject", "subject_type", "subject_id"),)

    provenance_id: Mapped[str] = mapped_column(_UUID, primary_key=True)
    subject_type: Mapped[str] = mapped_column(String, nullable=False)
    # Polymorphic target — not a foreign key (see module docstring).
    subject_id: Mapped[str] = mapped_column(_UUID, nullable=False)
    evidence_id: Mapped[str] = mapped_column(
        _UUID, ForeignKey("evidence_items.evidence_id"), nullable=False
    )
    relationship: Mapped[str] = mapped_column(String, nullable=False)
    transform_id: Mapped[Optional[str]] = mapped_column(_UUID, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    metadata_: Mapped[dict] = mapped_column("metadata", JSONB, nullable=False, default=dict)


class AuditEventRow(Base):
    """Persisted form of ``core.audit.AuditEvent``.

    Append-only BY DESIGN (PID §21): this class/module defines no
    update or delete path whatsoever — only insert (via the repository's
    ``record_audit_event``) and select (``get_audit_event``,
    ``list_by_correlation``, ``list_by_subject``) exist anywhere in
    ``persistence.postgres.audit_repository``. No DB-level
    ``REVOKE``/permission management is introduced here — out of scope
    for this work item; a repository class with no update/delete
    method is the enforcement PID §21 asks for.
    """

    __tablename__ = "audit_events"
    __table_args__ = (
        Index("ix_audit_events_correlation", "correlation_id"),
        Index("ix_audit_events_subject", "subject_type", "subject_id"),
    )

    audit_event_id: Mapped[str] = mapped_column(_UUID, primary_key=True)
    event_type: Mapped[str] = mapped_column(String, nullable=False)
    occurred_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    actor_type: Mapped[str] = mapped_column(String, nullable=False)
    actor_id: Mapped[str] = mapped_column(String, nullable=False)
    subject_type: Mapped[str] = mapped_column(String, nullable=False)
    # Polymorphic target — not a foreign key (see module docstring).
    subject_id: Mapped[str] = mapped_column(_UUID, nullable=False)
    correlation_id: Mapped[str] = mapped_column(_UUID, nullable=False)
    causation_id: Mapped[Optional[str]] = mapped_column(_UUID, nullable=True)
    payload: Mapped[dict] = mapped_column(JSONB, nullable=False, default=dict)
    schema_version: Mapped[str] = mapped_column(String, nullable=False)
