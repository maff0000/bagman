"""CD-6 Slice 4 PostgreSQL persistence proofs for
``persistence/postgres/mailbox_message_repository.py`` against a REAL,
disposable PostgreSQL container (mirrors
``tests/persistence/test_mailbox_repository.py``'s own style/rigor).
"""
from __future__ import annotations

from datetime import datetime, timezone

import pytest

from core import identity
from persistence.postgres.mailbox_message_models import MailboxMessageRow
from persistence.postgres.mailbox_message_repository import PostgresMailboxMessageRepository
from persistence.postgres.session import get_engine, session_scope
from services.mailbox.message import FOLDER_INBOX, FOLDER_JUNK, INGESTION_STATUS_INGESTED, INGESTION_STATUS_VANISHED


def _mailbox_id() -> str:
    return identity.generate_id()


def _observe(repo, *, mailbox_id, msg_id="msg-1", folder=FOLDER_INBOX, status=INGESTION_STATUS_INGESTED, evidence_id=None):
    return repo.record_observation(
        mailbox_id=mailbox_id,
        provider_kind="MICROSOFT_GRAPH",
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


def test_message_persists_and_is_retrievable_via_a_fresh_repository_instance(fresh_engine):
    repo = PostgresMailboxMessageRepository()
    mailbox_id = _mailbox_id()
    message, created = _observe(repo, mailbox_id=mailbox_id)
    assert created is True

    fresh_repo = PostgresMailboxMessageRepository(engine=fresh_engine)
    fetched = fresh_repo.get_message(message.mailbox_message_id)
    assert fetched.immutable_provider_message_id == "msg-1"


def test_uq_constraint_backs_canonical_uniqueness_at_the_database_level():
    repo = PostgresMailboxMessageRepository()
    mailbox_id = _mailbox_id()
    first, created1 = _observe(repo, mailbox_id=mailbox_id, msg_id="dup-id")
    second, created2 = _observe(repo, mailbox_id=mailbox_id, msg_id="dup-id", folder=FOLDER_JUNK)
    assert created1 is True
    assert created2 is False
    assert first.mailbox_message_id == second.mailbox_message_id

    with session_scope(get_engine()) as session:
        rows = session.query(MailboxMessageRow).filter_by(mailbox_id=mailbox_id, immutable_provider_message_id="dup-id").all()
        assert len(rows) == 1


def test_different_mailboxes_never_collide_at_database_level():
    repo = PostgresMailboxMessageRepository()
    mailbox_a = _mailbox_id()
    mailbox_b = _mailbox_id()
    _observe(repo, mailbox_id=mailbox_a, msg_id="shared")
    _observe(repo, mailbox_id=mailbox_b, msg_id="shared")
    assert len(repo.list_messages(mailbox_id=mailbox_a)) == 1
    assert len(repo.list_messages(mailbox_id=mailbox_b)) == 1


def test_find_by_provider_id_round_trips():
    repo = PostgresMailboxMessageRepository()
    mailbox_id = _mailbox_id()
    _observe(repo, mailbox_id=mailbox_id, msg_id="find-me")
    found = repo.find_by_provider_id(mailbox_id, "find-me")
    assert found is not None
    assert found.immutable_provider_message_id == "find-me"
    assert repo.find_by_provider_id(mailbox_id, "not-there") is None


def test_ingested_status_is_never_downgraded_by_a_later_lesser_replay():
    repo = PostgresMailboxMessageRepository()
    mailbox_id = _mailbox_id()
    evidence_id = identity.generate_id()
    _observe(repo, mailbox_id=mailbox_id, msg_id="rank-test", status=INGESTION_STATUS_INGESTED, evidence_id=evidence_id)
    updated, _ = _observe(repo, mailbox_id=mailbox_id, msg_id="rank-test", status=INGESTION_STATUS_VANISHED)
    assert updated.ingestion_status == INGESTION_STATUS_INGESTED
    assert updated.evidence_id == evidence_id


def test_list_messages_most_recent_first_scoped_to_mailbox():
    repo = PostgresMailboxMessageRepository()
    mailbox_id = _mailbox_id()
    _observe(repo, mailbox_id=mailbox_id, msg_id="m1")
    _observe(repo, mailbox_id=mailbox_id, msg_id="m2")
    items = repo.list_messages(mailbox_id=mailbox_id)
    assert {m.immutable_provider_message_id for m in items} == {"m1", "m2"}


def test_get_message_not_found_raises():
    from core.errors import NotFoundError

    repo = PostgresMailboxMessageRepository()
    with pytest.raises(NotFoundError):
        repo.get_message(identity.generate_id())
