"""Persistence proofs for `EvidenceClassificationJob`
(BAGMAN accounting platform, `evidence/automatic-classification-activation`
WO), against a REAL disposable PostgreSQL container
(`tests/persistence/conftest.py`'s own `postgres_container`/
`fresh_engine` fixtures).

Mirrors `tests/persistence/test_background_job_repository.py`'s own
structure closely — including its genuine-threaded `SELECT ... FOR
UPDATE SKIP LOCKED` proof and its real-database stale-claim-recovery
proofs — adapted for `EvidenceClassificationJob`'s DIFFERENT
evidence_id-keyed idempotency doctrine (see
`services.evidence.classification_job`'s own module docstring for the
full contrast with `ai.jobs.BackgroundJob`'s idempotency-key doctrine).
"""
from __future__ import annotations

import datetime
import hashlib
import threading
import time
from unittest import mock

import pytest
import sqlalchemy as sa

from core import identity
from core.audit import InMemoryAuditRepository
from core.errors import InvalidStateTransitionError, NotFoundError, ValidationError
from core.timestamps import utc_now
from persistence.postgres.evidence_classification_job_models import EvidenceClassificationJobRow
from persistence.postgres.evidence_classification_job_repository import PostgresEvidenceClassificationJobRepository
from persistence.postgres.evidence_repository import PostgresEvidenceRepository
from persistence.postgres.external_reference_repository import PostgresExternalReferenceRepository
from persistence.postgres.session import get_engine
from persistence.postgres.source_repository import PostgresSourceRepository
from services.evidence.classification_job import EVIDENCE_CLASSIFICATION_JOB_STALE_RECOVERY_AUDIT_EVENT_TYPE
from services.evidence.classification_orchestrator import OUTCOME_AI_IN_PROGRESS, OUTCOME_DETERMINISTIC_CLASSIFIED


def _real_evidence_id() -> str:
    """Creates one real, durable `EvidenceItem` (the real foreign key
    `evidence_classification_jobs.evidence_id` requires) and returns
    its id."""
    source = PostgresSourceRepository().register_source(
        source_type="MANUAL_UPLOAD", provider="INTERNAL", status="ACTIVE"
    )
    now = utc_now()
    evidence = PostgresEvidenceRepository(PostgresExternalReferenceRepository()).register_evidence(
        entity_id=None,
        evidence_type="DOCUMENT",
        source_id=source.source_id,
        observed_at=now,
        received_at=now,
        content_hash={
            "algorithm": "SHA-256",
            "value": hashlib.sha256(identity.generate_id().encode("utf-8")).hexdigest(),
        },
        mime_type="application/pdf",
        size_bytes=100,
        storage_reference=None,
    )
    return evidence.evidence_id


_UNSET = object()


def _submit(repo=None, *, evidence_id=_UNSET, **overrides):
    repo = repo or PostgresEvidenceClassificationJobRepository()
    kwargs = dict(
        evidence_id=_real_evidence_id() if evidence_id is _UNSET else evidence_id,
        actor_type="SYSTEM",
        actor_id="bagman-test-harness",
    )
    kwargs.update(overrides)
    return repo.submit_job(**kwargs)


def _job_count() -> int:
    with get_engine().connect() as conn:
        return conn.execute(sa.select(sa.func.count()).select_from(EvidenceClassificationJobRow)).scalar_one()


def _force_claimed_at(job_id: str, claimed_at: datetime.datetime) -> None:
    with get_engine().begin() as conn:
        conn.execute(
            sa.update(EvidenceClassificationJobRow)
            .where(EvidenceClassificationJobRow.job_id == job_id)
            .values(claimed_at=claimed_at)
        )


# ---------------------------------------------------------------------
# Create / get round trip + real FK enforcement
# ---------------------------------------------------------------------


def test_submit_and_get_round_trip():
    created = _submit()
    fetched = PostgresEvidenceClassificationJobRepository().get_job(created.job_id)
    assert fetched == created


def test_get_job_not_found_raises():
    with pytest.raises(NotFoundError):
        PostgresEvidenceClassificationJobRepository().get_job(identity.generate_id())


def test_get_job_malformed_id_raises_not_found_not_persistence_error():
    with pytest.raises(NotFoundError):
        PostgresEvidenceClassificationJobRepository().get_job("not-a-valid-uuid")


def test_get_by_evidence_returns_none_when_no_job_exists():
    assert PostgresEvidenceClassificationJobRepository().get_by_evidence(identity.generate_id()) is None


def test_submit_job_rejects_a_nonexistent_evidence_id_via_the_real_foreign_key():
    """The real `evidence_id` FK on `evidence_classification_jobs`
    (never an application-level check alone) rejects a job for an
    evidence_id that does not exist — proven directly against the
    table, mirroring `tests/persistence/test_xero_account_suggestion_repository.py`'s
    own "prove the real constraint, not just the app-level check"
    discipline."""
    from core.errors import PersistenceError

    with pytest.raises(PersistenceError):
        _submit(evidence_id=identity.generate_id())


# ---------------------------------------------------------------------
# Idempotent submission, keyed on evidence_id — including the REAL
# constraint-violation path
# ---------------------------------------------------------------------


def test_submit_job_is_idempotent_on_evidence_id():
    evidence_id = _real_evidence_id()
    first = _submit(evidence_id=evidence_id)
    second = _submit(evidence_id=evidence_id, actor_id="a-different-caller")
    assert second.job_id == first.job_id
    assert second.actor_id == first.actor_id  # the ORIGINAL row, unchanged
    assert _job_count() == 1


def test_submit_job_race_resolves_via_the_real_unique_constraint():
    """Mirrors `test_background_job_repository.py`'s own documented
    simulated-race pattern: force the SECOND caller's optimistic
    pre-check to report "nothing exists yet" ONCE, forcing it down the
    real insert-then-translate-the-constraint-violation path a genuine
    concurrent caller would hit."""
    evidence_id = _real_evidence_id()
    repo_a = PostgresEvidenceClassificationJobRepository()
    repo_b = PostgresEvidenceClassificationJobRepository()

    winner = _submit(repo_a, evidence_id=evidence_id)

    original = PostgresEvidenceClassificationJobRepository.get_by_evidence
    call_count = {"n": 0}

    def _pre_check_reports_nothing_once_then_real(self, evidence_id_):
        call_count["n"] += 1
        if call_count["n"] == 1:
            return None  # simulate: the race — nothing found yet
        return original(self, evidence_id_)

    with mock.patch.object(
        PostgresEvidenceClassificationJobRepository, "get_by_evidence", _pre_check_reports_nothing_once_then_real
    ):
        loser_result = _submit(repo_b, evidence_id=evidence_id)

    assert loser_result.job_id == winner.job_id
    assert _job_count() == 1


# ---------------------------------------------------------------------
# claim_next_pending — real SELECT ... FOR UPDATE SKIP LOCKED proof
# ---------------------------------------------------------------------


def test_claim_next_pending_claims_oldest_first_and_marks_claimed():
    job_a = _submit()
    job_b = _submit()
    repo = PostgresEvidenceClassificationJobRepository()
    claimed = repo.claim_next_pending(limit=2, claimed_by="worker-1")
    assert [j.job_id for j in claimed] == [job_a.job_id, job_b.job_id]
    for j in claimed:
        assert j.status == "CLAIMED"
        assert j.claimed_by == "worker-1"
        assert j.claimed_at is not None


def test_claim_next_pending_returns_empty_when_nothing_available():
    assert PostgresEvidenceClassificationJobRepository().claim_next_pending(limit=5, claimed_by="w") == []


def test_genuinely_concurrent_workers_claiming_overlapping_rows_never_claim_the_same_row_twice():
    """The real proof `SELECT ... FOR UPDATE SKIP LOCKED` exists for: N
    genuine worker threads, each with its OWN repository/engine, racing
    `claim_next_pending` against the SAME pool of available rows, at
    (as near as a `threading.Barrier` can arrange) the same instant. No
    row may ever be claimed by two workers."""
    n_jobs = 12
    n_workers = 4
    jobs = [_submit() for _ in range(n_jobs)]
    barrier = threading.Barrier(n_workers)
    results: list[list] = [[] for _ in range(n_workers)]
    windows: list[tuple] = [None] * n_workers

    def _worker(index: int) -> None:
        repo = PostgresEvidenceClassificationJobRepository()
        barrier.wait()
        start = time.monotonic()
        claimed = repo.claim_next_pending(limit=n_jobs, claimed_by=f"worker-{index}")
        results[index] = [j.job_id for j in claimed]
        windows[index] = (start, time.monotonic())

    threads = [threading.Thread(target=_worker, args=(i,)) for i in range(n_workers)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=30)

    all_claimed = [job_id for worker_result in results for job_id in worker_result]
    assert len(all_claimed) == n_jobs, f"expected exactly {n_jobs} rows claimed in total, got {len(all_claimed)}"
    assert len(set(all_claimed)) == n_jobs, (
        f"a row was claimed by more than one worker — SKIP LOCKED failed to prevent a double-claim: {results}"
    )

    overlap_found = any(
        a_start < b_end and b_start < a_end
        for i, (a_start, a_end) in enumerate(windows)
        for j, (b_start, b_end) in enumerate(windows)
        if i < j
    )
    assert overlap_found, f"no genuine wall-clock overlap detected between worker call windows: {windows}"

    for job in jobs:
        fetched = PostgresEvidenceClassificationJobRepository().get_job(job.job_id)
        assert fetched.status == "CLAIMED"
        assert fetched.claimed_by is not None


# ---------------------------------------------------------------------
# Full lifecycle
# ---------------------------------------------------------------------


def test_full_lifecycle_happy_path():
    job = _submit()
    repo = PostgresEvidenceClassificationJobRepository()
    [claimed] = repo.claim_next_pending(limit=1, claimed_by="worker-1")
    in_progress = repo.mark_in_progress(claimed.job_id)
    assert in_progress.status == "IN_PROGRESS"
    assert in_progress.attempt_count == 1

    succeeded = repo.mark_succeeded(job.job_id, classification_outcome=OUTCOME_DETERMINISTIC_CLASSIFIED)
    assert succeeded.status == "SUCCEEDED"
    assert succeeded.classification_outcome == OUTCOME_DETERMINISTIC_CLASSIFIED

    fetched = repo.get_job(job.job_id)
    assert fetched.status == "SUCCEEDED"
    assert fetched.classification_outcome == OUTCOME_DETERMINISTIC_CLASSIFIED


def test_retryable_failure_then_reclaim_then_succeeds():
    job = _submit(max_attempts=3)
    repo = PostgresEvidenceClassificationJobRepository()
    [claimed] = repo.claim_next_pending(limit=1, claimed_by="worker-1")
    repo.mark_in_progress(claimed.job_id)
    failed = repo.mark_failed(job.job_id, error="object store timeout", retryable=True)
    assert failed.status == "FAILED_RETRYABLE"

    [reclaimed] = repo.claim_next_pending(limit=1, claimed_by="worker-2")
    assert reclaimed.attempt_count == 1
    repo.mark_in_progress(reclaimed.job_id)
    succeeded = repo.mark_succeeded(job.job_id, classification_outcome=OUTCOME_DETERMINISTIC_CLASSIFIED)
    assert succeeded.status == "SUCCEEDED"


def test_retries_exhausted_then_terminal():
    job = _submit(max_attempts=2)
    repo = PostgresEvidenceClassificationJobRepository()
    for attempt in range(1, 3):
        [claimed] = repo.claim_next_pending(limit=1, claimed_by="worker")
        in_progress = repo.mark_in_progress(claimed.job_id)
        assert in_progress.attempt_count == attempt
        failed = repo.mark_failed(job.job_id, error=f"attempt {attempt}", retryable=True)
    assert failed.status == "FAILED_TERMINAL"
    assert repo.get_job(job.job_id).status == "FAILED_TERMINAL"
    # A FAILED_TERMINAL job is never reclaimable.
    assert repo.claim_next_pending(limit=5, claimed_by="worker-late") == []


def test_mark_deferred_round_trip():
    """Real, disposable-PostgreSQL proof (test plan item 9) that
    `mark_deferred` is a genuine, working repository method: status
    transitions to `DEFERRED`, `classification_outcome` is stamped,
    `attempt_count` is decremented by exactly 1 (undoing
    `mark_in_progress`'s own increment for this claim), and
    `claimed_by`/`claimed_at` are cleared."""
    job = _submit()
    repo = PostgresEvidenceClassificationJobRepository()
    [claimed] = repo.claim_next_pending(limit=1, claimed_by="worker-1")
    assert claimed.claimed_by == "worker-1"
    assert claimed.claimed_at is not None

    in_progress = repo.mark_in_progress(claimed.job_id)
    assert in_progress.attempt_count == 1

    deferred = repo.mark_deferred(job.job_id, classification_outcome=OUTCOME_AI_IN_PROGRESS)
    assert deferred.status == "DEFERRED"
    assert deferred.classification_outcome == OUTCOME_AI_IN_PROGRESS
    assert deferred.attempt_count == 0, "the compensating decrement must undo mark_in_progress's own +1"
    assert deferred.claimed_by is None
    assert deferred.claimed_at is None

    fetched = repo.get_job(job.job_id)
    assert fetched.status == "DEFERRED"
    assert fetched.classification_outcome == OUTCOME_AI_IN_PROGRESS
    assert fetched.attempt_count == 0

    # And it is genuinely reclaimable — a real second claim_next_pending
    # call, not merely a status label.
    [reclaimed] = repo.claim_next_pending(limit=5, claimed_by="worker-2")
    assert reclaimed.job_id == job.job_id
    assert reclaimed.status == "CLAIMED"
    assert reclaimed.claimed_by == "worker-2"


def test_mark_deferred_rejects_an_unknown_classification_outcome():
    job = _submit()
    repo = PostgresEvidenceClassificationJobRepository()
    [claimed] = repo.claim_next_pending(limit=1, claimed_by="worker-1")
    repo.mark_in_progress(claimed.job_id)
    with pytest.raises(ValidationError):
        repo.mark_deferred(job.job_id, classification_outcome="NOT_A_REAL_OUTCOME")


def test_invalid_transition_raises():
    job = _submit()
    repo = PostgresEvidenceClassificationJobRepository()
    with pytest.raises(InvalidStateTransitionError):
        repo.mark_in_progress(job.job_id)  # PENDING -> IN_PROGRESS is not a direct edge


def test_submit_job_rejects_empty_evidence_id():
    with pytest.raises(ValidationError):
        _submit(evidence_id="")


# ---------------------------------------------------------------------
# Stale-claim recovery — real database, both CLAIMED and IN_PROGRESS
# ---------------------------------------------------------------------


def test_recover_stale_claims_recovers_a_stale_claimed_row_to_pending():
    audit = InMemoryAuditRepository()
    repo = PostgresEvidenceClassificationJobRepository(audit_repository=audit)
    job = _submit(repo, max_attempts=3)
    [claimed] = repo.claim_next_pending(limit=1, claimed_by="worker-1")
    _force_claimed_at(claimed.job_id, utc_now() - datetime.timedelta(seconds=700))

    [recovered] = repo._recover_stale_claims(staleness_threshold_seconds=600.0)  # noqa: SLF001 - direct exercise, test-only
    assert recovered.status == "PENDING"
    assert recovered.claimed_by is None

    events = audit.list_by_subject("EvidenceClassificationJob", job.job_id)
    assert [e.event_type for e in events] == [EVIDENCE_CLASSIFICATION_JOB_STALE_RECOVERY_AUDIT_EVENT_TYPE]


def test_recover_stale_claims_recovers_a_stale_in_progress_row_to_failed_retryable():
    audit = InMemoryAuditRepository()
    repo = PostgresEvidenceClassificationJobRepository(audit_repository=audit)
    job = _submit(repo, max_attempts=3)
    [claimed] = repo.claim_next_pending(limit=1, claimed_by="worker-1")
    repo.mark_in_progress(claimed.job_id)
    _force_claimed_at(claimed.job_id, utc_now() - datetime.timedelta(seconds=700))

    [recovered] = repo._recover_stale_claims(staleness_threshold_seconds=600.0)  # noqa: SLF001
    assert recovered.status == "FAILED_RETRYABLE"
    assert recovered.attempt_count == 1

    events = audit.list_by_subject("EvidenceClassificationJob", job.job_id)
    assert [e.event_type for e in events] == [EVIDENCE_CLASSIFICATION_JOB_STALE_RECOVERY_AUDIT_EVENT_TYPE]


def test_recover_stale_claims_terminal_fails_when_budget_exhausted():
    repo = PostgresEvidenceClassificationJobRepository()
    job = _submit(repo, max_attempts=1)
    [claimed] = repo.claim_next_pending(limit=1, claimed_by="worker-1")
    repo.mark_in_progress(claimed.job_id)
    _force_claimed_at(claimed.job_id, utc_now() - datetime.timedelta(seconds=700))

    [recovered] = repo._recover_stale_claims(staleness_threshold_seconds=600.0)  # noqa: SLF001
    assert recovered.status == "FAILED_TERMINAL"


def test_a_genuinely_recent_claim_is_not_recovered():
    repo = PostgresEvidenceClassificationJobRepository()
    _submit(repo)
    [claimed] = repo.claim_next_pending(limit=1, claimed_by="worker-1")
    recovered = repo._recover_stale_claims(staleness_threshold_seconds=600.0)  # noqa: SLF001
    assert recovered == []
    assert repo.get_job(claimed.job_id).status == "CLAIMED"


def test_claim_next_pending_runs_recovery_first():
    """A stale `CLAIMED` job left by a crashed worker is silently
    recovered to `PENDING` and reclaimed by a LATER worker's own
    `claim_next_pending` call — this is exactly the mechanism test
    plan item 8 (`Worker crash simulation`) requires."""
    repo = PostgresEvidenceClassificationJobRepository()
    _submit(repo)
    [claimed] = repo.claim_next_pending(limit=1, claimed_by="worker-1")
    _force_claimed_at(claimed.job_id, utc_now() - datetime.timedelta(seconds=700))

    reclaimed = repo.claim_next_pending(limit=1, claimed_by="worker-2")
    assert len(reclaimed) == 1
    assert reclaimed[0].claimed_by == "worker-2"


def test_a_stale_in_progress_job_is_reclaimed_then_can_succeed():
    """Full crash-recovery round trip: a worker claims and starts a
    job, crashes (never calls mark_succeeded/mark_failed), the row goes
    stale, a later worker reclaims and completes it."""
    repo = PostgresEvidenceClassificationJobRepository()
    job = _submit(repo, max_attempts=3)
    [claimed] = repo.claim_next_pending(limit=1, claimed_by="worker-crashed")
    repo.mark_in_progress(claimed.job_id)
    _force_claimed_at(claimed.job_id, utc_now() - datetime.timedelta(seconds=700))

    [reclaimed] = repo.claim_next_pending(limit=1, claimed_by="worker-recovers")
    assert reclaimed.status == "CLAIMED"
    repo.mark_in_progress(reclaimed.job_id)
    succeeded = repo.mark_succeeded(job.job_id, classification_outcome=OUTCOME_DETERMINISTIC_CLASSIFIED)
    assert succeeded.status == "SUCCEEDED"


# ---------------------------------------------------------------------
# No secrets in persisted rows (test plan item 13)
# ---------------------------------------------------------------------


def test_job_row_carries_no_credential_shaped_fields():
    job = _submit()
    fields = set(job.to_dict().keys())
    assert fields == {
        "job_id", "evidence_id", "status", "actor_type", "actor_id", "correlation_id", "created_at",
        "updated_at", "attempt_count", "max_attempts", "claimed_by", "claimed_at", "last_error",
        "classification_outcome",
    }
    for forbidden in ("password", "secret", "token", "api_key", "credential"):
        assert forbidden not in " ".join(fields).lower()


# ---------------------------------------------------------------------
# No historical/backfill enqueueing (test plan item 11, structural half)
# ---------------------------------------------------------------------


def test_pre_existing_evidence_with_no_job_is_never_auto_enqueued_by_anything_in_this_module():
    """Historical evidence created before this feature shipped (no job
    row, and nothing ever calling `submit_job` for it) stays that way —
    there is no sweep/reconciliation function anywhere in this
    persistence module (only `submit_job`/`claim_next_pending`/the
    explicit lifecycle transitions above) that could pick it up."""
    evidence_id = _real_evidence_id()  # created, but NEVER submitted as a job
    assert PostgresEvidenceClassificationJobRepository().get_by_evidence(evidence_id) is None
    # claim_next_pending only ever claims EXISTING PENDING/FAILED_RETRYABLE
    # rows — it must never conjure one into existence for this evidence.
    PostgresEvidenceClassificationJobRepository().claim_next_pending(limit=200, claimed_by="sweep-attempt")
    assert PostgresEvidenceClassificationJobRepository().get_by_evidence(evidence_id) is None
