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


def test_discovery_fields_are_preserved_on_a_benign_reobservation_that_omits_them():
    """Second latent defect fix (WO instruction) — at the Postgres layer
    too: a re-observation that omits discovery_candidate/
    discovery_reason/discovery_checked_at must not blank an already-set
    value on the existing row."""
    repo = PostgresMailboxMessageRepository()
    mailbox_id = _mailbox_id()
    checked_at = datetime(2026, 1, 1, tzinfo=timezone.utc)
    first, _ = repo.record_observation(
        mailbox_id=mailbox_id,
        provider_kind="MICROSOFT_GRAPH",
        immutable_provider_message_id="m1",
        internet_message_id="<m1@b>",
        observed_folder=FOLDER_INBOX,
        subject="Invoice",
        sender_address="billing@vendor.com",
        sender_display_name="Vendor",
        received_at=datetime.now(timezone.utc),
        has_attachments=False,
        ingestion_status="CHECKED_NOT_CANDIDATE",
        sender_domain="vendor.com",
        discovery_candidate=True,
        discovery_reason="subject contains keyword 'invoice'",
        discovery_checked_at=checked_at,
    )
    assert first.discovery_candidate is True

    second, created = repo.record_observation(
        mailbox_id=mailbox_id,
        provider_kind="MICROSOFT_GRAPH",
        immutable_provider_message_id="m1",
        internet_message_id="<m1@b>",
        observed_folder=FOLDER_INBOX,
        subject="Invoice",
        sender_address="billing@vendor.com",
        sender_display_name="Vendor",
        received_at=datetime.now(timezone.utc),
        has_attachments=False,
        ingestion_status="CHECKED_NOT_CANDIDATE",
        sender_domain="vendor.com",
    )
    assert created is False
    assert second.discovery_candidate is True
    assert second.discovery_reason == "subject contains keyword 'invoice'"
    assert second.discovery_checked_at == checked_at


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


# ---------------------------------------------------------------------
# list_candidate_messages_for_domain (operational addendum, ahead of
# the first real large historical sweep)
# ---------------------------------------------------------------------


def _observe_candidate(
    repo, *, mailbox_id, msg_id, sender_domain, status="CHECKED_NOT_CANDIDATE", discovery_candidate=True
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
        received_at=datetime.now(timezone.utc),
        has_attachments=False,
        ingestion_status=status,
        sender_domain=sender_domain,
        discovery_candidate=discovery_candidate,
        discovery_reason="subject contains keyword 'invoice'" if discovery_candidate else None,
    )


def test_list_candidate_messages_for_domain_filters_mailbox_domain_and_status(fresh_engine):
    repo = PostgresMailboxMessageRepository()
    mailbox_id = _mailbox_id()
    other_mailbox_id = _mailbox_id()
    _observe_candidate(repo, mailbox_id=mailbox_id, msg_id="eligible", sender_domain="vendor.com")
    _observe_candidate(repo, mailbox_id=mailbox_id, msg_id="eligible-2", sender_domain="VENDOR.COM")
    _observe_candidate(repo, mailbox_id=mailbox_id, msg_id="other-domain", sender_domain="other.example")
    _observe_candidate(repo, mailbox_id=other_mailbox_id, msg_id="other-mailbox", sender_domain="vendor.com")
    _observe_candidate(
        repo, mailbox_id=mailbox_id, msg_id="already-ingested", sender_domain="vendor.com",
        status=INGESTION_STATUS_INGESTED,
    )

    fresh_repo = PostgresMailboxMessageRepository(engine=fresh_engine)
    results = fresh_repo.list_candidate_messages_for_domain(mailbox_id=mailbox_id, sender_domain="Vendor.com")
    assert {m.immutable_provider_message_id for m in results} == {"eligible", "eligible-2"}
    assert all(m.mailbox_id == mailbox_id for m in results)


def test_list_candidate_messages_for_domain_requires_discovery_candidate_true_at_the_sql_level(fresh_engine):
    """Second CD-6 architect amendment (persisted discovery decision) —
    an IGNORED-domain message and an ordinary non-credible UNKNOWN-domain
    message can share the identical CHECKED_NOT_CANDIDATE status a real
    candidate has; only `discovery_candidate is True` (filtered in SQL,
    not Python) may make a message eligible for back-processing."""
    repo = PostgresMailboxMessageRepository()
    mailbox_id = _mailbox_id()
    _observe_candidate(repo, mailbox_id=mailbox_id, msg_id="real-candidate", sender_domain="vendor.com", discovery_candidate=True)
    _observe_candidate(repo, mailbox_id=mailbox_id, msg_id="ignored-domain", sender_domain="vendor.com", discovery_candidate=None)
    _observe_candidate(repo, mailbox_id=mailbox_id, msg_id="non-credible", sender_domain="vendor.com", discovery_candidate=False)

    fresh_repo = PostgresMailboxMessageRepository(engine=fresh_engine)
    results = fresh_repo.list_candidate_messages_for_domain(mailbox_id=mailbox_id, sender_domain="vendor.com")
    assert {m.immutable_provider_message_id for m in results} == {"real-candidate"}
