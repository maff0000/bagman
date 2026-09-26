"""CD-6 Slice 4 tests for `services.mailbox.cursor.MailboxFolderCursor`
(opaque delta-link persistence, permanent bootstrap_timestamp) and
`services.mailbox.lock.MailboxSweepLock` (per-mailbox sweep
exclusivity lease, including expiry-based crash recovery).
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from core.errors import NotFoundError
from services.mailbox.cursor import InMemoryMailboxFolderCursorRepository
from services.mailbox.lock import InMemoryMailboxSweepLock, MailboxSweepLockError


# ---------------------------------------------------------------------
# MailboxFolderCursor
# ---------------------------------------------------------------------


@pytest.fixture
def cursor_repo() -> InMemoryMailboxFolderCursorRepository:
    return InMemoryMailboxFolderCursorRepository()


def test_get_or_bootstrap_creates_a_fresh_cursor_with_no_delta_link(cursor_repo):
    bootstrap_ts = datetime.now(timezone.utc) - timedelta(days=7)
    cursor = cursor_repo.get_or_bootstrap(
        mailbox_id="mb-1", provider_kind="MICROSOFT_GRAPH", folder="INBOX", bootstrap_timestamp=bootstrap_ts
    )
    assert cursor.delta_link is None
    assert cursor.bootstrap_timestamp == bootstrap_ts


def test_get_or_bootstrap_is_idempotent_and_preserves_original_bootstrap_timestamp(cursor_repo):
    first_ts = datetime.now(timezone.utc) - timedelta(days=7)
    later_ts = datetime.now(timezone.utc) - timedelta(days=1)
    first = cursor_repo.get_or_bootstrap(
        mailbox_id="mb-1", provider_kind="MICROSOFT_GRAPH", folder="INBOX", bootstrap_timestamp=first_ts
    )
    second = cursor_repo.get_or_bootstrap(
        mailbox_id="mb-1", provider_kind="MICROSOFT_GRAPH", folder="INBOX", bootstrap_timestamp=later_ts
    )
    assert second.bootstrap_timestamp == first.bootstrap_timestamp == first_ts


def test_inbox_and_junk_cursors_are_independent(cursor_repo):
    ts = datetime.now(timezone.utc)
    cursor_repo.get_or_bootstrap(mailbox_id="mb-1", provider_kind="MICROSOFT_GRAPH", folder="INBOX", bootstrap_timestamp=ts)
    cursor_repo.advance_cursor(mailbox_id="mb-1", provider_kind="MICROSOFT_GRAPH", folder="INBOX", delta_link="d-inbox")
    junk = cursor_repo.get_or_bootstrap(mailbox_id="mb-1", provider_kind="MICROSOFT_GRAPH", folder="JUNK", bootstrap_timestamp=ts)
    assert junk.delta_link is None


def test_advance_cursor_replaces_delta_link_opaquely(cursor_repo):
    ts = datetime.now(timezone.utc)
    cursor_repo.get_or_bootstrap(mailbox_id="mb-1", provider_kind="MICROSOFT_GRAPH", folder="INBOX", bootstrap_timestamp=ts)
    updated = cursor_repo.advance_cursor(mailbox_id="mb-1", provider_kind="MICROSOFT_GRAPH", folder="INBOX", delta_link="opaque-token-1")
    assert updated.delta_link == "opaque-token-1"
    # bootstrap_timestamp is a SEPARATE, permanently-preserved field.
    assert updated.bootstrap_timestamp == ts


def test_advance_cursor_without_bootstrap_first_raises_not_found(cursor_repo):
    with pytest.raises(NotFoundError):
        cursor_repo.advance_cursor(mailbox_id="mb-unknown", provider_kind="MICROSOFT_GRAPH", folder="INBOX", delta_link="x")


# ---------------------------------------------------------------------
# MailboxSweepLock
# ---------------------------------------------------------------------


@pytest.fixture
def lock() -> InMemoryMailboxSweepLock:
    return InMemoryMailboxSweepLock()


def test_acquire_then_release_allows_a_second_acquire(lock):
    token = lock.try_acquire("mb-1")
    lock.release("mb-1", token)
    token2 = lock.try_acquire("mb-1")
    assert token2 != token


def test_second_concurrent_acquire_declines_cleanly(lock):
    lock.try_acquire("mb-1")
    with pytest.raises(MailboxSweepLockError):
        lock.try_acquire("mb-1")


def test_release_with_wrong_token_is_a_safe_no_op(lock):
    token = lock.try_acquire("mb-1")
    lock.release("mb-1", "not-the-real-token")
    # Still held — a stale/wrong token must never release someone else's lease.
    with pytest.raises(MailboxSweepLockError):
        lock.try_acquire("mb-1")
    lock.release("mb-1", token)


def test_expired_lease_is_safely_reclaimable_without_an_operator_unlock(lock):
    lock.try_acquire("mb-1", lease_seconds=0.01)
    import time

    time.sleep(0.02)
    token = lock.try_acquire("mb-1")  # must not raise — the old lease has expired
    assert token


def test_different_mailboxes_never_contend_for_the_same_lease(lock):
    lock.try_acquire("mb-1")
    lock.try_acquire("mb-2")  # must not raise


def test_held_context_manager_always_releases_even_on_exception(lock):
    with pytest.raises(RuntimeError):
        with lock.held("mb-1"):
            raise RuntimeError("boom")
    # Released — a fresh acquire must succeed.
    lock.try_acquire("mb-1")
