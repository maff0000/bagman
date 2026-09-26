"""SQLAlchemy table definition for BAGMAN's durable `AIInvocation`
schema (CD-5 WI-1, PID §26-30/§73).

Kept as its own module rather than added to `persistence/postgres/models.py`
(that module's own docstring scopes it to the six CD-2 canonical
domain tables) or to `persistence/postgres/intake_models.py` (a CD-4
addition of its own) — exactly the precedent CD-4 WI-1 set for
`intake_records`: a new bounded-component table gets its own module.
Shares the same declarative `Base` (imported from
`persistence.postgres.models`) so every module's tables still live in
one `MetaData`/Alembic target.

Concurrency guard — the REAL, database-enforced mechanism (PID §73)
----------------------------------------------------------------------
`ai.invocation`'s module docstring documents the APPLICATION-level
"one active invocation per `(task_id, task_version,
primary_input_reference)`" doctrine; this module is what makes that
invariant genuinely hold even under a real race between two concurrent
callers, not merely under a single process's own pre-check.

The mechanism (the specific "legitimate approach" PID §73's own dispatch
text names): `primary_input_reference` is a PostgreSQL STORED GENERATED
column, computed from `input_references` (JSONB) with the exact same
precedence order `ai.invocation.PRIMARY_INPUT_REFERENCE_KEYS` uses in
Python — `COALESCE(input_references->>'evidence_id',
input_references->>'intake_id', input_references->>'entity_id')` —
combined with a PARTIAL unique index,
`uq_ai_invocations_active_subject`, on
`(task_id, task_version, primary_input_reference)` `WHERE status IN
('REQUESTED', 'RUNNING')`. PostgreSQL enforces this index at the
storage layer on every `INSERT`/`UPDATE`, so two concurrent
transactions racing to create/reactivate the same subject can never
both commit a non-terminal row for it — one loses with a genuine
`UniqueViolation`, which
`persistence.postgres.ai_invocation_repository.PostgresAIInvocationRepository`
translates to `core.errors.ActiveInvocationConflictError` (never a raw
driver exception). This is proven against a REAL disposable PostgreSQL
container (never just read off the DDL) in
`tests/persistence/test_ai_invocation_repository.py`.

Because the index is PARTIAL (`WHERE status IN (...)`), any number of
TERMINAL rows (`SUCCEEDED`/`FAILED`/`REJECTED`/`TIMED_OUT`/`CANCELLED`
— the last two added by the CD-6 reliability delta, PID §98/§100; the
partial index's own `WHERE status IN ('REQUESTED', 'RUNNING')` clause
did not need to change, since it already names the two NON-terminal
states by inclusion rather than naming the terminal ones by exclusion)
may share the same `(task_id, task_version, primary_input_reference)`
— that is exactly what PID §74's retry doctrine requires: an unbounded
sequence of terminal-then-new retries over time, each its own fully
auditable row, never overwritten. `status` itself is a plain `String`
column with no database-level `CHECK` constraint enumerating the
closed set (`ai.invocation.STATUSES` is the application-level source
of truth) — so adding `TIMED_OUT`/`CANCELLED` needed no DDL change to
this column itself, only to `primary_input_reference`'s generated
expression above (see its own docstring) for the new `conversation_id`
fallback key.

Documented edge case: if `input_references` contains NONE of the three
recognised keys, the generated column evaluates to SQL `NULL`, and
PostgreSQL's own unique-index semantics treat multiple `NULL`s in an
indexed column as mutually non-colliding (never enforcing uniqueness
among them) — so the partial index alone would not protect such a row.
This is intentionally not this table's problem to solve: the
DOMAIN layer (`ai.invocation.derive_primary_input_reference`, called by
every repository's `create_invocation` before it ever reaches this
table) already refuses to construct an `AIInvocation` whose
`input_references` yields no recognised primary reference at all (PID
§29). The database-level guard below is the source of truth for the
concurrency invariant AMONG rows that do have one — which, by
construction, is every row that ever reaches this table.

Design notes (mirrors `persistence/postgres/intake_models.py`'s own conventions)
-----------------------------------------------------------------------------------
* `ai_invocation_id` is a native PostgreSQL `UUID` (`as_uuid=False`),
  exactly like every other canonical `*_id` column elsewhere in this
  package.
* `correlation_id` is NOT a foreign key — like `audit_events.correlation_id`
  and `intake_records.correlation_id`, it identifies a workflow
  instance, not a canonical domain object.
* `primary_input_reference` is NOT a foreign key — it is polymorphic
  (depending on which recognised key `input_references` carried, it
  could be an `evidence_id`, an `intake_id`, or an `entity_id`), the
  same "polymorphic subject, no single FK target" doctrine
  `provenance.subject_id`/`audit_events.subject_id` already establish
  in `persistence/postgres/models.py`.
* `capability_alias`/`provider_model`/`prompt_contract_version`/`output`/
  `confidence`/`validation_result`/`error_code`/`latency_ms`/
  `completed_at` are all nullable — every one of them is genuinely
  unknown for at least part of an `AIInvocation`'s lifecycle (PID §27's
  own "state the question, even if unanswered" doctrine, same as
  `IntakeRecordRow`'s nullable columns).
* No `schema_version` column — like `IntakeRecordRow` (unlike
  `AuditEventRow`), this WI's only supported contract version is a
  fixed constant (`ai.invocation.SCHEMA_VERSION`), reconstructed by the
  repository at row-to-domain conversion time rather than persisted
  per-row.
"""
from __future__ import annotations

from datetime import datetime
from typing import Optional

from sqlalchemy import Computed, DateTime, Float, Index, Integer, String, text
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from persistence.postgres.models import Base

#: Matches `persistence/postgres/models.py`'s own `_UUID` — a
#: native-UUID column typed to hand back plain Python `str`.
_UUID = UUID(as_uuid=False)

#: The exact SQL expression backing `primary_input_reference` below —
#: MUST stay in lockstep with
#: `ai.invocation.PRIMARY_INPUT_REFERENCE_KEYS`'s precedence order
#: (`evidence_id`, `intake_id`, `entity_id`, `conversation_id` — the
#: last added by the CD-6 reliability delta, PID §98/§100, deliberately
#: LAST in precedence, see `ai.invocation.derive_primary_input_reference`'s
#: own module docstring for the reasoning). If that Python tuple ever
#: changes, this expression must change with it in the same migration
#: — see `alembic/versions/` for the migration that updated this
#: GENERATED column's expression from its original 3-key form.
_PRIMARY_INPUT_REFERENCE_SQL_EXPRESSION = (
    "COALESCE(input_references->>'evidence_id', "
    "input_references->>'intake_id', "
    "input_references->>'entity_id', "
    "input_references->>'conversation_id')"
)


class AIInvocationRow(Base):
    """Persisted form of `ai.invocation.AIInvocation` (CD-5 WI-1)."""

    __tablename__ = "ai_invocations"
    __table_args__ = (
        # THE real, database-enforced concurrency guard (PID §73) — see
        # module docstring's "Concurrency guard" section for the full
        # mechanism and its documented edge case.
        Index(
            "uq_ai_invocations_active_subject",
            "task_id",
            "task_version",
            "primary_input_reference",
            unique=True,
            postgresql_where=text("status IN ('REQUESTED', 'RUNNING')"),
        ),
        Index("ix_ai_invocations_correlation", "correlation_id"),
        Index("ix_ai_invocations_task", "task_id", "task_version"),
    )

    ai_invocation_id: Mapped[str] = mapped_column(_UUID, primary_key=True)
    task_id: Mapped[str] = mapped_column(String, nullable=False)
    task_version: Mapped[int] = mapped_column(Integer, nullable=False)
    role: Mapped[str] = mapped_column(String, nullable=False)
    provider: Mapped[str] = mapped_column(String, nullable=False)
    capability_alias: Mapped[Optional[str]] = mapped_column(String, nullable=True)
    # CD-6 §103 Inference Architecture Ruling: `MAC_LOCAL` /
    # `TRINITY_CORE_OVERFLOW`, audit-only (see ai.invocation.AIInvocation
    # .inference_backend's own docstring). `server_default="MAC_LOCAL"`
    # matches historical reality — every row created before this column
    # existed really was served by the Mac mini (see the Alembic
    # migration that added it).
    inference_backend: Mapped[str] = mapped_column(String, nullable=False, server_default="MAC_LOCAL")
    provider_model: Mapped[Optional[str]] = mapped_column(String, nullable=True)
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    completed_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    status: Mapped[str] = mapped_column(String, nullable=False)
    correlation_id: Mapped[str] = mapped_column(_UUID, nullable=False)
    actor_type: Mapped[str] = mapped_column(String, nullable=False)
    actor_id: Mapped[str] = mapped_column(String, nullable=False)
    input_references: Mapped[dict] = mapped_column(JSONB, nullable=False)
    # STORED GENERATED column — see module docstring's "Concurrency
    # guard" section. Never written to directly by the repository;
    # PostgreSQL computes it from `input_references` on every
    # insert/update.
    primary_input_reference: Mapped[Optional[str]] = mapped_column(
        String,
        Computed(_PRIMARY_INPUT_REFERENCE_SQL_EXPRESSION, persisted=True),
        nullable=True,
    )
    prompt_contract_version: Mapped[Optional[str]] = mapped_column(String, nullable=True)
    output: Mapped[Optional[dict]] = mapped_column(JSONB, nullable=True)
    confidence: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    validation_result: Mapped[Optional[dict]] = mapped_column(JSONB, nullable=True)
    error_code: Mapped[Optional[str]] = mapped_column(String, nullable=True)
    usage_metadata: Mapped[dict] = mapped_column(JSONB, nullable=False, default=dict)
    latency_ms: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
