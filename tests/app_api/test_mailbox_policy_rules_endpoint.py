"""HTTP-level tests for ``POST /internal/mailboxes/{mailbox_id}/policy-rules``
(CD-6 policy-rules-endpoint WO) — the provider-neutral surface that lets
an operator create/update a ``MailboxDomainRule`` directly, by its own
exact identity, independent of any Needs You item.

Mirrors ``tests/app_api/test_mailbox_microsoft_endpoints.py``'s own
lightweight ``TestClient``-only style/fixtures (in-memory repositories,
``FakeMicrosoftGraphClient``/``FakeMicrosoftOAuthClient`` — no Docker
required) — several helpers below are intentionally near-identical
duplicates of that file's own private helpers (this codebase's test
files stay self-contained; there is no shared cross-test-file helper
module for this area).
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
    GraphMessageHeadersResult,
    GraphMessageSummary,
    GraphOutcomeStatus,
    GraphWellKnownFoldersResult,
    MicrosoftIdentity,
    MicrosoftIdentityResult,
    MicrosoftTokenResult,
)

ACTOR_ID = "bagman-mailbox-policy-rules-endpoint-tests"

_INBOX_FOLDER_ID = "AAMkADinbox000000000000000000000"
_JUNK_FOLDER_ID = "AAMkADjunkemail0000000000000000"

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


def _connected_mailbox_with_entity_seeded(client):
    mailbox_id = _create_mailbox(client)
    _connect_and_complete(client, mailbox_id)
    comp = get_composition()
    entity = comp.api.register_entity(
        entity_type="COMPANY", canonical_name="TEST_POLICY_RULES_LTD", display_name="Test Ltd", status="ACTIVE",
        actor_type="SYSTEM", actor_id=ACTOR_ID,
        fiscal_year_start_month_day="01-01", historical_floor_override_at=datetime(2025, 1, 1, tzinfo=timezone.utc),
    )
    return mailbox_id, comp, entity


def _sweep_message_from(client, mailbox_id, *, sender_address: str, msg_id: str, subject: str = "Invoice attached"):
    comp = get_composition()
    now = datetime.now(timezone.utc)
    msg = GraphMessageSummary(
        immutable_id=msg_id, internet_message_id=f"<{msg_id}@b>", subject=subject,
        sender_address=sender_address, sender_display_name="Sender", received_at=now,
        has_attachments=False,
    )
    _queue_folder_discovery(comp)
    comp.microsoft_graph_client.queue_delta_result(GraphDeltaPageResult(status=GraphOutcomeStatus.OK, messages=(msg,), delta_link=f"d-{msg_id}"))
    comp.microsoft_graph_client.queue_delta_result(GraphDeltaPageResult(status=GraphOutcomeStatus.OK, messages=(), delta_link=f"d-{msg_id}-junk"))
    r = client.post(f"/internal/mailboxes/{mailbox_id}/microsoft/sweep", json={"actor_type": "USER", "actor_id": ACTOR_ID})
    assert r.status_code == 200, r.text
    return r.json()


def _sweep_unknown_domain_message(client, mailbox_id):
    return _sweep_message_from(
        client, mailbox_id, sender_address="billing@new-supplier.example", msg_id="AAMk-unknown-1"
    )


def _open_domain_review_item(comp, mailbox_id):
    items = [
        i for i in comp.needs_you_repository.list_needs_you_items(item_type="MAILBOX_DOMAIN_REVIEW")
        if i.metadata.get("mailbox_id") == mailbox_id
    ]
    assert len(items) == 1, f"expected exactly one OPEN domain-review item, got {len(items)}"
    return items[0]


def _setup_resolved_domain_with_address_override(client):
    """`new-supplier.example` is already MUST_READ/REVIEW_REQUIRED via a
    RESOLVED domain-review item; then a BLACKLIST EXACT_ADDRESS override
    is created for `billing@new-supplier.example` underneath it via the
    new policy-rules endpoint. Returns
    ``(mailbox_id, comp, entity, item_id, domain_rule_dict, override_response)``.
    """
    mailbox_id, comp, entity = _connected_mailbox_with_entity_seeded(client)
    _sweep_unknown_domain_message(client, mailbox_id)
    item = _open_domain_review_item(comp, mailbox_id)

    comp.microsoft_graph_client.queue_headers_result(_headers_ok())
    comp.microsoft_graph_client.queue_content_result(
        GraphMessageContentResult(
            status=GraphOutcomeStatus.OK, content=b"From: billing@new-supplier.example\r\nSubject: Invoice\r\n\r\nBody"
        )
    )
    resolve_r = client.post(
        f"/internal/mailboxes/{mailbox_id}/microsoft/domain-review/{item.item_id}/resolve",
        json={"actor_type": "USER", "actor_id": ACTOR_ID, "decision": "ALLOW", "destination_mode": "REVIEW_REQUIRED"},
    )
    assert resolve_r.status_code == 200, resolve_r.text
    domain_rule = resolve_r.json()["mailbox_domain_rule"]

    override_r = client.post(
        f"/internal/mailboxes/{mailbox_id}/policy-rules",
        json={
            "actor_type": "USER", "actor_id": ACTOR_ID,
            "match_mode": "EXACT_ADDRESS", "sender_domain": "new-supplier.example",
            "sender_address": "billing@new-supplier.example", "policy": "BLACKLIST",
            "reason": "Ignore this exact address — known spam from an otherwise-trusted domain",
        },
    )
    return mailbox_id, comp, entity, item.item_id, domain_rule, override_r


# =======================================================================
# A — create exact-address override after resolved domain review
# =======================================================================


def test_create_exact_address_override_after_resolved_domain_review(dev_client):
    mailbox_id, comp, _entity, item_id, domain_rule, override_r = _setup_resolved_domain_with_address_override(dev_client)

    assert override_r.status_code == 200, override_r.text
    body = override_r.json()
    assert body["mailbox_domain_rule"]["match_mode"] == "EXACT_ADDRESS"
    assert body["mailbox_domain_rule"]["policy"] == "BLACKLIST"
    assert body["mailbox_domain_rule"]["sender_address"] == "billing@new-supplier.example"
    assert body["mailbox_domain_rule"]["sender_domain"] == "new-supplier.example"
    # A brand-new address rule's own previous state must be null — never
    # inherited from the broader domain rule that already governs it.
    assert body["previous_policy"] is None
    assert body["previous_destination_mode"] is None
    assert body["previous_destination_entity_id"] is None
    assert body["was_no_op"] is False
    new_rule_id = body["mailbox_domain_rule"]["rule_id"]
    assert new_rule_id != domain_rule["rule_id"]

    # The OLD Needs You item is unchanged and still RESOLVED with its
    # original resolution.
    item = comp.needs_you_repository.get_needs_you_item(item_id)
    assert item.status == "RESOLVED"
    assert item.resolution["decision"] == "ALLOW"

    rules = dev_client.get(f"/internal/mailboxes/{mailbox_id}/microsoft/domain-rules").json()["items"]
    rules_by_id = {r["rule_id"]: r for r in rules}

    # The domain rule X is unchanged (same rule_id, same policy/destination).
    still_domain_rule = rules_by_id[domain_rule["rule_id"]]
    assert still_domain_rule["policy"] == domain_rule["policy"] == "MUST_READ"
    assert still_domain_rule["destination_mode"] == domain_rule["destination_mode"] == "REVIEW_REQUIRED"
    assert still_domain_rule["match_mode"] == "EXACT"

    # A SEPARATE new address rule exists with its own rule_id.
    assert new_rule_id in rules_by_id
    assert len(rules) == 2


# =======================================================================
# B — precedence: EXACT_ADDRESS wins for its own address; the domain
# rule still governs every other address under the same domain.
# =======================================================================


def test_exact_address_override_precedence_over_domain_rule(dev_client):
    mailbox_id, comp, _entity, _item_id, domain_rule, override_r = _setup_resolved_domain_with_address_override(dev_client)
    assert override_r.status_code == 200, override_r.text

    governing_for_override = comp.mailbox_domain_rule_repository.find_for_sender(
        mailbox_id=mailbox_id, sender_domain="new-supplier.example", sender_address="billing@new-supplier.example"
    )
    assert governing_for_override is not None
    assert governing_for_override.policy == "BLACKLIST"
    assert governing_for_override.match_mode == "EXACT_ADDRESS"

    governing_for_other_address = comp.mailbox_domain_rule_repository.find_for_sender(
        mailbox_id=mailbox_id, sender_domain="new-supplier.example", sender_address="someone-else@new-supplier.example"
    )
    assert governing_for_other_address is not None
    assert governing_for_other_address.rule_id == domain_rule["rule_id"]
    assert governing_for_other_address.policy == "MUST_READ"


# =======================================================================
# C — observed-address protection
# =======================================================================


def test_exact_address_rule_rejects_an_unobserved_address(dev_client):
    mailbox_id = _create_mailbox(dev_client)
    comp = get_composition()

    r = dev_client.post(
        f"/internal/mailboxes/{mailbox_id}/policy-rules",
        json={
            "actor_type": "USER", "actor_id": ACTOR_ID, "match_mode": "EXACT_ADDRESS",
            "sender_domain": "never-seen.example", "sender_address": "nobody@never-seen.example",
            "policy": "BLACKLIST", "reason": "test",
        },
    )
    assert r.status_code == 422, r.text

    assert dev_client.get(f"/internal/mailboxes/{mailbox_id}/microsoft/domain-rules").json()["count"] == 0
    events = comp.api.audit_repository.list_recent(limit=200, event_type_prefix="MAILBOX_POLICY_RULE")
    assert events == []


# =======================================================================
# D — domain mismatch
# =======================================================================


def test_exact_address_rule_rejects_a_domain_mismatch(dev_client):
    mailbox_id, comp, _entity = _connected_mailbox_with_entity_seeded(dev_client)
    _sweep_message_from(dev_client, mailbox_id, sender_address="foo@send.xero.com", msg_id="AAMk-mismatch-1")

    r = dev_client.post(
        f"/internal/mailboxes/{mailbox_id}/policy-rules",
        json={
            "actor_type": "USER", "actor_id": ACTOR_ID, "match_mode": "EXACT_ADDRESS",
            "sender_domain": "example.com", "sender_address": "foo@send.xero.com",
            "policy": "BLACKLIST", "reason": "test",
        },
    )
    assert r.status_code == 422, r.text
    assert dev_client.get(f"/internal/mailboxes/{mailbox_id}/microsoft/domain-rules").json()["count"] == 0


# =======================================================================
# E — idempotency: an identical resubmission never duplicates the rule
# and never emits a misleading second "state changed" audit event.
# =======================================================================


def test_identical_policy_rule_resubmission_is_idempotent(dev_client):
    mailbox_id = _create_mailbox(dev_client)
    comp = get_composition()
    payload = {
        "actor_type": "USER", "actor_id": ACTOR_ID, "match_mode": "EXACT",
        "sender_domain": "vendor.com", "policy": "BLACKLIST", "reason": "known spam vendor",
    }

    first = dev_client.post(f"/internal/mailboxes/{mailbox_id}/policy-rules", json=payload)
    assert first.status_code == 200, first.text
    first_body = first.json()
    assert first_body["was_no_op"] is False
    rule_id = first_body["mailbox_domain_rule"]["rule_id"]

    second = dev_client.post(f"/internal/mailboxes/{mailbox_id}/policy-rules", json=payload)
    assert second.status_code == 200, second.text
    second_body = second.json()
    assert second_body["mailbox_domain_rule"]["rule_id"] == rule_id
    assert second_body["was_no_op"] is True
    assert second_body["previous_policy"] == "BLACKLIST"

    # No duplicate rule row — same rule_id, one row.
    rules = dev_client.get(f"/internal/mailboxes/{mailbox_id}/microsoft/domain-rules").json()["items"]
    assert len(rules) == 1

    # Exactly ONE real audit event — the second, no-op call emitted none.
    events = comp.api.audit_repository.list_by_subject("MailboxDomainRule", rule_id)
    upsert_events = [e for e in events if e.event_type == "MAILBOX_POLICY_RULE_UPSERTED"]
    assert len(upsert_events) == 1


# =======================================================================
# F — change an existing exact-address rule; the broader domain rule is
# completely untouched.
# =======================================================================


def test_changing_an_existing_exact_address_rule_leaves_the_domain_rule_untouched(dev_client):
    mailbox_id, comp, entity = _connected_mailbox_with_entity_seeded(dev_client)
    domain_rule = comp.mailbox_domain_rule_repository.upsert_rule(
        mailbox_id=mailbox_id, sender_domain="example.com", match_mode="EXACT", policy="MUST_READ",
        destination_entity_id=entity.entity_id, destination_mode="FIXED", source="OPERATOR",
    )
    _sweep_message_from(dev_client, mailbox_id, sender_address="ap@example.com", msg_id="AAMk-change-1")

    create_r = dev_client.post(
        f"/internal/mailboxes/{mailbox_id}/policy-rules",
        json={
            "actor_type": "USER", "actor_id": ACTOR_ID, "match_mode": "EXACT_ADDRESS",
            "sender_domain": "example.com", "sender_address": "ap@example.com",
            "policy": "BLACKLIST", "reason": "block this one address for now",
        },
    )
    assert create_r.status_code == 200, create_r.text
    address_rule_id = create_r.json()["mailbox_domain_rule"]["rule_id"]

    change_r = dev_client.post(
        f"/internal/mailboxes/{mailbox_id}/policy-rules",
        json={
            "actor_type": "USER", "actor_id": ACTOR_ID, "match_mode": "EXACT_ADDRESS",
            "sender_domain": "example.com", "sender_address": "ap@example.com",
            "policy": "MUST_READ", "destination_mode": "REVIEW_REQUIRED",
            "reason": "actually this one is fine now",
        },
    )
    assert change_r.status_code == 200, change_r.text
    change_body = change_r.json()
    assert change_body["mailbox_domain_rule"]["rule_id"] == address_rule_id
    assert change_body["previous_policy"] == "BLACKLIST"
    assert change_body["mailbox_domain_rule"]["policy"] == "MUST_READ"
    assert change_body["was_no_op"] is False

    events = comp.api.audit_repository.list_by_subject("MailboxDomainRule", address_rule_id)
    upsert_events = [e for e in events if e.event_type == "MAILBOX_POLICY_RULE_UPSERTED"]
    assert len(upsert_events) == 2
    assert upsert_events[-1].payload["previous_policy"] == "BLACKLIST"
    assert upsert_events[-1].payload["new_policy"] == "MUST_READ"

    still_domain_rule = comp.mailbox_domain_rule_repository.get_rule(domain_rule.rule_id)
    assert still_domain_rule.policy == "MUST_READ"
    assert still_domain_rule.destination_entity_id == entity.entity_id
    assert still_domain_rule.destination_mode == "FIXED"
    assert still_domain_rule.updated_at == domain_rule.updated_at


# =======================================================================
# G — historical evidence invariant
# =======================================================================


def test_creating_address_blacklist_rule_never_mutates_existing_evidence(dev_client):
    mailbox_id, comp, _entity, _item_id, _domain_rule, override_r = _setup_resolved_domain_with_address_override(dev_client)
    # The setup itself already ingested one message + created one
    # EvidenceItem (the ALLOW/REVIEW_REQUIRED resolve's own immediate
    # back-processing) BEFORE the address-level override below exists —
    # capture that state, then prove the override call leaves it intact.
    message_before = comp.mailbox_message_repository.find_by_provider_id(mailbox_id, "AAMk-unknown-1")
    assert message_before is not None
    assert message_before.ingestion_status == "INGESTED"
    assert message_before.evidence_id is not None
    evidence_before = comp.api.get_evidence(message_before.evidence_id)
    messages_before = comp.mailbox_message_repository.list_messages(mailbox_id=mailbox_id, limit=200)

    assert override_r.status_code == 200, override_r.text

    message_after = comp.mailbox_message_repository.find_by_provider_id(mailbox_id, "AAMk-unknown-1")
    assert message_after.ingestion_status == "INGESTED"
    assert message_after.evidence_id == message_before.evidence_id
    evidence_after = comp.api.get_evidence(message_after.evidence_id)
    assert evidence_after.to_dict() == evidence_before.to_dict()
    messages_after = comp.mailbox_message_repository.list_messages(mailbox_id=mailbox_id, limit=200)
    assert len(messages_after) == len(messages_before)
    assert {m.mailbox_message_id for m in messages_after} == {m.mailbox_message_id for m in messages_before}


# =======================================================================
# Security — the EXISTING Stage-B sweep gate (services/mailbox/sweep.py)
# correctly honors an EXACT_ADDRESS BLACKLIST rule created through this
# NEW endpoint: never a MIME fetch, never a new EvidenceItem, never a
# COMPANY_REQUIRED item, never a re-raised domain-review item.
# =======================================================================


def test_message_from_a_newly_blacklisted_exact_address_is_never_fetched_or_evidenced(dev_client):
    mailbox_id, comp, _entity, item_id, _domain_rule, override_r = _setup_resolved_domain_with_address_override(dev_client)
    assert override_r.status_code == 200, override_r.text
    content_calls_before = len(comp.microsoft_graph_client.content_calls)
    # The setup's own REVIEW_REQUIRED back-processing already raised a
    # COMPANY_REQUIRED item for the ORIGINAL ingested message — capture
    # that count so this test proves no NEW one is added, not that zero
    # exist.
    company_required_before = len(comp.needs_you_repository.list_needs_you_items(item_type="COMPANY_REQUIRED"))

    _sweep_message_from(
        dev_client, mailbox_id, sender_address="billing@new-supplier.example", msg_id="AAMk-post-blacklist-1"
    )

    message = comp.mailbox_message_repository.find_by_provider_id(mailbox_id, "AAMk-post-blacklist-1")
    assert message is not None
    assert message.ingestion_status == "CHECKED_NOT_CANDIDATE"
    assert message.evidence_id is None

    # No MIME content fetch happened for this new sweep.
    assert len(comp.microsoft_graph_client.content_calls) == content_calls_before

    # No new COMPANY_REQUIRED item for this newly-blacklisted message.
    company_required_after = comp.needs_you_repository.list_needs_you_items(item_type="COMPANY_REQUIRED")
    assert len(company_required_after) == company_required_before
    assert all(i.metadata.get("mailbox_message_id") != message.mailbox_message_id for i in company_required_after)

    # No re-raised MAILBOX_DOMAIN_REVIEW item — only the original
    # (already-RESOLVED) item still exists.
    domain_review_items = [
        i for i in comp.needs_you_repository.list_needs_you_items(item_type="MAILBOX_DOMAIN_REVIEW")
        if i.metadata.get("mailbox_id") == mailbox_id
    ]
    assert len(domain_review_items) == 1
    assert domain_review_items[0].item_id == item_id
    assert domain_review_items[0].status == "RESOLVED"


# =======================================================================
# Miscellaneous validation
# =======================================================================


def test_unknown_mailbox_id_returns_404(dev_client):
    r = dev_client.post(
        "/internal/mailboxes/does-not-exist/policy-rules",
        json={
            "actor_type": "USER", "actor_id": ACTOR_ID, "match_mode": "EXACT",
            "sender_domain": "vendor.com", "policy": "BLACKLIST", "reason": "test",
        },
    )
    assert r.status_code == 404, r.text


def test_blank_reason_is_rejected(dev_client):
    mailbox_id = _create_mailbox(dev_client)
    r = dev_client.post(
        f"/internal/mailboxes/{mailbox_id}/policy-rules",
        json={
            "actor_type": "USER", "actor_id": ACTOR_ID, "match_mode": "EXACT",
            "sender_domain": "vendor.com", "policy": "BLACKLIST", "reason": "   ",
        },
    )
    assert r.status_code == 422, r.text


def test_graylist_with_a_destination_is_rejected(dev_client):
    mailbox_id, _comp, entity = _connected_mailbox_with_entity_seeded(dev_client)
    r = dev_client.post(
        f"/internal/mailboxes/{mailbox_id}/policy-rules",
        json={
            "actor_type": "USER", "actor_id": ACTOR_ID, "match_mode": "EXACT",
            "sender_domain": "vendor.com", "policy": "GRAYLIST",
            "destination_entity_id": entity.entity_id, "destination_mode": "FIXED",
            "reason": "should never be accepted",
        },
    )
    assert r.status_code == 422, r.text


def test_must_read_fixed_requires_a_real_entity(dev_client):
    mailbox_id = _create_mailbox(dev_client)
    r = dev_client.post(
        f"/internal/mailboxes/{mailbox_id}/policy-rules",
        json={
            "actor_type": "USER", "actor_id": ACTOR_ID, "match_mode": "EXACT",
            "sender_domain": "vendor.com", "policy": "MUST_READ", "destination_mode": "FIXED",
            "destination_entity_id": "does-not-exist",
            "reason": "test",
        },
    )
    assert r.status_code == 404, r.text


def test_endpoint_provider_neutral_never_requires_microsoft_provider_kind(dev_client):
    """Unlike every endpoint in ``mailboxes_microsoft.py``, this one
    never requires ``provider_kind == MICROSOFT_GRAPH``."""
    mailbox_id = _create_mailbox(dev_client, provider_kind="IMAP", email="ops@noustai.example")
    r = dev_client.post(
        f"/internal/mailboxes/{mailbox_id}/policy-rules",
        json={
            "actor_type": "USER", "actor_id": ACTOR_ID, "match_mode": "EXACT",
            "sender_domain": "vendor.com", "policy": "BLACKLIST", "reason": "provider-neutral proof",
        },
    )
    assert r.status_code == 200, r.text
    assert r.json()["mailbox_domain_rule"]["match_mode"] == "EXACT"


def test_never_touches_needs_you_and_never_back_processes_history(dev_client):
    mailbox_id, comp, entity = _connected_mailbox_with_entity_seeded(dev_client)
    _sweep_message_from(dev_client, mailbox_id, sender_address="ap@vendor.com", msg_id="AAMk-nobackfill-1")
    before_items = comp.needs_you_repository.list_needs_you_items(item_type="MAILBOX_DOMAIN_REVIEW")

    r = dev_client.post(
        f"/internal/mailboxes/{mailbox_id}/policy-rules",
        json={
            "actor_type": "USER", "actor_id": ACTOR_ID, "match_mode": "EXACT_ADDRESS",
            "sender_domain": "vendor.com", "sender_address": "ap@vendor.com", "policy": "MUST_READ",
            "destination_mode": "FIXED", "destination_entity_id": entity.entity_id,
            "reason": "trust this specific AP contact",
        },
    )
    assert r.status_code == 200, r.text

    # No back-processing — the historical candidate message is still
    # exactly as the discovery-only Stage-B path left it, never fetched.
    message = comp.mailbox_message_repository.find_by_provider_id(mailbox_id, "AAMk-nobackfill-1")
    assert message.ingestion_status == "CHECKED_NOT_CANDIDATE"
    assert message.evidence_id is None
    assert comp.microsoft_graph_client.content_calls == []

    # No Needs You item touched/created by this endpoint.
    after_items = comp.needs_you_repository.list_needs_you_items(item_type="MAILBOX_DOMAIN_REVIEW")
    assert len(after_items) == len(before_items)
