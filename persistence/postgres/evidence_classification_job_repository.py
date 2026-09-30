"""`PostgresEvidenceClassificationJobRepository` — durable implementation
of `services.evidence.classification_job.EvidenceClassificationJobRepository`
(BAGMAN accounting platform, `evidence/automatic-classification-activation`
WO).

Preserves, durably, the exact evidence_id-keyed-idempotent-submission
and stale-claim-recovery semantics
`services.evidence.classification_job.InMemoryEvidenceClassificationJobRepository`
documents (see that module's own docstring). Concurrency here is
genuinely real (multiple worker processes / concurrent evidence-creation
call sites), unlike the in-memory implementation's single-process
GIL-serialisation — two mechanisms below are what actually make this
hold under a real race, never an application-level pre-check alone
(mirrors `persistence/postgres/xero_account_suggestion_repository.py`
and `persistence/postgres/background_job_repository.py`'s own identical
disciplines):

1. **Idempotent submission on `evidence_id`** —
   `uq_evidence_classification_jobs_evidence_id` (a real unique index,
   see `evidence_classification_job_models.py`) is the source of truth;
   `submit_job` pre-checks for the common non-racing case, but a
   genuine race is resolved by catching the real `IntegrityError` and
   re-fetching the winning row, never by the pre-check alone — mirrors
   `PostgresXeroAccountSuggestionRepository.create_suggestion`'s own
   documented discipline exactly (the `xero/account-suggestion-producer`
   WO follow-up fix this WO's own spec explicitly cites as the pattern
   to reuse).
2. **Atomic multi-row claiming** — `claim_next_pending` uses
   `SELECT ... FOR UPDATE SKIP LOCKED`, mirroring
   `PostgresBackgroundJobRepository.claim_next_pending`'s own exact
   structure (that module's own docstring explains why `SKIP LOCKED`,
   not a plain `FOR UPDATE`, is the correct primitive here). Proven
   against a REAL disposable PostgreSQL container with genuine
   `threading.Thread`s in
   `tests/persistence/test_evidence_classification_job_repository.py`.
"""
from __future__ import annotations

from datetime import datetime
from typing import Optional

from sqlalchemy.engine import Engine
from sqlalchemy.exc import DataError, IntegrityError, SQLAlchemyError

from ai.invocation import STALE_RUNNING_THRESHOLD_SECONDS
from core import actor, identity
from core.audit import AuditRepository
from core.errors import InvalidStateTransitionError, NotFoundError, PersistenceError, ValidationError
from core.timestamps import utc_now
from persistence.postgres.db_errors import is_invalid_uuid_format, unique_violation_constraint
from persistence.postgres.evidence_classification_job_models import EvidenceClassificationJobRow
from persistence.postgres.session import get_engine, session_scope
from services.evidence.classification_job import (
    EVIDENCE_CLASSIFICATION_JOB_STALE_RECOVERY_AUDIT_EVENT_TYPE,
    EvidenceClassificationJob,
    EvidenceClassificationJobRepository,
    is_stale_claim,
    recover_stale_claim,
    transition_job,
)
from services.evidence.classification_orchestrator import CLASSIFY_EVIDENCE_OUTCOMES

_EVIDENCE_ID_CONSTRAINT = "uq_evidence_classification_jobs_evidence_id"

#: The statuses `claim_next_pending` may claim from — `PENDING`/
#: `FAILED_RETRYABLE` mirror
#: `persistence.postgres.background_job_repository._CLAIMABLE_STATUSES`
#: exactly (a plain tuple, not a set, since it is only ever used inside
#: a SQLAlchemy `.in_(...)` filter); `DEFERRED` is this module's own
#: addition (architect requirement, 2026-09-29 review, WO item 3, see
#: `services.evidence.classification_job`'s own module docstring's
#: "DEFERRED" section) — a `DEFERRED` job is claimable again exactly
#: like `PENDING`/`FAILED_RETRYABLE`.
_CLAIMABLE_STATUSES = ("PENDING", "FAILED_RETRYABLE", "DEFERRED")

#: The two statuses `recover_stale_claims` scans.
_STALE_CANDIDATE_STATUSES = ("CLAIMED", "IN_PROGRESS")


def _row_to_job(row: EvidenceClassificationJobRow) -> EvidenceClassificationJob:
    return EvidenceClassificationJob(
        job_id=row.job_id,
        evidence_id=row.evidence_id,
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
        last_error=row.last_error,
        classification_outcome=row.classification_outcome,
    )


def _row_from_job(job: EvidenceClassificationJob) -> EvidenceClassificationJobRow:
    return EvidenceClassificationJobRow(
        job_id=job.job_id,
        evidence_id=job.evidence_id,
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
        last_error=job.last_error,
        classification_outcome=job.classification_outcome,
    )


def _apply_job_to_row(row: EvidenceClassificationJobRow, job: EvidenceClassificationJob) -> None:
    """Copy every mutable field of ``job`` onto an already-loaded
    ``row`` (identity fields — `job_id`, `evidence_id`, `actor_type`,
    `actor_id`, `correlation_id`, `created_at` — never change after
    creation, so they are deliberately not touched here)."""
    row.status = job.status
    row.updated_at = job.updated_at
    row.attempt_count = job.attempt_count
    row.max_attempts = job.max_attempts
    row.claimed_by = job.claimed_by
    row.claimed_at = job.claimed_at
    row.last_error = job.last_error
    row.classification_outcome = job.classification_outcome


class PostgresEvidenceClassificationJobRepository(EvidenceClassificationJobRepository):
    """PostgreSQL-backed `EvidenceClassificationJobRepository`.
    Stateless: every method reads/writes the database directly via a
    fresh `Session` — mirrors `PostgresBackgroundJobRepository`'s own
    construction/statelessness discipline exactly.
    """

    def __init__(self, engine: Optional[Engine] = None, *, audit_repository: Optional[AuditRepository] = None) -> None:
        self._engine = engine or get_engine()
        if audit_repository is None:
            from persistence.postgres.audit_repository import PostgresAuditRepository

            audit_repository = PostgresAuditRepository(self._engine)
        self._audit_repository: AuditRepository = audit_repository

    # -- helpers -----------------------------------------------------------

    def _record_stale_recovery_audit_event(self, recovered: EvidenceClassificationJob) -> None:
        self._audit_repository.record_audit_event(
            event_type=EVIDENCE_CLASSIFICATION_JOB_STALE_RECOVERY_AUDIT_EVENT_TYPE,
            actor_type=actor.SYSTEM,
            actor_id="evidence-classification-job-stale-recovery",
            subject_type="EvidenceClassificationJob",
            subject_id=recovered.job_id,
            correlation_id=recovered.correlation_id,
            causation_id=None,
            payload={
                "evidence_id": recovered.evidence_id,
                "recovered_status": recovered.status,
                "attempt_count": recovered.attempt_count,
                "max_attempts": recovered.max_attempts,
            },
        )

    def _recover_stale_claims(
        self, *, staleness_threshold_seconds: float, now: Optional[datetime] = None
    ) -> list[EvidenceClassificationJob]:
        now = now if now is not None else utc_now()
        recovered: list[EvidenceClassificationJob] = []
        try:
            with session_scope(self._engine) as session:
                rows = (
                    session.query(EvidenceClassificationJobRow)
                    .filter(EvidenceClassificationJobRow.status.in_(_STALE_CANDIDATE_STATUSES))
                    .with_for_update()
                    .all()
                )
                for row in rows:
                    current = _row_to_job(row)
                    # Re-check UNDER THE LOCK — never trust a pre-lock
                    # read alone (mirrors
                    # PostgresBackgroundJobRepository.recover_stale_claims's
                    # own discipline exactly).
                    if not is_stale_claim(current, staleness_threshold_seconds=staleness_threshold_seconds, now=now):
                        continue
                    updated = recover_stale_claim(current, now=now)
                    _apply_job_to_row(row, updated)
                    recovered.append(updated)
                session.flush()
        except SQLAlchemyError as exc:
            raise PersistenceError(f"could not recover stale EvidenceClassificationJob claims: {exc}") from exc

        # Audit events emitted AFTER the row-transitioning transaction
        # commits — never inside the same transaction/session (mirrors
        # PostgresBackgroundJobRepository's own documented discipline).
        for job in recovered:
            self._record_stale_recovery_audit_event(job)
        return recovered

    # -- EvidenceClassificationJobRepository --------------------------------

    def submit_job(
        self, *, evidence_id: str, actor_type: str, actor_id: str, correlation_id: Optional[str] = None,
        max_attempts: int = 3,
    ) -> EvidenceClassificationJob:
        if not evidence_id:
            raise ValidationError("evidence_id must be a non-empty string")
        if not actor.is_valid(actor_type):
            raise ValidationError(
                f"actor_type '{actor_type}' is not one of the closed set {sorted(actor.ALL)} (PID §14)"
            )
        if max_attempts < 1:
            raise ValidationError(f"max_attempts must be >= 1; got {max_attempts}")

        # Optimistic pre-check (the common, non-racing case) — the real
        # unique index below is what actually proves correctness under
        # a genuine race, never this alone (see module docstring).
        existing = self.get_by_evidence(evidence_id)
        if existing is not None:
            return existing

        now = utc_now()
        candidate = EvidenceClassificationJob(
            job_id=identity.generate_id(),
            evidence_id=evidence_id,
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
            if unique_violation_constraint(exc) == _EVIDENCE_ID_CONSTRAINT:
                # A genuine race: another caller won. Re-fetch and
                # return THEIR row — never our own candidate, and never
                # raise (submit_job is idempotent, not a conflict).
                winner = self.get_by_evidence(evidence_id)
                if winner is not None:
                    return winner
                raise PersistenceError(
                    f"EvidenceClassificationJob unique-constraint conflict for evidence_id '{evidence_id}' "
                    "but no existing row could be re-read"
                ) from exc
            raise PersistenceError(f"could not submit EvidenceClassificationJob: {exc}") from exc
        except SQLAlchemyError as exc:
            raise PersistenceError(f"could not submit EvidenceClassificationJob: {exc}") from exc

        return candidate

    def claim_next_pending(
        self, *, limit: int, claimed_by: str, now: Optional[datetime] = None
    ) -> list[EvidenceClassificationJob]:
        if limit <= 0:
            return []
        now = now if now is not None else utc_now()

        # Bounded, lazy stale-claim recovery FIRST — mirrors
        # PostgresBackgroundJobRepository.claim_next_pending's own
        # documented discipline exactly.
        self._recover_stale_claims(staleness_threshold_seconds=STALE_RUNNING_THRESHOLD_SECONDS, now=now)

        claimed: list[EvidenceClassificationJob] = []
        try:
            with session_scope(self._engine) as session:
                # SKIP LOCKED: lets N concurrent workers each claim a
                # disjoint subset without blocking behind one another
                # (see module docstring).
                rows = (
                    session.query(EvidenceClassificationJobRow)
                    .filter(EvidenceClassificationJobRow.status.in_(_CLAIMABLE_STATUSES))
                    .order_by(EvidenceClassificationJobRow.created_at.asc(), EvidenceClassificationJobRow.job_id.asc())
                    .with_for_update(skip_locked=True)
                    .limit(limit)
                    .all()
                )
                for row in rows:
                    current = _row_to_job(row)
                    updated = transition_job(current, "CLAIMED", now=now, claimed_by=claimed_by, claimed_at=now)
                    _apply_job_to_row(row, updated)
                    claimed.append(updated)
                session.flush()
        except SQLAlchemyError as exc:
            raise PersistenceError(f"could not claim EvidenceClassificationJob rows: {exc}") from exc

        return claimed

    def mark_in_progress(self, job_id: str) -> EvidenceClassificationJob:
        return self._transition_locked(job_id, "IN_PROGRESS", attempt_count_delta=1)

    def mark_succeeded(self, job_id: str, *, classification_outcome: str) -> EvidenceClassificationJob:
        if classification_outcome not in CLASSIFY_EVIDENCE_OUTCOMES:
            raise ValidationError(
                f"classification_outcome '{classification_outcome}' is not one of "
                f"{sorted(CLASSIFY_EVIDENCE_OUTCOMES)}"
            )
        return self._transition_locked(job_id, "SUCCEEDED", classification_outcome=classification_outcome)

    def mark_deferred(self, job_id: str, *, classification_outcome: str) -> EvidenceClassificationJob:
        if classification_outcome not in CLASSIFY_EVIDENCE_OUTCOMES:
            raise ValidationError(
                f"classification_outcome '{classification_outcome}' is not one of "
                f"{sorted(CLASSIFY_EVIDENCE_OUTCOMES)}"
            )
        # attempt_count_delta=-1: the deliberate compensating decrement
        # documented on the ABC's own mark_deferred docstring, mirroring
        # mark_in_progress's own attempt_count_delta=+1 pattern exactly
        # (both go through the same `_transition_locked` helper, which
        # computes `current.attempt_count + attempt_count_delta` under
        # the same row lock — never a separate read/write race). Also
        # clears claimed_by/claimed_at — a DEFERRED row holds no claim
        # lock (see module docstring's own _CLAIMABLE_STATUSES note).
        return self._transition_locked(
            job_id,
            "DEFERRED",
            classification_outcome=classification_outcome,
            claimed_by=None,
            claimed_at=None,
            attempt_count_delta=-1,
        )

    def mark_failed(
        self, job_id: str, *, error: str, retryable: bool, classification_outcome: Optional[str] = None
    ) -> EvidenceClassificationJob:
        if classification_outcome is not None and classification_outcome not in CLASSIFY_EVIDENCE_OUTCOMES:
            raise ValidationError(
                f"classification_outcome '{classification_outcome}' is not one of "
                f"{sorted(CLASSIFY_EVIDENCE_OUTCOMES)}"
            )
        try:
            with session_scope(self._engine) as session:
                row = self._get_row_for_update(session, job_id)
                current = _row_to_job(row)
                target_status = (
                    "FAILED_RETRYABLE" if (retryable and current.attempt_count < current.max_attempts) else "FAILED_TERMINAL"
                )
                updated = transition_job(
                    current, target_status, last_error=error, classification_outcome=classification_outcome
                )
                _apply_job_to_row(row, updated)
                session.flush()
        except (NotFoundError, InvalidStateTransitionError, ValidationError, PersistenceError):
            raise
        except SQLAlchemyError as exc:
            raise PersistenceError(f"could not mark EvidenceClassificationJob failed: {exc}") from exc
        return updated

    def _get_row_for_update(self, session, job_id: str) -> EvidenceClassificationJobRow:
        try:
            row = session.get(EvidenceClassificationJobRow, job_id, with_for_update=True)
        except DataError as exc:
            if is_invalid_uuid_format(exc):
                raise NotFoundError(f"no EvidenceClassificationJob with job_id '{job_id}' (malformed identifier)") from exc
            raise
        if row is None:
            raise NotFoundError(f"no EvidenceClassificationJob with job_id '{job_id}'")
        return row

    def _transition_locked(self, job_id: str, new_status: str, **field_updates) -> EvidenceClassificationJob:
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
            raise PersistenceError(f"could not transition EvidenceClassificationJob: {exc}") from exc
        return updated

    def get_job(self, job_id: str) -> EvidenceClassificationJob:
        try:
            with session_scope(self._engine) as session:
                row = session.get(EvidenceClassificationJobRow, job_id)
                if row is None:
                    raise NotFoundError(f"no EvidenceClassificationJob with job_id '{job_id}'")
                return _row_to_job(row)
        except NotFoundError:
            raise
        except DataError as exc:
            if is_invalid_uuid_format(exc):
                raise NotFoundError(f"no EvidenceClassificationJob with job_id '{job_id}' (malformed identifier)") from exc
            raise PersistenceError(f"could not read EvidenceClassificationJob: {exc}") from exc
        except SQLAlchemyError as exc:
            raise PersistenceError(f"could not read EvidenceClassificationJob: {exc}") from exc

    def get_by_evidence(self, evidence_id: str) -> Optional[EvidenceClassificationJob]:
        try:
            with session_scope(self._engine) as session:
                row = (
                    session.query(EvidenceClassificationJobRow)
                    .filter_by(evidence_id=evidence_id)
                    .order_by(EvidenceClassificationJobRow.created_at.desc())
                    .first()
                )
                return _row_to_job(row) if row is not None else None
        except DataError as exc:
            if is_invalid_uuid_format(exc):
                return None
            raise PersistenceError(f"could not look up EvidenceClassificationJob by evidence_id: {exc}") from exc
        except SQLAlchemyError as exc:
            raise PersistenceError(f"could not look up EvidenceClassificationJob by evidence_id: {exc}") from exc
