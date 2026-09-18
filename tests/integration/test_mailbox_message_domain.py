"""CD-6 Slice 4 tests for `services.mailbox.message` — canonical
(mailbox_id, immutable_provider_message_id) identity, resolve-or-create
`record_observation`, and the "never downgrade an already-INGESTED
row" rank discipline.
"""
from __future__ import annotations

from datetime import datetime, timezone

import pytest

from core import identity
from services.mailbox.message import (
    FOLDER_INBOX,
    FOLDER_JUNK,
    INGESTION_STATUS_CHECKED_NOT_CANDIDATE,
    INGESTION_STATUS_FAILED,
    INGESTION_STATUS_INGESTED,
    INGESTION_STATUS_QUARANTINED,
    INGESTION_STATUS_VANISHED,
    InMemoryMailboxMessageRepository,
)


@pytest.fixture
def repo() -> InMemoryMailboxMessageRepository:
    return InMemoryMailboxMessageRepository()


@pytest.fixture
def mailbox_id() -> str:
    return identity.generate_id()


@pytest.fixture
def other_mailbox_id() -> str:
    return identity.generate_id()


@pytest.fixture
def evidence_id() -> str:
    return identity.generate_id()


@pytest.fixture
def other_evidence_id() -> str:
    return identity.generate_id()


def _observe(repo, *, mailbox_id, provider_kind="MICROSOFT_GRAPH", msg_id="msg-1", folder=FOLDER_INBOX, status=INGESTION_STATUS_INGESTED, evidence_id=None):
    return repo.record_observation(
        mailbox_id=mailbox_id,
        provider_kind=provider_kind,
        immutable_provider_message_id=msg_id,
        internet_message_id="<a@b>",
        observed_folder=folder,
        subject="Hi",
        sender_address="s@x.com",
        sender_display_name="S",
        received_at=datetime.now(timezone.utc),
        has_attachments=False,
        ingestion_status=status,
        evidence_id=evidence_id,
    )


def test_unseen_tuple_creates_a_new_row(repo, mailbox_id):
    message, created = _observe(repo, mailbox_id=mailbox_id)
    assert created is True
    assert message.mailbox_id == mailbox_id
    assert message.immutable_provider_message_id == "msg-1"


def test_same_tuple_resolves_to_the_same_row_never_a_second_one(repo, mailbox_id, evidence_id):
    first, _ = _observe(repo, mailbox_id=mailbox_id, evidence_id=evidence_id)
    second, created = _observe(repo, mailbox_id=mailbox_id, evidence_id=evidence_id)
    assert created is False
    assert first.mailbox_message_id == second.mailbox_message_id


def test_message_moving_folders_never_creates_a_second_evidence_object(repo, mailbox_id, evidence_id):
    """A message moving Inbox <-> Junk must update observed_folder on
    the SAME row, never mint a second one (architect spec)."""
    first, _ = _observe(repo, mailbox_id=mailbox_id, folder=FOLDER_INBOX, status=INGESTION_STATUS_INGESTED, evidence_id=evidence_id)
    second, created = _observe(repo, mailbox_id=mailbox_id, folder=FOLDER_JUNK, status=INGESTION_STATUS_INGESTED, evidence_id=evidence_id)
    assert created is False
    assert second.mailbox_message_id == first.mailbox_message_id
    assert second.observed_folder == FOLDER_JUNK
    assert second.evidence_id == evidence_id

    all_rows = repo.list_messages(mailbox_id=mailbox_id)
    assert len(all_rows) == 1


def test_different_mailboxes_never_collide_on_the_same_provider_message_id(repo, mailbox_id, other_mailbox_id):
    _observe(repo, mailbox_id=mailbox_id, msg_id="shared-id")
    _observe(repo, mailbox_id=other_mailbox_id, msg_id="shared-id")
    assert len(repo.list_messages(mailbox_id=mailbox_id)) == 1
    assert len(repo.list_messages(mailbox_id=other_mailbox_id)) == 1


def test_ingested_status_is_never_downgraded_by_a_later_lesser_replay(repo, mailbox_id, evidence_id):
    _observe(repo, mailbox_id=mailbox_id, status=INGESTION_STATUS_INGESTED, evidence_id=evidence_id)
    updated, _ = _observe(repo, mailbox_id=mailbox_id, status=INGESTION_STATUS_VANISHED)
    assert updated.ingestion_status == INGESTION_STATUS_INGESTED
    assert updated.evidence_id == evidence_id


def test_quarantined_can_still_be_upgraded_to_ingested_by_a_later_retry(repo, mailbox_id, other_evidence_id):
    _observe(repo, mailbox_id=mailbox_id, status=INGESTION_STATUS_QUARANTINED)
    updated, _ = _observe(repo, mailbox_id=mailbox_id, status=INGESTION_STATUS_INGESTED, evidence_id=other_evidence_id)
    assert updated.ingestion_status == INGESTION_STATUS_INGESTED
    assert updated.evidence_id == other_evidence_id


def test_first_seen_at_never_changes_on_re_observation(repo, mailbox_id):
    first, _ = _observe(repo, mailbox_id=mailbox_id)
    second, _ = _observe(repo, mailbox_id=mailbox_id)
    assert second.first_seen_at == first.first_seen_at
    assert second.last_seen_at >= first.last_seen_at


def test_find_by_provider_id_returns_none_for_unknown_tuple(repo, mailbox_id):
    assert repo.find_by_provider_id(mailbox_id, "does-not-exist") is None


def test_list_messages_scoped_to_mailbox_most_recent_first(repo, mailbox_id, other_mailbox_id):
    _observe(repo, mailbox_id=mailbox_id, msg_id="m1")
    _observe(repo, mailbox_id=mailbox_id, msg_id="m2")
    _observe(repo, mailbox_id=other_mailbox_id, msg_id="m3")
    items = repo.list_messages(mailbox_id=mailbox_id)
    assert {m.immutable_provider_message_id for m in items} == {"m1", "m2"}


def test_failed_status_represents_a_governed_oversize_outcome(repo, mailbox_id):
    message, _ = _observe(repo, mailbox_id=mailbox_id, status=INGESTION_STATUS_FAILED)
    assert message.ingestion_status == INGESTION_STATUS_FAILED
    assert message.evidence_id is None


# ---------------------------------------------------------------------
# list_candidate_messages_for_domain (operational addendum, ahead of
# the first real large historical sweep) — proof #7 of the WO's
# required tests
# ---------------------------------------------------------------------


def _observe_candidate(
    repo, *, mailbox_id, msg_id, sender_domain, status=INGESTION_STATUS_CHECKED_NOT_CANDIDATE, received_at=None
):
    return repo.record_observation(
        mailbox_id=mailbox_id,
        provider_kind="MICROSOFT_GRAPH",
        immutable_provider_message_id=msg_id,
        internet_message_id=f"<{msg_id}@b>",
        observed_folder=FOLDER_INBOX,
        subject="Invoice",
        sender_address=f"billing@{sender_domain}",
        sender_display_name="Vendor",
        received_at=received_at or datetime.now(timezone.utc),
        has_attachments=False,
        ingestion_status=status,
        sender_domain=sender_domain,
    )


def test_list_candidate_messages_filters_by_mailbox_id_and_normalised_sender_domain(repo, mailbox_id, other_mailbox_id):
    _observe_candidate(repo, mailbox_id=mailbox_id, msg_id="m1", sender_domain="vendor.com")
    # Different casing/whitespace — must still match via normalisation.
    _observe_candidate(repo, mailbox_id=mailbox_id, msg_id="m2", sender_domain="VENDOR.com")
    # A different domain entirely — never matched.
    _observe_candidate(repo, mailbox_id=mailbox_id, msg_id="m3", sender_domain="other.example")
    # Same domain, but a DIFFERENT mailbox — never matched.
    _observe_candidate(repo, mailbox_id=other_mailbox_id, msg_id="m4", sender_domain="vendor.com")

    results = repo.list_candidate_messages_for_domain(mailbox_id=mailbox_id, sender_domain="Vendor.COM")
    assert {m.immutable_provider_message_id for m in results} == {"m1", "m2"}
    assert all(m.mailbox_id == mailbox_id for m in results)


def test_list_candidate_messages_excludes_every_status_except_checked_not_candidate(repo, mailbox_id):
    _observe_candidate(repo, mailbox_id=mailbox_id, msg_id="eligible", sender_domain="vendor.com")
    _observe_candidate(
        repo, mailbox_id=mailbox_id, msg_id="already-ingested", sender_domain="vendor.com",
        status=INGESTION_STATUS_INGESTED,
    )
    _observe_candidate(
        repo, mailbox_id=mailbox_id, msg_id="already-quarantined", sender_domain="vendor.com",
        status=INGESTION_STATUS_QUARANTINED,
    )
    _observe_candidate(
        repo, mailbox_id=mailbox_id, msg_id="already-failed", sender_domain="vendor.com",
        status=INGESTION_STATUS_FAILED,
    )
    _observe_candidate(
        repo, mailbox_id=mailbox_id, msg_id="already-vanished", sender_domain="vendor.com",
        status=INGESTION_STATUS_VANISHED,
    )

    results = repo.list_candidate_messages_for_domain(mailbox_id=mailbox_id, sender_domain="vendor.com")
    assert [m.immutable_provider_message_id for m in results] == ["eligible"]


def test_list_candidate_messages_ordered_oldest_received_first(repo, mailbox_id):
    older = datetime(2025, 1, 1, tzinfo=timezone.utc)
    newer = datetime(2025, 6, 1, tzinfo=timezone.utc)
    _observe_candidate(repo, mailbox_id=mailbox_id, msg_id="m-newer", sender_domain="vendor.com", received_at=newer)
    _observe_candidate(repo, mailbox_id=mailbox_id, msg_id="m-older", sender_domain="vendor.com", received_at=older)

    results = repo.list_candidate_messages_for_domain(mailbox_id=mailbox_id, sender_domain="vendor.com")
    assert [m.immutable_provider_message_id for m in results] == ["m-older", "m-newer"]
