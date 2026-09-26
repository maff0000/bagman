"""PID §103 'background job persistence' proofs (CD-6 §103 Inference
Architecture Ruling), against a REAL disposable PostgreSQL container
(`tests/persistence/conftest.py`'s own `postgres_container`/
`fresh_engine` fixtures).

Mirrors `tests/persistence/test_ai_invocation_repository.py`'s own
structure — including its genuine-threaded-race proof pattern
(`test_genuinely_concurrent_threads_racing_the_same_subject_...`) —
adapted for `BackgroundJob`'s idempotent-submission doctrine and
`SELECT ... FOR UPDATE SKIP LOCKED` claiming (a DIFFERENT primitive
from `ai_invocations`' own partial-unique-index concurrency guard; see
`persistence/postgres/background_job_repository.py`'s own module
docstring for the contrast).
"""
from __future__ import annotations

import datetime
import threading
import time
from unittest import mock

import pytest
import sqlalchemy as sa

from ai.jobs import BACKGROUND_JOB_STALE_RECOVERY_AUDIT_EVENT_TYPE
from core import identity
from core.audit import InMemoryAuditRepository
from core.errors import InvalidStateTransitionError, NotFoundError, ValidationError
from core.timestamps import utc_now
from persistence.postgres.background_job_models import BackgroundJobRow
from persistence.postgres.background_job_repository import PostgresBackgroundJobRepository
from persistence.postgres.session import get_engine


def _submit(repo=None, *, idempotency_key=None, **overrides):
    repo = repo or PostgresBackgroundJobRepository()
    kwargs = dict(
        idempotency_key=idempotency_key or identity.generate_id(),
        task_id="DOCUMENT_TYPE_PROPOSAL",
        task_version=1,
        input_references={"evidence_id": identity.generate_id()},
        evidence_content="some rendered evidence content",
        inference_backend="TRINITY_CORE_OVERFLOW",
        capability_alias="trinity-core",
        actor_type="SYSTEM",
        actor_id="bagman-test-harness",
    )
    kwargs.update(overrides)
    return repo.submit_job(**kwargs)


def _job_count() -> int:
    with get_engine().connect() as conn:
        return conn.execute(sa.select(sa.func.count()).select_from(BackgroundJobRow)).scalar_one()


def _force_claimed_at(job_id: str, claimed_at: datetime.datetime) -> None:
    with get_engine().begin() as conn:
        conn.execute(
            sa.update(BackgroundJobRow).where(BackgroundJobRow.job_id == job_id).values(claimed_at=claimed_at)
        )


# ---------------------------------------------------------------------
# Create / get round trip
# ---------------------------------------------------------------------


def test_submit_and_get_round_trip():
    created = _submit()
    fetched = PostgresBackgroundJobRepository().get_job(created.job_id)
    assert fetched == created


def test_get_job_not_found_raises():
    with pytest.raises(NotFoundError):
        PostgresBackgroundJobRepository().get_job(identity.generate_id())


def test_get_job_malformed_id_raises_not_found_not_persistence_error():
    with pytest.raises(NotFoundError):
        PostgresBackgroundJobRepository().get_job("not-a-valid-uuid")


def test_submit_job_rejects_mac_local_backend():
    with pytest.raises(ValidationError):
        _submit(inference_backend="MAC_LOCAL", capability_alias="bagman-fast")


# ---------------------------------------------------------------------
# Idempotent submission — including the REAL constraint-violation path
# ---------------------------------------------------------------------


def test_submit_job_is_idempotent_on_idempotency_key():
    key = identity.generate_id()
    first = _submit(idempotency_key=key)
    second = _submit(idempotency_key=key, task_id="ENTITY_PROPOSAL")
    assert second.job_id == first.job_id
    assert second.task_id == first.task_id
    assert _job_count() == 1


def test_submit_job_race_resolves_via_the_real_unique_constraint():
    """Mirrors `test_ai_invocation_repository.py`'s own documented
    simulated-race pattern: force the SECOND caller's optimistic
    pre-check to report "nothing exists yet" ONCE (via
    `mock.patch.object`), forcing it down the real
    insert-then-translate-the-constraint-violation path a genuine
    concurrent caller would hit — the RE-fetch inside that exception
    handler must still use the REAL method (never patched twice), since
    it is what actually proves the winning row is returned, not the
    pre-check."""
    key = identity.generate_id()
    repo_a = PostgresBackgroundJobRepository()
    repo_b = PostgresBackgroundJobRepository()

    winner = _submit(repo_a, idempotency_key=key)

    original = PostgresBackgroundJobRepository._get_by_idempotency_key
    call_count = {"n": 0}

    def _pre_check_reports_nothing_once_then_real(self, idempotency_key_):
        call_count["n"] += 1
        if call_count["n"] == 1:
            return None  # simulate: the race — nothing found yet
        return original(self, idempotency_key_)

    with mock.patch.object(
        PostgresBackgroundJobRepository, "_get_by_idempotency_key", _pre_check_reports_nothing_once_then_real
    ):
        loser_result = _submit(repo_b, idempotency_key=key)

    assert loser_result.job_id == winner.job_id
    assert _job_count() == 1


# ---------------------------------------------------------------------
# claim_next_pending — real SELECT ... FOR UPDATE SKIP LOCKED proof
# ---------------------------------------------------------------------


def test_claim_next_pending_claims_oldest_first_and_marks_claimed():
    job_a = _submit()
    job_b = _submit()
    repo = PostgresBackgroundJobRepository()
    claimed = repo.claim_next_pending(limit=2, worker_id="worker-1")
    assert [j.job_id for j in claimed] == [job_a.job_id, job_b.job_id]
    for j in claimed:
        assert j.status == "CLAIMED"
        assert j.claimed_by == "worker-1"
        assert j.claimed_at is not None


def test_claim_next_pending_returns_empty_when_nothing_available():
    assert PostgresBackgroundJobRepository().claim_next_pending(limit=5, worker_id="w") == []


def test_genuinely_concurrent_workers_claiming_overlapping_rows_never_claim_the_same_row_twice():
    """The real proof `SELECT ... FOR UPDATE SKIP LOCKED` exists for:
    N genuine worker threads, each with its OWN repository/engine,
    racing `claim_next_pending` against the SAME pool of available
    rows, at (as near as a `threading.Barrier` can arrange) the same
    instant. No row may ever be claimed by two workers."""
    n_jobs = 12
    n_workers = 4
    jobs = [_submit() for _ in range(n_jobs)]
    barrier = threading.Barrier(n_workers)
    results: list[list] = [[] for _ in range(n_workers)]
    windows: list[tuple] = [None] * n_workers

    def _worker(index: int) -> None:
        repo = PostgresBackgroundJobRepository()
        barrier.wait()
        start = time.monotonic()
        claimed = repo.claim_next_pending(limit=n_jobs, worker_id=f"worker-{index}")
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
        fetched = PostgresBackgroundJobRepository().get_job(job.job_id)
        assert fetched.status == "CLAIMED"
        assert fetched.claimed_by is not None


# ---------------------------------------------------------------------
# Full lifecycle
# ---------------------------------------------------------------------


def test_full_lifecycle_happy_path():
    job = _submit()
    repo = PostgresBackgroundJobRepository()
    [claimed] = repo.claim_next_pending(limit=1, worker_id="worker-1")
    in_progress = repo.mark_in_progress(claimed.job_id)
    assert in_progress.status == "IN_PROGRESS"
    assert in_progress.attempt_count == 1

    invocation_id = identity.generate_id()
    succeeded = repo.mark_succeeded(job.job_id, ai_invocation_id=invocation_id)
    assert succeeded.status == "SUCCEEDED"
    assert succeeded.ai_invocation_id == invocation_id

    fetched = repo.get_job(job.job_id)
    assert fetched.status == "SUCCEEDED"


def test_retryable_failure_then_reclaim_then_succeeds():
    job = _submit(max_attempts=3)
    repo = PostgresBackgroundJobRepository()
    [claimed] = repo.claim_next_pending(limit=1, worker_id="worker-1")
    repo.mark_in_progress(claimed.job_id)
    failed = repo.mark_failed(job.job_id, error="transport timeout", retryable=True)
    assert failed.status == "FAILED_RETRYABLE"

    [reclaimed] = repo.claim_next_pending(limit=1, worker_id="worker-2")
    assert reclaimed.attempt_count == 1
    repo.mark_in_progress(reclaimed.job_id)
    succeeded = repo.mark_succeeded(job.job_id, ai_invocation_id=identity.generate_id())
    assert succeeded.status == "SUCCEEDED"


def test_retries_exhausted_then_terminal():
    job = _submit(max_attempts=2)
    repo = PostgresBackgroundJobRepository()
    for attempt in range(1, 3):
        [claimed] = repo.claim_next_pending(limit=1, worker_id="worker")
        in_progress = repo.mark_in_progress(claimed.job_id)
        assert in_progress.attempt_count == attempt
        failed = repo.mark_failed(job.job_id, error=f"attempt {attempt}", retryable=True)
    assert failed.status == "FAILED_TERMINAL"
    assert repo.get_job(job.job_id).status == "FAILED_TERMINAL"


def test_release_claim_returns_to_pending():
    job = _submit()
    repo = PostgresBackgroundJobRepository()
    [claimed] = repo.claim_next_pending(limit=1, worker_id="worker-1")
    released = repo.release_claim(claimed.job_id)
    assert released.status == "PENDING"
    assert released.claimed_by is None
    assert released.claimed_at is None


def test_invalid_transition_raises():
    job = _submit()
    repo = PostgresBackgroundJobRepository()
    with pytest.raises(InvalidStateTransitionError):
        repo.mark_in_progress(job.job_id)  # PENDING -> IN_PROGRESS is not a direct edge


# ---------------------------------------------------------------------
# Stale-claim recovery — real database, both CLAIMED and IN_PROGRESS
# ---------------------------------------------------------------------


def test_recover_stale_claims_recovers_a_stale_claimed_row_to_pending():
    audit = InMemoryAuditRepository()
    repo = PostgresBackgroundJobRepository(audit_repository=audit)
    job = _submit(repo, max_attempts=3)
    [claimed] = repo.claim_next_pending(limit=1, worker_id="worker-1")
    _force_claimed_at(claimed.job_id, utc_now() - datetime.timedelta(seconds=700))

    [recovered] = repo.recover_stale_claims(staleness_threshold_seconds=600.0)
    assert recovered.status == "PENDING"
    assert recovered.claimed_by is None

    events = audit.list_by_subject("BackgroundJob", job.job_id)
    assert [e.event_type for e in events] == [BACKGROUND_JOB_STALE_RECOVERY_AUDIT_EVENT_TYPE]


def test_recover_stale_claims_recovers_a_stale_in_progress_row_to_failed_retryable():
    audit = InMemoryAuditRepository()
    repo = PostgresBackgroundJobRepository(audit_repository=audit)
    job = _submit(repo, max_attempts=3)
    [claimed] = repo.claim_next_pending(limit=1, worker_id="worker-1")
    repo.mark_in_progress(claimed.job_id)
    _force_claimed_at(claimed.job_id, utc_now() - datetime.timedelta(seconds=700))

    [recovered] = repo.recover_stale_claims(staleness_threshold_seconds=600.0)
    assert recovered.status == "FAILED_RETRYABLE"
    assert recovered.attempt_count == 1

    events = audit.list_by_subject("BackgroundJob", job.job_id)
    assert [e.event_type for e in events] == [BACKGROUND_JOB_STALE_RECOVERY_AUDIT_EVENT_TYPE]


def test_recover_stale_claims_terminal_fails_when_budget_exhausted():
    repo = PostgresBackgroundJobRepository()
    job = _submit(repo, max_attempts=1)
    [claimed] = repo.claim_next_pending(limit=1, worker_id="worker-1")
    repo.mark_in_progress(claimed.job_id)
    _force_claimed_at(claimed.job_id, utc_now() - datetime.timedelta(seconds=700))

    [recovered] = repo.recover_stale_claims(staleness_threshold_seconds=600.0)
    assert recovered.status == "FAILED_TERMINAL"


def test_a_genuinely_recent_claim_is_not_recovered():
    repo = PostgresBackgroundJobRepository()
    job = _submit(repo)
    [claimed] = repo.claim_next_pending(limit=1, worker_id="worker-1")
    recovered = repo.recover_stale_claims(staleness_threshold_seconds=600.0)
    assert recovered == []
    assert repo.get_job(claimed.job_id).status == "CLAIMED"


def test_claim_next_pending_runs_recovery_first():
    repo = PostgresBackgroundJobRepository()
    _submit(repo)
    [claimed] = repo.claim_next_pending(limit=1, worker_id="worker-1")
    _force_claimed_at(claimed.job_id, utc_now() - datetime.timedelta(seconds=700))

    reclaimed = repo.claim_next_pending(limit=1, worker_id="worker-2")
    assert len(reclaimed) == 1
    assert reclaimed[0].claimed_by == "worker-2"


# ---------------------------------------------------------------------
# list_jobs
# ---------------------------------------------------------------------


def test_list_jobs_filters_by_status_and_task_id():
    repo = PostgresBackgroundJobRepository()
    job_a = _submit(repo, task_id="DOCUMENT_TYPE_PROPOSAL")
    job_b = _submit(repo, task_id="ENTITY_PROPOSAL")
    repo.claim_next_pending(limit=1, worker_id="w1")

    by_task = repo.list_jobs(task_id="ENTITY_PROPOSAL")
    assert [j.job_id for j in by_task] == [job_b.job_id]

    pending = repo.list_jobs(status="PENDING")
    assert len(pending) == 1
