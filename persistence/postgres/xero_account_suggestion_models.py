"""SQLAlchemy table definitions for the Xero Account Suggestion Producer
(BAGMAN accounting platform, `xero/account-suggestion-producer` WO).
Kept as its own module — the same layering decision
`persistence/postgres/evidence_classification_models.py`/
`persistence/postgres/needs_you_models.py` document for their own
domains — rather than folded into `persistence/postgres/xero_models.py`
(CD-6 Slice 2's own four tables): this is a materially distinct WO
adding two NEW governed record types with their own lifecycle, not an
extension of Slice 2's existing connection/account/sync-run schema.
Shares the same declarative `Base` (imported from
`persistence.postgres.models`) so both modules' tables live in one
`MetaData`/Alembic target.

Two tables:

* ``xero_account_suggestions`` — the AI proposal ledger
  (`services.xero.account_suggestion.XeroAccountSuggestion`). NO
  uniqueness constraint on `evidence_id` beyond the primary key —
  deliberate (see that module's own docstring: "one non-superseded
  suggestion attempt per fingerprint, enforced via the idempotency
  check in the orchestrator, not a DB constraint beyond a real PK").
  `ix_xero_account_suggestions_evidence` backs
  `XeroAccountSuggestionRepository.get_by_evidence`'s query.
* ``xero_account_assignments`` — the authoritative, write-once coding
  decision (`services.xero.account_assignment.XeroAccountAssignment`).
  `uq_xero_account_assignments_evidence_id` is a REAL, plain (non-
  partial — `evidence_id` is always present) unique constraint: exactly
  one assignment may ever exist per evidence item, the real
  database-level backstop for that module's own write-once/`ConflictError`
  discipline (mirrors `XeroConnectionRow.uq_xero_connections_entity_id`'s
  own "always-present column, plain unique constraint, no partial
  qualifier needed" reasoning).
"""
from __future__ import annotations

from datetime import datetime
from typing import Optional

from sqlalchemy import DateTime, Float, Index, String, UniqueConstraint
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from persistence.postgres.models import Base

#: Matches `persistence/postgres/models.py`'s own `_UUID` — a
#: native-UUID column typed to hand back plain Python `str`.
_UUID = UUID(as_uuid=False)


class XeroAccountSuggestionRow(Base):
    """Persisted form of
    `services.xero.account_suggestion.XeroAccountSuggestion`."""

    __tablename__ = "xero_account_suggestions"
    __table_args__ = (
        Index("ix_xero_account_suggestions_evidence", "evidence_id"),
        Index("ix_xero_account_suggestions_entity", "entity_id"),
        Index("ix_xero_account_suggestions_ai_invocation", "ai_invocation_id"),
    )

    suggestion_id: Mapped[str] = mapped_column(_UUID, primary_key=True)
    evidence_id: Mapped[str] = mapped_column(_UUID, nullable=False)
    entity_id: Mapped[str] = mapped_column(_UUID, nullable=False)
    tenant_id: Mapped[str] = mapped_column(String, nullable=False)
    suggested_account_id: Mapped[str] = mapped_column(String, nullable=False)
    confidence: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    signals: Mapped[list] = mapped_column(JSONB, nullable=False, default=list)
    ai_invocation_id: Mapped[str] = mapped_column(_UUID, nullable=False)
    status: Mapped[str] = mapped_column(String, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class XeroAccountAssignmentRow(Base):
    """Persisted form of
    `services.xero.account_assignment.XeroAccountAssignment`."""

    __tablename__ = "xero_account_assignments"
    __table_args__ = (
        UniqueConstraint("evidence_id", name="uq_xero_account_assignments_evidence_id"),
        Index("ix_xero_account_assignments_entity", "entity_id"),
    )

    assignment_id: Mapped[str] = mapped_column(_UUID, primary_key=True)
    evidence_id: Mapped[str] = mapped_column(_UUID, nullable=False)
    entity_id: Mapped[str] = mapped_column(_UUID, nullable=False)
    tenant_id: Mapped[str] = mapped_column(String, nullable=False)
    account_id: Mapped[str] = mapped_column(String, nullable=False)
    source: Mapped[str] = mapped_column(String, nullable=False)
    suggestion_id: Mapped[Optional[str]] = mapped_column(_UUID, nullable=True)
    assigned_by_actor_type: Mapped[str] = mapped_column(String, nullable=False)
    assigned_by_actor_id: Mapped[str] = mapped_column(String, nullable=False)
    assigned_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
