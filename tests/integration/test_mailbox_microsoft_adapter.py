"""CD-6 Slice 4 tests for
`services.mailbox.microsoft.adapter.MicrosoftGraphMailboxAdapter` —
token lifecycle orchestration (pre-emptive/reactive refresh, bounded
401-retry-once), connection-state side effects on refresh failure, and
server-side identity verification at OAuth callback time (never
trusting the callback's own query string — "wrong account" rejection).
All driven via `FakeMicrosoftOAuthClient`/`FakeMicrosoftGraphClient` —
no real Microsoft Entra app exists yet.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from core import identity
from services.mailbox.mailbox import (
    CONNECTION_STATE_AUTH_REQUIRED,
    CONNECTION_STATE_CONNECTED,
    PROVIDER_MICROSOFT_GRAPH,
    InMemoryMailboxSourceRepository,
)
from services.mailbox.microsoft.adapter import MicrosoftGraphMailboxAdapter
from services.mailbox.microsoft.fake_client import (
    FakeMicrosoftGraphClient,
    FakeMicrosoftOAuthClient,
    fake_token_bundle,
)
from services.mailbox.microsoft.graph_client import (
    GraphDeltaPageResult,
    GraphMessageContentResult,
    GraphOutcomeStatus,
    MicrosoftIdentity,
    MicrosoftIdentityResult,
    MicrosoftTokenResult,
)
from services.mailbox.microsoft.secrets import InMemoryMicrosoftTokenStore


@pytest.fixture
def mailbox_repo() -> InMemoryMailboxSourceRepository:
    return InMemoryMailboxSourceRepository()


@pytest.fixture
def mailbox(mailbox_repo):
    return mailbox_repo.create_mailbox(
        display_name="Matt — Infosecurs", email_address="matt@infosecurs.com", provider_kind=PROVIDER_MICROSOFT_GRAPH
    )


@pytest.fixture
def oauth_client() -> FakeMicrosoftOAuthClient:
    return FakeMicrosoftOAuthClient()


@pytest.fixture
def graph_client() -> FakeMicrosoftGraphClient:
    return FakeMicrosoftGraphClient()


@pytest.fixture
def token_store() -> InMemoryMicrosoftTokenStore:
    return InMemoryMicrosoftTokenStore()


@pytest.fixture
def adapter(oauth_client, graph_client, token_store, mailbox_repo) -> MicrosoftGraphMailboxAdapter:
    return MicrosoftGraphMailboxAdapter(
        oauth_client=oauth_client, graph_client=graph_client, token_store=token_store, mailbox_repository=mailbox_repo
    )


def _connect(mailbox_repo, mailbox_id: str) -> None:
    mailbox_repo.begin_microsoft_connect(mailbox_id)


# ---------------------------------------------------------------------
# Identity verification
# ---------------------------------------------------------------------


def test_successful_exchange_and_matching_identity_connects(adapter, oauth_client, mailbox_repo, mailbox):
    _connect(mailbox_repo, mailbox.mailbox_id)
    oauth_client.queue_exchange_result(MicrosoftTokenResult(status=GraphOutcomeStatus.OK, tokens=fake_token_bundle()))
    oauth_client.queue_me_result(
        MicrosoftIdentityResult(
            status=GraphOutcomeStatus.OK,
            identity=MicrosoftIdentity(mail="matt@infosecurs.com", user_principal_name="matt@infosecurs.com"),
        )
    )
    outcome = adapter.exchange_code_and_verify_identity(
        mailbox_id=mailbox.mailbox_id, expected_email_address=mailbox.email_address, code="c", redirect_uri="https://x"
    )
    assert outcome.ok
    assert mailbox_repo.get_mailbox(mailbox.mailbox_id).connection_state == CONNECTION_STATE_CONNECTED


def test_userPrincipalName_match_is_also_accepted(adapter, oauth_client, mailbox_repo, mailbox):
    """Architect spec: compare against BOTH `mail` and
    `userPrincipalName` claims — some tenants populate only one."""
    _connect(mailbox_repo, mailbox.mailbox_id)
    oauth_client.queue_exchange_result(MicrosoftTokenResult(status=GraphOutcomeStatus.OK, tokens=fake_token_bundle()))
    oauth_client.queue_me_result(
        MicrosoftIdentityResult(
            status=GraphOutcomeStatus.OK, identity=MicrosoftIdentity(mail=None, user_principal_name="matt@infosecurs.com")
        )
    )
    outcome = adapter.exchange_code_and_verify_identity(
        mailbox_id=mailbox.mailbox_id, expected_email_address=mailbox.email_address, code="c", redirect_uri="https://x"
    )
    assert outcome.ok


def test_wrong_account_fails_honestly_and_persists_no_token(adapter, oauth_client, token_store, mailbox_repo, mailbox):
    """Architect spec, verbatim: fail honestly, persist no usable
    token, do not mark CONNECTED, allow clean restart."""
    _connect(mailbox_repo, mailbox.mailbox_id)
    oauth_client.queue_exchange_result(MicrosoftTokenResult(status=GraphOutcomeStatus.OK, tokens=fake_token_bundle()))
    oauth_client.queue_me_result(
        MicrosoftIdentityResult(
            status=GraphOutcomeStatus.OK,
            identity=MicrosoftIdentity(mail="someone-else@infosecurs.com", user_principal_name="someone-else@infosecurs.com"),
        )
    )
    outcome = adapter.exchange_code_and_verify_identity(
        mailbox_id=mailbox.mailbox_id, expected_email_address=mailbox.email_address, code="c", redirect_uri="https://x"
    )
    assert not outcome.ok
    assert outcome.reason == "wrong_account"
    assert token_store.read(mailbox.mailbox_id) is None
    assert mailbox_repo.get_mailbox(mailbox.mailbox_id).connection_state == CONNECTION_STATE_AUTH_REQUIRED


def test_wrong_account_never_mutates_a_different_mailbox(adapter, oauth_client, mailbox_repo, mailbox):
    other = mailbox_repo.create_mailbox(
        display_name="NoustAI", email_address="ops@noustai.com", provider_kind=PROVIDER_MICROSOFT_GRAPH
    )
    _connect(mailbox_repo, mailbox.mailbox_id)
    oauth_client.queue_exchange_result(MicrosoftTokenResult(status=GraphOutcomeStatus.OK, tokens=fake_token_bundle()))
    oauth_client.queue_me_result(
        MicrosoftIdentityResult(
            status=GraphOutcomeStatus.OK, identity=MicrosoftIdentity(mail="ops@noustai.com", user_principal_name="ops@noustai.com")
        )
    )
    adapter.exchange_code_and_verify_identity(
        mailbox_id=mailbox.mailbox_id, expected_email_address=mailbox.email_address, code="c", redirect_uri="https://x"
    )
    assert mailbox_repo.get_mailbox(other.mailbox_id).connection_state == "NOT_CONFIGURED"


def test_token_exchange_failure_is_reported_honestly(adapter, oauth_client, mailbox_repo, mailbox):
    _connect(mailbox_repo, mailbox.mailbox_id)
    oauth_client.queue_exchange_result(MicrosoftTokenResult(status=GraphOutcomeStatus.AUTH_ERROR, error_detail="bad code"))
    outcome = adapter.exchange_code_and_verify_identity(
        mailbox_id=mailbox.mailbox_id, expected_email_address=mailbox.email_address, code="c", redirect_uri="https://x"
    )
    assert not outcome.ok
    assert outcome.reason == "token_exchange_failed"


# ---------------------------------------------------------------------
# Token refresh (pre-emptive + reactive)
# ---------------------------------------------------------------------


def test_pre_emptive_refresh_when_token_is_near_expiry(adapter, oauth_client, graph_client, token_store, mailbox):
    token_store.write(
        mailbox.mailbox_id, access_token="stale", refresh_token="rt",
        expires_at=datetime.now(timezone.utc) + timedelta(seconds=10),  # inside the refresh skew window
    )
    oauth_client.queue_refresh_result(MicrosoftTokenResult(status=GraphOutcomeStatus.OK, tokens=fake_token_bundle(access_token="fresh")))
    graph_client.queue_delta_result(GraphDeltaPageResult(status=GraphOutcomeStatus.OK, messages=(), delta_link="d1"))

    result = adapter.fetch_folder_delta(mailbox_id=mailbox.mailbox_id, folder="INBOX", delta_link=None, bootstrap_timestamp=None)
    assert result.status == GraphOutcomeStatus.OK
    assert token_store.read(mailbox.mailbox_id).access_token == "fresh"


def test_reactive_refresh_retries_the_same_request_once_on_401(adapter, oauth_client, graph_client, token_store, mailbox):
    token_store.write(
        mailbox.mailbox_id, access_token="believed-fresh", refresh_token="rt",
        expires_at=datetime.now(timezone.utc) + timedelta(hours=1),
    )
    graph_client.queue_delta_result(GraphDeltaPageResult(status=GraphOutcomeStatus.AUTH_ERROR, error_detail="401"))
    oauth_client.queue_refresh_result(MicrosoftTokenResult(status=GraphOutcomeStatus.OK, tokens=fake_token_bundle(access_token="rotated")))
    graph_client.queue_delta_result(GraphDeltaPageResult(status=GraphOutcomeStatus.OK, messages=(), delta_link="d1"))

    result = adapter.fetch_folder_delta(mailbox_id=mailbox.mailbox_id, folder="INBOX", delta_link=None, bootstrap_timestamp=None)
    assert result.status == GraphOutcomeStatus.OK
    assert len(graph_client.delta_calls) == 2


def test_refresh_failure_marks_mailbox_auth_required_not_error(adapter, oauth_client, graph_client, token_store, mailbox_repo, mailbox):
    _connect(mailbox_repo, mailbox.mailbox_id)
    mailbox_repo.mark_microsoft_connected(mailbox.mailbox_id)
    token_store.write(
        mailbox.mailbox_id, access_token="stale", refresh_token="rt",
        expires_at=datetime.now(timezone.utc) + timedelta(seconds=1),
    )
    oauth_client.queue_refresh_result(MicrosoftTokenResult(status=GraphOutcomeStatus.AUTH_ERROR, error_detail="revoked"))

    result = adapter.fetch_folder_delta(mailbox_id=mailbox.mailbox_id, folder="INBOX", delta_link=None, bootstrap_timestamp=None)
    assert result.status == GraphOutcomeStatus.AUTH_ERROR
    assert mailbox_repo.get_mailbox(mailbox.mailbox_id).connection_state == CONNECTION_STATE_AUTH_REQUIRED


def test_no_stored_tokens_at_all_is_a_clean_auth_error(adapter, mailbox):
    result = adapter.fetch_folder_delta(mailbox_id=mailbox.mailbox_id, folder="INBOX", delta_link=None, bootstrap_timestamp=None)
    assert result.status == GraphOutcomeStatus.AUTH_ERROR


def test_fetch_message_content_also_gets_one_reactive_retry(adapter, oauth_client, graph_client, token_store, mailbox):
    token_store.write(
        mailbox.mailbox_id, access_token="believed-fresh", refresh_token="rt",
        expires_at=datetime.now(timezone.utc) + timedelta(hours=1),
    )
    graph_client.queue_content_result(GraphMessageContentResult(status=GraphOutcomeStatus.AUTH_ERROR))
    oauth_client.queue_refresh_result(MicrosoftTokenResult(status=GraphOutcomeStatus.OK, tokens=fake_token_bundle()))
    graph_client.queue_content_result(GraphMessageContentResult(status=GraphOutcomeStatus.OK, content=b"raw"))

    result = adapter.fetch_message_content(mailbox_id=mailbox.mailbox_id, immutable_message_id="m1")
    assert result.status == GraphOutcomeStatus.OK
    assert result.content == b"raw"
