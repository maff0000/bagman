"""CD-6 Slice 4 tests for `services.mailbox.sweep_run.MailboxSweepRun`
— the sweep-attempt ledger's own closed state machine, mirroring
`services.xero.sync.XeroSyncRun`'s own tested shape.
"""
from __future__ import annotations

import pytest

from core import identity
from core.errors import InvalidStateTransitionError
from services.mailbox.sweep_run import (
    InMemoryMailboxSweepRunRepository,
    SweepFailureReason,
    TRIGGER_MANUAL,
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
        run.sweep_run_id, new_status="SUCCEEDED", folders_attempted=["INBOX", "JUNK"],
        messages_seen=5, messages_new=2, evidence_created=2, duplicates=3, quarantined=0, failures=0,
    )
    assert completed.status == "SUCCEEDED"
    assert completed.completed_at is not None
    assert completed.messages_seen == 5


def test_complete_run_partial_carries_error_code(repo, mailbox_id):
    run = repo.create_run(mailbox_id=mailbox_id, trigger=TRIGGER_MANUAL)
    completed = repo.complete_run(
        run.sweep_run_id, new_status="PARTIAL", folders_attempted=["INBOX"],
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
