"""CD-6 GUI-operations-foundation follow-on WO — real-Postgres
persistence proofs for
``persistence/postgres/mailbox_gmail_repository.py::PostgresMailboxGmailOAuthStateRepository``.
Mirrors ``tests/persistence/test_mailbox_microsoft_repository.py``'s
own "MailboxOAuthState (mailbox-scoped, Microsoft)" section — same
`fresh_engine` fixture usage, same plain `identity.generate_id()`
`mailbox_id` (the table carries no foreign-key constraint on
`mailbox_id`, exactly like `mailbox_microsoft_oauth_states` — see
`persistence/postgres/mailbox_gmail_models.py`) — against the REAL
disposable PostgreSQL container this directory's `conftest.py` starts
for the whole session, PLUS the WO's own additional adversarial
coverage (expiry, wrong-mailbox identifiability, primary-key
collision).

The acceptance-critical test in this file is
``test_state_created_by_one_repository_instance_is_consumable_by_a_
fresh_instance_against_the_same_database`` — the whole reason this WO
exists: the Gmail adapter's OAuth CSRF-state store must survive a
process restart/deploy mid-flow, exactly like Microsoft's own
Postgres-backed precedent (`test_oauth_state_round_trips_and_consumes`
in the file this mirrors) already does.
"""
from __future__ import annotations

from datetime import timedelta

import pytest
from sqlalchemy.exc import IntegrityError

from core import identity
from core.errors import NotFoundError, OAuthStateError
from core.timestamps import utc_now
from persistence.postgres.mailbox_gmail_models import MailboxGmailOAuthStateRow
from persistence.postgres.mailbox_gmail_repository import PostgresMailboxGmailOAuthStateRepository
from persistence.postgres.session import get_engine, session_scope
from services.mailbox.gmail import oauth_state as gmail_oauth_state
from services.mailbox.gmail.oauth_state import consume_state


def _mailbox_id() -> str:
    return identity.generate_id()


# ---------------------------------------------------------------------
# Restart-survival acceptance test — the actual defect this WO closes
# ---------------------------------------------------------------------


def test_state_created_by_one_repository_instance_is_consumable_by_a_fresh_instance_against_the_same_database(
    fresh_engine,
):
    mailbox_id = _mailbox_id()

    # Instance A: creates the state, then is discarded entirely — never
    # reused below.
    repository_a = PostgresMailboxGmailOAuthStateRepository(get_engine())
    created = repository_a.create_state(mailbox_id=mailbox_id)
    state_value = created.state
    del repository_a

    # Instance B: a brand-new repository object, constructed fresh,
    # against the SAME underlying database — but through `fresh_engine`,
    # its OWN independently-constructed `Engine`/connection pool (built
    # via a bare `create_engine`, never through the process-wide
    # `get_engine()` cache instance A used above — see that fixture's
    # own docstring in conftest.py). Since instance A and instance B
    # share no Python object at all and only ever communicate through
    # the database itself, instance B successfully consuming what
    # instance A created IS a valid simulation of "the state token
    # outlives the process that created it."
    repository_b = PostgresMailboxGmailOAuthStateRepository(fresh_engine)
    consumed = consume_state(repository_b, state_value)

    assert consumed.state == state_value
    assert consumed.mailbox_id == mailbox_id
    assert consumed.is_consumed


# ---------------------------------------------------------------------
# Adversarial tests
# ---------------------------------------------------------------------


def test_oauth_state_unknown_value_is_rejected():
    repo = PostgresMailboxGmailOAuthStateRepository()
    assert repo.get_state("never-minted-value") is None
    with pytest.raises(OAuthStateError):
        consume_state(repo, "never-minted-value")
    with pytest.raises(NotFoundError):
        repo.mark_consumed("never-minted-value")


def test_oauth_state_expired_is_rejected():
    repo = PostgresMailboxGmailOAuthStateRepository()
    created = repo.create_state(mailbox_id=_mailbox_id())

    # `mark_consumed`/`consume_state` both accept a `now` override for
    # testability — mirrors
    # `PostgresMailboxMicrosoftOAuthStateRepository.mark_consumed`'s own
    # signature exactly.
    past_expiry_now = created.expires_at + timedelta(seconds=1)
    with pytest.raises(OAuthStateError):
        consume_state(repo, created.state, now=past_expiry_now)


def test_oauth_state_bound_mailbox_is_identifiable_as_a_mismatch_for_a_different_mailbox():
    """The CALLER's responsibility (confirmed against
    `app/api/routers/mailboxes_gmail.py::gmail_oauth_callback`, which
    trusts ONLY the consumed state's own `mailbox_id` — there is no
    separate route-supplied `expected_mailbox_id` to compare against;
    the Microsoft callback route works identically) — this
    repository-level test proves `get_state` surfaces the TRUE bound
    `mailbox_id`, so any caller checking it against an expected id
    correctly identifies a mismatch rather than silently treating the
    state as valid for the wrong mailbox."""
    repo = PostgresMailboxGmailOAuthStateRepository()
    mailbox_a = _mailbox_id()
    mailbox_b = _mailbox_id()
    created = repo.create_state(mailbox_id=mailbox_a)

    fetched = repo.get_state(created.state)
    assert fetched is not None
    assert fetched.mailbox_id == mailbox_a
    assert fetched.mailbox_id != mailbox_b


def test_oauth_state_replay_after_successful_consumption_is_rejected():
    repo = PostgresMailboxGmailOAuthStateRepository()
    created = repo.create_state(mailbox_id=_mailbox_id())

    first = consume_state(repo, created.state)
    assert first.is_consumed

    with pytest.raises(OAuthStateError):
        consume_state(repo, created.state)


def test_oauth_state_duplicate_token_is_rejected_by_the_database_primary_key(monkeypatch):
    """A real, deliberate probe of the primary-key uniqueness
    constraint — forces `generate_state_value()` to return the same
    value twice (briefly, within this test only; the real generator is
    never weakened) so a second row with an identical `state` primary
    key cannot silently overwrite the first."""
    mailbox_a = _mailbox_id()
    mailbox_b = _mailbox_id()

    forced_state_value = f"forced-collision-{identity.generate_id()}"
    monkeypatch.setattr(gmail_oauth_state, "generate_state_value", lambda: forced_state_value)

    repo = PostgresMailboxGmailOAuthStateRepository()
    repo.create_state(mailbox_id=mailbox_a)

    with pytest.raises(IntegrityError):
        with session_scope(get_engine()) as session:
            session.add(
                MailboxGmailOAuthStateRow(
                    state=forced_state_value, mailbox_id=mailbox_b, created_at=utc_now(),
                    expires_at=utc_now() + timedelta(seconds=600), consumed_at=None,
                )
            )
            session.flush()
