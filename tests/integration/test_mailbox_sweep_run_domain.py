"""CD-6 Slice 4 tests for `services.mailbox.sweep_run.MailboxSweepRun`
— the sweep-attempt ledger's own closed state machine, mirroring
`services.xero.sync.XeroSyncRun`'s own tested shape.

The stale-`RUNNING`-recovery tests below (Slice 3/4/5 governance-
reconciliation delta) prove the real, independently-found gap this
delta closes: a `MailboxSweepRun` abandoned by a killed process
previously had no path back to a terminal state at all."""
from __future__ import annotations

from datetime import timedelta

import pytest

from core import identity
from core.errors import InvalidStateTransitionError
from core.timestamps import utc_now
from services.mailbox.sweep_run import (
    STALE_RUNNING_THRESHOLD_SECONDS,
    InMemoryMailboxSweepRunRepository,
    SweepFailureReason,
    TRIGGER_MANUAL,
    is_stale_running,
    recover_stale_run,
)


@pytest.fixture
def repo() -> InMemoryMailboxSweepRunRepository:
    return InMemoryMailboxSweepRunRepository()


@pytest.fixture
def mailbox_id() -> str:
    return identity.generate_id()


def test_create_run_starts_running(repo, mailbox_id):
    run = repo.create_run(mailbox_id=mailbox_id, trigger=TRIGGER_MANUAL)
    assert run.status == "RUNNING"
    assert run.completed_at is None


def test_complete_run_succeeded_stamps_completed_at(repo, mailbox_id):
    run = repo.create_run(mailbox_id=mailbox_id, trigger=TRIGGER_MANUAL)
    completed = repo.complete_run(
        run.sweep_run_id, new_status="SUCCEEDED",
        folders_attempted=[
            {"folder_id": "AAMkADinbox00000000000000000000", "display_name": "Inbox"},
            {"folder_id": "AAMkADjunkemail000000000000000", "display_name": "Junk Email"},
        ],
        messages_seen=5, messages_new=2, evidence_created=2, duplicates=3, quarantined=0, failures=0,
    )
    assert completed.status == "SUCCEEDED"
    assert completed.completed_at is not None
    assert completed.messages_seen == 5


def test_complete_run_partial_carries_error_code(repo, mailbox_id):
    run = repo.create_run(mailbox_id=mailbox_id, trigger=TRIGGER_MANUAL)
    completed = repo.complete_run(
        run.sweep_run_id, new_status="PARTIAL",
        folders_attempted=[{"folder_id": "AAMkADinbox00000000000000000000", "display_name": "Inbox"}],
        messages_seen=1, messages_new=1, evidence_created=1, duplicates=0, quarantined=0, failures=1,
        error_code=SweepFailureReason.PARTIAL_FAILURES, error_detail="one message failed",
    )
    assert completed.status == "PARTIAL"
    assert completed.error_code == SweepFailureReason.PARTIAL_FAILURES


def test_a_terminal_run_can_never_transition_again(repo, mailbox_id):
    run = repo.create_run(mailbox_id=mailbox_id, trigger=TRIGGER_MANUAL)
    repo.complete_run(
        run.sweep_run_id, new_status="SUCCEEDED", folders_attempted=[], messages_seen=0, messages_new=0,
        evidence_created=0, duplicates=0, quarantined=0, failures=0,
    )
    with pytest.raises(InvalidStateTransitionError):
        repo.complete_run(
            run.sweep_run_id, new_status="FAILED", folders_attempted=[], messages_seen=0, messages_new=0,
            evidence_created=0, duplicates=0, quarantined=0, failures=1, error_code="X", error_detail="y",
        )


def test_list_runs_most_recent_first_scoped_to_mailbox(repo, mailbox_id):
    other_mailbox_id = identity.generate_id()
    r1 = repo.create_run(mailbox_id=mailbox_id, trigger=TRIGGER_MANUAL)
    r2 = repo.create_run(mailbox_id=mailbox_id, trigger=TRIGGER_MANUAL)
    repo.create_run(mailbox_id=other_mailbox_id, trigger=TRIGGER_MANUAL)
    runs = repo.list_runs(mailbox_id=mailbox_id)
    assert {r.sweep_run_id for r in runs} == {r1.sweep_run_id, r2.sweep_run_id}
    assert runs[0].started_at >= runs[1].started_at


def test_list_runs_respects_limit(repo, mailbox_id):
    for _ in range(5):
        repo.create_run(mailbox_id=mailbox_id, trigger=TRIGGER_MANUAL)
    assert len(repo.list_runs(mailbox_id=mailbox_id, limit=2)) == 2


# ---------------------------------------------------------------------
# Stale-`RUNNING` recovery (Slice 3/4/5 governance-reconciliation delta)
# ---------------------------------------------------------------------


def test_is_stale_running_false_for_a_fresh_run(repo, mailbox_id):
    run = repo.create_run(mailbox_id=mailbox_id, trigger=TRIGGER_MANUAL)
    assert is_stale_running(run) is False


def test_is_stale_running_false_for_a_terminal_run_even_if_old(repo, mailbox_id):
    run = repo.create_run(mailbox_id=mailbox_id, trigger=TRIGGER_MANUAL)
    completed = repo.complete_run(
        run.sweep_run_id, new_status="SUCCEEDED", folders_attempted=[], messages_seen=0, messages_new=0,
        evidence_created=0, duplicates=0, quarantined=0, failures=0,
    )
    far_future = utc_now() + timedelta(seconds=STALE_RUNNING_THRESHOLD_SECONDS * 10)
    assert is_stale_running(completed, now=far_future) is False


def test_is_stale_running_true_once_the_threshold_is_exceeded(repo, mailbox_id):
    run = repo.create_run(mailbox_id=mailbox_id, trigger=TRIGGER_MANUAL)
    just_past_threshold = run.started_at + timedelta(seconds=STALE_RUNNING_THRESHOLD_SECONDS + 1)
    assert is_stale_running(run, now=just_past_threshold) is True


def test_is_stale_running_false_just_under_the_threshold(repo, mailbox_id):
    run = repo.create_run(mailbox_id=mailbox_id, trigger=TRIGGER_MANUAL)
    just_under_threshold = run.started_at + timedelta(seconds=STALE_RUNNING_THRESHOLD_SECONDS - 1)
    assert is_stale_running(run, now=just_under_threshold) is False


def test_recover_stale_run_always_targets_failed_never_succeeded_or_partial(repo, mailbox_id):
    run = repo.create_run(mailbox_id=mailbox_id, trigger=TRIGGER_MANUAL)
    recovered = recover_stale_run(run)
    assert recovered.status == "FAILED"
    assert recovered.error_code == SweepFailureReason.STALE_RECOVERY_TIMEOUT
    assert recovered.completed_at is not None
    # Every other field preserved exactly as it was when abandoned —
    # never fabricated to look like a completed sweep.
    assert recovered.messages_seen == run.messages_seen == 0


def test_recover_stale_runs_recovers_only_the_stale_run_for_this_mailbox(repo, mailbox_id):
    other_mailbox_id = identity.generate_id()
    stale_run = repo.create_run(mailbox_id=mailbox_id, trigger=TRIGGER_MANUAL)
    fresh_run = repo.create_run(mailbox_id=mailbox_id, trigger=TRIGGER_MANUAL)
    other_mailbox_stale_run = repo.create_run(mailbox_id=other_mailbox_id, trigger=TRIGGER_MANUAL)

    far_future = utc_now() + timedelta(seconds=STALE_RUNNING_THRESHOLD_SECONDS * 2)
    recovered = repo.recover_stale_runs(mailbox_id=mailbox_id, now=far_future)

    assert {r.sweep_run_id for r in recovered} == {stale_run.sweep_run_id, fresh_run.sweep_run_id}
    for r in recovered:
        assert r.status == "FAILED"
        assert r.error_code == SweepFailureReason.STALE_RECOVERY_TIMEOUT

    # A different mailbox's own stale run is untouched by this call —
    # recover_stale_runs is scoped, never a global sweep.
    untouched = repo.get_run(other_mailbox_stale_run.sweep_run_id)
    assert untouched.status == "RUNNING"


def test_recover_stale_runs_is_a_genuine_noop_when_nothing_is_stale(repo, mailbox_id):
    repo.create_run(mailbox_id=mailbox_id, trigger=TRIGGER_MANUAL)
    assert repo.recover_stale_runs(mailbox_id=mailbox_id) == []


def test_recovered_run_can_never_transition_again(repo, mailbox_id):
    run = repo.create_run(mailbox_id=mailbox_id, trigger=TRIGGER_MANUAL)
    far_future = utc_now() + timedelta(seconds=STALE_RUNNING_THRESHOLD_SECONDS * 2)
    repo.recover_stale_runs(mailbox_id=mailbox_id, now=far_future)
    recovered = repo.get_run(run.sweep_run_id)
    with pytest.raises(InvalidStateTransitionError):
        repo.complete_run(
            recovered.sweep_run_id, new_status="SUCCEEDED", folders_attempted=[], messages_seen=0,
            messages_new=0, evidence_created=0, duplicates=0, quarantined=0, failures=0,
        )
