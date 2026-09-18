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
    GraphFolderListResult,
    GraphFolderSummary,
    GraphMessageContentResult,
    GraphMessageSummary,
    GraphOutcomeStatus,
    GraphWellKnownFoldersResult,
    MicrosoftIdentity,
    MicrosoftIdentityResult,
    MicrosoftTokenResult,
)

ACTOR_ID = "bagman-mailbox-microsoft-endpoint-tests"

#: CD-6 architect amendment (recursive folder discovery) — `run_sweep`
#: now calls `adapter.discover_monitored_folders` once before iterating
#: any folder; queue a plain Inbox+Junk discovery result (matches the
#: OLD, pre-amendment monitored set) so this file's existing HTTP-level
#: sweep tests need no other changes.
_INBOX_FOLDER_ID = "AAMkADinbox000000000000000000000"
_JUNK_FOLDER_ID = "AAMkADjunkemail0000000000000000"


def _queue_folder_discovery(comp) -> None:
    comp.microsoft_graph_client.queue_folder_list_result(
        GraphFolderListResult(
            status=GraphOutcomeStatus.OK,
            folders=(
                GraphFolderSummary(
                    folder_id=_INBOX_FOLDER_ID, display_name="Inbox", parent_folder_id=None, child_folder_count=0
                ),
                GraphFolderSummary(
                    folder_id=_JUNK_FOLDER_ID, display_name="Junk Email", parent_folder_id=None, child_folder_count=0
                ),
            ),
        )
    )
    comp.microsoft_graph_client.queue_well_known_folders_result(
        GraphWellKnownFoldersResult(
            status=GraphOutcomeStatus.OK, folder_ids={"inbox": _INBOX_FOLDER_ID, "junkemail": _JUNK_FOLDER_ID}
        )
    )


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
    # CD-6 architect amendment (two-stage mail processing) — Stage B
    # only proceeds to full MIME fetch/evidence-ingest for a domain
    # under an ALLOWED MailboxDomainRule; this test is proving the
    # ALLOWED-path behaviour end to end, so the rule is set up first
    # exactly like a real operator approval would.
    comp.mailbox_domain_rule_repository.upsert_rule(
        mailbox_id=mailbox_id,
        sender_domain="example.com",
        match_mode="EXACT",
        policy="ALLOWED",
        destination_entity_id=None,
        destination_mode="REVIEW_REQUIRED",
        source="OPERATOR",
    )

    now = datetime.now(timezone.utc)
    msg = GraphMessageSummary(
        immutable_id="AAMk-1", internet_message_id="<a@b>", subject="Invoice", sender_address="v@example.com",
        sender_display_name="Vendor", received_at=now, has_attachments=False,
    )
    _queue_folder_discovery(comp)
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
    _queue_folder_discovery(comp)
    comp.microsoft_graph_client.queue_delta_result(GraphDeltaPageResult(status=GraphOutcomeStatus.OK, messages=(), delta_link="super-secret-delta-token"))
    comp.microsoft_graph_client.queue_delta_result(GraphDeltaPageResult(status=GraphOutcomeStatus.OK, messages=(), delta_link="super-secret-delta-token-2"))

    r = dev_client.post(f"/internal/mailboxes/{mailbox_id}/microsoft/sweep", json={"actor_type": "USER", "actor_id": ACTOR_ID})
    assert "super-secret-delta-token" not in r.text


# ---------------------------------------------------------------------
# CD-6 architect amendment — domain-review resolution (Stage B gate)
# ---------------------------------------------------------------------


def _connected_mailbox_with_entity_seeded(client):
    mailbox_id = _create_mailbox(client)
    _connect_and_complete(client, mailbox_id)
    comp = get_composition()
    entity = comp.api.register_entity(
        entity_type="COMPANY", canonical_name="TEST_DOMAIN_REVIEW_LTD", display_name="Test Ltd", status="ACTIVE",
        actor_type="SYSTEM", actor_id=ACTOR_ID,
        fiscal_year_start_month_day="01-01", historical_floor_override_at=datetime(2025, 1, 1, tzinfo=timezone.utc),
    )
    return mailbox_id, comp, entity


def _sweep_unknown_domain_message(client, mailbox_id):
    comp = get_composition()
    now = datetime.now(timezone.utc)
    msg = GraphMessageSummary(
        immutable_id="AAMk-unknown-1", internet_message_id="<u1@b>", subject="Invoice attached",
        sender_address="billing@new-supplier.example", sender_display_name="New Supplier", received_at=now,
        has_attachments=False,
    )
    _queue_folder_discovery(comp)
    comp.microsoft_graph_client.queue_delta_result(GraphDeltaPageResult(status=GraphOutcomeStatus.OK, messages=(msg,), delta_link="d1"))
    comp.microsoft_graph_client.queue_delta_result(GraphDeltaPageResult(status=GraphOutcomeStatus.OK, messages=(), delta_link="d-junk"))
    r = client.post(f"/internal/mailboxes/{mailbox_id}/microsoft/sweep", json={"actor_type": "USER", "actor_id": ACTOR_ID})
    assert r.status_code == 200, r.text
    return r.json()


def test_unknown_domain_credible_message_raises_a_domain_review_item(dev_client):
    mailbox_id, comp, _entity = _connected_mailbox_with_entity_seeded(dev_client)
    _sweep_unknown_domain_message(dev_client, mailbox_id)

    items = comp.needs_you_repository.list_needs_you_items(item_type="MAILBOX_DOMAIN_REVIEW")
    matching = [i for i in items if i.metadata.get("mailbox_id") == mailbox_id]
    assert len(matching) == 1
    assert matching[0].metadata["sender_domain"] == "new-supplier.example"
    assert matching[0].status == "OPEN"


def test_resolve_domain_review_allow_fixed_creates_rule_and_reprocesses_message(dev_client):
    mailbox_id, comp, entity = _connected_mailbox_with_entity_seeded(dev_client)
    _sweep_unknown_domain_message(dev_client, mailbox_id)
    item = [
        i for i in comp.needs_you_repository.list_needs_you_items(item_type="MAILBOX_DOMAIN_REVIEW")
        if i.metadata.get("mailbox_id") == mailbox_id
    ][0]

    comp.microsoft_graph_client.queue_content_result(
        GraphMessageContentResult(
            status=GraphOutcomeStatus.OK, content=b"From: billing@new-supplier.example\r\nSubject: Invoice\r\n\r\nBody"
        )
    )
    r = dev_client.post(
        f"/internal/mailboxes/{mailbox_id}/microsoft/domain-review/{item.item_id}/resolve",
        json={
            "actor_type": "USER", "actor_id": ACTOR_ID, "decision": "ALLOW",
            "destination_entity_id": entity.entity_id, "destination_mode": "FIXED",
        },
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["needs_you_item"]["status"] == "RESOLVED"
    assert body["mailbox_domain_rule"]["policy"] == "ALLOWED"
    assert body["mailbox_domain_rule"]["destination_entity_id"] == entity.entity_id
    assert len(body["reprocessed_messages"]) == 1
    assert body["reprocessed_messages"][0]["ingestion_status"] == "INGESTED"
    assert body["reprocessed_messages"][0]["evidence_id"] is not None

    rules = dev_client.get(f"/internal/mailboxes/{mailbox_id}/microsoft/domain-rules").json()
    assert rules["count"] == 1
    assert rules["items"][0]["sender_domain"] == "new-supplier.example"


def test_resolve_domain_review_allow_review_required_needs_no_entity(dev_client):
    mailbox_id, comp, _entity = _connected_mailbox_with_entity_seeded(dev_client)
    _sweep_unknown_domain_message(dev_client, mailbox_id)
    item = [
        i for i in comp.needs_you_repository.list_needs_you_items(item_type="MAILBOX_DOMAIN_REVIEW")
        if i.metadata.get("mailbox_id") == mailbox_id
    ][0]

    comp.microsoft_graph_client.queue_content_result(
        GraphMessageContentResult(status=GraphOutcomeStatus.OK, content=b"From: x\r\nSubject: Invoice\r\n\r\nBody")
    )
    r = dev_client.post(
        f"/internal/mailboxes/{mailbox_id}/microsoft/domain-review/{item.item_id}/resolve",
        json={"actor_type": "USER", "actor_id": ACTOR_ID, "decision": "ALLOW", "destination_mode": "REVIEW_REQUIRED"},
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["mailbox_domain_rule"]["destination_entity_id"] is None
    assert body["mailbox_domain_rule"]["destination_mode"] == "REVIEW_REQUIRED"
    assert len(body["reprocessed_messages"]) == 1
    assert body["reprocessed_messages"][0]["ingestion_status"] == "INGESTED"


def test_resolve_domain_review_ignore_creates_ignored_rule_no_reprocess(dev_client):
    mailbox_id, comp, _entity = _connected_mailbox_with_entity_seeded(dev_client)
    _sweep_unknown_domain_message(dev_client, mailbox_id)
    item = [
        i for i in comp.needs_you_repository.list_needs_you_items(item_type="MAILBOX_DOMAIN_REVIEW")
        if i.metadata.get("mailbox_id") == mailbox_id
    ][0]

    r = dev_client.post(
        f"/internal/mailboxes/{mailbox_id}/microsoft/domain-review/{item.item_id}/resolve",
        json={"actor_type": "USER", "actor_id": ACTOR_ID, "decision": "IGNORE"},
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["mailbox_domain_rule"]["policy"] == "IGNORED"
    assert body["reprocessed_messages"] == []
    # No content fetch happened for an IGNORE decision.
    assert comp.microsoft_graph_client.content_calls == []


def test_resolve_domain_review_double_submit_same_decision_is_idempotent(dev_client):
    mailbox_id, comp, _entity = _connected_mailbox_with_entity_seeded(dev_client)
    _sweep_unknown_domain_message(dev_client, mailbox_id)
    item = [
        i for i in comp.needs_you_repository.list_needs_you_items(item_type="MAILBOX_DOMAIN_REVIEW")
        if i.metadata.get("mailbox_id") == mailbox_id
    ][0]
    payload = {"actor_type": "USER", "actor_id": ACTOR_ID, "decision": "IGNORE"}
    first = dev_client.post(f"/internal/mailboxes/{mailbox_id}/microsoft/domain-review/{item.item_id}/resolve", json=payload)
    assert first.status_code == 200
    second = dev_client.post(f"/internal/mailboxes/{mailbox_id}/microsoft/domain-review/{item.item_id}/resolve", json=payload)
    assert second.status_code == 200
    assert second.json()["needs_you_item"]["status"] == "RESOLVED"


def test_resolve_domain_review_conflicting_second_decision_is_409(dev_client):
    mailbox_id, comp, entity = _connected_mailbox_with_entity_seeded(dev_client)
    _sweep_unknown_domain_message(dev_client, mailbox_id)
    item = [
        i for i in comp.needs_you_repository.list_needs_you_items(item_type="MAILBOX_DOMAIN_REVIEW")
        if i.metadata.get("mailbox_id") == mailbox_id
    ][0]
    dev_client.post(
        f"/internal/mailboxes/{mailbox_id}/microsoft/domain-review/{item.item_id}/resolve",
        json={"actor_type": "USER", "actor_id": ACTOR_ID, "decision": "IGNORE"},
    )
    r = dev_client.post(
        f"/internal/mailboxes/{mailbox_id}/microsoft/domain-review/{item.item_id}/resolve",
        json={
            "actor_type": "USER", "actor_id": ACTOR_ID, "decision": "ALLOW",
            "destination_entity_id": entity.entity_id, "destination_mode": "FIXED",
        },
    )
    assert r.status_code == 409


def _sweep_three_unknown_domain_messages(client, mailbox_id):
    """Operational addendum (ahead of the first real large historical
    sweep) — a small 'N invoices from one new supplier' scenario at HTTP
    level: THREE distinct candidate messages from the SAME unknown
    domain, in one sweep."""
    comp = get_composition()
    now = datetime.now(timezone.utc)
    messages = tuple(
        GraphMessageSummary(
            immutable_id=f"AAMk-unknown-{i}", internet_message_id=f"<u{i}@b>", subject="Invoice attached",
            sender_address="billing@new-supplier.example", sender_display_name="New Supplier", received_at=now,
            has_attachments=False,
        )
        for i in range(1, 4)
    )
    _queue_folder_discovery(comp)
    comp.microsoft_graph_client.queue_delta_result(GraphDeltaPageResult(status=GraphOutcomeStatus.OK, messages=messages, delta_link="d1"))
    comp.microsoft_graph_client.queue_delta_result(GraphDeltaPageResult(status=GraphOutcomeStatus.OK, messages=(), delta_link="d-junk"))
    r = client.post(f"/internal/mailboxes/{mailbox_id}/microsoft/sweep", json={"actor_type": "USER", "actor_id": ACTOR_ID})
    assert r.status_code == 200, r.text
    return r.json()


def test_resolve_domain_review_allow_back_processes_every_historical_candidate_for_the_domain(dev_client):
    """Proof #5 (the key differentiating proof) at the HTTP layer: THREE
    historical `CHECKED_NOT_CANDIDATE` messages from the same unknown
    domain trigger exactly ONE Needs You item, and approving that ONE
    item causes ALL THREE historical messages to be fetched/ingested —
    not just the one that happened to trigger it."""
    mailbox_id, comp, entity = _connected_mailbox_with_entity_seeded(dev_client)
    _sweep_three_unknown_domain_messages(dev_client, mailbox_id)

    items = [
        i for i in comp.needs_you_repository.list_needs_you_items(item_type="MAILBOX_DOMAIN_REVIEW")
        if i.metadata.get("mailbox_id") == mailbox_id
    ]
    assert len(items) == 1  # exactly ONE item, despite THREE candidate messages
    item = items[0]
    assert item.metadata["candidate_message_count"] == 3

    comp.microsoft_graph_client.queue_content_result(
        GraphMessageContentResult(
            status=GraphOutcomeStatus.OK, content=b"From: billing@new-supplier.example\r\nSubject: Invoice\r\n\r\nBody"
        )
    )
    comp.microsoft_graph_client.queue_content_result(
        GraphMessageContentResult(
            status=GraphOutcomeStatus.OK, content=b"From: billing@new-supplier.example\r\nSubject: Invoice\r\n\r\nBody"
        )
    )
    comp.microsoft_graph_client.queue_content_result(
        GraphMessageContentResult(
            status=GraphOutcomeStatus.OK, content=b"From: billing@new-supplier.example\r\nSubject: Invoice\r\n\r\nBody"
        )
    )
    r = dev_client.post(
        f"/internal/mailboxes/{mailbox_id}/microsoft/domain-review/{item.item_id}/resolve",
        json={
            "actor_type": "USER", "actor_id": ACTOR_ID, "decision": "ALLOW",
            "destination_entity_id": entity.entity_id, "destination_mode": "FIXED",
        },
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert len(body["reprocessed_messages"]) == 3
    assert all(m["ingestion_status"] == "INGESTED" for m in body["reprocessed_messages"])

    messages = comp.mailbox_message_repository.list_messages(mailbox_id=mailbox_id)
    matching = [m for m in messages if m.sender_address == "billing@new-supplier.example"]
    assert len(matching) == 3
    assert all(m.ingestion_status == "INGESTED" for m in matching)

    # A genuine double-submit of the same approval never re-fetches/
    # re-ingests anything a second time.
    r2 = dev_client.post(
        f"/internal/mailboxes/{mailbox_id}/microsoft/domain-review/{item.item_id}/resolve",
        json={
            "actor_type": "USER", "actor_id": ACTOR_ID, "decision": "ALLOW",
            "destination_entity_id": entity.entity_id, "destination_mode": "FIXED",
        },
    )
    assert r2.status_code == 200, r2.text
    assert r2.json()["reprocessed_messages"] == []


def test_noustai_imap_mailbox_never_reaches_the_microsoft_router(dev_client):
    mailbox_id = _create_mailbox(dev_client, provider_kind="IMAP", email="ops@noustai.com")
    r = dev_client.post(f"/internal/mailboxes/{mailbox_id}/microsoft/sweep", json={"actor_type": "USER", "actor_id": ACTOR_ID})
    assert r.status_code == 422
    mailbox = dev_client.get(f"/internal/mailboxes/{mailbox_id}").json()
    assert mailbox["connection_state"] == "NOT_CONFIGURED"
