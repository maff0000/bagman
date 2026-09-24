"""SQLAlchemy table definition for BAGMAN's durable
``evidence_classifications`` schema (CD-6 Slice 5 WI-1).

Kept as its own module, exactly the precedent
``persistence/postgres/ai_invocation_models.py`` and
``persistence/postgres/mailbox_domain_rule_models.py`` both set: a new
bounded-component table gets its own module rather than being folded
into ``persistence/postgres/models.py`` (that module's own docstring
scopes it to the six original CD-2 canonical domain tables). Shares the
same declarative ``Base`` so every module's tables still live in one
``MetaData``/Alembic target.

``evidence_items`` is untouched — no column, no schema change of any
kind (the WO's own explicit architectural invariant). This table only
carries a real, indexed foreign key TO ``evidence_items.evidence_id``,
never the reverse.

Four index groups (see ``services.evidence.classification``'s own
module docstring for the full doctrine each one backs)
------------------------------------------------------------------------
1. **Branch prevention** — ``uq_evidence_classifications_supersedes`` —
   a PARTIAL unique index on ``supersedes_classification_id`` (``WHERE
   supersedes_classification_id IS NOT NULL``): at most one row may ever
   supersede a given ``classification_id``, so a lineage for one
   ``(evidence_id, classification_type)`` is always a single linear
   chain, never a tree. This is the real, database-enforced backstop
   behind the application-level "expected current classification"
   check in ``persistence.postgres.evidence_classification_repository
   .PostgresEvidenceClassificationRepository.create_classification``.
2. **Producer idempotency** — THREE separate partial unique indexes,
   one per ``source`` (mirrors
   ``persistence/postgres/mailbox_domain_rule_models.py``'s own
   "three partial unique indexes, not one plain UniqueConstraint"
   doctrine — a producer identity's SHAPE differs by source, so a
   single constraint spanning all three optional reference columns
   could never express "unique per source's own real reference column"
   correctly; NULL-is-distinct semantics would silently defeat it for
   every row whose OTHER two reference columns are null):

   * ``uq_evidence_classifications_rule_producer`` on
     ``(evidence_id, classification_type, rule_id)`` ``WHERE source =
     'DETERMINISTIC_RULE'``.
   * ``uq_evidence_classifications_ai_producer`` on
     ``(evidence_id, classification_type, ai_invocation_id)`` ``WHERE
     source = 'AI_PROPOSAL'``.
   * ``uq_evidence_classifications_operator_producer`` on
     ``(evidence_id, classification_type, operator_action_id)`` ``WHERE
     source = 'OPERATOR_ASSIGNED'``.
3. **Ordinary lookup index** —
   ``ix_evidence_classifications_evidence_type_created`` on
   ``(evidence_id, classification_type, created_at)`` — the shape every
   "resolve the current/history for this evidence item's document_type
   classification" query filters and orders by.

Foreign keys — real, not hints
------------------------------------------------------------------------
``evidence_id`` (not null), ``rule_id`` (nullable), ``ai_invocation_id``
(nullable), and ``supersedes_classification_id`` (nullable,
self-referencing) are all REAL foreign keys, not mere hints — unlike
``MailboxDomainRuleRow.destination_entity_id`` (deliberately never a FK
— "hint/approved routing, never a hard ownership constraint") or
``sources.governed_entity_hint``, these ARE hard identity references
once populated: a classification genuinely cannot exist without the
evidence/rule/invocation row it names actually existing, and a real FK
is the correct, architecturally-appropriate enforcement (``rule_id`` ->
``evidence_classification_rules.rule_id`` and ``ai_invocation_id`` ->
``ai_invocations.ai_invocation_id`` both have UUID primary keys, so a
real FK fits cleanly with no technical obstacle — this was a judgment
call the WO explicitly asked to be flagged; see the WI-1 delivery
report). ``rule_id``/``ai_invocation_id`` are ALSO validated at the
application layer BEFORE insert (see
``services.evidence.classification``'s own "prove real before
trusting" doctrine) — the FK is a backstop, not the only proof.
"""
from __future__ import annotations

from datetime import datetime
from typing import Optional

import sqlalchemy as sa
from sqlalchemy import DateTime, ForeignKey, Index, String
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from persistence.postgres.models import Base

_UUID = UUID(as_uuid=False)


class EvidenceClassificationRow(Base):
    """Persisted form of
    ``services.evidence.classification.EvidenceClassification``.
    Append-only: no update/delete path exists anywhere in
    ``persistence.postgres.evidence_classification_repository`` — only
    insert and select, mirroring ``AuditEventRow``'s own append-only
    discipline."""

    __tablename__ = "evidence_classifications"
    __table_args__ = (
        Index(
            "uq_evidence_classifications_supersedes",
            "supersedes_classification_id",
            unique=True,
            postgresql_where=sa.text("supersedes_classification_id IS NOT NULL"),
        ),
        Index(
            "uq_evidence_classifications_rule_producer",
            "evidence_id", "classification_type", "rule_id",
            unique=True,
            postgresql_where=sa.text("source = 'DETERMINISTIC_RULE'"),
        ),
        Index(
            "uq_evidence_classifications_ai_producer",
            "evidence_id", "classification_type", "ai_invocation_id",
            unique=True,
            postgresql_where=sa.text("source = 'AI_PROPOSAL'"),
        ),
        Index(
            "uq_evidence_classifications_operator_producer",
            "evidence_id", "classification_type", "operator_action_id",
            unique=True,
            postgresql_where=sa.text("source = 'OPERATOR_ASSIGNED'"),
        ),
        Index(
            "ix_evidence_classifications_evidence_type_created",
            "evidence_id", "classification_type", "created_at",
        ),
    )

    classification_id: Mapped[str] = mapped_column(_UUID, primary_key=True)
    evidence_id: Mapped[str] = mapped_column(_UUID, ForeignKey("evidence_items.evidence_id"), nullable=False)
    classification_type: Mapped[str] = mapped_column(String, nullable=False)
    document_type: Mapped[str] = mapped_column(String, nullable=False)
    status: Mapped[str] = mapped_column(String, nullable=False)
    source: Mapped[str] = mapped_column(String, nullable=False)
    confidence: Mapped[Optional[float]] = mapped_column(sa.Float, nullable=True)
    rule_id: Mapped[Optional[str]] = mapped_column(
        _UUID, ForeignKey("evidence_classification_rules.rule_id"), nullable=True
    )
    ai_invocation_id: Mapped[Optional[str]] = mapped_column(
        _UUID, ForeignKey("ai_invocations.ai_invocation_id"), nullable=True
    )
    operator_action_id: Mapped[Optional[str]] = mapped_column(String, nullable=True)
    # A plain list of short strings — JSONB per this codebase's own
    # "free-form/structured fields are JSONB" convention (see
    # persistence/postgres/models.py's own module docstring); no
    # dedicated Postgres ARRAY column type is used elsewhere in this
    # package, so JSONB keeps this row consistent with every other
    # table rather than introducing a new column-type precedent for a
    # field this narrow.
    reason_codes: Mapped[list] = mapped_column(JSONB, nullable=False, default=list)
    # Self-referencing — see module docstring's "Branch prevention"
    # section for the partial unique index this backs.
    supersedes_classification_id: Mapped[Optional[str]] = mapped_column(
        _UUID, ForeignKey("evidence_classifications.classification_id"), nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    # No schema_version column — like AIInvocationRow/IntakeRecordRow,
    # this WI's only supported contract version is a fixed constant
    # (services.evidence.classification.SCHEMA_VERSION), reconstructed
    # by the repository at row-to-domain conversion time.
