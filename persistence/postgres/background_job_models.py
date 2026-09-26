"""SQLAlchemy table definition for BAGMAN's durable `background_jobs`
table (CD-6 §103 Inference Architecture Ruling — the durable,
Postgres-backed job mechanism PID §103 point 7 / §103.4 item 1
requires for Trinity backlog/overflow processing).

Kept as its own module rather than added to `persistence/postgres/models.py`
or `ai_invocation_models.py` — exactly the precedent
`ai_invocation_models.py`'s own docstring set for `ai_invocations`
itself: a new bounded-component table gets its own module. Shares the
same declarative `Base` (imported from `persistence.postgres.models`)
so every module's tables still live in one `MetaData`/Alembic target.

This table is a DIFFERENT concurrency shape from `ai_invocations`
------------------------------------------------------------------------
`ai_invocations` enforces "at most one ACTIVE row per subject" via a
partial unique index (see `ai_invocation_models.py`'s own docstring).
`background_jobs` has no such constraint at all — many jobs may be
`PENDING`/`CLAIMED`/`IN_PROGRESS` simultaneously; the only uniqueness
constraint this table enforces is `idempotency_key` (a caller-supplied
dedupe key — "make sure this exact piece of backlog work exists exactly
once", never "at most one in flight" — see `ai.jobs`'s own module
docstring for the full idempotent-submission doctrine and its
deliberate contrast with `ai_invocations`' own "new row every retry"
doctrine).

Atomic claiming (PID §103.4 item 1: "`SELECT ... FOR UPDATE SKIP
LOCKED` or equivalent")
------------------------------------------------------------------------
`persistence.postgres.background_job_repository.PostgresBackgroundJobRepository
.claim_next_pending` uses `SELECT ... FOR UPDATE SKIP LOCKED` against
this table — the textbook-correct primitive for atomic multi-row
claiming among several concurrent workers, each skipping past rows a
concurrent claimer already holds a lock on rather than blocking behind
them. This repo does not use `SKIP LOCKED` anywhere else; it is
introduced here deliberately (see that repository's own module
docstring for the full reasoning and its real, threaded-race proof in
`tests/persistence/test_background_job_repository.py`).
"""
from __future__ import annotations

from datetime import datetime
from typing import Optional

from sqlalchemy import DateTime, Index, Integer, String, Text
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from persistence.postgres.models import Base

#: Matches `persistence/postgres/models.py`'s own `_UUID` — a
#: native-UUID column typed to hand back plain Python `str`.
_UUID = UUID(as_uuid=False)


class BackgroundJobRow(Base):
    """Persisted form of `ai.jobs.BackgroundJob` (CD-6 §103)."""

    __tablename__ = "background_jobs"
    __table_args__ = (
        Index("uq_background_jobs_idempotency_key", "idempotency_key", unique=True),
        # Supports the common "list every job for this task" query
        # (ai.jobs.BackgroundJobRepository.list_jobs's own `task_id`
        # filter) — mirrors `ix_ai_invocations_task` exactly.
        Index("ix_background_jobs_task", "task_id", "task_version"),
        # Supports `claim_next_pending`'s own scan (claimable rows,
        # oldest-first) and `list_jobs`'s `status` filter — mirrors
        # `ai_invocations`' own partial-index discipline (an index
        # scoped to exactly the rows a hot-path query actually needs),
        # though this one is NOT unique (see module docstring: many
        # rows may legitimately be claimable at once).
        Index(
            "ix_background_jobs_claimable",
            "status",
            "created_at",
        ),
        Index("ix_background_jobs_correlation", "correlation_id"),
    )

    job_id: Mapped[str] = mapped_column(_UUID, primary_key=True)
    idempotency_key: Mapped[str] = mapped_column(String, nullable=False)
    task_id: Mapped[str] = mapped_column(String, nullable=False)
    task_version: Mapped[int] = mapped_column(Integer, nullable=False)
    input_references: Mapped[dict] = mapped_column(JSONB, nullable=False)
    #: Already-resolved, already-rendered evidence content (see
    #: `ai.jobs` module docstring: this table/module is
    #: content-agnostic — it never itself fetches/renders evidence).
    evidence_content: Mapped[str] = mapped_column(Text, nullable=False)
    inference_backend: Mapped[str] = mapped_column(String, nullable=False)
    capability_alias: Mapped[str] = mapped_column(String, nullable=False)
    status: Mapped[str] = mapped_column(String, nullable=False)
    attempt_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    max_attempts: Mapped[int] = mapped_column(Integer, nullable=False, default=3)
    claimed_by: Mapped[Optional[str]] = mapped_column(String, nullable=True)
    claimed_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    #: Populated once a real `AIInvocation` is created for a
    #: successful/attempted execution of this job — links the job back
    #: into the existing `ai_invocations` audit trail. NOT a foreign
    #: key with `ON DELETE` cascade behaviour of any kind — plain
    #: informational linkage, same non-FK-enforced-lifecycle doctrine
    #: `ai_invocations.primary_input_reference` itself uses (see that
    #: module's own docstring), chosen here because a `BackgroundJob`'s
    #: own lifecycle must never be blocked/cascaded by anything
    #: happening to the `AIInvocation` it references.
    ai_invocation_id: Mapped[Optional[str]] = mapped_column(_UUID, nullable=True)
    last_error: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    actor_type: Mapped[str] = mapped_column(String, nullable=False)
    actor_id: Mapped[str] = mapped_column(String, nullable=False)
    correlation_id: Mapped[str] = mapped_column(_UUID, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
