"""HTTP-level tests for ``/internal/mailboxes/*/microsoft/*`` (CD-6
Slice 4). Runs against DEVELOPMENT/TEST composition — in-memory
mailbox/sweep repositories plus
``FakeMicrosoftOAuthClient``/``FakeMicrosoftGraphClient`` (this
codebase's established `Fake*` pattern; no real Microsoft Entra app
exists yet). Mirrors ``tests/app_api/test_xero_endpoints.py``'s own
lightweight `TestClient`-only style — no Docker required.
"""
from __future__ import annotations

import urllib.parse
from datetime import datetime, timezone

import pytest
from fastapi.testclient import TestClient

from app.api.composition import get_composition, reset_composition_for_tests
from app.api.main import app
from services.mailbox.microsoft.fake_client import fake_token_bundle
from services.mailbox.microsoft.graph_client import (
    GraphDeltaPageResult,
    GraphMessageContentResult,
    GraphMessageSummary,
    GraphOutcomeStatus,
    MicrosoftIdentity,
    MicrosoftIdentityResult,
    MicrosoftTokenResult,
)

ACTOR_ID = "bagman-mailbox-microsoft-endpoint-tests"


@pytest.fixture
def dev_client(monkeypatch):
    monkeypatch.setenv("BAGMAN_RUNTIME_ENV", "development")
    reset_composition_for_tests()
    with TestClient(app) as test_client:
        yield test_client
    reset_composition_for_tests()


def _create_mailbox(client, *, provider_kind="MICROSOFT_GRAPH", email="matt@infosecurs.com"):
    r = client.post(
        "/internal/mailboxes",
        json={
            "display_name": "Matt — Infosecurs",
            "email_address": email,
            "provider_kind": provider_kind,
            "actor_type": "USER",
            "actor_id": ACTOR_ID,
        },
    )
    assert r.status_code == 201, r.text
    return r.json()["mailbox_id"]


def _begin_connect(client, mailbox_id: str) -> str:
    r = client.post(f"/internal/mailboxes/{mailbox_id}/microsoft/connect", json={"actor_type": "USER", "actor_id": ACTOR_ID})
    assert r.status_code == 201, r.text
    return urllib.parse.parse_qs(urllib.parse.urlparse(r.json()["authorize_url"]).query)["state"][0]


def _connect_and_complete(client, mailbox_id: str, *, email="matt@infosecurs.com") -> None:
    state = _begin_connect(client, mailbox_id)
    comp = get_composition()
    comp.microsoft_oauth_client.queue_exchange_result(MicrosoftTokenResult(status=GraphOutcomeStatus.OK, tokens=fake_token_bundle()))
    comp.microsoft_oauth_client.queue_me_result(
        MicrosoftIdentityResult(status=GraphOutcomeStatus.OK, identity=MicrosoftIdentity(mail=email, user_principal_name=email))
    )
    r = client.get("/internal/mailboxes/microsoft/oauth/callback", params={"code": "abc", "state": state}, follow_redirects=False)
    assert r.status_code == 303
    assert "ok=true" in r.headers["location"]


# ---------------------------------------------------------------------
# connect
# ---------------------------------------------------------------------


def test_connect_returns_401_config_error_when_microsoft_not_configured(dev_client):
    get_composition().microsoft_oauth_client.configured = False
    mailbox_id = _create_mailbox(dev_client)
    r = dev_client.post(f"/internal/mailboxes/{mailbox_id}/microsoft/connect", json={"actor_type": "USER", "actor_id": ACTOR_ID})
    assert r.status_code == 422


def test_connect_rejects_a_non_microsoft_mailbox(dev_client):
    mailbox_id = _create_mailbox(dev_client, provider_kind="IMAP", email="ops@noustai.com")
    r = dev_client.post(f"/internal/mailboxes/{mailbox_id}/microsoft/connect", json={"actor_type": "USER", "actor_id": ACTOR_ID})
    assert r.status_code == 422


def test_connect_returns_an_authorize_url_and_moves_to_auth_required(dev_client):
    mailbox_id = _create_mailbox(dev_client)
    r = dev_client.post(f"/internal/mailboxes/{mailbox_id}/microsoft/connect", json={"actor_type": "USER", "actor_id": ACTOR_ID})
    assert r.status_code == 201
    assert "authorize_url" in r.json()
    mailbox = dev_client.get(f"/internal/mailboxes/{mailbox_id}").json()
    assert mailbox["connection_state"] == "AUTH_REQUIRED"


def test_connect_never_returns_a_token_or_secret_in_the_response(dev_client):
    mailbox_id = _create_mailbox(dev_client)
    r = dev_client.post(f"/internal/mailboxes/{mailbox_id}/microsoft/connect", json={"actor_type": "USER", "actor_id": ACTOR_ID})
    body_text = r.text.lower()
    assert "client_secret" not in body_text
    assert "access_token" not in body_text
    assert "refresh_token" not in body_text


def test_connect_creates_exactly_one_needs_you_item(dev_client):
    mailbox_id = _create_mailbox(dev_client)
    dev_client.post(f"/internal/mailboxes/{mailbox_id}/microsoft/connect", json={"actor_type": "USER", "actor_id": ACTOR_ID})
    comp = get_composition()
    items = comp.needs_you_repository.list_needs_you_items(item_type="MAILBOX_AUTH_REQUIRED")
    matching = [i for i in items if i.source_object_reference == mailbox_id]
    assert len(matching) == 1
    assert matching[0].status == "OPEN"

    # Re-clicking Connect must never duplicate the item.
    dev_client.post(f"/internal/mailboxes/{mailbox_id}/microsoft/connect", json={"actor_type": "USER", "actor_id": ACTOR_ID})
    matching_again = [
        i for i in comp.needs_you_repository.list_needs_you_items(item_type="MAILBOX_AUTH_REQUIRED")
        if i.source_object_reference == mailbox_id
    ]
    assert len(matching_again) == 1


# ---------------------------------------------------------------------
# callback / identity verification
# ---------------------------------------------------------------------


def test_full_connect_flow_reaches_connected_and_resolves_needs_you(dev_client):
    mailbox_id = _create_mailbox(dev_client)
    _connect_and_complete(dev_client, mailbox_id)
    mailbox = dev_client.get(f"/internal/mailboxes/{mailbox_id}").json()
    assert mailbox["connection_state"] == "CONNECTED"

    comp = get_composition()
    items = [
        i for i in comp.needs_you_repository.list_needs_you_items(item_type="MAILBOX_AUTH_REQUIRED")
        if i.source_object_reference == mailbox_id
    ]
    assert all(i.status == "RESOLVED" for i in items)


def test_callback_missing_state_redirects_to_failure(dev_client):
    r = dev_client.get("/internal/mailboxes/microsoft/oauth/callback", params={"code": "abc"}, follow_redirects=False)
    assert r.status_code == 303
    assert "reason=missing_state" in r.headers["location"]


def test_callback_unknown_state_redirects_to_invalid_state(dev_client):
    r = dev_client.get(
        "/internal/mailboxes/microsoft/oauth/callback", params={"code": "abc", "state": "never-minted"}, follow_redirects=False
    )
    assert r.status_code == 303
    assert "reason=invalid_state" in r.headers["location"]


def test_wrong_account_at_callback_never_connects_and_stays_retryable(dev_client):
    mailbox_id = _create_mailbox(dev_client)
    state = _begin_connect(dev_client, mailbox_id)
    comp = get_composition()
    comp.microsoft_oauth_client.queue_exchange_result(MicrosoftTokenResult(status=GraphOutcomeStatus.OK, tokens=fake_token_bundle()))
    comp.microsoft_oauth_client.queue_me_result(
        MicrosoftIdentityResult(
            status=GraphOutcomeStatus.OK,
            identity=MicrosoftIdentity(mail="someone-else@infosecurs.com", user_principal_name="someone-else@infosecurs.com"),
        )
    )
    r = dev_client.get("/internal/mailboxes/microsoft/oauth/callback", params={"code": "abc", "state": state}, follow_redirects=False)
    assert "reason=wrong_account" in r.headers["location"]
    mailbox = dev_client.get(f"/internal/mailboxes/{mailbox_id}").json()
    assert mailbox["connection_state"] == "AUTH_REQUIRED"

    # Retryable: begin connect again and succeed.
    _connect_and_complete(dev_client, mailbox_id)
    assert dev_client.get(f"/internal/mailboxes/{mailbox_id}").json()["connection_state"] == "CONNECTED"


def test_oauth_result_page_never_reflects_unrecognised_reason_text(dev_client):
    """XSS-closing doctrine (mirrors xero.py's own hardening finding):
    an unrecognised `reason` query value must never be echoed back
    verbatim."""
    r = dev_client.get("/internal/mailboxes/microsoft/oauth/result", params={"ok": "false", "reason": "<script>evil</script>"})
    assert "<script>evil</script>" not in r.text


# ---------------------------------------------------------------------
# disconnect
# ---------------------------------------------------------------------


def test_disconnect_returns_to_not_configured_and_deletes_tokens(dev_client):
    mailbox_id = _create_mailbox(dev_client)
    _connect_and_complete(dev_client, mailbox_id)
    comp = get_composition()
    assert comp.microsoft_token_store.read(mailbox_id) is not None

    r = dev_client.post(f"/internal/mailboxes/{mailbox_id}/microsoft/disconnect", json={"actor_type": "USER", "actor_id": ACTOR_ID})
    assert r.status_code == 200
    assert r.json()["connection_state"] == "NOT_CONFIGURED"
    assert comp.microsoft_token_store.read(mailbox_id) is None


# ---------------------------------------------------------------------
# sweep
# ---------------------------------------------------------------------


def test_sweep_before_connected_is_a_409(dev_client):
    mailbox_id = _create_mailbox(dev_client)
    r = dev_client.post(f"/internal/mailboxes/{mailbox_id}/microsoft/sweep", json={"actor_type": "USER", "actor_id": ACTOR_ID})
    assert r.status_code == 409


def test_sweep_after_connect_ingests_a_message_and_lists_it(dev_client):
    mailbox_id = _create_mailbox(dev_client)
    _connect_and_complete(dev_client, mailbox_id)
    comp = get_composition()

    now = datetime.now(timezone.utc)
    msg = GraphMessageSummary(
        immutable_id="AAMk-1", internet_message_id="<a@b>", subject="Invoice", sender_address="v@example.com",
        sender_display_name="Vendor", received_at=now, has_attachments=False,
    )
    comp.microsoft_graph_client.queue_delta_result(GraphDeltaPageResult(status=GraphOutcomeStatus.OK, messages=(msg,), delta_link="d1"))
    comp.microsoft_graph_client.queue_content_result(
        GraphMessageContentResult(status=GraphOutcomeStatus.OK, content=b"From: v@example.com\r\nSubject: Invoice\r\n\r\nBody")
    )
    comp.microsoft_graph_client.queue_delta_result(GraphDeltaPageResult(status=GraphOutcomeStatus.OK, messages=(), delta_link="d2"))

    r = dev_client.post(f"/internal/mailboxes/{mailbox_id}/microsoft/sweep", json={"actor_type": "USER", "actor_id": ACTOR_ID})
    assert r.status_code == 200
    body = r.json()
    assert body["status"] == "SUCCEEDED"
    assert body["evidence_created"] == 1

    messages = dev_client.get(f"/internal/mailboxes/{mailbox_id}/microsoft/messages").json()
    assert messages["count"] == 1
    assert messages["items"][0]["subject"] == "Invoice"
    # Never a body/raw MIME field on this projection.
    assert "body" not in messages["items"][0]

    sweeps = dev_client.get(f"/internal/mailboxes/{mailbox_id}/microsoft/sweeps").json()
    assert sweeps["count"] == 1


def test_sweep_response_never_leaks_a_token_or_delta_link(dev_client):
    mailbox_id = _create_mailbox(dev_client)
    _connect_and_complete(dev_client, mailbox_id)
    comp = get_composition()
    comp.microsoft_graph_client.queue_delta_result(GraphDeltaPageResult(status=GraphOutcomeStatus.OK, messages=(), delta_link="super-secret-delta-token"))
    comp.microsoft_graph_client.queue_delta_result(GraphDeltaPageResult(status=GraphOutcomeStatus.OK, messages=(), delta_link="super-secret-delta-token-2"))

    r = dev_client.post(f"/internal/mailboxes/{mailbox_id}/microsoft/sweep", json={"actor_type": "USER", "actor_id": ACTOR_ID})
    assert "super-secret-delta-token" not in r.text


def test_noustai_imap_mailbox_never_reaches_the_microsoft_router(dev_client):
    mailbox_id = _create_mailbox(dev_client, provider_kind="IMAP", email="ops@noustai.com")
    r = dev_client.post(f"/internal/mailboxes/{mailbox_id}/microsoft/sweep", json={"actor_type": "USER", "actor_id": ACTOR_ID})
    assert r.status_code == 422
    mailbox = dev_client.get(f"/internal/mailboxes/{mailbox_id}").json()
    assert mailbox["connection_state"] == "NOT_CONFIGURED"
