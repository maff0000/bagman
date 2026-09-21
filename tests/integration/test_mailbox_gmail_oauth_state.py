"""CD-6 GUI-operations-foundation follow-on WO tests for
`services.mailbox.gmail.oauth_state` — the mailbox-scoped Gmail OAuth
anti-CSRF/replay state, mirroring
`tests/integration/test_mailbox_microsoft_oauth_state.py`'s own tested
security properties exactly (state-bound-to-mailbox, expiry, replay
rejection), PLUS the two-independent-Gmail-mailboxes-in-flight-at-once
proof this delivery's own WO calls out explicitly.
"""
from __future__ import annotations

from datetime import timedelta

import pytest

from core import identity
from core.errors import NotFoundError, OAuthStateError
from services.mailbox.gmail.oauth_state import InMemoryGmailOAuthStateRepository, consume_state


@pytest.fixture
def repo() -> InMemoryGmailOAuthStateRepository:
    return InMemoryGmailOAuthStateRepository()


@pytest.fixture
def mailbox_id() -> str:
    return identity.generate_id()


def test_created_state_is_bound_to_the_mailbox(repo, mailbox_id):
    state = repo.create_state(mailbox_id=mailbox_id)
    assert state.mailbox_id == mailbox_id
    assert not state.is_consumed


def test_state_values_are_unique_and_unguessable(repo, mailbox_id):
    a = repo.create_state(mailbox_id=mailbox_id)
    b = repo.create_state(mailbox_id=mailbox_id)
    assert a.state != b.state
    assert len(a.state) >= 32


def test_unknown_state_is_rejected(repo):
    with pytest.raises(OAuthStateError):
        consume_state(repo, "never-minted-value")


def test_consume_returns_the_bound_mailbox_id(repo, mailbox_id):
    state = repo.create_state(mailbox_id=mailbox_id)
    consumed = consume_state(repo, state.state)
    assert consumed.mailbox_id == mailbox_id
    assert consumed.is_consumed


def test_replaying_an_already_consumed_state_is_rejected(repo, mailbox_id):
    state = repo.create_state(mailbox_id=mailbox_id)
    consume_state(repo, state.state)
    with pytest.raises(OAuthStateError):
        consume_state(repo, state.state)


def test_expired_state_is_rejected(repo, mailbox_id):
    from core.timestamps import utc_now
    from services.mailbox.gmail.oauth_state import STATE_TTL_SECONDS

    state = repo.create_state(mailbox_id=mailbox_id)
    far_future = utc_now() + timedelta(seconds=STATE_TTL_SECONDS + 60.0)
    with pytest.raises(OAuthStateError):
        consume_state(repo, state.state, now=far_future)


def test_mark_consumed_on_unknown_state_raises_not_found(repo):
    with pytest.raises(NotFoundError):
        repo.mark_consumed("never-minted")


def test_two_independent_gmail_mailboxes_can_be_in_flight_at_once(repo):
    """The core WO-named proof: `mgs241171@gmail.com` and
    `matt.george.scott@gmail.com` each independently begin a connect
    flow (two states, two mailbox ids), and consuming ONE never disturbs
    the other, and each consumed state resolves to the RIGHT mailbox."""
    mailbox_a = identity.generate_id()
    mailbox_b = identity.generate_id()
    state_a = repo.create_state(mailbox_id=mailbox_a)
    state_b = repo.create_state(mailbox_id=mailbox_b)
    assert state_a.state != state_b.state

    consumed_a = consume_state(repo, state_a.state)
    assert consumed_a.mailbox_id == mailbox_a

    # state_b remains valid and unconsumed — routes correctly to mailbox_b.
    consumed_b = consume_state(repo, state_b.state)
    assert consumed_b.mailbox_id == mailbox_b
    assert consumed_a.mailbox_id != consumed_b.mailbox_id
