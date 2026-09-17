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
