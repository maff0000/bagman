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


# ---------------------------------------------------------------------
# CD-6 mailbox-list GUI-completion WO — the "Test" connection action
# (POST /{mailbox_id}/imap/test). Mirrors
# test_mailbox_microsoft_endpoints.py's own equivalent suite, using
# IMAP's own already-existing `attempt_login` primitive (a bare
# connect+login probe that always logs out immediately — see
# `services/mailbox/imap/imap_adapter.py::ImapMailboxAdapter.attempt_login`'s
# own docstring).
# ---------------------------------------------------------------------


def test_imap_test_endpoint_reports_not_configured_when_no_credentials_exist(dev_client):
    mailbox_id = _create_imap_mailbox(dev_client)
    response = dev_client.post(f"/internal/mailboxes/{mailbox_id}/imap/test", json={"actor_type": "USER", "actor_id": ACTOR_ID})
    assert response.status_code == 200
    body = response.json()
    assert body["ok"] is False
    assert "checked_at" in body


def test_imap_test_endpoint_succeeds_and_logs_out(dev_client):
    mailbox_id = _create_imap_mailbox(dev_client)
    composition = get_composition()
    composition.imap_mailbox_adapter._credentials_provider = _fake_credentials_provider()
    composition.imap_client.queue_connect_result(ImapConnectResult(status=ImapOutcomeStatus.OK))

    response = dev_client.post(f"/internal/mailboxes/{mailbox_id}/imap/test", json={"actor_type": "USER", "actor_id": ACTOR_ID})
    assert response.status_code == 200
    body = response.json()
    assert body["ok"] is True
    assert composition.imap_client.logout_calls == 1  # never leaves a connection open


def test_imap_test_endpoint_reports_auth_error_without_mutating_connection_state(dev_client):
    mailbox_id = _create_imap_mailbox(dev_client)
    composition = get_composition()
    composition.imap_mailbox_adapter._credentials_provider = _fake_credentials_provider()
    composition.imap_client.queue_connect_result(ImapConnectResult(status=ImapOutcomeStatus.OK))
    dev_client.post(f"/internal/mailboxes/{mailbox_id}/imap/connect", json={"actor_type": "USER", "actor_id": ACTOR_ID})
    assert dev_client.get(f"/internal/mailboxes/{mailbox_id}").json()["connection_state"] == "CONNECTED"

    composition.imap_client.queue_connect_result(
        ImapConnectResult(status=ImapOutcomeStatus.AUTH_ERROR, error_detail="bad credentials")
    )
    response = dev_client.post(f"/internal/mailboxes/{mailbox_id}/imap/test", json={"actor_type": "USER", "actor_id": ACTOR_ID})
    assert response.json()["ok"] is False
    # Never demoted to AUTH_REQUIRED by a /test call — only the real
    # connect endpoint's own `_apply_login_outcome` mutates connection_state.
    assert dev_client.get(f"/internal/mailboxes/{mailbox_id}").json()["connection_state"] == "CONNECTED"


def test_imap_test_endpoint_has_no_side_effects_on_sweep_needs_you_or_domain_rules(dev_client):
    mailbox_id = _create_imap_mailbox(dev_client)
    composition = get_composition()
    composition.imap_mailbox_adapter._credentials_provider = _fake_credentials_provider()
    composition.imap_client.queue_connect_result(ImapConnectResult(status=ImapOutcomeStatus.OK))
    dev_client.post(f"/internal/mailboxes/{mailbox_id}/imap/connect", json={"actor_type": "USER", "actor_id": ACTOR_ID})

    runs_before = composition.mailbox_sweep_run_repository.list_runs(mailbox_id=mailbox_id)
    rules_before = composition.mailbox_domain_rule_repository.list_rules(mailbox_id=mailbox_id)
    needs_you_before = composition.needs_you_repository.list_needs_you_items(item_type="MAILBOX_DOMAIN_REVIEW")
    messages_before = composition.mailbox_message_repository.list_messages(mailbox_id=mailbox_id)

    composition.imap_client.queue_connect_result(ImapConnectResult(status=ImapOutcomeStatus.OK))
    response = dev_client.post(f"/internal/mailboxes/{mailbox_id}/imap/test", json={"actor_type": "USER", "actor_id": ACTOR_ID})
    assert response.status_code == 200

    assert composition.mailbox_sweep_run_repository.list_runs(mailbox_id=mailbox_id) == runs_before
    assert composition.mailbox_domain_rule_repository.list_rules(mailbox_id=mailbox_id) == rules_before
    assert composition.needs_you_repository.list_needs_you_items(item_type="MAILBOX_DOMAIN_REVIEW") == needs_you_before
    assert composition.mailbox_message_repository.list_messages(mailbox_id=mailbox_id) == messages_before


def test_imap_test_endpoint_rejects_a_non_imap_mailbox(dev_client):
    r = dev_client.post(
        "/internal/mailboxes",
        json={
            "display_name": "Matt Infosecurs", "email_address": "matt-nonimap@infosecurs.com", "provider_kind": "MICROSOFT_GRAPH",
            "actor_type": "USER", "actor_id": ACTOR_ID,
        },
    )
    mailbox_id = r.json()["mailbox_id"]
    response = dev_client.post(f"/internal/mailboxes/{mailbox_id}/imap/test", json={"actor_type": "USER", "actor_id": ACTOR_ID})
    assert response.status_code == 422


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


# ---------------------------------------------------------------------
# Deterministic subject-aware mailbox domain policy (CD-6 GUI-operations-
# foundation follow-on WO) — provider-neutral request forwarding (item 17)
# ---------------------------------------------------------------------


def test_resolve_imap_domain_review_forwards_subject_predicate_fields(dev_client, monkeypatch):
    """Mirrors `tests/app_api/test_mailbox_gmail_endpoints.py`'s own
    equivalent proof — the IMAP router's request model accepts and
    forwards `subject_predicate_type`/`subject_predicate_value`
    identically into the SAME shared `resolve_domain_review`."""
    import app.api.routers.mailboxes_imap as imap_router

    mailbox_id = _create_imap_mailbox(dev_client)

    captured = {}

    def _fake_resolve_domain_review(**kwargs):
        captured.update(kwargs)
        return {"needs_you_item": {"status": "OPEN"}, "mailbox_domain_rule": None, "reprocessed_messages": []}

    monkeypatch.setattr(imap_router, "resolve_domain_review", _fake_resolve_domain_review)

    response = dev_client.post(
        f"/internal/mailboxes/{mailbox_id}/imap/domain-review/some-item-id/resolve",
        json={
            "actor_type": "USER", "actor_id": ACTOR_ID, "decision": "ALLOW", "destination_mode": "REVIEW_REQUIRED",
            "match_mode": "EXACT_DOMAIN_SUBJECT", "subject_predicate_type": "EXACT",
            "subject_predicate_value": "Monthly Statement",
        },
    )
    assert response.status_code == 200, response.text
    assert captured["match_mode"] == "EXACT_DOMAIN_SUBJECT"
    assert captured["subject_predicate_type"] == "EXACT"
    assert captured["subject_predicate_value"] == "Monthly Statement"


def test_batch_resolve_imap_domain_review_forwards_subject_predicate_fields(dev_client, monkeypatch):
    import app.api.routers.mailboxes_imap as imap_router

    mailbox_id = _create_imap_mailbox(dev_client)

    captured = []

    def _fake_resolve_domain_review(**kwargs):
        captured.append(kwargs)
        return {"needs_you_item": {"status": "OPEN"}, "mailbox_domain_rule": None, "reprocessed_messages": []}

    monkeypatch.setattr(imap_router, "resolve_domain_review", _fake_resolve_domain_review)

    response = dev_client.post(
        f"/internal/mailboxes/{mailbox_id}/imap/domain-review/batch-resolve",
        json={
            "actor_type": "USER", "actor_id": ACTOR_ID,
            "items": [
                {
                    "item_id": "batch-item-1", "decision": "ALLOW", "destination_mode": "REVIEW_REQUIRED",
                    "match_mode": "EXACT_DOMAIN_SUBJECT", "subject_predicate_type": "STARTS_WITH",
                    "subject_predicate_value": "Order confirmed:",
                }
            ],
        },
    )
    assert response.status_code == 200, response.text
    assert len(captured) == 1
    assert captured[0]["subject_predicate_type"] == "STARTS_WITH"
    assert captured[0]["subject_predicate_value"] == "Order confirmed:"


# ---------------------------------------------------------------------
# WO-BAGMAN-118 §6.A.4 — provider-neutral Needs-You wiring at connect/
# disconnect/sweep (IMAP has no OAuth callback — see module docstring
# and services.mailbox.health.needs_you_sync's own docstring).
# ---------------------------------------------------------------------


def test_imap_has_no_oauth_callback_route_anywhere():
    """Independently confirms WO §12 criterion 7's own "no IMAP OAuth
    callback or callback-equivalent route exists anywhere" requirement
    — read directly off the real FastAPI route table, not merely
    asserted from code inspection."""
    from app.api.routers import mailboxes_imap

    paths = [route.path for route in mailboxes_imap.router.routes]
    assert not any("callback" in path for path in paths)
    assert not any("oauth" in path.lower() for path in paths)


def test_failed_connect_auth_error_creates_exactly_one_needs_you_item(dev_client):
    mailbox_id = _create_imap_mailbox(dev_client)
    composition = get_composition()
    composition.imap_mailbox_adapter._credentials_provider = _fake_credentials_provider(password="wrong")
    composition.imap_client.queue_connect_result(
        ImapConnectResult(status=ImapOutcomeStatus.AUTH_ERROR, error_detail="bad credentials")
    )
    dev_client.post(f"/internal/mailboxes/{mailbox_id}/imap/connect", json={"actor_type": "USER", "actor_id": ACTOR_ID})

    items = [
        i for i in composition.needs_you_repository.list_needs_you_items(item_type="MAILBOX_AUTH_REQUIRED")
        if i.source_object_reference == mailbox_id
    ]
    assert len(items) == 1
    assert items[0].status == "OPEN"
    assert items[0].allowed_action_type == "RECONNECT_IMAP_MAILBOX"

    # retrying the failed connect must never duplicate the item
    composition.imap_client.queue_connect_result(
        ImapConnectResult(status=ImapOutcomeStatus.AUTH_ERROR, error_detail="bad credentials")
    )
    dev_client.post(f"/internal/mailboxes/{mailbox_id}/imap/connect", json={"actor_type": "USER", "actor_id": ACTOR_ID})
    items_again = [
        i for i in composition.needs_you_repository.list_needs_you_items(item_type="MAILBOX_AUTH_REQUIRED")
        if i.source_object_reference == mailbox_id
    ]
    assert len(items_again) == 1


def test_successful_connect_resolves_the_needs_you_item(dev_client):
    mailbox_id = _create_imap_mailbox(dev_client)
    composition = get_composition()
    composition.imap_mailbox_adapter._credentials_provider = _fake_credentials_provider(password="wrong")
    composition.imap_client.queue_connect_result(
        ImapConnectResult(status=ImapOutcomeStatus.AUTH_ERROR, error_detail="bad credentials")
    )
    dev_client.post(f"/internal/mailboxes/{mailbox_id}/imap/connect", json={"actor_type": "USER", "actor_id": ACTOR_ID})

    composition.imap_mailbox_adapter._credentials_provider = _fake_credentials_provider()
    composition.imap_client.queue_connect_result(ImapConnectResult(status=ImapOutcomeStatus.OK))
    dev_client.post(f"/internal/mailboxes/{mailbox_id}/imap/connect", json={"actor_type": "USER", "actor_id": ACTOR_ID})

    items = [
        i for i in composition.needs_you_repository.list_needs_you_items(item_type="MAILBOX_AUTH_REQUIRED")
        if i.source_object_reference == mailbox_id
    ]
    assert all(i.status == "RESOLVED" for i in items)


def test_connect_success_without_a_prior_auth_required_episode_creates_no_item(dev_client):
    """A mailbox whose very first connect attempt succeeds immediately
    (never having been `AUTH_REQUIRED` from the sync helper's own point
    of view — `sync_needs_you_for_connection_state` is only ever called
    once per request, with the FINAL connection_state) creates no
    `MAILBOX_AUTH_REQUIRED` item at all — there was never an unresolved
    question to raise. Mirrors Microsoft's own identical, pre-existing
    behaviour."""
    mailbox_id = _create_imap_mailbox(dev_client)
    composition = get_composition()
    composition.imap_mailbox_adapter._credentials_provider = _fake_credentials_provider()
    composition.imap_client.queue_connect_result(ImapConnectResult(status=ImapOutcomeStatus.OK))
    dev_client.post(f"/internal/mailboxes/{mailbox_id}/imap/connect", json={"actor_type": "USER", "actor_id": ACTOR_ID})

    items = [
        i for i in composition.needs_you_repository.list_needs_you_items(item_type="MAILBOX_AUTH_REQUIRED")
        if i.source_object_reference == mailbox_id
    ]
    assert items == []


def test_disconnecting_a_connected_mailbox_surfaces_a_fresh_needs_you_item(dev_client):
    """The real gap this delivery closes (PID §118.B): disconnecting a
    mailbox that silently connected (no prior `AUTH_REQUIRED` item ever
    needed) now correctly surfaces a fresh, actionable
    `MAILBOX_AUTH_REQUIRED` item — previously IMAP had no wiring at all,
    so this state was invisible to the operator."""
    mailbox_id = _create_imap_mailbox(dev_client)
    composition = get_composition()
    composition.imap_mailbox_adapter._credentials_provider = _fake_credentials_provider()
    composition.imap_client.queue_connect_result(ImapConnectResult(status=ImapOutcomeStatus.OK))
    dev_client.post(f"/internal/mailboxes/{mailbox_id}/imap/connect", json={"actor_type": "USER", "actor_id": ACTOR_ID})

    response = dev_client.post(f"/internal/mailboxes/{mailbox_id}/imap/disconnect", json={"actor_type": "USER", "actor_id": ACTOR_ID})
    assert response.status_code == 200
    items = [
        i for i in composition.needs_you_repository.list_needs_you_items(item_type="MAILBOX_AUTH_REQUIRED")
        if i.source_object_reference == mailbox_id
    ]
    assert len(items) == 1
    assert items[0].status == "OPEN"
    assert items[0].allowed_action_type == "RECONNECT_IMAP_MAILBOX"
