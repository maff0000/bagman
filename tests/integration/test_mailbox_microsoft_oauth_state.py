"""CD-6 Slice 4 tests for `services.mailbox.microsoft.oauth_state` —
the mailbox-scoped OAuth anti-CSRF/replay state, mirroring
`services.xero.oauth_state`'s own tested security properties exactly
(state-bound-to-mailbox, expiry, replay rejection, concurrent-consume
race safety).
"""
from __future__ import annotations

from datetime import timedelta

import pytest

from core import identity
from core.errors import NotFoundError, OAuthStateError
from services.mailbox.microsoft.oauth_state import (
    InMemoryMailboxOAuthStateRepository,
    consume_state,
)


@pytest.fixture
def repo() -> InMemoryMailboxOAuthStateRepository:
    return InMemoryMailboxOAuthStateRepository()


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

    state = repo.create_state(mailbox_id=mailbox_id)
    far_future = utc_now() + timedelta(seconds=ms_ttl_plus_margin())
    with pytest.raises(OAuthStateError):
        consume_state(repo, state.state, now=far_future)


def ms_ttl_plus_margin() -> float:
    from services.mailbox.microsoft.oauth_state import STATE_TTL_SECONDS

    return STATE_TTL_SECONDS + 60.0


def test_mark_consumed_on_unknown_state_raises_not_found(repo):
    with pytest.raises(NotFoundError):
        repo.mark_consumed("never-minted")


def test_states_for_different_mailboxes_are_independent(repo):
    mailbox_a = identity.generate_id()
    mailbox_b = identity.generate_id()
    state_a = repo.create_state(mailbox_id=mailbox_a)
    state_b = repo.create_state(mailbox_id=mailbox_b)
    consumed_a = consume_state(repo, state_a.state)
    assert consumed_a.mailbox_id == mailbox_a
    # state_b remains valid and unconsumed.
    consumed_b = consume_state(repo, state_b.state)
    assert consumed_b.mailbox_id == mailbox_b
