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
from datetime import datetime, timedelta, timezone

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
    GraphMessageHeadersResult,
    GraphMessageSummary,
    GraphOutcomeStatus,
    GraphWellKnownFoldersResult,
    MicrosoftIdentity,
    MicrosoftIdentityResult,
    MicrosoftTokenResult,
)
from services.xero.client import (
    RawXeroBankTransaction,
    RawXeroContact,
    RawXeroPurchaseInvoice,
    XeroBankTransactionsResult,
    XeroConnectionInfo,
    XeroConnectionsResult,
    XeroContactsResult,
    XeroInvoicesResult,
    XeroOutcomeStatus,
    XeroTokenResult,
)
from services.xero.fake_client import fake_token_bundle as fake_xero_token_bundle

ACTOR_ID = "bagman-mailbox-microsoft-endpoint-tests"

#: CD-6 architect amendment (recursive folder discovery) — `run_sweep`
#: now calls `adapter.discover_monitored_folders` once before iterating
#: any folder; queue a plain Inbox+Junk discovery result (matches the
#: OLD, pre-amendment monitored set) so this file's existing HTTP-level
#: sweep tests need no other changes.
_INBOX_FOLDER_ID = "AAMkADinbox000000000000000000000"
_JUNK_FOLDER_ID = "AAMkADjunkemail0000000000000000"

#: CD-6 GUI-operations-foundation follow-on WO (item B) — the real
#: per-message authentication gate now drives directly off
#: `GraphMessageSummary.raw_headers` (never the flat `auth_signals` dict
#: alone). A real, trusted, passing `Authentication-Results` header
#: (`compauth=pass`, the real live-diagnostic shape) for every MUST_READ-
#: path test in this file that exercises the ORDINARY (non-domain-
#: review-triggering) sweep path.
_PASSING_AUTH_HEADERS = (
    {
        "name": "Authentication-Results",
        "value": "spf=pass (sender IP is 10.0.0.1) smtp.mailfrom=example.com;"
        "dkim=pass (signature was verified) header.d=example.com;"
        "dmarc=pass action=none header.from=example.com;"
        "compauth=pass reason=100",
    },
)


def _headers_ok() -> GraphMessageHeadersResult:
    """CD-6 GUI-operations-foundation follow-on WO (item C) — historical
    back-processing (`_reprocess_one_message`) now fetches FRESH headers
    before ever fetching MIME; queue a real, trusted, passing result for
    every such call. Harmless/unused when queued for an ordinary
    (non-reprocessing) sweep call, which never touches this queue at
    all — a SEPARATE queue from `queue_content_result` (see
    `FakeMicrosoftGraphClient`'s own docstring)."""
    return GraphMessageHeadersResult(status=GraphOutcomeStatus.OK, raw_headers=_PASSING_AUTH_HEADERS)


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
        policy="MUST_READ",
        destination_entity_id=None,
        destination_mode="REVIEW_REQUIRED",
        source="OPERATOR",
    )

    now = datetime.now(timezone.utc)
    msg = GraphMessageSummary(
        immutable_id="AAMk-1", internet_message_id="<a@b>", subject="Invoice", sender_address="v@example.com",
        sender_display_name="Vendor", received_at=now, has_attachments=False,
        # CD-6 GUI-operations-foundation follow-on WO — a MUST_READ-
        # policy message's own authentication signals are now checked
        # before MIME fetch; a real passing signal set proves the
        # ORDINARY path here (the dedicated authentication-escalation
        # tests below exercise a failing/missing signal set instead).
        auth_signals={"spf": "pass", "dkim": "pass", "dmarc": "pass"},
        raw_headers=_PASSING_AUTH_HEADERS,
    )
    _queue_folder_discovery(comp)
    comp.microsoft_graph_client.queue_delta_result(GraphDeltaPageResult(status=GraphOutcomeStatus.OK, messages=(msg,), delta_link="d1"))
    comp.microsoft_graph_client.queue_headers_result(_headers_ok())
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

    comp.microsoft_graph_client.queue_headers_result(_headers_ok())
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
    assert body["mailbox_domain_rule"]["policy"] == "MUST_READ"
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

    comp.microsoft_graph_client.queue_headers_result(_headers_ok())
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
    assert body["mailbox_domain_rule"]["policy"] == "BLACKLIST"
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

    comp.microsoft_graph_client.queue_headers_result(_headers_ok())
    comp.microsoft_graph_client.queue_content_result(
        GraphMessageContentResult(
            status=GraphOutcomeStatus.OK, content=b"From: billing@new-supplier.example\r\nSubject: Invoice\r\n\r\nBody"
        )
    )
    comp.microsoft_graph_client.queue_headers_result(_headers_ok())
    comp.microsoft_graph_client.queue_content_result(
        GraphMessageContentResult(
            status=GraphOutcomeStatus.OK, content=b"From: billing@new-supplier.example\r\nSubject: Invoice\r\n\r\nBody"
        )
    )
    comp.microsoft_graph_client.queue_headers_result(_headers_ok())
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


# ---------------------------------------------------------------------
# batch domain-review resolution — CD-6 bounded Xero-assisted supplier-
# domain-correlation addendum (backend-only; efficient review of the 90
# real OPEN MAILBOX_DOMAIN_REVIEW items a Phase A historical sweep
# produced, before any bulk domain approval).
# ---------------------------------------------------------------------


def _sweep_unknown_domain(client, mailbox_id, *, domain: str, local_part: str = "billing"):
    """Generalises `_sweep_unknown_domain_message` (fixed to
    `new-supplier.example`) to an arbitrary sender domain, so a batch
    test can raise several DISTINCT `MAILBOX_DOMAIN_REVIEW` items in one
    mailbox."""
    comp = get_composition()
    now = datetime.now(timezone.utc)
    msg = GraphMessageSummary(
        immutable_id=f"AAMk-{domain}-1",
        internet_message_id=f"<{domain}-1@b>",
        subject="Invoice attached",
        sender_address=f"{local_part}@{domain}",
        sender_display_name="Supplier",
        received_at=now,
        has_attachments=False,
    )
    _queue_folder_discovery(comp)
    comp.microsoft_graph_client.queue_delta_result(GraphDeltaPageResult(status=GraphOutcomeStatus.OK, messages=(msg,), delta_link=f"d-{domain}-1"))
    comp.microsoft_graph_client.queue_delta_result(GraphDeltaPageResult(status=GraphOutcomeStatus.OK, messages=(), delta_link=f"d-{domain}-junk"))
    r = client.post(f"/internal/mailboxes/{mailbox_id}/microsoft/sweep", json={"actor_type": "USER", "actor_id": ACTOR_ID})
    assert r.status_code == 200, r.text
    return r.json()


def _open_domain_review_item_for_domain(comp, mailbox_id, domain):
    items = [
        i
        for i in comp.needs_you_repository.list_needs_you_items(item_type="MAILBOX_DOMAIN_REVIEW", status="OPEN")
        if i.metadata.get("mailbox_id") == mailbox_id and i.metadata.get("sender_domain") == domain
    ]
    assert len(items) == 1, f"expected exactly one OPEN item for domain {domain!r}, got {len(items)}"
    return items[0]


def test_batch_resolve_three_domains_matches_three_individual_calls_end_state(dev_client):
    mailbox_id, comp, entity = _connected_mailbox_with_entity_seeded(dev_client)
    domains = ["batch-supplier-one.example", "batch-supplier-two.example", "batch-supplier-three.example"]
    for domain in domains:
        _sweep_unknown_domain(dev_client, mailbox_id, domain=domain)

    items = [_open_domain_review_item_for_domain(comp, mailbox_id, d) for d in domains]

    for _ in domains:
        comp.microsoft_graph_client.queue_headers_result(_headers_ok())
        comp.microsoft_graph_client.queue_content_result(
            GraphMessageContentResult(status=GraphOutcomeStatus.OK, content=b"From: x\r\nSubject: Invoice\r\n\r\nBody")
        )

    r = dev_client.post(
        f"/internal/mailboxes/{mailbox_id}/microsoft/domain-review/batch-resolve",
        json={
            "actor_type": "USER",
            "actor_id": ACTOR_ID,
            "items": [
                {
                    "item_id": item.item_id,
                    "decision": "ALLOW",
                    "destination_entity_id": entity.entity_id,
                    "destination_mode": "FIXED",
                }
                for item in items
            ],
        },
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["count"] == 3
    assert body["succeeded_count"] == 3
    assert body["failed_count"] == 0
    for result in body["results"]:
        assert result["ok"] is True
        assert result["needs_you_item"]["status"] == "RESOLVED"
        assert result["mailbox_domain_rule"]["policy"] == "MUST_READ"
        assert len(result["reprocessed_messages"]) == 1
        assert result["reprocessed_messages"][0]["ingestion_status"] == "INGESTED"
        assert result["error"] is None

    # Same end-state three individual calls would produce: three real
    # MailboxDomainRule rows, three RESOLVED items, three ALLOWED audit
    # events.
    rules = dev_client.get(f"/internal/mailboxes/{mailbox_id}/microsoft/domain-rules").json()
    assert rules["count"] == 3
    assert {r["sender_domain"] for r in rules["items"]} == set(domains)
    for item in items:
        assert comp.needs_you_repository.get_needs_you_item(item.item_id).status == "RESOLVED"
    audit_events = comp.api.audit_repository.list_recent(limit=50, event_type_prefix="MAILBOX_DOMAIN_RULE_MUST_READ")
    assert len(audit_events) == 3


def test_batch_resolve_one_bad_item_never_blocks_the_others(dev_client):
    mailbox_id, comp, entity = _connected_mailbox_with_entity_seeded(dev_client)
    domains = ["batch-good-one.example", "batch-good-two.example"]
    for domain in domains:
        _sweep_unknown_domain(dev_client, mailbox_id, domain=domain)
    items = [_open_domain_review_item_for_domain(comp, mailbox_id, d) for d in domains]

    # A second mailbox (distinct email — mailbox email uniqueness is
    # global), with its own OPEN item — used to prove an item_id
    # belonging to a DIFFERENT mailbox is reported as a clean per-item
    # failure, not silently accepted.
    other_mailbox_id = _create_mailbox(dev_client, email="ops-other@infosecurs.com")
    _connect_and_complete(dev_client, other_mailbox_id, email="ops-other@infosecurs.com")
    other_comp = get_composition()
    other_comp.api.register_entity(
        entity_type="COMPANY",
        canonical_name="TEST_DOMAIN_REVIEW_OTHER_LTD",
        display_name="Test Other Ltd",
        status="ACTIVE",
        actor_type="SYSTEM",
        actor_id=ACTOR_ID,
        fiscal_year_start_month_day="01-01",
        historical_floor_override_at=datetime(2025, 1, 1, tzinfo=timezone.utc),
    )
    _sweep_unknown_domain(dev_client, other_mailbox_id, domain="other-mailbox-domain.example")
    foreign_item = _open_domain_review_item_for_domain(other_comp, other_mailbox_id, "other-mailbox-domain.example")

    for _ in domains:
        comp.microsoft_graph_client.queue_headers_result(_headers_ok())
        comp.microsoft_graph_client.queue_content_result(
            GraphMessageContentResult(status=GraphOutcomeStatus.OK, content=b"From: x\r\nSubject: Invoice\r\n\r\nBody")
        )

    r = dev_client.post(
        f"/internal/mailboxes/{mailbox_id}/microsoft/domain-review/batch-resolve",
        json={
            "actor_type": "USER",
            "actor_id": ACTOR_ID,
            "items": [
                {
                    "item_id": items[0].item_id,
                    "decision": "ALLOW",
                    "destination_entity_id": entity.entity_id,
                    "destination_mode": "FIXED",
                },
                # A genuinely non-existent item_id.
                {"item_id": "does-not-exist", "decision": "IGNORE"},
                {
                    "item_id": items[1].item_id,
                    "decision": "ALLOW",
                    "destination_entity_id": entity.entity_id,
                    "destination_mode": "FIXED",
                },
                # An item_id that is real, but belongs to a DIFFERENT
                # mailbox than the one this batch call is scoped to.
                {"item_id": foreign_item.item_id, "decision": "IGNORE"},
            ],
        },
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["count"] == 4
    assert body["succeeded_count"] == 2
    assert body["failed_count"] == 2

    results_by_item_id = {res["item_id"]: res for res in body["results"]}
    assert results_by_item_id[items[0].item_id]["ok"] is True
    assert results_by_item_id[items[1].item_id]["ok"] is True
    assert results_by_item_id["does-not-exist"]["ok"] is False
    assert results_by_item_id["does-not-exist"]["error_type"] == "NotFoundError"
    assert results_by_item_id[foreign_item.item_id]["ok"] is False
    assert results_by_item_id[foreign_item.item_id]["error_type"] == "ValidationError"

    # Both VALID items in the same batch fully succeeded despite the two
    # bad entries — never lost, never corrupted.
    assert comp.needs_you_repository.get_needs_you_item(items[0].item_id).status == "RESOLVED"
    assert comp.needs_you_repository.get_needs_you_item(items[1].item_id).status == "RESOLVED"
    rules = dev_client.get(f"/internal/mailboxes/{mailbox_id}/microsoft/domain-rules").json()
    assert rules["count"] == 2

    # The foreign mailbox's own item was never touched by this batch.
    assert other_comp.needs_you_repository.get_needs_you_item(foreign_item.item_id).status == "OPEN"


def test_batch_resolve_requires_at_least_one_item(dev_client):
    mailbox_id, _comp, _entity = _connected_mailbox_with_entity_seeded(dev_client)
    r = dev_client.post(
        f"/internal/mailboxes/{mailbox_id}/microsoft/domain-review/batch-resolve",
        json={"actor_type": "USER", "actor_id": ACTOR_ID, "items": []},
    )
    assert r.status_code == 422


# ---------------------------------------------------------------------
# GET domain-review — CD-6 GUI-operations-foundation WO (the new
# per-mailbox list endpoint feeding the domain-review batch-triage GUI
# page).
# ---------------------------------------------------------------------


def test_list_domain_review_scopes_to_the_requested_mailbox_only(dev_client):
    mailbox_id, comp, _entity = _connected_mailbox_with_entity_seeded(dev_client)
    _sweep_unknown_domain(dev_client, mailbox_id, domain="scope-test-one.example")

    other_mailbox_id = _create_mailbox(dev_client, email="ops-scope@infosecurs.com")
    _connect_and_complete(dev_client, other_mailbox_id, email="ops-scope@infosecurs.com")
    other_comp = get_composition()
    other_comp.api.register_entity(
        entity_type="COMPANY", canonical_name="TEST_DOMAIN_REVIEW_SCOPE_LTD", display_name="Test Scope Ltd",
        status="ACTIVE", actor_type="SYSTEM", actor_id=ACTOR_ID,
        fiscal_year_start_month_day="01-01", historical_floor_override_at=datetime(2025, 1, 1, tzinfo=timezone.utc),
    )
    _sweep_unknown_domain(dev_client, other_mailbox_id, domain="scope-test-two.example")

    r = dev_client.get(f"/internal/mailboxes/{mailbox_id}/microsoft/domain-review")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["mailbox_id"] == mailbox_id
    assert body["count"] == 1
    assert body["items"][0]["metadata"]["sender_domain"] == "scope-test-one.example"
    # The second mailbox's own item never appears in THIS mailbox's list.
    assert all(i["metadata"]["mailbox_id"] == mailbox_id for i in body["items"])


def test_list_domain_review_defaults_to_open_and_status_override_works(dev_client):
    mailbox_id, comp, _entity = _connected_mailbox_with_entity_seeded(dev_client)
    _sweep_unknown_domain(dev_client, mailbox_id, domain="default-status-test.example")
    item = _open_domain_review_item_for_domain(comp, mailbox_id, "default-status-test.example")
    dev_client.post(
        f"/internal/mailboxes/{mailbox_id}/microsoft/domain-review/{item.item_id}/resolve",
        json={"actor_type": "USER", "actor_id": ACTOR_ID, "decision": "IGNORE"},
    )

    # Default (no `status` query param at all): OPEN only — the one
    # item raised above is now RESOLVED, so the default listing is empty.
    default_r = dev_client.get(f"/internal/mailboxes/{mailbox_id}/microsoft/domain-review")
    assert default_r.status_code == 200, default_r.text
    assert default_r.json()["count"] == 0

    # An explicit `status` override reaches the now-RESOLVED item.
    explicit_r = dev_client.get(
        f"/internal/mailboxes/{mailbox_id}/microsoft/domain-review", params={"status": "RESOLVED"}
    )
    assert explicit_r.status_code == 200, explicit_r.text
    assert explicit_r.json()["count"] == 1
    assert explicit_r.json()["items"][0]["item_id"] == item.item_id


def test_domain_review_endpoints_reject_a_non_microsoft_mailbox(dev_client):
    mailbox_id = _create_mailbox(dev_client, provider_kind="IMAP", email="ops-domain-review@noustai.com")
    r_get = dev_client.get(f"/internal/mailboxes/{mailbox_id}/microsoft/domain-review")
    assert r_get.status_code == 422
    r_post = dev_client.post(
        f"/internal/mailboxes/{mailbox_id}/microsoft/domain-review/xero-correlate",
        json={"entity_id": "whatever", "actor_type": "USER", "actor_id": ACTOR_ID},
    )
    assert r_post.status_code == 422


# ---------------------------------------------------------------------
# GET domain-review — mailbox-evidence-based triage addendum
# (review_priority / discovery_reason_counts). A real sweep drives 5
# distinct candidate messages for ONE domain, each hitting a DIFFERENT
# one of services.mailbox.domain_review_priority's 5 discovery-reason
# buckets, deliberately spanning >30 days with a high attachment ratio
# — this is the module's own worked "HIGH" shape (see that module's
# docstring Example A), proven here end-to-end through the real
# GET response rather than re-asserted in isolation.
# ---------------------------------------------------------------------


def _sweep_five_bucket_domain(client, mailbox_id, *, domain="priority-test.example"):
    comp = get_composition()
    now = datetime.now(timezone.utc)
    messages = (
        # invoice_subject_signal_count
        GraphMessageSummary(
            immutable_id=f"AAMk-{domain}-1", internet_message_id=f"<{domain}-1@b>",
            subject="Invoice ready", sender_address=f"billing@{domain}", sender_display_name="Supplier",
            received_at=now - timedelta(days=40), has_attachments=False,
        ),
        # receipt_subject_signal_count
        GraphMessageSummary(
            immutable_id=f"AAMk-{domain}-2", internet_message_id=f"<{domain}-2@b>",
            subject="See attached receipt", sender_address=f"billing@{domain}", sender_display_name="Supplier",
            received_at=now - timedelta(days=30), has_attachments=True,
            attachment_metadata=({"filename": "receipt.pdf", "content_type": "application/pdf", "size_bytes": 100},),
        ),
        # other_bounded_heuristic_reason_count (attachment filename 'statement')
        GraphMessageSummary(
            immutable_id=f"AAMk-{domain}-3", internet_message_id=f"<{domain}-3@b>",
            subject="FYI", sender_address=f"billing@{domain}", sender_display_name="Supplier",
            received_at=now - timedelta(days=20), has_attachments=True,
            attachment_metadata=({"filename": "statement.pdf", "content_type": "application/octet-stream", "size_bytes": 200},),
        ),
        # accounting_document_attachment_signal_count (content_type match, no keyword anywhere)
        GraphMessageSummary(
            immutable_id=f"AAMk-{domain}-4", internet_message_id=f"<{domain}-4@b>",
            subject="Scan", sender_address=f"billing@{domain}", sender_display_name="Supplier",
            received_at=now - timedelta(days=10), has_attachments=True,
            attachment_metadata=({"filename": "scan001.jpg", "content_type": "application/pdf", "size_bytes": 300},),
        ),
        # invoice_like_attachment_filename_count
        GraphMessageSummary(
            immutable_id=f"AAMk-{domain}-5", internet_message_id=f"<{domain}-5@b>",
            subject="Latest doc", sender_address=f"billing@{domain}", sender_display_name="Supplier",
            received_at=now, has_attachments=True,
            attachment_metadata=({"filename": "invoice_2026.pdf", "content_type": "application/octet-stream", "size_bytes": 400},),
        ),
    )
    _queue_folder_discovery(comp)
    comp.microsoft_graph_client.queue_delta_result(GraphDeltaPageResult(status=GraphOutcomeStatus.OK, messages=messages, delta_link=f"d-{domain}-1"))
    comp.microsoft_graph_client.queue_delta_result(GraphDeltaPageResult(status=GraphOutcomeStatus.OK, messages=(), delta_link=f"d-{domain}-junk"))
    r = client.post(f"/internal/mailboxes/{mailbox_id}/microsoft/sweep", json={"actor_type": "USER", "actor_id": ACTOR_ID})
    assert r.status_code == 200, r.text
    return r.json()


def test_list_domain_review_includes_review_priority_and_discovery_reason_counts(dev_client):
    mailbox_id, comp, _entity = _connected_mailbox_with_entity_seeded(dev_client)
    _sweep_five_bucket_domain(dev_client, mailbox_id)

    r = dev_client.get(f"/internal/mailboxes/{mailbox_id}/microsoft/domain-review")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["count"] == 1
    item = body["items"][0]

    assert item["metadata"]["candidate_message_count"] == 5
    assert item["metadata"]["attachment_bearing_count"] == 4

    assert item["discovery_reason_counts"] == {
        "invoice_subject_signal_count": 1,
        "receipt_subject_signal_count": 1,
        "invoice_like_attachment_filename_count": 1,
        "accounting_document_attachment_signal_count": 1,
        "other_bounded_heuristic_reason_count": 1,
    }
    # 5 candidates (>=5, +3), 4/5 attachment ratio (>=0.5, +3), ~40-day
    # span (>=30, +2), no Xero correlation yet (+0), 5 distinct buckets
    # hit (diversity, +1) => score 9 => HIGH. Mirrors
    # services/mailbox/domain_review_priority.py's own worked Example A.
    assert item["review_priority"] == "HIGH"

    # Presentation-layer only — never persisted onto the real
    # NeedsYouItem.metadata this GET response was built from.
    stored = comp.needs_you_repository.get_needs_you_item(item["item_id"])
    assert "review_priority" not in stored.metadata
    assert "discovery_reason_counts" not in stored.metadata


def test_review_priority_reflects_shared_domain_xero_cap(dev_client):
    """Same 5-bucket, high-volume/attachment/span candidate shape as
    the HIGH test above, but the sender domain itself is a real
    `SHARED_PUBLIC_EMAIL_DOMAINS` entry (`gmail.com`) with a real,
    qualifying Xero Contact behind it — end-to-end proof (real sweep +
    real correlation run + real GET) that `review_priority` is never
    `"HIGH"` once `xero_correlation_class` comes back
    `SHARED_DOMAIN_REQUIRES_MANUAL_REVIEW`, mirroring
    `services/mailbox/domain_review_priority.py`'s own worked
    Example D."""
    mailbox_id, comp, entity = _connected_mailbox_with_entity_seeded(dev_client)
    _sweep_five_bucket_domain(dev_client, mailbox_id, domain="gmail.com")

    _connect_xero_entity(dev_client, entity.entity_id)
    comp.xero_accounting_client.queue_contacts_result(
        XeroContactsResult(
            status=XeroOutcomeStatus.OK,
            contacts=(
                RawXeroContact(
                    contact_id="ct-shared",
                    name="Shared Domain Co",
                    email_address="billing@gmail.com",
                    is_customer=False,
                    is_supplier=True,
                    contact_status="ACTIVE",
                ),
            ),
        )
    )
    comp.xero_accounting_client.queue_invoices_result(
        XeroInvoicesResult(
            status=XeroOutcomeStatus.OK,
            invoices=(
                RawXeroPurchaseInvoice(
                    invoice_id="inv-shared-1",
                    contact_id="ct-shared",
                    invoice_type="ACCPAY",
                    invoice_date=datetime(2026, 3, 1, tzinfo=timezone.utc),
                    status="AUTHORISED",
                ),
            ),
        )
    )
    comp.xero_accounting_client.queue_bank_transactions_result(
        XeroBankTransactionsResult(status=XeroOutcomeStatus.OK, bank_transactions=())
    )

    r = dev_client.post(
        f"/internal/mailboxes/{mailbox_id}/microsoft/domain-review/xero-correlate",
        json={"entity_id": entity.entity_id, "actor_type": "USER", "actor_id": ACTOR_ID},
    )
    assert r.status_code == 200, r.text

    r_get = dev_client.get(f"/internal/mailboxes/{mailbox_id}/microsoft/domain-review")
    assert r_get.status_code == 200, r_get.text
    item = r_get.json()["items"][0]
    assert item["metadata"]["xero_correlation_class"] == "SHARED_DOMAIN_REQUIRES_MANUAL_REVIEW"
    assert item["review_priority"] != "HIGH"
    assert item["review_priority"] == "MEDIUM"


# ---------------------------------------------------------------------
# POST domain-review/xero-correlate — CD-6 GUI-operations-foundation WO
# (the new endpoint that calls
# services.xero.supplier_correlation.correlate_xero_suppliers_for_open_domain_review_items
# via a real, resolved-and-refreshed Xero access token).
# ---------------------------------------------------------------------

_XERO_CORRELATE_CONTACTS = (
    RawXeroContact(
        contact_id="ct-strong",
        name="Strong Supplier Ltd",
        email_address="billing@correlate-strong.example",
        is_customer=False,
        is_supplier=True,
        contact_status="ACTIVE",
    ),
)
_XERO_CORRELATE_INVOICES = (
    RawXeroPurchaseInvoice(
        invoice_id="inv-1",
        contact_id="ct-strong",
        invoice_type="ACCPAY",
        invoice_date=datetime(2026, 3, 1, tzinfo=timezone.utc),
        status="AUTHORISED",
    ),
)


def _connect_xero_entity(client, entity_id: str, *, tenant_id="tenant-xero-correlate", tenant_name="Infosecurs Limited") -> None:
    """Connect `entity_id`'s `XeroConnection` via the real HTTP OAuth
    flow (mirrors tests/app_api/test_xero_endpoints.py
    ::_connect_and_complete exactly — that helper is file-local there,
    not importable, so it is reproduced here rather than reached into
    across test files), then writes a FRESH (not near-expiry) token
    into the token store — the ordinary "already connected, tokens
    fresh" starting state most of this file's own xero-correlate tests
    want."""
    comp = get_composition()
    r = client.post("/internal/xero/connect", json={"entity_id": entity_id, "actor_type": "USER", "actor_id": ACTOR_ID})
    assert r.status_code == 201, r.text
    state = urllib.parse.parse_qs(urllib.parse.urlparse(r.json()["authorize_url"]).query)["state"][0]

    comp.xero_oauth_client.queue_exchange_result(XeroTokenResult(status=XeroOutcomeStatus.OK, tokens=fake_xero_token_bundle()))
    comp.xero_oauth_client.queue_connections_result(
        XeroConnectionsResult(
            status=XeroOutcomeStatus.OK,
            connections=(XeroConnectionInfo(connection_id="c1", tenant_id=tenant_id, tenant_name=tenant_name, tenant_type="ORGANISATION"),),
        )
    )
    r = client.get("/internal/xero/oauth/callback", params={"code": "abc123", "state": state})
    assert r.status_code == 200, r.text
    comp.xero_token_store.write(
        entity_id, access_token="tok", refresh_token="ref", expires_at=fake_xero_token_bundle().expires_at
    )


def _queue_xero_correlation_success(comp, *, bank_transactions: tuple = ()) -> None:
    comp.xero_accounting_client.queue_contacts_result(
        XeroContactsResult(status=XeroOutcomeStatus.OK, contacts=_XERO_CORRELATE_CONTACTS)
    )
    comp.xero_accounting_client.queue_invoices_result(
        XeroInvoicesResult(status=XeroOutcomeStatus.OK, invoices=_XERO_CORRELATE_INVOICES)
    )
    # CD-6 second-correlation-source WO: the endpoint now also calls
    # `list_bank_transactions` unconditionally — empty by default (most
    # of this file's own scenarios only care about the invoice-based
    # path); `test_xero_correlate_*_bank_spend*` below queues a
    # non-empty tuple.
    comp.xero_accounting_client.queue_bank_transactions_result(
        XeroBankTransactionsResult(status=XeroOutcomeStatus.OK, bank_transactions=bank_transactions)
    )


def test_xero_correlate_success_enriches_open_items_and_returns_summary(dev_client):
    """(a) a successful correlation run returns ok:true and a correct
    summary, and the items' metadata is genuinely enriched."""
    mailbox_id, comp, entity = _connected_mailbox_with_entity_seeded(dev_client)
    _sweep_unknown_domain(dev_client, mailbox_id, domain="correlate-strong.example")
    item = _open_domain_review_item_for_domain(comp, mailbox_id, "correlate-strong.example")

    _connect_xero_entity(dev_client, entity.entity_id)
    _queue_xero_correlation_success(comp)

    r = dev_client.post(
        f"/internal/mailboxes/{mailbox_id}/microsoft/domain-review/xero-correlate",
        json={"entity_id": entity.entity_id, "actor_type": "USER", "actor_id": ACTOR_ID},
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["ok"] is True
    assert body["contacts_read"] == 1
    assert body["purchase_invoices_examined"] == 1
    assert body["bank_transactions_examined"] == 0
    assert body["domain_review_items_updated"] == 1
    assert body["strong_purchase_bill_count"] == 1
    assert body["strong_bank_spend_count"] == 0

    updated = comp.needs_you_repository.get_needs_you_item(item.item_id)
    assert updated.metadata["xero_correlation_class"] == "STRONG_PURCHASE_BILL"
    assert updated.metadata["xero_contact_match"] is True
    assert updated.metadata["xero_purchase_invoice_count"] == 1
    assert updated.metadata["xero_correlated_at"] is not None
    assert updated.status == "OPEN"  # correlation never resolves anything itself


def test_xero_correlate_bank_spend_only_enriches_items_and_wins_strong_bank_spend(dev_client):
    """CD-6 second-correlation-source WO: the real, live finding that
    Infosecurs has zero ACCPAY invoices but real SPEND BankTransactions
    — proves the endpoint's own summary/metadata reflect
    `STRONG_BANK_SPEND` end-to-end through the real HTTP surface, not
    only at the domain layer (already covered by
    `tests/integration/test_xero_supplier_correlation.py`)."""
    mailbox_id, comp, entity = _connected_mailbox_with_entity_seeded(dev_client)
    _sweep_unknown_domain(dev_client, mailbox_id, domain="correlate-strong.example")
    item = _open_domain_review_item_for_domain(comp, mailbox_id, "correlate-strong.example")

    _connect_xero_entity(dev_client, entity.entity_id)
    _queue_xero_correlation_success(
        comp,
        bank_transactions=(
            RawXeroBankTransaction(
                bank_transaction_id="bt-1",
                transaction_type="SPEND",
                status="AUTHORISED",
                date=datetime(2026, 3, 1, tzinfo=timezone.utc),
                contact_id="ct-strong",
                reference="ref",
                total=250.0,
                currency_code="GBP",
                is_reconciled=True,
            ),
        ),
    )
    # No ACCPAY invoice history for this run (the real Infosecurs shape).
    comp.xero_accounting_client._invoices_queue.clear()
    comp.xero_accounting_client.queue_invoices_result(XeroInvoicesResult(status=XeroOutcomeStatus.OK, invoices=()))

    r = dev_client.post(
        f"/internal/mailboxes/{mailbox_id}/microsoft/domain-review/xero-correlate",
        json={"entity_id": entity.entity_id, "actor_type": "USER", "actor_id": ACTOR_ID},
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["ok"] is True
    assert body["bank_transactions_examined"] == 1
    assert body["strong_bank_spend_count"] == 1
    assert body["strong_purchase_bill_count"] == 0

    updated = comp.needs_you_repository.get_needs_you_item(item.item_id)
    assert updated.metadata["xero_correlation_class"] == "STRONG_BANK_SPEND"
    assert updated.metadata["xero_bank_spend_count"] == 1
    assert updated.metadata["xero_bank_spend_total_amount"] == 250.0
    assert updated.metadata["xero_bank_spend_currency"] == "GBP"


def test_xero_correlate_failure_never_touches_any_item_metadata(dev_client):
    """(b) a fake Xero client returning AUTH_ERROR (simulating the
    CURRENT real scope-insufficient Infosecurs state) returns ok:false
    with a clear error, and — critically — does NOT touch any item's
    metadata at all."""
    mailbox_id, comp, entity = _connected_mailbox_with_entity_seeded(dev_client)
    _sweep_unknown_domain(dev_client, mailbox_id, domain="correlate-fail.example")
    item = _open_domain_review_item_for_domain(comp, mailbox_id, "correlate-fail.example")

    _connect_xero_entity(dev_client, entity.entity_id)
    comp.xero_accounting_client.queue_contacts_result(
        XeroContactsResult(
            status=XeroOutcomeStatus.AUTH_ERROR,
            error_detail="insufficient scope — accounting.contacts.read not granted",
        )
    )

    r = dev_client.post(
        f"/internal/mailboxes/{mailbox_id}/microsoft/domain-review/xero-correlate",
        json={"entity_id": entity.entity_id, "actor_type": "USER", "actor_id": ACTOR_ID},
    )
    assert r.status_code == 200, r.text  # a correlation failure is DATA, never an HTTP-level failure
    body = r.json()
    assert body["ok"] is False
    assert body["error_type"] == "XeroSupplierCorrelationFailedError"
    assert body["error"]  # a real, non-empty message

    untouched = comp.needs_you_repository.get_needs_you_item(item.item_id)
    assert "xero_correlation_class" not in untouched.metadata
    assert untouched.status == "OPEN"


def test_xero_correlate_refreshes_a_near_expiry_token_exactly_once(dev_client):
    """(c) a token close to expires_at triggers exactly one refresh
    before the real Xero call, mirroring run_sync's own already-tested
    refresh behaviour (same FakeXeroOAuthClient fixture)."""
    mailbox_id, comp, entity = _connected_mailbox_with_entity_seeded(dev_client)
    _sweep_unknown_domain(dev_client, mailbox_id, domain="correlate-refresh.example")

    _connect_xero_entity(dev_client, entity.entity_id)
    # Force the pre-emptive refresh path (mirrors
    # tests/integration/test_xero_domain.py's own
    # "already-expired token" technique).
    comp.xero_token_store.write(
        entity.entity_id, access_token="stale", refresh_token="ref",
        expires_at=datetime.now(timezone.utc) - timedelta(seconds=5),
    )
    comp.xero_oauth_client.queue_refresh_result(
        XeroTokenResult(status=XeroOutcomeStatus.OK, tokens=fake_xero_token_bundle())
    )
    _queue_xero_correlation_success(comp)

    r = dev_client.post(
        f"/internal/mailboxes/{mailbox_id}/microsoft/domain-review/xero-correlate",
        json={"entity_id": entity.entity_id, "actor_type": "USER", "actor_id": ACTOR_ID},
    )
    assert r.status_code == 200, r.text
    assert r.json()["ok"] is True

    refresh_calls = [c for c in comp.xero_oauth_client.token_calls if c.kind == "refresh"]
    assert len(refresh_calls) == 1
    # The refreshed access token — not the stale one — was actually used
    # for the real Contacts/Invoices calls.
    assert comp.xero_accounting_client.contact_calls[-1][1] == "fake-access-token"
    assert comp.xero_accounting_client.invoice_calls[-1][1] == "fake-access-token"


def test_xero_correlate_entity_with_no_xero_connection_at_all_is_409(dev_client):
    """(d) entity_id with no XeroConnection at all raises/maps to the
    same 409 this router's other Xero-touching code paths already
    produce for that case."""
    mailbox_id, _comp, entity = _connected_mailbox_with_entity_seeded(dev_client)
    r = dev_client.post(
        f"/internal/mailboxes/{mailbox_id}/microsoft/domain-review/xero-correlate",
        json={"entity_id": entity.entity_id, "actor_type": "USER", "actor_id": ACTOR_ID},
    )
    assert r.status_code == 409, r.text


def test_xero_correlate_entity_with_non_connected_xero_connection_is_409(dev_client):
    """(d) a non-CONNECTED XeroConnection (here: PENDING — the OAuth
    flow was begun but never completed) is the same genuine
    precondition failure as "no connection at all", not a different
    error shape."""
    mailbox_id, _comp, entity = _connected_mailbox_with_entity_seeded(dev_client)
    r = dev_client.post(
        "/internal/xero/connect", json={"entity_id": entity.entity_id, "actor_type": "USER", "actor_id": ACTOR_ID}
    )
    assert r.status_code == 201, r.text  # PENDING — never completed

    r2 = dev_client.post(
        f"/internal/mailboxes/{mailbox_id}/microsoft/domain-review/xero-correlate",
        json={"entity_id": entity.entity_id, "actor_type": "USER", "actor_id": ACTOR_ID},
    )
    assert r2.status_code == 409, r2.text


def test_xero_correlate_unknown_entity_id_is_404(dev_client):
    mailbox_id, _comp, _entity = _connected_mailbox_with_entity_seeded(dev_client)
    r = dev_client.post(
        f"/internal/mailboxes/{mailbox_id}/microsoft/domain-review/xero-correlate",
        json={"entity_id": "not-a-real-entity", "actor_type": "USER", "actor_id": ACTOR_ID},
    )
    assert r.status_code == 404, r.text


# =======================================================================
# CD-6 GUI-operations-foundation follow-on WO — three-state policy model
# at the HTTP layer: KEEP_GRAY, the document-level COMPANY_REQUIRED
# scoping proof (WO required test #6), and the audit-trail before/after
# proof (WO required test #10).
# =======================================================================

_PASSING_AUTH_SIGNALS = {"spf": "pass", "dkim": "pass", "dmarc": "pass"}


def _sweep_must_read_review_required_message(client, mailbox_id, *, msg_id, domain="review-required.example"):
    """Sweeps ONE message from `domain` under a MUST_READ +
    REVIEW_REQUIRED rule for it — real end-to-end evidence-creation +
    COMPANY_REQUIRED-raise path (never the discovery/candidate path)."""
    comp = get_composition()
    comp.mailbox_domain_rule_repository.upsert_rule(
        mailbox_id=mailbox_id, sender_domain=domain, match_mode="EXACT", policy="MUST_READ",
        destination_entity_id=None, destination_mode="REVIEW_REQUIRED", source="OPERATOR",
    )
    now = datetime.now(timezone.utc)
    msg = GraphMessageSummary(
        immutable_id=msg_id, internet_message_id=f"<{msg_id}@b>", subject="Invoice",
        sender_address=f"billing@{domain}", sender_display_name="Supplier", received_at=now,
        has_attachments=False, auth_signals=_PASSING_AUTH_SIGNALS, raw_headers=_PASSING_AUTH_HEADERS,
    )
    _queue_folder_discovery(comp)
    comp.microsoft_graph_client.queue_delta_result(GraphDeltaPageResult(status=GraphOutcomeStatus.OK, messages=(msg,), delta_link=f"d-{msg_id}"))
    comp.microsoft_graph_client.queue_headers_result(_headers_ok())
    comp.microsoft_graph_client.queue_content_result(
        GraphMessageContentResult(status=GraphOutcomeStatus.OK, content=f"From: billing@{domain}\r\nSubject: Invoice\r\n\r\nBody".encode())
    )
    comp.microsoft_graph_client.queue_delta_result(GraphDeltaPageResult(status=GraphOutcomeStatus.OK, messages=(), delta_link=f"d-{msg_id}-junk"))
    r = client.post(f"/internal/mailboxes/{mailbox_id}/microsoft/sweep", json={"actor_type": "USER", "actor_id": ACTOR_ID})
    assert r.status_code == 200, r.text
    return r.json()


def test_resolving_one_company_required_item_never_touches_the_domain_rule(dev_client):
    """WO required test #6, verbatim: resolving ONE COMPANY_REQUIRED
    item (via the EXISTING, untouched generic resolve endpoint) does
    NOT change the MailboxDomainRule's own destination_mode/
    destination_entity_id — a second, different ambiguous message from
    the same domain still creates its OWN separate COMPANY_REQUIRED
    item, still REVIEW_REQUIRED at the rule level."""
    mailbox_id, comp, entity = _connected_mailbox_with_entity_seeded(dev_client)
    _sweep_must_read_review_required_message(dev_client, mailbox_id, msg_id="doc-1")
    _sweep_must_read_review_required_message(dev_client, mailbox_id, msg_id="doc-2")

    company_items = [
        i for i in comp.needs_you_repository.list_needs_you_items(item_type="COMPANY_REQUIRED")
        if i.metadata.get("mailbox_id") == mailbox_id
    ]
    assert len(company_items) == 2
    first, second = company_items[0], company_items[1]
    assert first.source_object_reference != second.source_object_reference

    rule_before = comp.mailbox_domain_rule_repository.find_for_sender(
        mailbox_id=mailbox_id, sender_domain="review-required.example"
    )
    assert rule_before.destination_mode == "REVIEW_REQUIRED"
    assert rule_before.destination_entity_id is None

    r = dev_client.post(
        f"/internal/needs-you/{first.item_id}/resolve",
        json={
            "actor_type": "USER", "actor_id": ACTOR_ID,
            "resolution": {"entity_id": entity.entity_id, "what": "Invoice", "why": "Ops"},
        },
    )
    assert r.status_code == 200, r.text
    assert r.json()["status"] == "RESOLVED"

    # The rule itself — never touched by resolving one document's
    # own destination question.
    rule_after = comp.mailbox_domain_rule_repository.find_for_sender(
        mailbox_id=mailbox_id, sender_domain="review-required.example"
    )
    assert rule_after.destination_mode == "REVIEW_REQUIRED"
    assert rule_after.destination_entity_id is None
    assert rule_after.rule_id == rule_before.rule_id
    assert rule_after.updated_at == rule_before.updated_at

    # The SECOND item is still open, completely unaffected.
    still_open = comp.needs_you_repository.get_needs_you_item(second.item_id)
    assert still_open.status == "OPEN"


def test_resolving_mailbox_origin_company_required_with_missing_entity_id_fails_validation(dev_client):
    """CD-6 second architect review — Finding 1: applies identically to
    the mailbox-origin `COMPANY_REQUIRED` producer, not just the manual-
    upload one — the generic resolve endpoint's validation is shared by
    both producers."""
    mailbox_id, comp, _entity = _connected_mailbox_with_entity_seeded(dev_client)
    _sweep_must_read_review_required_message(dev_client, mailbox_id, msg_id="finding1-doc-1")

    company_items = [
        i for i in comp.needs_you_repository.list_needs_you_items(item_type="COMPANY_REQUIRED")
        if i.metadata.get("mailbox_id") == mailbox_id
    ]
    item = company_items[0]

    response = dev_client.post(
        f"/internal/needs-you/{item.item_id}/resolve",
        json={
            "new_status": "RESOLVED",
            "resolution": {"entity_id": None, "what": "Invoice", "why": "Ops"},
            "actor_type": "USER", "actor_id": ACTOR_ID,
        },
    )
    assert response.status_code == 422, response.text

    still_open = comp.needs_you_repository.get_needs_you_item(item.item_id)
    assert still_open.status == "OPEN"
    evidence_after = comp.api.get_evidence(item.source_object_reference)
    assert evidence_after.entity_id is None


def test_resolving_mailbox_origin_company_required_with_unknown_entity_id_fails(dev_client):
    mailbox_id, comp, _entity = _connected_mailbox_with_entity_seeded(dev_client)
    _sweep_must_read_review_required_message(dev_client, mailbox_id, msg_id="finding1-doc-2")

    company_items = [
        i for i in comp.needs_you_repository.list_needs_you_items(item_type="COMPANY_REQUIRED")
        if i.metadata.get("mailbox_id") == mailbox_id
    ]
    item = company_items[0]

    response = dev_client.post(
        f"/internal/needs-you/{item.item_id}/resolve",
        json={
            "new_status": "RESOLVED",
            "resolution": {"entity_id": "018f5b3e-0000-7a4e-8b2d-000000000003", "what": "Invoice", "why": "Ops"},
            "actor_type": "USER", "actor_id": ACTOR_ID,
        },
    )
    assert response.status_code == 404, response.text

    still_open = comp.needs_you_repository.get_needs_you_item(item.item_id)
    assert still_open.status == "OPEN"
    evidence_after = comp.api.get_evidence(item.source_object_reference)
    assert evidence_after.entity_id is None


def test_policy_change_audit_event_carries_before_after_and_is_live_on_next_sweep(dev_client):
    """WO required test #10: a policy change (GRAYLIST -> MUST_READ)
    produces a real audit event carrying the before/after policy and
    destination values, and immediately affects the NEXT sweep's
    handling of that domain (not merely recorded, but functionally
    live)."""
    mailbox_id, comp, entity = _connected_mailbox_with_entity_seeded(dev_client)
    _sweep_unknown_domain_message(dev_client, mailbox_id)  # domain: new-supplier.example
    item = [
        i for i in comp.needs_you_repository.list_needs_you_items(item_type="MAILBOX_DOMAIN_REVIEW")
        if i.metadata.get("mailbox_id") == mailbox_id
    ][0]

    # Step 1: KEEP_GRAY — a real GRAYLIST rule, item stays OPEN.
    r_gray = dev_client.post(
        f"/internal/mailboxes/{mailbox_id}/microsoft/domain-review/{item.item_id}/resolve",
        json={"actor_type": "USER", "actor_id": ACTOR_ID, "decision": "KEEP_GRAY"},
    )
    assert r_gray.status_code == 200, r_gray.text
    assert r_gray.json()["mailbox_domain_rule"]["policy"] == "GRAYLIST"
    assert comp.needs_you_repository.get_needs_you_item(item.item_id).status == "OPEN"

    gray_events = comp.api.audit_repository.list_recent(limit=50, event_type_prefix="MAILBOX_DOMAIN_RULE_GRAYLIST")
    matching_gray = [e for e in gray_events if e.payload.get("needs_you_item_id") == item.item_id]
    assert len(matching_gray) == 1
    assert matching_gray[0].payload["previous_policy"] is None  # no rule existed before
    assert matching_gray[0].payload["new_policy"] == "GRAYLIST"

    # Step 2: ALLOW (MUST_READ + FIXED) — the real transition under test.
    comp.microsoft_graph_client.queue_headers_result(_headers_ok())
    comp.microsoft_graph_client.queue_content_result(
        GraphMessageContentResult(status=GraphOutcomeStatus.OK, content=b"From: billing@new-supplier.example\r\nSubject: Invoice\r\n\r\nBody")
    )
    r_allow = dev_client.post(
        f"/internal/mailboxes/{mailbox_id}/microsoft/domain-review/{item.item_id}/resolve",
        json={
            "actor_type": "USER", "actor_id": ACTOR_ID, "decision": "ALLOW",
            "destination_entity_id": entity.entity_id, "destination_mode": "FIXED",
        },
    )
    assert r_allow.status_code == 200, r_allow.text

    must_read_events = comp.api.audit_repository.list_recent(limit=50, event_type_prefix="MAILBOX_DOMAIN_RULE_MUST_READ")
    matching_allow = [e for e in must_read_events if e.payload.get("needs_you_item_id") == item.item_id]
    assert len(matching_allow) == 1
    payload = matching_allow[0].payload
    assert payload["previous_policy"] == "GRAYLIST"
    assert payload["new_policy"] == "MUST_READ"
    assert payload["previous_destination_entity_id"] is None
    assert payload["new_destination_entity_id"] == entity.entity_id
    assert payload["previous_destination_mode"] is None
    assert payload["new_destination_mode"] == "FIXED"

    # Functionally live, not merely recorded: a NEW message from the
    # SAME domain, on the NEXT sweep, is now deep-processed under the
    # new MUST_READ+FIXED rule — no further Needs You item, real entity_id.
    now = datetime.now(timezone.utc)
    msg2 = GraphMessageSummary(
        immutable_id="AAMk-unknown-2", internet_message_id="<u2@b>", subject="Invoice attached",
        sender_address="billing@new-supplier.example", sender_display_name="New Supplier", received_at=now,
        has_attachments=False, auth_signals=_PASSING_AUTH_SIGNALS, raw_headers=_PASSING_AUTH_HEADERS,
    )
    _queue_folder_discovery(comp)
    comp.microsoft_graph_client.queue_delta_result(GraphDeltaPageResult(status=GraphOutcomeStatus.OK, messages=(msg2,), delta_link="d-next"))
    comp.microsoft_graph_client.queue_headers_result(_headers_ok())
    comp.microsoft_graph_client.queue_content_result(
        GraphMessageContentResult(status=GraphOutcomeStatus.OK, content=b"From: billing@new-supplier.example\r\nSubject: Invoice\r\n\r\nBody")
    )
    comp.microsoft_graph_client.queue_delta_result(GraphDeltaPageResult(status=GraphOutcomeStatus.OK, messages=(), delta_link="d-next-junk"))
    r_sweep = dev_client.post(f"/internal/mailboxes/{mailbox_id}/microsoft/sweep", json={"actor_type": "USER", "actor_id": ACTOR_ID})
    assert r_sweep.status_code == 200, r_sweep.text
    assert r_sweep.json()["evidence_created"] == 1

    message2 = comp.mailbox_message_repository.find_by_provider_id(mailbox_id, "AAMk-unknown-2")
    assert message2.ingestion_status == "INGESTED"
    evidence2 = comp.api.get_evidence(message2.evidence_id)
    assert evidence2.entity_id == entity.entity_id

    # Still exactly the ONE original MAILBOX_DOMAIN_REVIEW item for this
    # domain — the new message never raised a second one.
    domain_review_items = [
        i for i in comp.needs_you_repository.list_needs_you_items(item_type="MAILBOX_DOMAIN_REVIEW")
        if i.metadata.get("mailbox_id") == mailbox_id and i.metadata.get("sender_domain") == "new-supplier.example"
    ]
    assert len(domain_review_items) == 1


def test_get_domain_rules_reflects_current_policy_after_keep_gray(dev_client):
    mailbox_id, comp, _entity = _connected_mailbox_with_entity_seeded(dev_client)
    _sweep_unknown_domain_message(dev_client, mailbox_id)
    item = [
        i for i in comp.needs_you_repository.list_needs_you_items(item_type="MAILBOX_DOMAIN_REVIEW")
        if i.metadata.get("mailbox_id") == mailbox_id
    ][0]
    r = dev_client.post(
        f"/internal/mailboxes/{mailbox_id}/microsoft/domain-review/{item.item_id}/resolve",
        json={"actor_type": "USER", "actor_id": ACTOR_ID, "decision": "KEEP_GRAY"},
    )
    assert r.status_code == 200, r.text
    # KEEP_GRAY never resolves the item — it stays OPEN for a later look.
    assert dev_client.get(f"/internal/mailboxes/{mailbox_id}/microsoft/domain-review").json()["count"] == 1

    rules = dev_client.get(f"/internal/mailboxes/{mailbox_id}/microsoft/domain-rules").json()
    assert rules["count"] == 1
    assert rules["items"][0]["policy"] == "GRAYLIST"
    assert rules["items"][0]["sender_address"] is None


# =======================================================================
# CD-6 GUI-operations-foundation follow-on WO ("five confirmed
# integration gaps" fix) — item A: EXACT_ADDRESS wired through the real
# operator HTTP API.
# =======================================================================


def test_resolve_domain_review_allow_exact_address_creates_scoped_rule(dev_client):
    mailbox_id, comp, entity = _connected_mailbox_with_entity_seeded(dev_client)
    _sweep_unknown_domain_message(dev_client, mailbox_id)
    item = [
        i for i in comp.needs_you_repository.list_needs_you_items(item_type="MAILBOX_DOMAIN_REVIEW")
        if i.metadata.get("mailbox_id") == mailbox_id
    ][0]

    comp.microsoft_graph_client.queue_headers_result(_headers_ok())
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
            "match_mode": "EXACT_ADDRESS", "sender_address": "billing@new-supplier.example",
        },
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["mailbox_domain_rule"]["match_mode"] == "EXACT_ADDRESS"
    assert body["mailbox_domain_rule"]["sender_address"] == "billing@new-supplier.example"
    assert body["mailbox_domain_rule"]["sender_domain"] == "new-supplier.example"
    assert len(body["reprocessed_messages"]) == 1
    assert body["reprocessed_messages"][0]["ingestion_status"] == "INGESTED"


def test_resolve_domain_review_exact_address_rejects_an_unobserved_address(dev_client):
    """An operator must never be able to pre-authorize an address BAGMAN
    has never actually seen mail from."""
    mailbox_id, comp, entity = _connected_mailbox_with_entity_seeded(dev_client)
    _sweep_unknown_domain_message(dev_client, mailbox_id)
    item = [
        i for i in comp.needs_you_repository.list_needs_you_items(item_type="MAILBOX_DOMAIN_REVIEW")
        if i.metadata.get("mailbox_id") == mailbox_id
    ][0]

    r = dev_client.post(
        f"/internal/mailboxes/{mailbox_id}/microsoft/domain-review/{item.item_id}/resolve",
        json={
            "actor_type": "USER", "actor_id": ACTOR_ID, "decision": "ALLOW",
            "destination_entity_id": entity.entity_id, "destination_mode": "FIXED",
            "match_mode": "EXACT_ADDRESS", "sender_address": "never-seen@new-supplier.example",
        },
    )
    assert r.status_code == 422, r.text


def test_resolve_domain_review_exact_address_rejects_an_address_from_a_different_domain(dev_client):
    mailbox_id, comp, entity = _connected_mailbox_with_entity_seeded(dev_client)
    _sweep_unknown_domain_message(dev_client, mailbox_id)
    item = [
        i for i in comp.needs_you_repository.list_needs_you_items(item_type="MAILBOX_DOMAIN_REVIEW")
        if i.metadata.get("mailbox_id") == mailbox_id
    ][0]

    r = dev_client.post(
        f"/internal/mailboxes/{mailbox_id}/microsoft/domain-review/{item.item_id}/resolve",
        json={
            "actor_type": "USER", "actor_id": ACTOR_ID, "decision": "ALLOW",
            "destination_entity_id": entity.entity_id, "destination_mode": "FIXED",
            "match_mode": "EXACT_ADDRESS", "sender_address": "billing@totally-different.example",
        },
    )
    assert r.status_code == 422, r.text


def test_resolve_domain_review_domain_level_mode_rejects_a_stray_sender_address(dev_client):
    """A domain-level match_mode must never silently accept a stray
    sender_address."""
    mailbox_id, comp, entity = _connected_mailbox_with_entity_seeded(dev_client)
    _sweep_unknown_domain_message(dev_client, mailbox_id)
    item = [
        i for i in comp.needs_you_repository.list_needs_you_items(item_type="MAILBOX_DOMAIN_REVIEW")
        if i.metadata.get("mailbox_id") == mailbox_id
    ][0]

    r = dev_client.post(
        f"/internal/mailboxes/{mailbox_id}/microsoft/domain-review/{item.item_id}/resolve",
        json={
            "actor_type": "USER", "actor_id": ACTOR_ID, "decision": "ALLOW",
            "destination_entity_id": entity.entity_id, "destination_mode": "FIXED",
            "match_mode": "EXACT", "sender_address": "billing@new-supplier.example",
        },
    )
    assert r.status_code == 422, r.text


def test_keep_gray_rejects_exact_address_match_mode(dev_client):
    mailbox_id, comp, _entity = _connected_mailbox_with_entity_seeded(dev_client)
    _sweep_unknown_domain_message(dev_client, mailbox_id)
    item = [
        i for i in comp.needs_you_repository.list_needs_you_items(item_type="MAILBOX_DOMAIN_REVIEW")
        if i.metadata.get("mailbox_id") == mailbox_id
    ][0]

    r = dev_client.post(
        f"/internal/mailboxes/{mailbox_id}/microsoft/domain-review/{item.item_id}/resolve",
        json={
            "actor_type": "USER", "actor_id": ACTOR_ID, "decision": "KEEP_GRAY",
            "match_mode": "EXACT_ADDRESS", "sender_address": "billing@new-supplier.example",
        },
    )
    assert r.status_code == 422, r.text


def test_batch_resolve_supports_exact_address_scope(dev_client):
    mailbox_id, comp, entity = _connected_mailbox_with_entity_seeded(dev_client)
    _sweep_unknown_domain_message(dev_client, mailbox_id)
    item = [
        i for i in comp.needs_you_repository.list_needs_you_items(item_type="MAILBOX_DOMAIN_REVIEW")
        if i.metadata.get("mailbox_id") == mailbox_id
    ][0]

    comp.microsoft_graph_client.queue_headers_result(_headers_ok())
    comp.microsoft_graph_client.queue_content_result(
        GraphMessageContentResult(
            status=GraphOutcomeStatus.OK, content=b"From: billing@new-supplier.example\r\nSubject: Invoice\r\n\r\nBody"
        )
    )
    r = dev_client.post(
        f"/internal/mailboxes/{mailbox_id}/microsoft/domain-review/batch-resolve",
        json={
            "actor_type": "USER", "actor_id": ACTOR_ID,
            "items": [
                {
                    "item_id": item.item_id, "decision": "ALLOW",
                    "destination_entity_id": entity.entity_id, "destination_mode": "FIXED",
                    "match_mode": "EXACT_ADDRESS", "sender_address": "billing@new-supplier.example",
                }
            ],
        },
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["succeeded_count"] == 1
    assert body["results"][0]["mailbox_domain_rule"]["match_mode"] == "EXACT_ADDRESS"
    assert body["results"][0]["mailbox_domain_rule"]["sender_address"] == "billing@new-supplier.example"


def test_exact_address_precedence_over_domain_rules_at_http_level(dev_client):
    """Exact-address > exact-domain > parent-subdomain precedence — the
    HTTP-level proof (the repository layer's own equivalent proof
    already exists; this exercises the full sweep-endpoint path)."""
    mailbox_id, comp, entity = _connected_mailbox_with_entity_seeded(dev_client)
    comp.mailbox_domain_rule_repository.upsert_rule(
        mailbox_id=mailbox_id, sender_domain="example.com", match_mode="INCLUDE_SUBDOMAINS",
        policy="BLACKLIST", destination_entity_id=None, destination_mode=None, source="OPERATOR",
    )
    comp.mailbox_domain_rule_repository.upsert_rule(
        mailbox_id=mailbox_id, sender_domain="billing.example.com", match_mode="EXACT",
        policy="BLACKLIST", destination_entity_id=None, destination_mode=None, source="OPERATOR",
    )

    now = datetime.now(timezone.utc)
    msg1 = GraphMessageSummary(
        immutable_id="AAMk-precedence-1", internet_message_id="<p1@b>", subject="Invoice",
        sender_address="ap@billing.example.com", sender_display_name="AP", received_at=now,
        has_attachments=False, raw_headers=_PASSING_AUTH_HEADERS,
    )
    _queue_folder_discovery(comp)
    comp.microsoft_graph_client.queue_delta_result(GraphDeltaPageResult(status=GraphOutcomeStatus.OK, messages=(msg1,), delta_link="d1"))
    comp.microsoft_graph_client.queue_delta_result(GraphDeltaPageResult(status=GraphOutcomeStatus.OK, messages=(), delta_link="d1-junk"))
    r1 = dev_client.post(f"/internal/mailboxes/{mailbox_id}/microsoft/sweep", json={"actor_type": "USER", "actor_id": ACTOR_ID})
    assert r1.status_code == 200, r1.text
    assert r1.json()["evidence_created"] == 0  # the EXACT domain-level BLACKLIST rule governs so far

    # A real EXACT_ADDRESS rule now governs THIS specific address —
    # requires the address to have been actually observed, which the
    # sweep above just did.
    comp.mailbox_domain_rule_repository.upsert_rule(
        mailbox_id=mailbox_id, sender_domain="billing.example.com", sender_address="ap@billing.example.com",
        match_mode="EXACT_ADDRESS", policy="MUST_READ", destination_entity_id=entity.entity_id,
        destination_mode="FIXED", source="OPERATOR",
    )

    msg2 = GraphMessageSummary(
        immutable_id="AAMk-precedence-2", internet_message_id="<p2@b>", subject="Invoice",
        sender_address="ap@billing.example.com", sender_display_name="AP", received_at=now,
        has_attachments=False, raw_headers=_PASSING_AUTH_HEADERS,
    )
    _queue_folder_discovery(comp)
    comp.microsoft_graph_client.queue_delta_result(GraphDeltaPageResult(status=GraphOutcomeStatus.OK, messages=(msg2,), delta_link="d2"))
    comp.microsoft_graph_client.queue_content_result(
        GraphMessageContentResult(status=GraphOutcomeStatus.OK, content=b"From: ap@billing.example.com\r\nSubject: Invoice\r\n\r\nBody")
    )
    comp.microsoft_graph_client.queue_delta_result(GraphDeltaPageResult(status=GraphOutcomeStatus.OK, messages=(), delta_link="d2-junk"))
    r2 = dev_client.post(f"/internal/mailboxes/{mailbox_id}/microsoft/sweep", json={"actor_type": "USER", "actor_id": ACTOR_ID})
    assert r2.status_code == 200, r2.text
    assert r2.json()["evidence_created"] == 1  # EXACT_ADDRESS wins over both domain-level BLACKLIST rules

    message2 = comp.mailbox_message_repository.find_by_provider_id(mailbox_id, "AAMk-precedence-2")
    assert message2.ingestion_status == "INGESTED"
    evidence2 = comp.api.get_evidence(message2.evidence_id)
    assert evidence2.entity_id == entity.entity_id


# =======================================================================
# CD-6 GUI-operations-foundation follow-on WO ("five confirmed
# integration gaps" fix) — item D: the real SECURITY_REVIEW resolution
# workflow.
# =======================================================================


def _sweep_security_review_message(dev_client, mailbox_id, *, msg_id="sec-1", domain=None):
    """Sweeps ONE message under a real MUST_READ rule with a genuine
    trusted authentication FAIL — the message lands SECURITY_REVIEW and
    raises exactly one MAILBOX_AUTHENTICATION_ESCALATION item."""
    comp = get_composition()
    resolved_domain = domain or _DEFAULT_ALLOWED_DOMAIN_HTTP
    now = datetime.now(timezone.utc)
    msg = GraphMessageSummary(
        immutable_id=msg_id, internet_message_id=f"<{msg_id}@b>", subject="Invoice",
        sender_address=f"billing@{resolved_domain}", sender_display_name="Vendor", received_at=now,
        has_attachments=False,
        raw_headers=(
            {
                "name": "Authentication-Results",
                "value": "spf=fail smtp.mailfrom=vendor.com; dkim=fail header.d=vendor.com; "
                "dmarc=fail action=quarantine header.from=vendor.com; compauth=fail reason=001",
            },
        ),
    )
    _queue_folder_discovery(comp)
    comp.microsoft_graph_client.queue_delta_result(GraphDeltaPageResult(status=GraphOutcomeStatus.OK, messages=(msg,), delta_link=f"d-{msg_id}"))
    comp.microsoft_graph_client.queue_delta_result(GraphDeltaPageResult(status=GraphOutcomeStatus.OK, messages=(), delta_link=f"d-{msg_id}-junk"))
    r = dev_client.post(f"/internal/mailboxes/{mailbox_id}/microsoft/sweep", json={"actor_type": "USER", "actor_id": ACTOR_ID})
    assert r.status_code == 200, r.text
    return r.json()


_DEFAULT_ALLOWED_DOMAIN_HTTP = "security-review.example"


def _connected_mailbox_with_must_read_rule(client, *, domain=_DEFAULT_ALLOWED_DOMAIN_HTTP, destination_mode="REVIEW_REQUIRED", destination_entity_id=None):
    mailbox_id, comp, entity = _connected_mailbox_with_entity_seeded(client)
    comp.mailbox_domain_rule_repository.upsert_rule(
        mailbox_id=mailbox_id, sender_domain=domain, match_mode="EXACT", policy="MUST_READ",
        destination_entity_id=destination_entity_id, destination_mode=destination_mode, source="OPERATOR",
    )
    return mailbox_id, comp, entity


def test_security_review_list_endpoint_returns_the_open_item(dev_client):
    mailbox_id, comp, _entity = _connected_mailbox_with_must_read_rule(dev_client)
    _sweep_security_review_message(dev_client, mailbox_id, msg_id="sec-1")

    listing = dev_client.get(f"/internal/mailboxes/{mailbox_id}/microsoft/security-review").json()
    assert listing["count"] == 1
    assert listing["items"][0]["item_type"] == "MAILBOX_AUTHENTICATION_ESCALATION"
    assert listing["items"][0]["status"] == "OPEN"


def test_resolve_security_review_process_once_ingests_exactly_one_mime_fetch(dev_client):
    mailbox_id, comp, _entity = _connected_mailbox_with_must_read_rule(dev_client)
    _sweep_security_review_message(dev_client, mailbox_id, msg_id="sec-1")
    item = comp.needs_you_repository.list_needs_you_items(item_type="MAILBOX_AUTHENTICATION_ESCALATION")[0]

    comp.microsoft_graph_client.queue_content_result(
        GraphMessageContentResult(
            status=GraphOutcomeStatus.OK,
            content=f"From: billing@{_DEFAULT_ALLOWED_DOMAIN_HTTP}\r\nSubject: Invoice\r\n\r\nBody".encode(),
        )
    )
    r = dev_client.post(
        f"/internal/mailboxes/{mailbox_id}/microsoft/security-review/{item.item_id}/resolve",
        json={"actor_type": "USER", "actor_id": ACTOR_ID, "decision": "PROCESS_THIS_MESSAGE_ONCE"},
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["needs_you_item"]["status"] == "RESOLVED"
    assert body["mailbox_message"]["ingestion_status"] == "INGESTED"
    assert body["mailbox_message"]["evidence_id"] is not None
    assert len(comp.microsoft_graph_client.content_calls) == 1

    # The governing rule itself is never touched — still MUST_READ.
    rule = comp.mailbox_domain_rule_repository.find_for_sender(mailbox_id=mailbox_id, sender_domain=_DEFAULT_ALLOWED_DOMAIN_HTTP)
    assert rule.policy == "MUST_READ"


def test_resolve_security_review_process_once_is_idempotent_no_duplicate_evidence(dev_client):
    mailbox_id, comp, _entity = _connected_mailbox_with_must_read_rule(dev_client)
    _sweep_security_review_message(dev_client, mailbox_id, msg_id="sec-1")
    item = comp.needs_you_repository.list_needs_you_items(item_type="MAILBOX_AUTHENTICATION_ESCALATION")[0]

    comp.microsoft_graph_client.queue_content_result(
        GraphMessageContentResult(
            status=GraphOutcomeStatus.OK,
            content=f"From: billing@{_DEFAULT_ALLOWED_DOMAIN_HTTP}\r\nSubject: Invoice\r\n\r\nBody".encode(),
        )
    )
    payload = {"actor_type": "USER", "actor_id": ACTOR_ID, "decision": "PROCESS_THIS_MESSAGE_ONCE"}
    first = dev_client.post(f"/internal/mailboxes/{mailbox_id}/microsoft/security-review/{item.item_id}/resolve", json=payload)
    assert first.status_code == 200, first.text
    first_evidence_id = first.json()["mailbox_message"]["evidence_id"]

    second = dev_client.post(f"/internal/mailboxes/{mailbox_id}/microsoft/security-review/{item.item_id}/resolve", json=payload)
    assert second.status_code == 200, second.text
    assert second.json()["mailbox_message"]["evidence_id"] == first_evidence_id
    # No second MIME fetch at all.
    assert len(comp.microsoft_graph_client.content_calls) == 1


def test_resolve_security_review_do_not_process_never_fetches_mime_or_blacklists(dev_client):
    mailbox_id, comp, _entity = _connected_mailbox_with_must_read_rule(dev_client)
    _sweep_security_review_message(dev_client, mailbox_id, msg_id="sec-1")
    item = comp.needs_you_repository.list_needs_you_items(item_type="MAILBOX_AUTHENTICATION_ESCALATION")[0]

    r = dev_client.post(
        f"/internal/mailboxes/{mailbox_id}/microsoft/security-review/{item.item_id}/resolve",
        json={"actor_type": "USER", "actor_id": ACTOR_ID, "decision": "DO_NOT_PROCESS_THIS_MESSAGE"},
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["needs_you_item"]["status"] == "RESOLVED"
    assert body["mailbox_message"]["ingestion_status"] == "SECURITY_REVIEW"
    assert body["mailbox_message"]["evidence_id"] is None
    assert comp.microsoft_graph_client.content_calls == []

    rule = comp.mailbox_domain_rule_repository.find_for_sender(mailbox_id=mailbox_id, sender_domain=_DEFAULT_ALLOWED_DOMAIN_HTTP)
    assert rule.policy == "MUST_READ"  # never blacklisted


def test_resolve_security_review_conflicting_second_decision_is_409(dev_client):
    mailbox_id, comp, _entity = _connected_mailbox_with_must_read_rule(dev_client)
    _sweep_security_review_message(dev_client, mailbox_id, msg_id="sec-1")
    item = comp.needs_you_repository.list_needs_you_items(item_type="MAILBOX_AUTHENTICATION_ESCALATION")[0]

    first = dev_client.post(
        f"/internal/mailboxes/{mailbox_id}/microsoft/security-review/{item.item_id}/resolve",
        json={"actor_type": "USER", "actor_id": ACTOR_ID, "decision": "DO_NOT_PROCESS_THIS_MESSAGE"},
    )
    assert first.status_code == 200

    conflicting = dev_client.post(
        f"/internal/mailboxes/{mailbox_id}/microsoft/security-review/{item.item_id}/resolve",
        json={"actor_type": "USER", "actor_id": ACTOR_ID, "decision": "PROCESS_THIS_MESSAGE_ONCE"},
    )
    assert conflicting.status_code == 409


# =======================================================================
# CD-6 GUI-operations-foundation follow-on WO ("five confirmed
# integration gaps" fix) — required mixed-destination acceptance proof
# (Amazon-style).
# =======================================================================


def test_amazon_style_two_documents_two_destinations_never_reasks_relevance(dev_client):
    """A MUST_READ + REVIEW_REQUIRED rule for one domain; two SEPARATE
    messages/evidence items from it. Document A resolved to one entity,
    Document B (a different evidence item, same source) — source
    relevance is never re-asked, evidence starts entity_id=None again
    (never inherited from Document A), gets resolved to a DIFFERENT
    entity via its OWN separate COMPANY_REQUIRED item, and the governing
    MailboxDomainRule stays MUST_READ+REVIEW_REQUIRED afterward — never
    silently learned/promoted to a FIXED destination from either
    document's own resolution."""
    mailbox_id, comp, entity_a = _connected_mailbox_with_entity_seeded(dev_client)
    entity_b = comp.api.register_entity(
        entity_type="PERSON", canonical_name="AMAZON_STYLE_PERSON_B", display_name="Person B", status="ACTIVE",
        actor_type="SYSTEM", actor_id=ACTOR_ID, fiscal_year_start_month_day="01-01",
    )
    domain = "amazon-style.example"
    _sweep_must_read_review_required_message(dev_client, mailbox_id, msg_id="amazon-doc-1", domain=domain)
    _sweep_must_read_review_required_message(dev_client, mailbox_id, msg_id="amazon-doc-2", domain=domain)

    message1 = comp.mailbox_message_repository.find_by_provider_id(mailbox_id, "amazon-doc-1")
    message2 = comp.mailbox_message_repository.find_by_provider_id(mailbox_id, "amazon-doc-2")
    evidence1 = comp.api.get_evidence(message1.evidence_id)
    evidence2 = comp.api.get_evidence(message2.evidence_id)
    assert evidence1.entity_id is None
    assert evidence2.entity_id is None  # never inherited from Document A

    company_items = [
        i for i in comp.needs_you_repository.list_needs_you_items(item_type="COMPANY_REQUIRED")
        if i.metadata.get("mailbox_id") == mailbox_id
    ]
    item1 = next(i for i in company_items if i.source_object_reference == evidence1.evidence_id)
    item2 = next(i for i in company_items if i.source_object_reference == evidence2.evidence_id)
    assert item1.item_id != item2.item_id

    r1 = dev_client.post(
        f"/internal/needs-you/{item1.item_id}/resolve",
        json={
            "new_status": "RESOLVED",
            "resolution": {"entity_id": entity_a.entity_id, "what": "Software subscription", "why": "R&D tooling"},
            "actor_type": "USER", "actor_id": ACTOR_ID,
        },
    )
    assert r1.status_code == 200, r1.text
    r2 = dev_client.post(
        f"/internal/needs-you/{item2.item_id}/resolve",
        json={
            "new_status": "RESOLVED",
            "resolution": {"entity_id": entity_b.entity_id, "what": "Personal item", "why": "Reimbursement"},
            "actor_type": "USER", "actor_id": ACTOR_ID,
        },
    )
    assert r2.status_code == 200, r2.text

    evidence1_after = comp.api.get_evidence(evidence1.evidence_id)
    evidence2_after = comp.api.get_evidence(evidence2.evidence_id)
    assert evidence1_after.entity_id == entity_a.entity_id
    assert evidence2_after.entity_id == entity_b.entity_id

    rule = comp.mailbox_domain_rule_repository.find_for_sender(mailbox_id=mailbox_id, sender_domain=domain)
    assert rule.policy == "MUST_READ"
    assert rule.destination_mode == "REVIEW_REQUIRED"
    assert rule.destination_entity_id is None  # never silently promoted to FIXED
