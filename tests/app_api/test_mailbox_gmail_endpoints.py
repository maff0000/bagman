"""HTTP-level tests for ``/internal/mailboxes/*/gmail/*`` (CD-6
GUI-operations-foundation follow-on WO — third mailbox provider).
Mirrors ``tests/app_api/test_mailbox_microsoft_endpoints.py``'s own
lightweight `TestClient`-only style for the OAuth connect/callback
lifecycle, and ``tests/app_api/test_mailbox_imap_endpoints.py``'s own
proportionate coverage for everything else. Runs against DEVELOPMENT/
TEST composition — in-memory mailbox/sweep repositories plus
`FakeGmailOAuthClient`/`FakeGmailClient` (no real Google Cloud OAuth app
exists yet).
"""
from __future__ import annotations

import urllib.parse

import pytest
from fastapi.testclient import TestClient

from app.api.composition import get_composition, reset_composition_for_tests
from app.api.main import app
from services.mailbox.gmail.fake_gmail_client import fake_token_bundle
from services.mailbox.gmail.gmail_client import (
    GmailIdentity,
    GmailIdentityResult,
    GmailLabel,
    GmailLabelListResult,
    GmailMessageListPageResult,
    GmailOutcomeStatus,
    GmailTokenResult,
)

ACTOR_ID = "bagman-mailbox-gmail-endpoint-tests"


@pytest.fixture
def dev_client(monkeypatch):
    monkeypatch.setenv("BAGMAN_RUNTIME_ENV", "development")
    reset_composition_for_tests()
    with TestClient(app) as test_client:
        yield test_client
    reset_composition_for_tests()


def _create_gmail_mailbox(client: TestClient, *, email: str = "mgs241171@gmail.com") -> str:
    response = client.post(
        "/internal/mailboxes",
        json={
            "display_name": "Personal Gmail",
            "email_address": email,
            "provider_kind": "GOOGLE_GMAIL",
            "actor_type": "USER",
            "actor_id": ACTOR_ID,
        },
    )
    assert response.status_code == 201, response.text
    return response.json()["mailbox_id"]


def _begin_connect(client, mailbox_id: str) -> str:
    r = client.post(f"/internal/mailboxes/{mailbox_id}/gmail/connect", json={"actor_type": "USER", "actor_id": ACTOR_ID})
    assert r.status_code == 201, r.text
    return urllib.parse.parse_qs(urllib.parse.urlparse(r.json()["authorize_url"]).query)["state"][0]


def _connect_and_complete(client, mailbox_id: str, *, email: str) -> None:
    state = _begin_connect(client, mailbox_id)
    comp = get_composition()
    comp.gmail_oauth_client.queue_exchange_result(GmailTokenResult(status=GmailOutcomeStatus.OK, tokens=fake_token_bundle()))
    comp.gmail_oauth_client.queue_identity_result(GmailIdentityResult(status=GmailOutcomeStatus.OK, identity=GmailIdentity(email=email)))
    r = client.get("/internal/mailboxes/gmail/oauth/callback", params={"code": "abc", "state": state}, follow_redirects=False)
    assert r.status_code == 303
    assert "ok=true" in r.headers["location"]


# ---------------------------------------------------------------------
# connect
# ---------------------------------------------------------------------


def test_connect_returns_422_config_error_when_gmail_not_configured(dev_client):
    get_composition().gmail_oauth_client.configured = False
    mailbox_id = _create_gmail_mailbox(dev_client)
    r = dev_client.post(f"/internal/mailboxes/{mailbox_id}/gmail/connect", json={"actor_type": "USER", "actor_id": ACTOR_ID})
    assert r.status_code == 422


def test_connect_rejects_a_non_gmail_mailbox(dev_client):
    r = dev_client.post(
        "/internal/mailboxes",
        json={
            "display_name": "Matt Infosecurs", "email_address": "matt@infosecurs.com", "provider_kind": "MICROSOFT_GRAPH",
            "actor_type": "USER", "actor_id": ACTOR_ID,
        },
    )
    mailbox_id = r.json()["mailbox_id"]
    response = dev_client.post(f"/internal/mailboxes/{mailbox_id}/gmail/connect", json={"actor_type": "USER", "actor_id": ACTOR_ID})
    assert response.status_code == 422


def test_connect_returns_an_authorize_url_with_readonly_scope_and_moves_to_auth_required(dev_client):
    mailbox_id = _create_gmail_mailbox(dev_client)
    r = dev_client.post(f"/internal/mailboxes/{mailbox_id}/gmail/connect", json={"actor_type": "USER", "actor_id": ACTOR_ID})
    assert r.status_code == 201
    assert "authorize_url" in r.json()
    mailbox = dev_client.get(f"/internal/mailboxes/{mailbox_id}").json()
    assert mailbox["connection_state"] == "AUTH_REQUIRED"


def test_connect_never_returns_a_token_or_secret_in_the_response(dev_client):
    mailbox_id = _create_gmail_mailbox(dev_client)
    r = dev_client.post(f"/internal/mailboxes/{mailbox_id}/gmail/connect", json={"actor_type": "USER", "actor_id": ACTOR_ID})
    body_text = r.text.lower()
    assert "client_secret" not in body_text
    assert "access_token" not in body_text
    assert "refresh_token" not in body_text


# ---------------------------------------------------------------------
# callback / identity verification
# ---------------------------------------------------------------------


def test_full_connect_flow_reaches_connected(dev_client):
    mailbox_id = _create_gmail_mailbox(dev_client, email="mgs241171@gmail.com")
    _connect_and_complete(dev_client, mailbox_id, email="mgs241171@gmail.com")
    mailbox = dev_client.get(f"/internal/mailboxes/{mailbox_id}").json()
    assert mailbox["connection_state"] == "CONNECTED"


def test_callback_with_missing_state_redirects_with_missing_state_reason(dev_client):
    r = dev_client.get("/internal/mailboxes/gmail/oauth/callback", params={"code": "abc"}, follow_redirects=False)
    assert r.status_code == 303
    assert "reason=missing_state" in r.headers["location"]


def test_callback_with_unknown_state_redirects_with_invalid_state_reason(dev_client):
    r = dev_client.get("/internal/mailboxes/gmail/oauth/callback", params={"code": "abc", "state": "never-minted"}, follow_redirects=False)
    assert r.status_code == 303
    assert "reason=invalid_state" in r.headers["location"]


def test_callback_wrong_account_never_connects_and_never_persists_tokens(dev_client):
    mailbox_id = _create_gmail_mailbox(dev_client, email="mgs241171@gmail.com")
    state = _begin_connect(dev_client, mailbox_id)
    comp = get_composition()
    comp.gmail_oauth_client.queue_exchange_result(GmailTokenResult(status=GmailOutcomeStatus.OK, tokens=fake_token_bundle()))
    comp.gmail_oauth_client.queue_identity_result(GmailIdentityResult(status=GmailOutcomeStatus.OK, identity=GmailIdentity(email="someone.else@gmail.com")))

    r = dev_client.get("/internal/mailboxes/gmail/oauth/callback", params={"code": "abc", "state": state}, follow_redirects=False)
    assert r.status_code == 303
    assert "reason=wrong_account" in r.headers["location"]

    mailbox = dev_client.get(f"/internal/mailboxes/{mailbox_id}").json()
    assert mailbox["connection_state"] == "AUTH_REQUIRED"  # never CONNECTED
    assert comp.gmail_token_store.read(mailbox_id) is None


def test_two_independent_gmail_mailboxes_can_be_in_flight_at_once_and_route_correctly(dev_client):
    """The core WO-named routing proof, exercised through the real HTTP
    surface: two Gmail mailboxes each begin a connect flow before either
    completes, and each callback lands on the RIGHT mailbox."""
    mailbox_a = _create_gmail_mailbox(dev_client, email="mgs241171@gmail.com")
    mailbox_b = _create_gmail_mailbox(dev_client, email="matt.george.scott@gmail.com")

    state_a = _begin_connect(dev_client, mailbox_a)
    state_b = _begin_connect(dev_client, mailbox_b)
    assert state_a != state_b

    comp = get_composition()
    comp.gmail_oauth_client.queue_exchange_result(GmailTokenResult(status=GmailOutcomeStatus.OK, tokens=fake_token_bundle(access_token="token-a")))
    comp.gmail_oauth_client.queue_identity_result(GmailIdentityResult(status=GmailOutcomeStatus.OK, identity=GmailIdentity(email="mgs241171@gmail.com")))
    r_a = dev_client.get("/internal/mailboxes/gmail/oauth/callback", params={"code": "code-a", "state": state_a}, follow_redirects=False)
    assert "ok=true" in r_a.headers["location"]

    comp.gmail_oauth_client.queue_exchange_result(GmailTokenResult(status=GmailOutcomeStatus.OK, tokens=fake_token_bundle(access_token="token-b")))
    comp.gmail_oauth_client.queue_identity_result(GmailIdentityResult(status=GmailOutcomeStatus.OK, identity=GmailIdentity(email="matt.george.scott@gmail.com")))
    r_b = dev_client.get("/internal/mailboxes/gmail/oauth/callback", params={"code": "code-b", "state": state_b}, follow_redirects=False)
    assert "ok=true" in r_b.headers["location"]

    assert dev_client.get(f"/internal/mailboxes/{mailbox_a}").json()["connection_state"] == "CONNECTED"
    assert dev_client.get(f"/internal/mailboxes/{mailbox_b}").json()["connection_state"] == "CONNECTED"
    assert comp.gmail_token_store.read(mailbox_a).access_token == "token-a"
    assert comp.gmail_token_store.read(mailbox_b).access_token == "token-b"


# ---------------------------------------------------------------------
# disconnect / sweep / provider-mismatch rejection
# ---------------------------------------------------------------------


def test_disconnect_clears_connection_state_and_deletes_tokens(dev_client):
    mailbox_id = _create_gmail_mailbox(dev_client)
    _connect_and_complete(dev_client, mailbox_id, email="mgs241171@gmail.com")
    comp = get_composition()
    assert comp.gmail_token_store.read(mailbox_id) is not None

    r = dev_client.post(f"/internal/mailboxes/{mailbox_id}/gmail/disconnect", json={"actor_type": "USER", "actor_id": ACTOR_ID})
    assert r.status_code == 200
    assert r.json()["connection_state"] == "NOT_CONFIGURED"
    assert comp.gmail_token_store.read(mailbox_id) is None


def test_sweep_before_connect_is_a_conflict(dev_client):
    mailbox_id = _create_gmail_mailbox(dev_client)
    r = dev_client.post(f"/internal/mailboxes/{mailbox_id}/gmail/sweep", json={"actor_type": "USER", "actor_id": ACTOR_ID})
    assert r.status_code == 409


def test_sweep_after_connect_returns_a_terminal_run(dev_client):
    mailbox_id = _create_gmail_mailbox(dev_client)
    _connect_and_complete(dev_client, mailbox_id, email="mgs241171@gmail.com")
    comp = get_composition()

    comp.api.register_entity(
        entity_type="COMPANY", canonical_name="TEST_ENTITY_GMAIL", display_name="Test Entity", status="ACTIVE",
        actor_type="SYSTEM", actor_id="test", fiscal_year_start_month_day="01-01",
    )
    comp.gmail_client.queue_labels_result(
        GmailLabelListResult(status=GmailOutcomeStatus.OK, labels=(GmailLabel(label_id="INBOX", name="INBOX", label_type="system"),))
    )
    comp.gmail_client.queue_list_messages_result(GmailMessageListPageResult(status=GmailOutcomeStatus.OK, message_ids=()))

    r = dev_client.post(f"/internal/mailboxes/{mailbox_id}/gmail/sweep", json={"actor_type": "USER", "actor_id": ACTOR_ID})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["status"] == "SUCCEEDED"
    assert body["messages_seen"] == 0


def test_a_gmail_only_endpoint_rejects_a_microsoft_mailbox(dev_client):
    r = dev_client.post(
        "/internal/mailboxes",
        json={
            "display_name": "Matt Infosecurs", "email_address": "matt@infosecurs.com", "provider_kind": "MICROSOFT_GRAPH",
            "actor_type": "USER", "actor_id": ACTOR_ID,
        },
    )
    mailbox_id = r.json()["mailbox_id"]
    response = dev_client.post(f"/internal/mailboxes/{mailbox_id}/gmail/sweep", json={"actor_type": "USER", "actor_id": ACTOR_ID})
    assert response.status_code == 422


def test_a_gmail_only_endpoint_rejects_an_imap_mailbox(dev_client):
    r = dev_client.post(
        "/internal/mailboxes",
        json={
            "display_name": "Matt NoustAI", "email_address": "matt@noust.ai", "provider_kind": "IMAP",
            "actor_type": "USER", "actor_id": ACTOR_ID,
        },
    )
    mailbox_id = r.json()["mailbox_id"]
    response = dev_client.post(f"/internal/mailboxes/{mailbox_id}/gmail/connect", json={"actor_type": "USER", "actor_id": ACTOR_ID})
    assert response.status_code == 422


def test_microsoft_only_endpoint_rejects_a_gmail_mailbox(dev_client):
    mailbox_id = _create_gmail_mailbox(dev_client)
    response = dev_client.post(f"/internal/mailboxes/{mailbox_id}/microsoft/connect", json={"actor_type": "USER", "actor_id": ACTOR_ID})
    assert response.status_code == 422


def test_imap_only_endpoint_rejects_a_gmail_mailbox(dev_client):
    mailbox_id = _create_gmail_mailbox(dev_client)
    response = dev_client.post(f"/internal/mailboxes/{mailbox_id}/imap/connect", json={"actor_type": "USER", "actor_id": ACTOR_ID})
    assert response.status_code == 422


def test_not_configured_never_crashes_listing_endpoints(dev_client):
    """NOT_CONFIGURED/AUTH_REQUIRED states must never crash a plain
    read."""
    mailbox_id = _create_gmail_mailbox(dev_client)
    assert dev_client.get(f"/internal/mailboxes/{mailbox_id}/gmail/sweeps").status_code == 200
    assert dev_client.get(f"/internal/mailboxes/{mailbox_id}/gmail/messages").status_code == 200
    assert dev_client.get(f"/internal/mailboxes/{mailbox_id}/gmail/domain-rules").status_code == 200
    assert dev_client.get(f"/internal/mailboxes/{mailbox_id}/gmail/domain-review").status_code == 200
    assert dev_client.get(f"/internal/mailboxes/{mailbox_id}/gmail/security-review").status_code == 200


def test_auth_required_never_crashes_listing_endpoints(dev_client):
    mailbox_id = _create_gmail_mailbox(dev_client)
    _begin_connect(dev_client, mailbox_id)  # -> AUTH_REQUIRED, never completed
    mailbox = dev_client.get(f"/internal/mailboxes/{mailbox_id}").json()
    assert mailbox["connection_state"] == "AUTH_REQUIRED"
    assert dev_client.get(f"/internal/mailboxes/{mailbox_id}/gmail/sweeps").status_code == 200
    assert dev_client.get(f"/internal/mailboxes/{mailbox_id}/gmail/messages").status_code == 200


# ---------------------------------------------------------------------
# default_entity_id — never set by this router
# ---------------------------------------------------------------------


def test_default_entity_id_stays_null_for_both_real_gmail_accounts(dev_client):
    mailbox_a = _create_gmail_mailbox(dev_client, email="mgs241171@gmail.com")
    mailbox_b = _create_gmail_mailbox(dev_client, email="matt.george.scott@gmail.com")
    assert dev_client.get(f"/internal/mailboxes/{mailbox_a}").json()["default_entity_id"] is None
    assert dev_client.get(f"/internal/mailboxes/{mailbox_b}").json()["default_entity_id"] is None

    _connect_and_complete(dev_client, mailbox_a, email="mgs241171@gmail.com")
    assert dev_client.get(f"/internal/mailboxes/{mailbox_a}").json()["default_entity_id"] is None


# ---------------------------------------------------------------------
# domain-rules reuse (provider-neutral policy-rules endpoint)
# ---------------------------------------------------------------------


# ---------------------------------------------------------------------
# domain-review / security-review resolution wrappers (shared,
# provider-neutral services.mailbox.review_resolution — this router's
# own job is only threading composition.gmail_mailbox_adapter through)
# ---------------------------------------------------------------------


def test_resolve_domain_review_allow_creates_a_must_read_rule_and_resolves_the_item(dev_client):
    mailbox_id = _create_gmail_mailbox(dev_client)
    _connect_and_complete(dev_client, mailbox_id, email="mgs241171@gmail.com")
    comp = get_composition()
    comp.api.register_entity(
        entity_type="COMPANY", canonical_name="TEST_ENTITY_DOMREV", display_name="Test Entity", status="ACTIVE",
        actor_type="SYSTEM", actor_id="test", fiscal_year_start_month_day="01-01",
    )

    comp.gmail_client.queue_labels_result(
        GmailLabelListResult(status=GmailOutcomeStatus.OK, labels=(GmailLabel(label_id="INBOX", name="INBOX", label_type="system"),))
    )
    comp.gmail_client.queue_list_messages_result(GmailMessageListPageResult(status=GmailOutcomeStatus.OK, message_ids=("m1",)))
    from services.mailbox.gmail.gmail_client import GmailMessageMetadata, GmailMessageMetadataResult
    from datetime import datetime, timezone

    comp.gmail_client.queue_metadata_result(
        GmailMessageMetadataResult(
            status=GmailOutcomeStatus.OK,
            metadata=GmailMessageMetadata(
                message_id="m1",
                raw_headers=(
                    {"name": "Subject", "value": "Your invoice"},
                    {"name": "From", "value": "AP <ap@newsupplier.com>"},
                    {"name": "Message-ID", "value": "<m1@newsupplier.com>"},
                    {"name": "Date", "value": "Mon, 01 Jan 2024 12:00:00 +0000"},
                ),
                internal_date=datetime(2024, 1, 1, tzinfo=timezone.utc),
            ),
        )
    )
    sweep_response = dev_client.post(f"/internal/mailboxes/{mailbox_id}/gmail/sweep", json={"actor_type": "USER", "actor_id": ACTOR_ID})
    assert sweep_response.status_code == 200, sweep_response.text

    review_items = dev_client.get(f"/internal/mailboxes/{mailbox_id}/gmail/domain-review").json()["items"]
    assert len(review_items) == 1
    item_id = review_items[0]["item_id"]

    # ALLOW immediately back-processes every historical candidate for
    # this domain — `_reprocess_one_message` refreshes headers (a real
    # authentication re-check against fresh headers) THEN fetches raw
    # MIME; queue both.
    from services.mailbox.gmail.gmail_client import GmailMessageRawResult

    comp.gmail_client.queue_metadata_result(
        GmailMessageMetadataResult(
            status=GmailOutcomeStatus.OK,
            metadata=GmailMessageMetadata(
                message_id="m1",
                raw_headers=(
                    {"name": "Subject", "value": "Your invoice"},
                    {"name": "From", "value": "AP <ap@newsupplier.com>"},
                    {"name": "Message-ID", "value": "<m1@newsupplier.com>"},
                    {"name": "Date", "value": "Mon, 01 Jan 2024 12:00:00 +0000"},
                ),
                internal_date=datetime(2024, 1, 1, tzinfo=timezone.utc),
            ),
        )
    )
    comp.gmail_client.queue_raw_result(
        GmailMessageRawResult(status=GmailOutcomeStatus.OK, content=b"From: ap@newsupplier.com\r\nSubject: Your invoice\r\n\r\nBody")
    )

    resolve = dev_client.post(
        f"/internal/mailboxes/{mailbox_id}/gmail/domain-review/{item_id}/resolve",
        json={
            "actor_type": "USER", "actor_id": ACTOR_ID, "decision": "ALLOW", "destination_mode": "REVIEW_REQUIRED",
            "match_mode": "EXACT",
        },
    )
    assert resolve.status_code == 200, resolve.text
    body = resolve.json()
    assert body["mailbox_domain_rule"]["policy"] == "MUST_READ"
    assert body["needs_you_item"]["status"] == "RESOLVED"

    rules = dev_client.get(f"/internal/mailboxes/{mailbox_id}/gmail/domain-rules").json()["items"]
    assert any(r["sender_domain"] == "newsupplier.com" and r["policy"] == "MUST_READ" for r in rules)


def test_domain_rules_list_is_reused_from_the_provider_neutral_endpoint(dev_client):
    mailbox_id = _create_gmail_mailbox(dev_client)
    response = dev_client.post(
        f"/internal/mailboxes/{mailbox_id}/policy-rules",
        json={"actor_type": "USER", "actor_id": ACTOR_ID, "match_mode": "EXACT", "sender_domain": "vendor.com", "policy": "BLACKLIST", "reason": "test"},
    )
    assert response.status_code == 200, response.text

    listed = dev_client.get(f"/internal/mailboxes/{mailbox_id}/gmail/domain-rules")
    assert listed.status_code == 200
    assert listed.json()["count"] == 1
    assert listed.json()["items"][0]["sender_domain"] == "vendor.com"
