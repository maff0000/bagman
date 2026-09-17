"""CD-6 Slice 3 PostgreSQL persistence proofs (Mailbox Management) —
``persistence/postgres/mailbox_repository.py`` against a REAL,
disposable PostgreSQL container (``tests/persistence/conftest.py``'s
session-scoped ``postgres_container`` fixture, migrated via a real
``alembic upgrade head``, never manual SQL). Mirrors
``tests/persistence/test_xero_repository.py``'s own style and rigor
for the closest structural precedent.
"""
from __future__ import annotations

import pytest
from sqlalchemy.exc import IntegrityError

from core import identity
from core.errors import ConflictError, InvalidStateTransitionError, NotFoundError
from core.timestamps import utc_now
from persistence.postgres.mailbox_models import MailboxSourceRow
from persistence.postgres.mailbox_repository import PostgresMailboxSourceRepository
from persistence.postgres.session import get_engine, session_scope
from services.mailbox.mailbox import CONNECTION_STATE_NOT_CONFIGURED, PROVIDER_IMAP, PROVIDER_MICROSOFT_GRAPH


def _email() -> str:
    return f"matt-{identity.generate_id()}@infosecurs.com"


# ---------------------------------------------------------------------
# Round-trip create/read
# ---------------------------------------------------------------------


def test_mailbox_persists_and_is_retrievable_via_a_fresh_repository_instance(fresh_engine):
    repo = PostgresMailboxSourceRepository()
    email = _email()
    created = repo.create_mailbox(
        display_name="Matt — Infosecurs", email_address=email, provider_kind=PROVIDER_MICROSOFT_GRAPH
    )
    assert created.status == "ACTIVE"
    assert created.connection_state == CONNECTION_STATE_NOT_CONFIGURED

    fresh_repo = PostgresMailboxSourceRepository(engine=fresh_engine)
    fetched = fresh_repo.get_mailbox(created.mailbox_id)
    assert fetched.email_address == email.lower()
    assert fetched.provider_kind == PROVIDER_MICROSOFT_GRAPH


def test_email_is_normalised_lowercase_at_rest():
    repo = PostgresMailboxSourceRepository()
    mixed_case = f"Matt.{identity.generate_id()}@Infosecurs.COM"
    created = repo.create_mailbox(display_name="Matt", email_address=mixed_case, provider_kind=PROVIDER_IMAP)
    assert created.email_address == mixed_case.lower()

    with session_scope(get_engine()) as session:
        row = session.get(MailboxSourceRow, created.mailbox_id)
        assert row.email_address == mixed_case.lower()


def test_created_and_updated_at_are_utc_and_equal_on_creation():
    repo = PostgresMailboxSourceRepository()
    created = repo.create_mailbox(display_name="Matt", email_address=_email(), provider_kind=PROVIDER_IMAP)
    assert created.created_at.tzinfo is not None
    assert created.updated_at.tzinfo is not None
    assert created.created_at == created.updated_at


# ---------------------------------------------------------------------
# Duplicate-email constraint — real DB-backed
# ---------------------------------------------------------------------


def test_email_uniqueness_is_enforced_at_the_database_level_not_only_in_application_code():
    """The real backstop, proven directly at the ORM/table level —
    bypassing `create_mailbox`'s own resolve-or-create logic entirely
    — so this test cannot pass merely because the repository method
    happens to check first."""
    email = _email()
    now = utc_now()
    row_1 = MailboxSourceRow(
        mailbox_id=identity.generate_id(), display_name="A", email_address=email, provider_kind=PROVIDER_IMAP,
        enabled=True, status="ACTIVE", connection_state=CONNECTION_STATE_NOT_CONFIGURED,
        created_at=now, updated_at=now, metadata_={},
    )
    row_2 = MailboxSourceRow(
        mailbox_id=identity.generate_id(), display_name="B", email_address=email, provider_kind=PROVIDER_IMAP,
        enabled=True, status="ACTIVE", connection_state=CONNECTION_STATE_NOT_CONFIGURED,
        created_at=now, updated_at=now, metadata_={},
    )
    with session_scope(get_engine()) as session:
        session.add(row_1)
    with pytest.raises(IntegrityError):
        with session_scope(get_engine()) as session:
            session.add(row_2)


def test_create_mailbox_via_repository_raises_conflict_error_for_a_duplicate_email():
    """The application-facing path translates the real constraint
    violation into `core.errors.ConflictError` — the same proof as the
    domain-layer in-memory test, against the real database's own
    constraint."""
    repo = PostgresMailboxSourceRepository()
    email = _email()
    repo.create_mailbox(display_name="First", email_address=email, provider_kind=PROVIDER_IMAP)
    with pytest.raises(ConflictError):
        repo.create_mailbox(display_name="Second", email_address=email.upper(), provider_kind=PROVIDER_MICROSOFT_GRAPH)


def test_email_uniqueness_is_not_freed_by_retirement_against_real_postgres():
    repo = PostgresMailboxSourceRepository()
    email = _email()
    original = repo.create_mailbox(display_name="Original", email_address=email, provider_kind=PROVIDER_IMAP)
    repo.retire_mailbox(original.mailbox_id)

    with pytest.raises(ConflictError):
        repo.create_mailbox(display_name="Re-registration attempt", email_address=email, provider_kind=PROVIDER_MICROSOFT_GRAPH)


def test_update_reassigning_email_to_another_mailboxs_address_raises_conflict():
    repo = PostgresMailboxSourceRepository()
    email_a, email_b = _email(), _email()
    repo.create_mailbox(display_name="A", email_address=email_a, provider_kind=PROVIDER_IMAP)
    mailbox_b = repo.create_mailbox(display_name="B", email_address=email_b, provider_kind=PROVIDER_IMAP)

    with pytest.raises(ConflictError):
        repo.update_mailbox(
            mailbox_b.mailbox_id,
            display_name="B",
            email_address=email_a,
            provider_kind=PROVIDER_IMAP,
            default_entity_id=None,
        )


# ---------------------------------------------------------------------
# Lifecycle persistence
# ---------------------------------------------------------------------


def test_lifecycle_transitions_persist_correctly(fresh_engine):
    repo = PostgresMailboxSourceRepository()
    mailbox = repo.create_mailbox(display_name="Matt", email_address=_email(), provider_kind=PROVIDER_IMAP)

    repo.disable_mailbox(mailbox.mailbox_id)
    fresh_repo = PostgresMailboxSourceRepository(engine=fresh_engine)
    fetched = fresh_repo.get_mailbox(mailbox.mailbox_id)
    assert fetched.status == "DISABLED"
    assert fetched.enabled is False

    repo.enable_mailbox(mailbox.mailbox_id)
    assert fresh_repo.get_mailbox(mailbox.mailbox_id).status == "ACTIVE"

    repo.retire_mailbox(mailbox.mailbox_id)
    retired = fresh_repo.get_mailbox(mailbox.mailbox_id)
    assert retired.status == "RETIRED"
    assert retired.enabled is False


def test_retired_is_terminal_against_real_postgres():
    repo = PostgresMailboxSourceRepository()
    mailbox = repo.create_mailbox(display_name="Matt", email_address=_email(), provider_kind=PROVIDER_IMAP)
    repo.retire_mailbox(mailbox.mailbox_id)
    with pytest.raises(InvalidStateTransitionError):
        repo.enable_mailbox(mailbox.mailbox_id)


def test_re_retiring_an_already_retired_mailbox_is_idempotent_against_real_postgres():
    """PL-review finding, proven against the real row-locked
    implementation: a second retire call on an already-RETIRED row
    (the same 'stale browser tab' race Slice 2 caught twice) must be a
    safe no-op, never InvalidStateTransitionError."""
    repo = PostgresMailboxSourceRepository()
    mailbox = repo.create_mailbox(display_name="Matt", email_address=_email(), provider_kind=PROVIDER_IMAP)
    retired_once = repo.retire_mailbox(mailbox.mailbox_id)

    retired_again = repo.retire_mailbox(mailbox.mailbox_id)
    assert retired_again.status == "RETIRED"
    assert retired_again.updated_at == retired_once.updated_at


def test_update_mailbox_full_replace_round_trips():
    repo = PostgresMailboxSourceRepository()
    mailbox = repo.create_mailbox(display_name="Original", email_address=_email(), provider_kind=PROVIDER_IMAP)
    updated = repo.update_mailbox(
        mailbox.mailbox_id,
        display_name="Renamed",
        email_address=mailbox.email_address,
        provider_kind=PROVIDER_MICROSOFT_GRAPH,
        default_entity_id=None,
    )
    assert updated.display_name == "Renamed"
    assert updated.provider_kind == PROVIDER_MICROSOFT_GRAPH
    assert repo.get_mailbox(mailbox.mailbox_id).display_name == "Renamed"


# ---------------------------------------------------------------------
# Reads
# ---------------------------------------------------------------------


def test_get_by_email_returns_none_not_notfounderror_for_an_unknown_address():
    repo = PostgresMailboxSourceRepository()
    assert repo.get_by_email("nobody@nowhere.example") is None


def test_malformed_mailbox_id_lookup_returns_not_found_never_persistence_error():
    repo = PostgresMailboxSourceRepository()
    with pytest.raises(NotFoundError):
        repo.get_mailbox("not-a-valid-uuid")


def test_list_mailboxes_ordered_by_display_name():
    repo = PostgresMailboxSourceRepository()
    unique = identity.generate_id()
    repo.create_mailbox(display_name=f"Zzz-{unique}", email_address=_email(), provider_kind=PROVIDER_IMAP)
    repo.create_mailbox(display_name=f"Aaa-{unique}", email_address=_email(), provider_kind=PROVIDER_IMAP)

    names = [m.display_name for m in repo.list_mailboxes() if unique in m.display_name]
    assert names == sorted(names)
