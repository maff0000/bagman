"""SQLAlchemy table definition for BAGMAN's durable
``evidence_classification_rules`` schema (CD-6 Slice 5 WI-1).

See ``services.evidence.classification_rule``'s own module docstring
for the full domain doctrine this table backs. Kept as its own module,
mirroring ``persistence/postgres/mailbox_domain_rule_models.py``'s own
"a new bounded-component table gets its own module" precedent.

Active-rule identity — the real, database-enforced backstop
------------------------------------------------------------------------
``uq_evidence_classification_rules_active_identity`` is a PARTIAL unique
index on ``(sender_scope_type, sender_scope_value,
subject_predicate_type, subject_predicate_value)`` scoped ``WHERE status
= 'ACTIVE'`` — at most one ACTIVE rule may exist at a given identity;
any number of RETIRED rows may share the same identity over time (each
representing one governed decision that was later corrected), exactly
the same "partial index scoped to the live/active subset" doctrine
``uq_ai_invocations_active_subject`` and
``uq_mailbox_domain_rules_mailbox_domain_scope`` both already establish
for their own tables. Values are stored already-normalised (see
``services.evidence.classification_rule.normalize_sender_scope_value``/
``services.mailbox.domain_rule.normalize_subject_for_policy``), so the
index compares like-for-like.

``ix_evidence_classification_rules_sender_scope`` is an ordinary
(non-unique) lookup index on ``(sender_scope_type, sender_scope_value)``
— the shape a future matcher (WI-2) will filter by first, before
narrowing on subject predicates.
"""
from __future__ import annotations

from datetime import datetime
from typing import Optional

import sqlalchemy as sa
from sqlalchemy import DateTime, ForeignKey, Index, String
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from persistence.postgres.models import Base

_UUID = UUID(as_uuid=False)


class EvidenceClassificationRuleRow(Base):
    """Persisted form of
    ``services.evidence.classification_rule.EvidenceClassificationRule``.
    Semantic fields are set once at creation and never mutated; only
    ``status``/``retired_at`` are ever updated in place, by
    ``persistence.postgres.evidence_classification_rule_repository
    .PostgresEvidenceClassificationRuleRepository.retire_rule``."""

    __tablename__ = "evidence_classification_rules"
    __table_args__ = (
        Index(
            "uq_evidence_classification_rules_active_identity",
            "sender_scope_type", "sender_scope_value", "subject_predicate_type", "subject_predicate_value",
            unique=True,
            postgresql_where=sa.text("status = 'ACTIVE'"),
        ),
        Index(
            "ix_evidence_classification_rules_sender_scope",
            "sender_scope_type", "sender_scope_value",
        ),
    )

    rule_id: Mapped[str] = mapped_column(_UUID, primary_key=True)
    sender_scope_type: Mapped[str] = mapped_column(String, nullable=False)
    sender_scope_value: Mapped[str] = mapped_column(String, nullable=False)
    subject_predicate_type: Mapped[str] = mapped_column(String, nullable=False)
    subject_predicate_value: Mapped[str] = mapped_column(String, nullable=False)
    document_type: Mapped[str] = mapped_column(String, nullable=False)
    status: Mapped[str] = mapped_column(String, nullable=False)
    source: Mapped[str] = mapped_column(String, nullable=False)
    # Self-referencing — a documentation/lineage pointer, not itself a
    # uniqueness-governed relationship the way
    # evidence_classifications.supersedes_classification_id is (no
    # branch-prevention requirement was specified for rule supersession
    # in WI-1 — a rule's replacement is always a fresh create_rule call
    # after retire_rule, never itself concurrency-guarded the way
    # classification creation is).
    supersedes_rule_id: Mapped[Optional[str]] = mapped_column(
        _UUID, ForeignKey("evidence_classification_rules.rule_id"), nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    approved_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    retired_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    # No schema_version column — see EvidenceClassificationRow's own
    # docstring for the identical reasoning.
