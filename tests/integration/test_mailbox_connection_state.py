"""CD-6 Slice 4 tests for `services.mailbox.mailbox`'s new
`connection_state` machine (`ALLOWED_CONNECTION_TRANSITIONS`,
`transition_connection_state`, and the six new
`InMemoryMailboxSourceRepository` methods) — the domain-layer half of
the first real mailbox connection lifecycle producer.
"""
from __future__ import annotations

import pytest

from core.errors import InvalidStateTransitionError
from services.mailbox.mailbox import (
    CONNECTION_STATE_AUTH_REQUIRED,
    CONNECTION_STATE_CONNECTED,
    CONNECTION_STATE_ERROR,
    CONNECTION_STATE_NOT_CONFIGURED,
    PROVIDER_MICROSOFT_GRAPH,
    InMemoryMailboxSourceRepository,
    transition_connection_state,
)


@pytest.fixture
def repo() -> InMemoryMailboxSourceRepository:
    return InMemoryMailboxSourceRepository()


@pytest.fixture
def mailbox(repo):
    return repo.create_mailbox(
        display_name="Matt — Infosecurs", email_address="matt@infosecurs.com", provider_kind=PROVIDER_MICROSOFT_GRAPH
    )


def test_new_mailbox_starts_not_configured(mailbox):
    assert mailbox.connection_state == CONNECTION_STATE_NOT_CONFIGURED


def test_begin_connect_moves_to_auth_required(repo, mailbox):
    updated = repo.begin_microsoft_connect(mailbox.mailbox_id)
    assert updated.connection_state == CONNECTION_STATE_AUTH_REQUIRED


def test_begin_connect_is_idempotent_when_already_auth_required(repo, mailbox):
    first = repo.begin_microsoft_connect(mailbox.mailbox_id)
    second = repo.begin_microsoft_connect(mailbox.mailbox_id)
    assert first.connection_state == second.connection_state == CONNECTION_STATE_AUTH_REQUIRED


def test_mark_connected_from_auth_required(repo, mailbox):
    repo.begin_microsoft_connect(mailbox.mailbox_id)
    updated = repo.mark_microsoft_connected(mailbox.mailbox_id)
    assert updated.connection_state == CONNECTION_STATE_CONNECTED
    assert updated.last_connection_check_at is not None
    assert updated.last_error_code is None
    assert updated.last_error_detail is None


def test_mark_connected_is_idempotent(repo, mailbox):
    repo.begin_microsoft_connect(mailbox.mailbox_id)
    repo.mark_microsoft_connected(mailbox.mailbox_id)
    again = repo.mark_microsoft_connected(mailbox.mailbox_id)
    assert again.connection_state == CONNECTION_STATE_CONNECTED


def test_cannot_mark_connected_directly_from_not_configured(repo, mailbox):
    """NOT_CONFIGURED -> CONNECTED is not a legal edge — a connect
    attempt must always pass through AUTH_REQUIRED first (mirrors
    XeroConnection's own PENDING-first discipline)."""
    with pytest.raises(InvalidStateTransitionError):
        transition_connection_state(mailbox, CONNECTION_STATE_CONNECTED)


def test_refresh_failure_moves_connected_back_to_auth_required_not_error(repo, mailbox):
    """Architect spec, verbatim: 'On refresh failure (revoked consent):
    connection_state becomes AUTH_REQUIRED' — never ERROR."""
    repo.begin_microsoft_connect(mailbox.mailbox_id)
    repo.mark_microsoft_connected(mailbox.mailbox_id)
    updated = repo.mark_microsoft_auth_required(mailbox.mailbox_id, error_detail="refresh_token invalid")
    assert updated.connection_state == CONNECTION_STATE_AUTH_REQUIRED
    assert updated.last_error_detail == "refresh_token invalid"


def test_connection_error_moves_connected_to_error(repo, mailbox):
    repo.begin_microsoft_connect(mailbox.mailbox_id)
    repo.mark_microsoft_connected(mailbox.mailbox_id)
    updated = repo.mark_microsoft_connection_error(mailbox.mailbox_id, error_code="PERMISSION_ERROR", error_detail="403")
    assert updated.connection_state == CONNECTION_STATE_ERROR
    assert updated.last_error_code == "PERMISSION_ERROR"


def test_connection_error_when_already_error_updates_detail_in_place(repo, mailbox):
    repo.begin_microsoft_connect(mailbox.mailbox_id)
    repo.mark_microsoft_connected(mailbox.mailbox_id)
    repo.mark_microsoft_connection_error(mailbox.mailbox_id, error_code="PERMISSION_ERROR", error_detail="first")
    updated = repo.mark_microsoft_connection_error(mailbox.mailbox_id, error_code="PERMISSION_ERROR", error_detail="second")
    assert updated.connection_state == CONNECTION_STATE_ERROR
    assert updated.last_error_detail == "second"


def test_error_can_recover_to_auth_required_or_connected(repo, mailbox):
    repo.begin_microsoft_connect(mailbox.mailbox_id)
    repo.mark_microsoft_connected(mailbox.mailbox_id)
    repo.mark_microsoft_connection_error(mailbox.mailbox_id, error_code="X", error_detail="y")
    recovered = repo.mark_microsoft_connected(mailbox.mailbox_id)
    assert recovered.connection_state == CONNECTION_STATE_CONNECTED


def test_disconnect_returns_to_not_configured_from_any_state(repo, mailbox):
    repo.begin_microsoft_connect(mailbox.mailbox_id)
    repo.mark_microsoft_connected(mailbox.mailbox_id)
    updated = repo.disconnect_microsoft(mailbox.mailbox_id)
    assert updated.connection_state == CONNECTION_STATE_NOT_CONFIGURED
    assert updated.last_error_code is None


def test_disconnect_is_idempotent(repo, mailbox):
    first = repo.disconnect_microsoft(mailbox.mailbox_id)
    second = repo.disconnect_microsoft(mailbox.mailbox_id)
    assert first.connection_state == second.connection_state == CONNECTION_STATE_NOT_CONFIGURED


def test_record_sweep_success_stamps_last_successful_sweep_at_without_touching_connection_state(repo, mailbox):
    repo.begin_microsoft_connect(mailbox.mailbox_id)
    repo.mark_microsoft_connected(mailbox.mailbox_id)
    from core.timestamps import utc_now

    now = utc_now()
    updated = repo.record_microsoft_sweep_success(mailbox.mailbox_id, swept_at=now)
    assert updated.last_successful_sweep_at == now
    assert updated.connection_state == CONNECTION_STATE_CONNECTED


def test_lifecycle_status_and_connection_state_are_fully_independent(repo, mailbox):
    """Disabling a mailbox must never itself change connection_state —
    the two state machines are separate (module docstring)."""
    repo.begin_microsoft_connect(mailbox.mailbox_id)
    repo.mark_microsoft_connected(mailbox.mailbox_id)
    disabled = repo.disable_mailbox(mailbox.mailbox_id)
    assert disabled.status == "DISABLED"
    assert disabled.connection_state == CONNECTION_STATE_CONNECTED
