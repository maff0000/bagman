"""SQLAlchemy table definition for `EvidenceClassificationJob`
(BAGMAN accounting platform, `evidence/automatic-classification-activation`
WO). Kept as its own module — the same layering decision
`persistence/postgres/xero_account_suggestion_models.py`/
`persistence/postgres/background_job_models.py` document for their own
domains — rather than folded into `persistence/postgres/evidence_classification_models.py`
(which owns the unrelated `EvidenceClassification`/
`EvidenceClassificationRule` tables): this is a materially distinct
record type (a durable job-queue row, not a classification result)
with its own lifecycle. Shares the same declarative `Base` (imported
from `persistence.postgres.models`) so both modules' tables live in
one `MetaData`/Alembic target.

``uq_evidence_classification_jobs_evidence_id`` is a REAL, plain
(non-partial — `evidence_id` is always present) unique constraint: at
most one job may ever exist per evidence item (see
`services.evidence.classification_job`'s own module docstring for the
full idempotency doctrine) — mirrors
`XeroAccountAssignmentRow.uq_xero_account_assignments_evidence_id`'s
own "always-present column, plain unique constraint, no partial
qualifier needed" reasoning exactly. A real foreign key to
`evidence_items.evidence_id` backs the "an `EvidenceClassificationJob`
can only ever exist for a real `EvidenceItem`" invariant at the
database level too — the WO's own worker script
(`scripts/process_evidence_classification_jobs.py`) still handles a
`NotFoundError` on lookup defensively (structurally impossible given
this FK, but never assumed).
"""
from __future__ import annotations

from datetime import datetime
from typing import Optional

from sqlalchemy import DateTime, ForeignKey, Index, Integer, String, UniqueConstraint
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from persistence.postgres.models import Base

#: Matches `persistence/postgres/models.py`'s own `_UUID` — a
#: native-UUID column typed to hand back plain Python `str`.
_UUID = UUID(as_uuid=False)


class EvidenceClassificationJobRow(Base):
    """Persisted form of
    `services.evidence.classification_job.EvidenceClassificationJob`."""

    __tablename__ = "evidence_classification_jobs"
    __table_args__ = (
        UniqueConstraint("evidence_id", name="uq_evidence_classification_jobs_evidence_id"),
        Index("ix_evidence_classification_jobs_status", "status"),
    )

    job_id: Mapped[str] = mapped_column(_UUID, primary_key=True)
    evidence_id: Mapped[str] = mapped_column(_UUID, ForeignKey("evidence_items.evidence_id"), nullable=False)
    status: Mapped[str] = mapped_column(String, nullable=False)
    actor_type: Mapped[str] = mapped_column(String, nullable=False)
    actor_id: Mapped[str] = mapped_column(String, nullable=False)
    correlation_id: Mapped[str] = mapped_column(String, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    attempt_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    max_attempts: Mapped[int] = mapped_column(Integer, nullable=False, default=3)
    claimed_by: Mapped[Optional[str]] = mapped_column(String, nullable=True)
    claimed_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    last_error: Mapped[Optional[str]] = mapped_column(String, nullable=True)
    #: The exact `classify_evidence` outcome this job's one dispatch
    #: attempt produced — one of `services.evidence
    #: .classification_orchestrator.CLASSIFY_EVIDENCE_OUTCOMES`, never a
    #: retyped string (architect requirement, WO item 3; migration
    #: chained off `a7f34c9e2d18`). Nullable: `None` for every job that
    #: has never left `PENDING`/`CLAIMED`, and for a
    #: `FAILED_RETRYABLE`/`FAILED_TERMINAL` row produced by a genuinely
    #: RAISED infrastructure exception (which never reaches
    #: `classify_evidence`'s own outcome vocabulary at all).
    classification_outcome: Mapped[Optional[str]] = mapped_column(String, nullable=True)


class EvidenceClassificationReconciliationCursorRow(Base):
    """Persisted form of `services.evidence
    .classification_reconciliation_cursor.EvidenceClassificationReconciliationCursor`
    (preflight review correction, item B — closes the reconciliation
    starvation bug; see that module's own docstring for the full
    reasoning). Primary-keyed directly on `cursor_key` (the value's own
    identity IS the lookup key — mirrors
    `persistence/postgres/mailbox_microsoft_models.py
    ::MailboxFolderCursorRow`'s own equivalent reasoning), kept
    colocated with `EvidenceClassificationJobRow` above: both back the
    same `evidence/automatic-classification-activation` WO's
    reconciliation subsystem, and this table has no meaning independent
    of it."""

    __tablename__ = "evidence_classification_reconciliation_cursors"

    cursor_key: Mapped[str] = mapped_column(String, primary_key=True)
    last_created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    #: EVERY `evidence_id` actually inspected (never merely
    #: cursor-skipped) at exactly `last_created_at` — a plain JSONB
    #: list of strings, per this codebase's own "a plain list of short
    #: strings is JSONB" convention (mirrors
    #: `EvidenceClassificationRow.reason_codes`/
    #: `XeroAccountSuggestionRow.signals`). Post-merge audit correction:
    #: replaces the earlier single-scalar `last_evidence_id` column —
    #: see `services.evidence.classification_reconciliation_cursor`'s
    #: own docstring, "Why the tie-break is SET membership" section,
    #: for why an ordering-based scalar tie-break was wrong (UUIDv7
    #: monotonicity is only ever guaranteed within one process, never
    #: across two).
    last_evidence_ids: Mapped[list[str]] = mapped_column(JSONB, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
