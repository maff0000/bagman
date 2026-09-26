"""`PostgresBackgroundJobRepository` — durable implementation of
`ai.jobs.BackgroundJobRepository` (CD-6 §103 Inference Architecture
Ruling).

Preserves, durably, the exact idempotent-submission and
stale-claim-recovery semantics `ai.jobs.InMemoryBackgroundJobRepository`
documents (see that module's own docstring for the full
idempotency-vs-`AIInvocation`-retry contrast, and the "attempt_count
increments at `mark_in_progress` only" design). Concurrency here is
genuinely real (multiple worker processes), unlike the in-memory
implementation's single-process GIL-serialisation — two mechanisms
below are what actually make this hold under a real race, never an
application-level pre-check alone:

1. **Idempotent submission** — `uq_background_jobs_idempotency_key`
   (a real unique index, see `background_job_models.py`) is the source
   of truth; `submit_job` pre-checks for the common non-racing case,
   but a genuine race is resolved by catching the real
   `IntegrityError` and re-fetching the winning row, never by the
   pre-check alone (mirrors
   `PostgresAIInvocationRepository.create_invocation`'s own documented
   discipline).
2. **Atomic multi-row claiming** — `claim_next_pending` uses
   `SELECT ... FOR UPDATE SKIP LOCKED`: the textbook-correct primitive
   for letting N concurrent workers each claim a DISJOINT subset of
   available rows without blocking behind one another (a plain
   `FOR UPDATE`, without `SKIP LOCKED`, would make every worker but one
   block until the first transaction commits — serialising claims that
   should be able to proceed in parallel). Proven against a REAL
   disposable PostgreSQL container with genuine `threading.Thread`s in
   `tests/persistence/test_background_job_repository.py` (mirrors
   `tests/persistence/test_ai_invocation_repository.py`'s own
   real-threaded race proof for `ai_invocations`).
"""
from __future__ import annotations

from datetime import datetime
from typing import Any, Mapping, Optional

from sqlalchemy.engine import Engine
from sqlalchemy.exc import DataError, IntegrityError, SQLAlchemyError

from ai.invocation import STALE_RUNNING_THRESHOLD_SECONDS
from ai.jobs import (
    BACKGROUND_JOB_STALE_RECOVERY_AUDIT_EVENT_TYPE,
    BackgroundJob,
    BackgroundJobRepository,
    is_stale_claim,
    recover_stale_claim,
    transition_job,
    validate_background_job_backend_and_alias,
)
from core import actor, identity
from core.audit import AuditRepository
from core.errors import InvalidStateTransitionError, NotFoundError, PersistenceError, ValidationError
from core.timestamps import utc_now
from persistence.postgres.background_job_models import BackgroundJobRow
from persistence.postgres.db_errors import is_invalid_uuid_format, unique_violation_constraint
from persistence.postgres.session import get_engine, session_scope

_IDEMPOTENCY_KEY_CONSTRAINT = "uq_background_jobs_idempotency_key"

#: The two statuses `claim_next_pending` may claim from — a plain
#: module-level tuple (not a set) since it is only ever used inside a
#: SQLAlchemy `.in_(...)` filter, which wants an ordered iterable.
_CLAIMABLE_STATUSES = ("PENDING", "FAILED_RETRYABLE")

#: The two statuses `recover_stale_claims` scans — mirrors
#: `ai.invocation`'s own `("REQUESTED", "RUNNING")` non-terminal filter
#: shape.
_STALE_CANDIDATE_STATUSES = ("CLAIMED", "IN_PROGRESS")


def _row_to_job(row: BackgroundJobRow) -> BackgroundJob:
    return BackgroundJob(
        job_id=row.job_id,
        idempotency_key=row.idempotency_key,
        task_id=row.task_id,
        task_version=row.task_version,
        input_references=dict(row.input_references),
        evidence_content=row.evidence_content,
        inference_backend=row.inference_backend,
        capability_alias=row.capability_alias,
        status=row.status,
        actor_type=row.actor_type,
        actor_id=row.actor_id,
        correlation_id=row.correlation_id,
        created_at=row.created_at,
        updated_at=row.updated_at,
        attempt_count=row.attempt_count,
        max_attempts=row.max_attempts,
        claimed_by=row.claimed_by,
        claimed_at=row.claimed_at,
        ai_invocation_id=row.ai_invocation_id,
        last_error=row.last_error,
    )


def _row_from_job(job: BackgroundJob) -> BackgroundJobRow:
    return BackgroundJobRow(
        job_id=job.job_id,
        idempotency_key=job.idempotency_key,
        task_id=job.task_id,
        task_version=job.task_version,
        input_references=dict(job.input_references),
        evidence_content=job.evidence_content,
        inference_backend=job.inference_backend,
        capability_alias=job.capability_alias,
        status=job.status,
        actor_type=job.actor_type,
        actor_id=job.actor_id,
        correlation_id=job.correlation_id,
        created_at=job.created_at,
        updated_at=job.updated_at,
        attempt_count=job.attempt_count,
        max_attempts=job.max_attempts,
        claimed_by=job.claimed_by,
        claimed_at=job.claimed_at,
        ai_invocation_id=job.ai_invocation_id,
        last_error=job.last_error,
    )


def _apply_job_to_row(row: BackgroundJobRow, job: BackgroundJob) -> None:
    """Copy every mutable field of ``job`` onto an already-loaded
    ``row`` (identity fields — `job_id`, `idempotency_key`, `task_id`,
    `task_version`, `input_references`, `evidence_content`,
    `inference_backend`, `capability_alias`, `actor_type`, `actor_id`,
    `correlation_id`, `created_at` — never change after creation, so
    they are deliberately not touched here)."""
    row.status = job.status
    row.updated_at = job.updated_at
    row.attempt_count = job.attempt_count
    row.max_attempts = job.max_attempts
    row.claimed_by = job.claimed_by
    row.claimed_at = job.claimed_at
    row.ai_invocation_id = job.ai_invocation_id
    row.last_error = job.last_error


class PostgresBackgroundJobRepository(BackgroundJobRepository):
    """PostgreSQL-backed `BackgroundJobRepository`. Stateless: every
    method reads/writes the database directly via a fresh `Session` —
    mirrors `PostgresAIInvocationRepository`'s own construction/
    statelessness discipline exactly.
    """

    def __init__(self, engine: Optional[Engine] = None, *, audit_repository: Optional[AuditRepository] = None) -> None:
        self._engine = engine or get_engine()
        if audit_repository is None:
            from persistence.postgres.audit_repository import PostgresAuditRepository

            audit_repository = PostgresAuditRepository(self._engine)
        self._audit_repository: AuditRepository = audit_repository

    # -- helpers -----------------------------------------------------------

    def _get_by_idempotency_key(self, idempotency_key: str) -> Optional[BackgroundJob]:
        try:
            with session_scope(self._engine) as session:
                row = (
                    session.query(BackgroundJobRow)
                    .filter(BackgroundJobRow.idempotency_key == idempotency_key)
                    .one_or_none()
                )
                return _row_to_job(row) if row is not None else None
        except SQLAlchemyError as exc:
            raise PersistenceError(f"could not look up BackgroundJob by idempotency_key: {exc}") from exc

    def _record_stale_recovery_audit_event(self, recovered: BackgroundJob) -> None:
        self._audit_repository.record_audit_event(
            event_type=BACKGROUND_JOB_STALE_RECOVERY_AUDIT_EVENT_TYPE,
            actor_type=actor.SYSTEM,
            actor_id="background-job-stale-recovery",
            subject_type="BackgroundJob",
            subject_id=recovered.job_id,
            correlation_id=recovered.correlation_id,
            causation_id=None,
            payload={
                "task_id": recovered.task_id,
                "task_version": recovered.task_version,
                "recovered_status": recovered.status,
                "attempt_count": recovered.attempt_count,
                "max_attempts": recovered.max_attempts,
            },
        )

    # -- BackgroundJobRepository ---------------------------------------------

    def submit_job(
        self,
        *,
        idempotency_key: str,
        task_id: str,
        task_version: int,
        input_references: Mapping[str, Any],
        evidence_content: str,
        inference_backend: str,
        capability_alias: str,
        actor_type: str,
        actor_id: str,
        correlation_id: Optional[str] = None,
        max_attempts: int = 3,
    ) -> BackgroundJob:
        if not idempotency_key:
            raise ValidationError("idempotency_key must be a non-empty string")
        validate_background_job_backend_and_alias(
            inference_backend=inference_backend, capability_alias=capability_alias
        )
        if not actor.is_valid(actor_type):
            raise ValidationError(
                f"actor_type '{actor_type}' is not one of the closed set {sorted(actor.ALL)} (PID §14)"
            )
        if max_attempts < 1:
            raise ValidationError(f"max_attempts must be >= 1; got {max_attempts}")

        # Optimistic pre-check (the common, non-racing case) — the real
        # unique index below is what actually proves correctness under
        # a genuine race, never this alone (see module docstring).
        existing = self._get_by_idempotency_key(idempotency_key)
        if existing is not None:
            return existing

        now = utc_now()
        candidate = BackgroundJob(
            job_id=identity.generate_id(),
            idempotency_key=idempotency_key,
            task_id=task_id,
            task_version=task_version,
            input_references=dict(input_references),
            evidence_content=evidence_content,
            inference_backend=inference_backend,
            capability_alias=capability_alias,
            status="PENDING",
            actor_type=actor_type,
            actor_id=actor_id,
            correlation_id=correlation_id if correlation_id is not None else identity.generate_id(),
            created_at=now,
            updated_at=now,
            max_attempts=max_attempts,
        )
        row = _row_from_job(candidate)
        try:
            with session_scope(self._engine) as session:
                session.add(row)
        except IntegrityError as exc:
            if unique_violation_constraint(exc) == _IDEMPOTENCY_KEY_CONSTRAINT:
                # A genuine race: another caller won. Re-fetch and
                # return THEIR row — never our own candidate, and never
                # raise (submit_job is idempotent, not a conflict).
                winner = self._get_by_idempotency_key(idempotency_key)
                if winner is not None:
                    return winner
                raise PersistenceError(
                    f"idempotency_key unique violation on submit, but no row found on re-fetch: {exc}"
                ) from exc
            raise PersistenceError(f"could not submit BackgroundJob: {exc}") from exc
        except SQLAlchemyError as exc:
            raise PersistenceError(f"could not submit BackgroundJob: {exc}") from exc

        return candidate

    def claim_next_pending(
        self, *, limit: int, worker_id: str, now: Optional[datetime] = None
    ) -> list[BackgroundJob]:
        if limit <= 0:
            return []
        now = now if now is not None else utc_now()

        # Bounded, lazy stale-claim recovery FIRST (module docstring on
        # `ai.jobs.BackgroundJobRepository.claim_next_pending`) — a
        # genuinely abandoned row must become claimable (or terminally
        # fail) before this method decides what is available.
        self.recover_stale_claims(now=now)

        claimed: list[BackgroundJob] = []
        try:
            with session_scope(self._engine) as session:
                # SKIP LOCKED: a genuinely different primitive from
                # every other `.with_for_update()` call in this
                # package (see module docstring) — lets N concurrent
                # workers each claim a disjoint subset without
                # blocking behind one another.
                rows = (
                    session.query(BackgroundJobRow)
                    .filter(BackgroundJobRow.status.in_(_CLAIMABLE_STATUSES))
                    .order_by(BackgroundJobRow.created_at.asc(), BackgroundJobRow.job_id.asc())
                    .with_for_update(skip_locked=True)
                    .limit(limit)
                    .all()
                )
                for row in rows:
                    current = _row_to_job(row)
                    updated = transition_job(current, "CLAIMED", now=now, claimed_by=worker_id, claimed_at=now)
                    _apply_job_to_row(row, updated)
                    claimed.append(updated)
                session.flush()
        except SQLAlchemyError as exc:
            raise PersistenceError(f"could not claim BackgroundJob rows: {exc}") from exc

        return claimed

    def recover_stale_claims(
        self, *, staleness_threshold_seconds: float = STALE_RUNNING_THRESHOLD_SECONDS, now: Optional[datetime] = None
    ) -> list[BackgroundJob]:
        now = now if now is not None else utc_now()
        recovered: list[BackgroundJob] = []
        try:
            with session_scope(self._engine) as session:
                rows = (
                    session.query(BackgroundJobRow)
                    .filter(BackgroundJobRow.status.in_(_STALE_CANDIDATE_STATUSES))
                    .with_for_update()
                    .all()
                )
                for row in rows:
                    current = _row_to_job(row)
                    # Re-check UNDER THE LOCK — never trust a pre-lock
                    # read alone (mirrors
                    # PostgresAIInvocationRepository._recover_if_stale's
                    # own discipline exactly).
                    if not is_stale_claim(current, staleness_threshold_seconds=staleness_threshold_seconds, now=now):
                        continue
                    updated = recover_stale_claim(current, now=now)
                    _apply_job_to_row(row, updated)
                    recovered.append(updated)
                session.flush()
        except SQLAlchemyError as exc:
            raise PersistenceError(f"could not recover stale BackgroundJob claims: {exc}") from exc

        # Audit events emitted AFTER the row-transitioning transaction
        # commits — never inside the same transaction/session (mirrors
        # PostgresAIInvocationRepository's own documented discipline).
        for job in recovered:
            self._record_stale_recovery_audit_event(job)

        return recovered

    def mark_in_progress(self, job_id: str) -> BackgroundJob:
        return self._transition_locked(job_id, "IN_PROGRESS", attempt_count_delta=1)

    def mark_succeeded(self, job_id: str, *, ai_invocation_id: str) -> BackgroundJob:
        return self._transition_locked(job_id, "SUCCEEDED", ai_invocation_id=ai_invocation_id)

    def mark_failed(self, job_id: str, *, error: str, retryable: bool) -> BackgroundJob:
        try:
            with session_scope(self._engine) as session:
                row = self._get_row_for_update(session, job_id)
                current = _row_to_job(row)
                target_status = (
                    "FAILED_RETRYABLE" if (retryable and current.attempt_count < current.max_attempts) else "FAILED_TERMINAL"
                )
                updated = transition_job(current, target_status, last_error=error)
                _apply_job_to_row(row, updated)
                session.flush()
        except (NotFoundError, InvalidStateTransitionError, ValidationError, PersistenceError):
            raise
        except SQLAlchemyError as exc:
            raise PersistenceError(f"could not mark BackgroundJob failed: {exc}") from exc
        return updated

    def release_claim(self, job_id: str) -> BackgroundJob:
        return self._transition_locked(job_id, "PENDING", claimed_by=None, claimed_at=None)

    def _get_row_for_update(self, session, job_id: str) -> BackgroundJobRow:
        try:
            row = session.get(BackgroundJobRow, job_id, with_for_update=True)
        except DataError as exc:
            if is_invalid_uuid_format(exc):
                raise NotFoundError(f"no BackgroundJob with job_id '{job_id}' (malformed identifier)") from exc
            raise
        if row is None:
            raise NotFoundError(f"no BackgroundJob with job_id '{job_id}'")
        return row

    def _transition_locked(self, job_id: str, new_status: str, **field_updates: Any) -> BackgroundJob:
        attempt_count_delta = field_updates.pop("attempt_count_delta", 0)
        try:
            with session_scope(self._engine) as session:
                row = self._get_row_for_update(session, job_id)
                current = _row_to_job(row)
                if attempt_count_delta:
                    field_updates["attempt_count"] = current.attempt_count + attempt_count_delta
                updated = transition_job(current, new_status, **field_updates)
                _apply_job_to_row(row, updated)
                session.flush()
        except (NotFoundError, InvalidStateTransitionError, ValidationError, PersistenceError):
            raise
        except SQLAlchemyError as exc:
            raise PersistenceError(f"could not transition BackgroundJob: {exc}") from exc
        return updated

    def get_job(self, job_id: str) -> BackgroundJob:
        try:
            with session_scope(self._engine) as session:
                row = session.get(BackgroundJobRow, job_id)
                if row is None:
                    raise NotFoundError(f"no BackgroundJob with job_id '{job_id}'")
                return _row_to_job(row)
        except NotFoundError:
            raise
        except DataError as exc:
            if is_invalid_uuid_format(exc):
                raise NotFoundError(f"no BackgroundJob with job_id '{job_id}' (malformed identifier)") from exc
            raise PersistenceError(f"could not read BackgroundJob: {exc}") from exc
        except SQLAlchemyError as exc:
            raise PersistenceError(f"could not read BackgroundJob: {exc}") from exc

    def list_jobs(
        self,
        *,
        status: Optional[str] = None,
        inference_backend: Optional[str] = None,
        task_id: Optional[str] = None,
        limit: Optional[int] = None,
        offset: int = 0,
    ) -> list[BackgroundJob]:
        try:
            with session_scope(self._engine) as session:
                query = session.query(BackgroundJobRow)
                if status is not None:
                    query = query.filter(BackgroundJobRow.status == status)
                if inference_backend is not None:
                    query = query.filter(BackgroundJobRow.inference_backend == inference_backend)
                if task_id is not None:
                    query = query.filter(BackgroundJobRow.task_id == task_id)
                query = query.order_by(BackgroundJobRow.created_at.desc(), BackgroundJobRow.job_id.desc())
                if offset:
                    query = query.offset(offset)
                if limit is not None:
                    query = query.limit(limit)
                rows = query.all()
                return [_row_to_job(row) for row in rows]
        except SQLAlchemyError as exc:
            raise PersistenceError(f"could not list BackgroundJob rows: {exc}") from exc
