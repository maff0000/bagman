"""CD-6 Slice 4 PostgreSQL persistence proofs for
``persistence/postgres/mailbox_microsoft_repository.py`` (sweep runs,
folder cursors, sweep lease, mailbox-scoped OAuth state) against a
REAL, disposable PostgreSQL container.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from core import identity
from core.errors import NotFoundError, OAuthStateError
from persistence.postgres.mailbox_microsoft_repository import (
    PostgresMailboxFolderCursorRepository,
    PostgresMailboxMicrosoftOAuthStateRepository,
    PostgresMailboxSweepLock,
    PostgresMailboxSweepRunRepository,
)
from services.mailbox.lock import MailboxSweepLockError
from services.mailbox.microsoft.oauth_state import consume_state
from services.mailbox.sweep_run import TRIGGER_MANUAL


def _mailbox_id() -> str:
    return identity.generate_id()


# ---------------------------------------------------------------------
# MailboxSweepRun
# ---------------------------------------------------------------------


def test_sweep_run_persists_and_completes(fresh_engine):
    repo = PostgresMailboxSweepRunRepository()
    mailbox_id = _mailbox_id()
    run = repo.create_run(mailbox_id=mailbox_id, trigger=TRIGGER_MANUAL)
    assert run.status == "RUNNING"

    fresh_repo = PostgresMailboxSweepRunRepository(engine=fresh_engine)
    folders_attempted = [
        {"folder_id": "AAMkADinbox00000000000000000000", "display_name": "Inbox"},
        {"folder_id": "AAMkADjunkemail000000000000000", "display_name": "Junk Email"},
    ]
    completed = fresh_repo.complete_run(
        run.sweep_run_id, new_status="SUCCEEDED", folders_attempted=folders_attempted,
        messages_seen=3, messages_new=1, evidence_created=1, duplicates=2, quarantined=0, failures=0,
    )
    assert completed.status == "SUCCEEDED"
    assert completed.completed_at is not None

    fetched = fresh_repo.get_run(run.sweep_run_id)
    assert fetched.messages_seen == 3
    assert list(fetched.folders_attempted) == folders_attempted


def test_sweep_run_persists_operational_addendum_aggregate_fields(fresh_engine):
    """Operational addendum (ahead of the first real large historical
    sweep) — the seven new aggregate-reporting fields round-trip
    through a real, disposable PostgreSQL container."""
    repo = PostgresMailboxSweepRunRepository()
    mailbox_id = _mailbox_id()
    run = repo.create_run(mailbox_id=mailbox_id, trigger=TRIGGER_MANUAL)

    fresh_repo = PostgresMailboxSweepRunRepository(engine=fresh_engine)
    completed = fresh_repo.complete_run(
        run.sweep_run_id, new_status="SUCCEEDED",
        folders_attempted=[{"folder_id": "AAMkADinbox00000000000000000000", "display_name": "Inbox"}],
        messages_seen=4, messages_new=4, evidence_created=1, duplicates=0, quarantined=0, failures=0,
        unique_sender_domains=4, allowed_domain_messages=1, ignored_domain_messages=1,
        unknown_domain_messages=2, likely_financial_candidates=1, messages_with_attachments=1,
        graph_throttle_retries=1,
    )
    assert completed.unique_sender_domains == 4
    assert completed.allowed_domain_messages == 1
    assert completed.ignored_domain_messages == 1
    assert completed.unknown_domain_messages == 2
    assert completed.likely_financial_candidates == 1
    assert completed.messages_with_attachments == 1
    assert completed.graph_throttle_retries == 1

    fetched = fresh_repo.get_run(run.sweep_run_id)
    assert fetched.unique_sender_domains == 4
    assert fetched.graph_throttle_retries == 1


def test_sweep_run_operational_addendum_fields_default_to_zero_when_not_supplied():
    """A caller that doesn't pass the new addendum kwargs (e.g. older
    test coverage exercising only the original fields) still gets a
    real, honest `0` for every new field — never a validation failure."""
    repo = PostgresMailboxSweepRunRepository()
    mailbox_id = _mailbox_id()
    run = repo.create_run(mailbox_id=mailbox_id, trigger=TRIGGER_MANUAL)
    completed = repo.complete_run(
        run.sweep_run_id, new_status="SUCCEEDED", folders_attempted=[],
        messages_seen=0, messages_new=0, evidence_created=0, duplicates=0, quarantined=0, failures=0,
    )
    assert completed.unique_sender_domains == 0
    assert completed.allowed_domain_messages == 0
    assert completed.graph_throttle_retries == 0


def test_sweep_run_get_not_found_raises():
    repo = PostgresMailboxSweepRunRepository()
    with pytest.raises(NotFoundError):
        repo.get_run(identity.generate_id())


def test_list_runs_most_recent_first_scoped_to_mailbox():
    repo = PostgresMailboxSweepRunRepository()
    mailbox_id = _mailbox_id()
    other = _mailbox_id()
    r1 = repo.create_run(mailbox_id=mailbox_id, trigger=TRIGGER_MANUAL)
    r2 = repo.create_run(mailbox_id=mailbox_id, trigger=TRIGGER_MANUAL)
    repo.create_run(mailbox_id=other, trigger=TRIGGER_MANUAL)
    runs = repo.list_runs(mailbox_id=mailbox_id)
    assert {r.sweep_run_id for r in runs} == {r1.sweep_run_id, r2.sweep_run_id}


# ---------------------------------------------------------------------
# MailboxFolderCursor
# ---------------------------------------------------------------------


def test_cursor_round_trips_and_advances(fresh_engine):
    repo = PostgresMailboxFolderCursorRepository()
    mailbox_id = _mailbox_id()
    bootstrap_ts = datetime.now(timezone.utc) - timedelta(days=7)
    cursor = repo.get_or_bootstrap(
        mailbox_id=mailbox_id, provider_kind="MICROSOFT_GRAPH", folder="INBOX", bootstrap_timestamp=bootstrap_ts
    )
    assert cursor.delta_link is None

    fresh_repo = PostgresMailboxFolderCursorRepository(engine=fresh_engine)
    advanced = fresh_repo.advance_cursor(
        mailbox_id=mailbox_id, provider_kind="MICROSOFT_GRAPH", folder="INBOX", delta_link="opaque-token"
    )
    assert advanced.delta_link == "opaque-token"
    assert advanced.bootstrap_timestamp == bootstrap_ts


def test_cursor_preserves_original_bootstrap_timestamp_on_repeat_bootstrap():
    repo = PostgresMailboxFolderCursorRepository()
    mailbox_id = _mailbox_id()
    first_ts = datetime.now(timezone.utc) - timedelta(days=7)
    later_ts = datetime.now(timezone.utc)
    repo.get_or_bootstrap(mailbox_id=mailbox_id, provider_kind="MICROSOFT_GRAPH", folder="INBOX", bootstrap_timestamp=first_ts)
    second = repo.get_or_bootstrap(mailbox_id=mailbox_id, provider_kind="MICROSOFT_GRAPH", folder="INBOX", bootstrap_timestamp=later_ts)
    assert second.bootstrap_timestamp == first_ts


def test_advance_cursor_without_bootstrap_raises_not_found():
    repo = PostgresMailboxFolderCursorRepository()
    with pytest.raises(NotFoundError):
        repo.advance_cursor(mailbox_id=_mailbox_id(), provider_kind="MICROSOFT_GRAPH", folder="INBOX", delta_link="x")


# ---------------------------------------------------------------------
# MailboxSweepLock
# ---------------------------------------------------------------------


def test_lock_acquire_release_round_trip(fresh_engine):
    lock = PostgresMailboxSweepLock()
    mailbox_id = _mailbox_id()
    token = lock.try_acquire(mailbox_id)
    with pytest.raises(MailboxSweepLockError):
        PostgresMailboxSweepLock(engine=fresh_engine).try_acquire(mailbox_id)
    lock.release(mailbox_id, token)
    PostgresMailboxSweepLock(engine=fresh_engine).try_acquire(mailbox_id)  # must not raise


def test_expired_lease_is_reclaimable():
    import time

    lock = PostgresMailboxSweepLock()
    mailbox_id = _mailbox_id()
    lock.try_acquire(mailbox_id, lease_seconds=0.01)
    time.sleep(0.05)
    token = lock.try_acquire(mailbox_id)
    assert token


# ---------------------------------------------------------------------
# MailboxOAuthState (mailbox-scoped, Microsoft)
# ---------------------------------------------------------------------


def test_oauth_state_round_trips_and_consumes(fresh_engine):
    repo = PostgresMailboxMicrosoftOAuthStateRepository()
    mailbox_id = _mailbox_id()
    state = repo.create_state(mailbox_id=mailbox_id)

    fresh_repo = PostgresMailboxMicrosoftOAuthStateRepository(engine=fresh_engine)
    consumed = consume_state(fresh_repo, state.state)
    assert consumed.mailbox_id == mailbox_id
    assert consumed.is_consumed


def test_oauth_state_replay_is_rejected():
    repo = PostgresMailboxMicrosoftOAuthStateRepository()
    state = repo.create_state(mailbox_id=_mailbox_id())
    consume_state(repo, state.state)
    with pytest.raises(OAuthStateError):
        consume_state(repo, state.state)


def test_oauth_state_unknown_value_returns_none_on_get():
    repo = PostgresMailboxMicrosoftOAuthStateRepository()
    assert repo.get_state("never-minted") is None
