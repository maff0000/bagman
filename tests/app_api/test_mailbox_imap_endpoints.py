"""HTTP-level tests for ``/internal/mailboxes/*/imap/*`` (CD-6
GUI-operations-foundation follow-on WO — second mailbox provider).
Mirrors ``tests/app_api/test_mailbox_microsoft_endpoints.py``'s own
lightweight `TestClient`-only style — runs against DEVELOPMENT/TEST
composition (in-memory repositories + `FakeImapClient`; no real
`mail.noust.ai` credentials exist yet).
"""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from app.api.composition import get_composition, reset_composition_for_tests
from app.api.main import app
from services.mailbox.imap.imap_client import (
    ImapCapabilityResult,
    ImapConnectResult,
    ImapFolderInfo,
    ImapFolderListResult,
    ImapOutcomeStatus,
    ImapSearchResult,
)
from services.mailbox.imap.secrets import NoustAIImapCredentials

ACTOR_ID = "bagman-mailbox-imap-endpoint-tests"


def _fake_credentials_provider(username: str = "matt@noust.ai", password: str = "pw"):
    return lambda: NoustAIImapCredentials(username=username, password=password)


@pytest.fixture
def dev_client(monkeypatch):
    monkeypatch.setenv("BAGMAN_RUNTIME_ENV", "development")
    reset_composition_for_tests()
    with TestClient(app) as test_client:
        yield test_client
    reset_composition_for_tests()


def _create_imap_mailbox(client: TestClient) -> str:
    response = client.post(
        "/internal/mailboxes",
        json={
            "display_name": "Matt NoustAI",
            "email_address": "matt@noust.ai",
            "provider_kind": "IMAP",
            "actor_type": "USER",
            "actor_id": ACTOR_ID,
        },
    )
    assert response.status_code == 201, response.text
    return response.json()["mailbox_id"]


def test_connect_is_not_configured_when_no_credentials_exist(dev_client):
    mailbox_id = _create_imap_mailbox(dev_client)
    response = dev_client.post(f"/internal/mailboxes/{mailbox_id}/imap/connect", json={"actor_type": "USER", "actor_id": ACTOR_ID})
    assert response.status_code == 422
    body = response.json()
    assert "not configured" in body["message"].lower()

    mailbox = dev_client.get(f"/internal/mailboxes/{mailbox_id}").json()
    assert mailbox["connection_state"] == "NOT_CONFIGURED"


def test_a_microsoft_only_endpoint_rejects_an_imap_mailbox(dev_client):
    mailbox_id = _create_imap_mailbox(dev_client)
    response = dev_client.post(
        f"/internal/mailboxes/{mailbox_id}/microsoft/connect", json={"actor_type": "USER", "actor_id": ACTOR_ID}
    )
    assert response.status_code == 422


def test_an_imap_only_endpoint_rejects_a_microsoft_mailbox(dev_client):
    response = dev_client.post(
        "/internal/mailboxes",
        json={
            "display_name": "Matt Infosecurs",
            "email_address": "matt@infosecurs.com",
            "provider_kind": "MICROSOFT_GRAPH",
            "actor_type": "USER",
            "actor_id": ACTOR_ID,
        },
    )
    mailbox_id = response.json()["mailbox_id"]
    imap_response = dev_client.post(f"/internal/mailboxes/{mailbox_id}/imap/connect", json={"actor_type": "USER", "actor_id": ACTOR_ID})
    assert imap_response.status_code == 422


def test_successful_connect_with_credentials_present_marks_mailbox_connected(dev_client, monkeypatch):
    mailbox_id = _create_imap_mailbox(dev_client)
    composition = get_composition()

    # A real credential IS present (mirrors the "PL has placed the
    # files" state) — monkeypatch the adapter's own credentials_provider
    # directly rather than writing real files, and queue a successful
    # login on the fake client.
    composition.imap_mailbox_adapter._credentials_provider = _fake_credentials_provider()
    composition.imap_client.queue_connect_result(ImapConnectResult(status=ImapOutcomeStatus.OK))

    response = dev_client.post(f"/internal/mailboxes/{mailbox_id}/imap/connect", json={"actor_type": "USER", "actor_id": ACTOR_ID})
    assert response.status_code == 201, response.text
    body = response.json()
    assert body["connection_state"] == "CONNECTED"
    assert composition.imap_client.logout_calls == 1


def test_failed_connect_auth_error_marks_auth_required_never_crashes(dev_client):
    mailbox_id = _create_imap_mailbox(dev_client)
    composition = get_composition()
    composition.imap_mailbox_adapter._credentials_provider = _fake_credentials_provider(password="wrong")
    composition.imap_client.queue_connect_result(
        ImapConnectResult(status=ImapOutcomeStatus.AUTH_ERROR, error_detail="bad credentials")
    )

    response = dev_client.post(f"/internal/mailboxes/{mailbox_id}/imap/connect", json={"actor_type": "USER", "actor_id": ACTOR_ID})
    assert response.status_code == 201
    body = response.json()
    assert body["connection_state"] == "AUTH_REQUIRED"


def test_sweep_before_connect_is_a_conflict(dev_client):
    mailbox_id = _create_imap_mailbox(dev_client)
    response = dev_client.post(f"/internal/mailboxes/{mailbox_id}/imap/sweep", json={"actor_type": "USER", "actor_id": ACTOR_ID})
    assert response.status_code == 409


def test_sweep_after_connect_returns_a_terminal_run(dev_client):
    mailbox_id = _create_imap_mailbox(dev_client)
    composition = get_composition()
    composition.imap_mailbox_adapter._credentials_provider = _fake_credentials_provider()
    composition.imap_client.queue_connect_result(ImapConnectResult(status=ImapOutcomeStatus.OK))
    dev_client.post(f"/internal/mailboxes/{mailbox_id}/imap/connect", json={"actor_type": "USER", "actor_id": ACTOR_ID})

    # Register a governed entity — the bootstrap-floor computation
    # requires at least one.
    composition.api.register_entity(
        entity_type="COMPANY", canonical_name="TEST_ENTITY", display_name="Test Entity", status="ACTIVE",
        actor_type="SYSTEM", actor_id="test", fiscal_year_start_month_day="01-01",
    )

    composition.imap_client.queue_connect_result(ImapConnectResult(status=ImapOutcomeStatus.OK))
    composition.imap_client.queue_capability_result(ImapCapabilityResult(status=ImapOutcomeStatus.OK, capabilities=("IMAP4rev1",)))
    composition.imap_client.queue_list_folders_result(
        ImapFolderListResult(status=ImapOutcomeStatus.OK, folders=(ImapFolderInfo(name="INBOX"),))
    )
    composition.imap_client.queue_connect_result(ImapConnectResult(status=ImapOutcomeStatus.OK))
    composition.imap_client.queue_search_result(ImapSearchResult(status=ImapOutcomeStatus.OK, uids=(), uidvalidity=1))

    response = dev_client.post(f"/internal/mailboxes/{mailbox_id}/imap/sweep", json={"actor_type": "USER", "actor_id": ACTOR_ID})
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["status"] == "SUCCEEDED"
    assert body["messages_seen"] == 0


def test_domain_rules_list_is_reused_from_the_provider_neutral_endpoint(dev_client):
    mailbox_id = _create_imap_mailbox(dev_client)
    response = dev_client.post(
        f"/internal/mailboxes/{mailbox_id}/policy-rules",
        json={
            "actor_type": "USER",
            "actor_id": ACTOR_ID,
            "match_mode": "EXACT",
            "sender_domain": "vendor.com",
            "policy": "BLACKLIST",
            "reason": "test",
        },
    )
    assert response.status_code == 200, response.text

    listed = dev_client.get(f"/internal/mailboxes/{mailbox_id}/imap/domain-rules")
    assert listed.status_code == 200
    assert listed.json()["count"] == 1
    assert listed.json()["items"][0]["sender_domain"] == "vendor.com"
