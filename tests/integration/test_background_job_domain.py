"""Domain-behaviour tests for `ai.jobs` (CD-6 §103 Inference
Architecture Ruling).

Exercises `InMemoryBackgroundJobRepository` and the pure
`transition_job()` state-machine helper directly — mirrors
`tests/integration/test_ai_invocation_domain.py`'s own structure,
adapted for `BackgroundJob`'s different state machine and its
idempotent-submission doctrine (a DIFFERENT doctrine from
`AIInvocation`'s own "new row every retry" concurrency guard — see
`ai.jobs`'s module docstring for the contrast).
"""
from __future__ import annotations

import datetime

import pytest

from ai.jobs import (
    BACKGROUND_JOB_ALLOWED_TRANSITIONS,
    BACKGROUND_JOB_STALE_RECOVERY_AUDIT_EVENT_TYPE,
    BACKGROUND_JOB_STATUSES,
    BACKGROUND_JOB_TERMINAL_STATUSES,
    InMemoryBackgroundJobRepository,
    is_stale_claim,
    transition_job,
)
from core import identity
from core.audit import InMemoryAuditRepository
from core.errors import InvalidStateTransitionError, NotFoundError, ValidationError
from core.timestamps import utc_now


@pytest.fixture
def audit() -> InMemoryAuditRepository:
    return InMemoryAuditRepository()


@pytest.fixture
def repo(audit) -> InMemoryBackgroundJobRepository:
    return InMemoryBackgroundJobRepository(audit_repository=audit)


def _submit(repo, *, idempotency_key=None, **overrides) -> "BackgroundJob":
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


# ---------------------------------------------------------------------
# Submission / idempotency
# ---------------------------------------------------------------------


def test_submit_job_starts_pending_with_zero_attempts(repo):
    job = _submit(repo)
    assert job.status == "PENDING"
    assert job.attempt_count == 0
    assert job.claimed_by is None
    assert job.claimed_at is None
    assert job.ai_invocation_id is None


def test_submit_job_is_idempotent_on_idempotency_key(repo):
    """A second submit_job call with the SAME idempotency_key returns
    the EXISTING row unchanged — never a second row (module docstring's
    own contrasting doctrine vs. AIInvocation.create_invocation)."""
    key = identity.generate_id()
    first = _submit(repo, idempotency_key=key)
    second = _submit(repo, idempotency_key=key, task_id="ENTITY_PROPOSAL")  # different args, same key
    assert second.job_id == first.job_id
    assert second.task_id == first.task_id  # the FIRST call's data wins, unchanged
    assert len(repo.list_jobs()) == 1


def test_submit_job_rejects_mac_local_backend(repo):
    """This first delivery ONLY authorises TRINITY_CORE_OVERFLOW jobs —
    see ai.jobs's module docstring's "Scope" section."""
    with pytest.raises(ValidationError):
        _submit(repo, inference_backend="MAC_LOCAL", capability_alias="bagman-fast")


def test_submit_job_rejects_any_alias_other_than_trinity_core(repo):
    with pytest.raises(ValidationError):
        _submit(repo, capability_alias="trinity-deep")


def test_submit_job_rejects_empty_idempotency_key(repo):
    with pytest.raises(ValidationError):
        repo.submit_job(
            idempotency_key="",
            task_id="DOCUMENT_TYPE_PROPOSAL",
            task_version=1,
            input_references={"evidence_id": identity.generate_id()},
            evidence_content="content",
            inference_backend="TRINITY_CORE_OVERFLOW",
            capability_alias="trinity-core",
            actor_type="SYSTEM",
            actor_id="bagman-test-harness",
        )


# ---------------------------------------------------------------------
# Full lifecycle — happy path
# ---------------------------------------------------------------------


def test_full_lifecycle_happy_path(repo):
    job = _submit(repo)
    [claimed] = repo.claim_next_pending(limit=1, worker_id="worker-1")
    assert claimed.status == "CLAIMED"
    assert claimed.claimed_by == "worker-1"
    assert claimed.attempt_count == 0  # not yet bumped — see module docstring

    in_progress = repo.mark_in_progress(claimed.job_id)
    assert in_progress.status == "IN_PROGRESS"
    assert in_progress.attempt_count == 1  # bumped exactly here

    invocation_id = identity.generate_id()
    succeeded = repo.mark_succeeded(in_progress.job_id, ai_invocation_id=invocation_id)
    assert succeeded.status == "SUCCEEDED"
    assert succeeded.ai_invocation_id == invocation_id
    assert succeeded.status in BACKGROUND_JOB_TERMINAL_STATUSES


def test_claim_next_pending_returns_fewer_than_limit_when_not_enough_available(repo):
    _submit(repo)
    claimed = repo.claim_next_pending(limit=5, worker_id="worker-1")
    assert len(claimed) == 1


def test_claim_next_pending_returns_empty_list_when_nothing_available(repo):
    assert repo.claim_next_pending(limit=5, worker_id="worker-1") == []


def test_claim_next_pending_claims_oldest_first(repo):
    job_a = _submit(repo)
    job_b = _submit(repo)
    claimed = repo.claim_next_pending(limit=2, worker_id="worker-1")
    assert [j.job_id for j in claimed] == [job_a.job_id, job_b.job_id]


# ---------------------------------------------------------------------
# Retry / terminal-failure semantics
# ---------------------------------------------------------------------


def test_retryable_failure_then_reclaim_then_succeeds(repo):
    job = _submit(repo, max_attempts=3)
    [claimed] = repo.claim_next_pending(limit=1, worker_id="worker-1")
    in_progress = repo.mark_in_progress(claimed.job_id)
    assert in_progress.attempt_count == 1

    failed = repo.mark_failed(in_progress.job_id, error="transport timeout", retryable=True)
    assert failed.status == "FAILED_RETRYABLE"
    assert failed.last_error == "transport timeout"

    [reclaimed] = repo.claim_next_pending(limit=1, worker_id="worker-2")
    assert reclaimed.status == "CLAIMED"
    assert reclaimed.attempt_count == 1  # unchanged at claim time — see module docstring

    in_progress_2 = repo.mark_in_progress(reclaimed.job_id)
    assert in_progress_2.attempt_count == 2

    succeeded = repo.mark_succeeded(in_progress_2.job_id, ai_invocation_id=identity.generate_id())
    assert succeeded.status == "SUCCEEDED"


def test_retries_exhausted_then_terminal(repo):
    job = _submit(repo, max_attempts=2)
    worker = "worker-1"

    for attempt in range(1, 3):  # attempts 1 and 2
        [claimed] = repo.claim_next_pending(limit=1, worker_id=worker)
        in_progress = repo.mark_in_progress(claimed.job_id)
        assert in_progress.attempt_count == attempt
        failed = repo.mark_failed(in_progress.job_id, error=f"attempt {attempt} failed", retryable=True)
        if attempt < 2:
            assert failed.status == "FAILED_RETRYABLE"
        else:
            # attempt_count (2) is no longer < max_attempts (2) — exhausted.
            assert failed.status == "FAILED_TERMINAL"

    final = repo.get_job(job.job_id)
    assert final.status == "FAILED_TERMINAL"
    assert final.status in BACKGROUND_JOB_TERMINAL_STATUSES


def test_non_retryable_failure_goes_straight_to_terminal_even_with_budget_remaining(repo):
    """A structured-output/schema-validation failure retrying the exact
    same content against the exact same model will never fix — see
    ai.jobs.BackgroundJobRepository.mark_failed's own docstring."""
    job = _submit(repo, max_attempts=5)
    [claimed] = repo.claim_next_pending(limit=1, worker_id="worker-1")
    repo.mark_in_progress(claimed.job_id)
    failed = repo.mark_failed(job.job_id, error="OUTPUT_SCHEMA_INVALID", retryable=False)
    assert failed.status == "FAILED_TERMINAL"


# ---------------------------------------------------------------------
# release_claim
# ---------------------------------------------------------------------


def test_release_claim_returns_to_pending_without_touching_attempt_count(repo):
    job = _submit(repo)
    [claimed] = repo.claim_next_pending(limit=1, worker_id="worker-1")
    released = repo.release_claim(claimed.job_id)
    assert released.status == "PENDING"
    assert released.claimed_by is None
    assert released.claimed_at is None
    assert released.attempt_count == 0


# ---------------------------------------------------------------------
# Stale-claim recovery — from BOTH CLAIMED and IN_PROGRESS
# ---------------------------------------------------------------------


def test_recover_stale_claims_recovers_a_stale_claimed_row_to_pending(repo, audit):
    job = _submit(repo, max_attempts=3)
    [claimed] = repo.claim_next_pending(limit=1, worker_id="worker-1")
    assert claimed.status == "CLAIMED"

    stale_now = utc_now() + datetime.timedelta(seconds=700)
    recovered_list = repo.recover_stale_claims(staleness_threshold_seconds=600.0, now=stale_now)
    assert len(recovered_list) == 1
    recovered = recovered_list[0]
    assert recovered.status == "PENDING"
    assert recovered.claimed_by is None
    assert recovered.attempt_count == 0  # never reached mark_in_progress, so unchanged

    events = audit.list_by_subject("BackgroundJob", job.job_id)
    assert [e.event_type for e in events] == [BACKGROUND_JOB_STALE_RECOVERY_AUDIT_EVENT_TYPE]


def test_recover_stale_claims_recovers_a_stale_in_progress_row_to_failed_retryable_when_budget_remains(repo, audit):
    """An IN_PROGRESS row (a genuine dispatch attempt already started,
    attempt_count already bumped by mark_in_progress) recovers to
    FAILED_RETRYABLE, not PENDING — IN_PROGRESS -> PENDING is not even
    a modelled transition; FAILED_RETRYABLE is the correct "claimable
    again after a consumed attempt" state (see
    ai.jobs.recover_stale_claim's own docstring)."""
    job = _submit(repo, max_attempts=3)
    [claimed] = repo.claim_next_pending(limit=1, worker_id="worker-1")
    in_progress = repo.mark_in_progress(claimed.job_id)
    assert in_progress.attempt_count == 1

    stale_now = utc_now() + datetime.timedelta(seconds=700)
    [recovered] = repo.recover_stale_claims(staleness_threshold_seconds=600.0, now=stale_now)
    assert recovered.status == "FAILED_RETRYABLE"
    assert recovered.attempt_count == 1  # already-consumed attempt is preserved

    events = audit.list_by_subject("BackgroundJob", job.job_id)
    assert [e.event_type for e in events] == [BACKGROUND_JOB_STALE_RECOVERY_AUDIT_EVENT_TYPE]

    # And it really is reclaimable again.
    [reclaimed] = repo.claim_next_pending(limit=1, worker_id="worker-2")
    assert reclaimed.status == "CLAIMED"


def test_recover_stale_claims_terminal_fails_when_attempt_budget_exhausted(repo):
    job = _submit(repo, max_attempts=1)
    [claimed] = repo.claim_next_pending(limit=1, worker_id="worker-1")
    in_progress = repo.mark_in_progress(claimed.job_id)
    assert in_progress.attempt_count == 1  # == max_attempts already

    stale_now = utc_now() + datetime.timedelta(seconds=700)
    [recovered] = repo.recover_stale_claims(staleness_threshold_seconds=600.0, now=stale_now)
    assert recovered.status == "FAILED_TERMINAL"


def test_a_genuinely_recent_claim_is_not_recovered(repo):
    _submit(repo)
    [claimed] = repo.claim_next_pending(limit=1, worker_id="worker-1")
    recovered_list = repo.recover_stale_claims(staleness_threshold_seconds=600.0)
    assert recovered_list == []
    assert repo.get_job(claimed.job_id).status == "CLAIMED"


def test_claim_next_pending_runs_recovery_first_and_can_reclaim_the_recovered_row(repo):
    _submit(repo)
    [claimed] = repo.claim_next_pending(limit=1, worker_id="worker-1")
    stale_now = utc_now() + datetime.timedelta(seconds=700)
    # Directly exercise the lazy-recovery-then-claim path inside claim_next_pending.
    reclaimed = repo.claim_next_pending(limit=1, worker_id="worker-2", now=stale_now)
    assert len(reclaimed) == 1
    assert reclaimed[0].claimed_by == "worker-2"


# ---------------------------------------------------------------------
# get_job / list_jobs
# ---------------------------------------------------------------------


def test_get_job_not_found_raises(repo):
    with pytest.raises(NotFoundError):
        repo.get_job(identity.generate_id())


def test_list_jobs_orders_most_recently_created_first(repo):
    job_a = _submit(repo)
    job_b = _submit(repo)
    listed = repo.list_jobs()
    assert [j.job_id for j in listed] == [job_b.job_id, job_a.job_id]


def test_list_jobs_filters_by_status(repo):
    _submit(repo)
    job_b = _submit(repo)
    repo.claim_next_pending(limit=1, worker_id="w1")
    # Whichever job got claimed first (oldest), filter for PENDING should
    # exclude it and keep exactly the other one.
    pending = repo.list_jobs(status="PENDING")
    assert len(pending) == 1


# ---------------------------------------------------------------------
# transition_job / state machine
# ---------------------------------------------------------------------


def test_transition_job_rejects_invalid_transition(repo):
    job = _submit(repo)
    with pytest.raises(InvalidStateTransitionError):
        transition_job(job, "SUCCEEDED")  # PENDING -> SUCCEEDED is not allowed


def test_transition_job_rejects_explicit_updated_at():
    job = _submit(InMemoryBackgroundJobRepository())
    with pytest.raises(ValidationError):
        transition_job(job, "CLAIMED", updated_at=utc_now())


def test_every_status_is_a_member_of_the_closed_set():
    for status in BACKGROUND_JOB_ALLOWED_TRANSITIONS:
        assert status in BACKGROUND_JOB_STATUSES


def test_is_stale_claim_is_false_for_terminal_statuses():
    job = _submit(InMemoryBackgroundJobRepository())
    terminal = transition_job(
        transition_job(job, "CLAIMED", claimed_by="w", claimed_at=utc_now() - datetime.timedelta(seconds=9999)),
        "IN_PROGRESS",
        attempt_count=1,
    )
    succeeded = transition_job(terminal, "SUCCEEDED", ai_invocation_id=identity.generate_id())
    assert is_stale_claim(succeeded, staleness_threshold_seconds=1.0) is False
