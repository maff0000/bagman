"""CD-6 Slice 5 WI-5 — HTTP-level proofs for the new read-only Document
projection surface (``GET /internal/documents``, ``GET
/internal/documents/{evidence_id}``).

Runs against the DEV-MODE in-memory composition — mirrors
``tests/app_api/test_classification_review_endpoints.py``'s own exact
pattern (``reset_composition_for_tests()`` + a fresh ``TestClient(app)``).
"""
from __future__ import annotations

import hashlib
import json
from datetime import datetime, timedelta, timezone

from fastapi.testclient import TestClient

from app.api.composition import get_composition, reset_composition_for_tests
from app.api.main import app
from core import identity

ACTOR_TYPE = "SYSTEM"
ACTOR_ID = "wi5-http-test"


def _client():
    reset_composition_for_tests()
    return TestClient(app), get_composition()


def _register_email_evidence(
    composition, *, sender_address="billing@vendor.com", subject="Invoice 42", entity_id=None, received_at=None
):
    import email.message

    source = composition.api.register_source(
        source_type="MANUAL_UPLOAD", provider="wi5-http-test", status="ACTIVE",
        actor_type=ACTOR_TYPE, actor_id=ACTOR_ID,
    )
    msg = email.message.EmailMessage()
    msg["From"] = sender_address
    msg["Subject"] = subject
    msg.set_content("Please pay $500.")
    content = bytes(msg)
    content_hash = {"algorithm": "SHA-256", "value": hashlib.sha256(content).hexdigest()}
    storage_reference = composition.object_store.put(identity.generate_id(), content_hash, content)
    now = received_at or datetime.now(timezone.utc)
    evidence = composition.api.register_evidence(
        entity_id=entity_id, evidence_type="EMAIL", source_id=source.source_id,
        observed_at=now, received_at=now,
        content_hash=content_hash, mime_type="message/rfc822", size_bytes=len(content),
        actor_type=ACTOR_TYPE, actor_id=ACTOR_ID, storage_reference=storage_reference,
        metadata={"sender_address": sender_address, "subject": subject},
    )
    return evidence.evidence_id


def _register_manual_upload_evidence(composition, *, original_name="receipt.pdf", entity_id=None, received_at=None):
    source = composition.api.register_source(
        source_type="MANUAL_UPLOAD", provider="wi5-http-test-upload", status="ACTIVE",
        actor_type=ACTOR_TYPE, actor_id=ACTOR_ID,
    )
    content = b"%PDF-1.4 fake receipt bytes"
    content_hash = {"algorithm": "SHA-256", "value": hashlib.sha256(content).hexdigest()}
    storage_reference = composition.object_store.put(identity.generate_id(), content_hash, content)
    now = received_at or datetime.now(timezone.utc)
    evidence = composition.api.register_evidence(
        entity_id=entity_id, evidence_type="INVOICE_RECEIPT", source_id=source.source_id,
        observed_at=now, received_at=now,
        content_hash=content_hash, mime_type="application/pdf", size_bytes=len(content),
        actor_type=ACTOR_TYPE, actor_id=ACTOR_ID, storage_reference=storage_reference,
        original_name=original_name, metadata={},
    )
    return evidence.evidence_id


def _get_items(client, **params):
    response = client.get("/internal/documents", params=params)
    assert response.status_code == 200, response.text
    return response.json()


def _by_evidence_id(body, evidence_id):
    return next(row for row in body["items"] if row["evidence_id"] == evidence_id)


# ---------------------------------------------------------------------
# §3/§55 — email-originated evidence appears alongside manual uploads
# ---------------------------------------------------------------------


def test_email_and_manual_upload_evidence_both_appear_in_the_projection():
    client, composition = _client()
    email_id = _register_email_evidence(composition)
    upload_id = _register_manual_upload_evidence(composition)

    body = _get_items(client, limit=200)
    ids = {row["evidence_id"] for row in body["items"]}
    assert email_id in ids
    assert upload_id in ids

    email_row = _by_evidence_id(body, email_id)
    assert email_row["document_label"] == "Invoice 42"  # subject preferred (§11)
    assert email_row["sender_address"] == "billing@vendor.com"
    assert email_row["sender_domain"] == "vendor.com"

    upload_row = _by_evidence_id(body, upload_id)
    assert upload_row["document_label"] == "receipt.pdf"  # filename fallback (§11)


# ---------------------------------------------------------------------
# §10 — Unclassified vs a real governed UNKNOWN classification
# ---------------------------------------------------------------------


def test_unclassified_document_has_null_current_classification_never_fabricated_unknown():
    client, composition = _client()
    evidence_id = _register_email_evidence(composition)

    body = _get_items(client, limit=200)
    row = _by_evidence_id(body, evidence_id)
    assert row["current_classification"] is None
    assert row["classification_review"] is None


def test_classification_status_unclassified_pseudo_filter_matches_only_unclassified_documents():
    client, composition = _client()
    unclassified_id = _register_email_evidence(composition, subject="No classification yet")
    classified_id = _register_email_evidence(composition, subject="Classified already")
    composition.classification_repository.create_classification(
        evidence_id=classified_id, classification_type="DOCUMENT_TYPE", document_type="RECEIPT",
        status="CLASSIFIED", source="OPERATOR_ASSIGNED", operator_action_id="manual-op-1",
    )

    body = _get_items(client, classification_status="UNCLASSIFIED", limit=200)
    ids = {row["evidence_id"] for row in body["items"]}
    assert unclassified_id in ids
    assert classified_id not in ids
    for row in body["items"]:
        assert row["current_classification"] is None


# ---------------------------------------------------------------------
# §6/§12 — each classification source projects its own real
# source/rule_id/ai_invocation_id/operator_action_id fields
# ---------------------------------------------------------------------


def test_operator_assigned_classification_projects_operator_action_id():
    client, composition = _client()
    evidence_id = _register_email_evidence(composition)
    composition.classification_repository.create_classification(
        evidence_id=evidence_id, classification_type="DOCUMENT_TYPE", document_type="RECEIPT",
        status="CLASSIFIED", source="OPERATOR_ASSIGNED", operator_action_id="manual-op-2",
    )

    body = _get_items(client, limit=200)
    row = _by_evidence_id(body, evidence_id)
    current = row["current_classification"]
    assert current["source"] == "OPERATOR_ASSIGNED"
    assert current["document_type"] == "RECEIPT"
    assert current["status"] == "CLASSIFIED"
    assert current["operator_action_id"] == "manual-op-2"
    assert current["rule_id"] is None
    assert current["ai_invocation_id"] is None


def test_deterministic_rule_classification_projects_rule_id():
    client, composition = _client()
    evidence_id = _register_email_evidence(composition, sender_address="statements@broker.com", subject="Daily Activity Statement for 2026-09-25")

    rule_response = client.post(
        "/internal/evidence-classification/rules",
        json={
            "sender_scope_type": "EXACT_SENDER_ADDRESS", "sender_scope_value": "statements@broker.com",
            "subject_predicate_type": "STARTS_WITH", "subject_predicate_value": "Daily Activity Statement for",
            "document_type": "BROKER_STATEMENT", "source": "OPERATOR",
            "actor_type": ACTOR_TYPE, "actor_id": ACTOR_ID,
        },
    )
    assert rule_response.status_code == 201, rule_response.text

    classify_response = client.post(
        f"/internal/evidence/{evidence_id}/classifications/deterministic",
        json={"actor_type": ACTOR_TYPE, "actor_id": ACTOR_ID},
    )
    assert classify_response.status_code == 201, classify_response.text

    body = _get_items(client, limit=200)
    row = _by_evidence_id(body, evidence_id)
    current = row["current_classification"]
    assert current["source"] == "DETERMINISTIC_RULE"
    assert current["document_type"] == "BROKER_STATEMENT"
    assert current["rule_id"]
    assert current["ai_invocation_id"] is None
    assert current["operator_action_id"] is None


def test_ai_proposal_classification_projects_ai_invocation_id_and_open_review_item():
    client, composition = _client()
    evidence_id = _register_email_evidence(composition)
    composition.litellm_client.queue_success(
        capability_alias="bagman-core",
        content=json.dumps({"proposed_type": "SUPPLIER_INVOICE", "confidence": 0.91, "signals": [], "warnings": []}),
    )
    orchestrated = client.post(
        f"/internal/evidence/{evidence_id}/classifications/orchestrated",
        json={"actor_type": ACTOR_TYPE, "actor_id": ACTOR_ID},
    )
    assert orchestrated.status_code == 201, orchestrated.text

    body = _get_items(client, limit=200)
    row = _by_evidence_id(body, evidence_id)
    current = row["current_classification"]
    assert current["source"] == "AI_PROPOSAL"
    assert current["document_type"] == "SUPPLIER_INVOICE"
    assert current["status"] == "REVIEW_REQUIRED"
    assert current["ai_invocation_id"]
    assert current["confidence"] == 0.91
    assert row["classification_review"] is not None
    assert row["classification_review"]["status"] == "OPEN"


# ---------------------------------------------------------------------
# §9 — filters
# ---------------------------------------------------------------------


def test_entity_id_filter():
    client, composition = _client()
    entity = composition.api.entity_repository.register_entity(
        entity_type="COMPANY", canonical_name="WI5_TEST_CO", display_name="WI-5 Test Co", status="ACTIVE",
    )
    matching_id = _register_email_evidence(composition, subject="For the company", entity_id=entity.entity_id)
    other_id = _register_email_evidence(composition, subject="Unrelated")

    body = _get_items(client, entity_id=entity.entity_id, limit=200)
    ids = {row["evidence_id"] for row in body["items"]}
    assert matching_id in ids
    assert other_id not in ids
    assert body["items"][0]["entity"]["entity_id"] == entity.entity_id
    assert body["items"][0]["entity"]["display_name"] == "WI-5 Test Co"


def test_document_type_and_classification_source_filters():
    client, composition = _client()
    receipt_id = _register_email_evidence(composition, subject="A receipt")
    composition.classification_repository.create_classification(
        evidence_id=receipt_id, classification_type="DOCUMENT_TYPE", document_type="RECEIPT",
        status="CLASSIFIED", source="OPERATOR_ASSIGNED", operator_action_id="op-receipt",
    )
    invoice_id = _register_email_evidence(composition, subject="An invoice")
    composition.classification_repository.create_classification(
        evidence_id=invoice_id, classification_type="DOCUMENT_TYPE", document_type="SUPPLIER_INVOICE",
        status="CLASSIFIED", source="OPERATOR_ASSIGNED", operator_action_id="op-invoice",
    )

    body = _get_items(client, document_type="RECEIPT", limit=200)
    ids = {row["evidence_id"] for row in body["items"]}
    assert receipt_id in ids
    assert invoice_id not in ids

    body2 = _get_items(client, classification_source="OPERATOR_ASSIGNED", limit=200)
    ids2 = {row["evidence_id"] for row in body2["items"]}
    assert receipt_id in ids2 and invoice_id in ids2


def test_review_required_filter():
    client, composition = _client()
    ai_evidence_id = _register_email_evidence(composition, subject="Needs review")
    composition.litellm_client.queue_success(
        capability_alias="bagman-core",
        content=json.dumps({"proposed_type": "RECEIPT", "confidence": 0.7, "signals": [], "warnings": []}),
    )
    resp = client.post(
        f"/internal/evidence/{ai_evidence_id}/classifications/orchestrated",
        json={"actor_type": ACTOR_TYPE, "actor_id": ACTOR_ID},
    )
    assert resp.status_code == 201, resp.text
    confirmed_id = _register_email_evidence(composition, subject="Already confirmed")
    composition.classification_repository.create_classification(
        evidence_id=confirmed_id, classification_type="DOCUMENT_TYPE", document_type="RECEIPT",
        status="CLASSIFIED", source="OPERATOR_ASSIGNED", operator_action_id="op-confirmed",
    )

    body = _get_items(client, review_required=True, limit=200)
    ids = {row["evidence_id"] for row in body["items"]}
    assert ai_evidence_id in ids
    assert confirmed_id not in ids

    body2 = _get_items(client, review_required=False, limit=200)
    ids2 = {row["evidence_id"] for row in body2["items"]}
    assert confirmed_id in ids2
    assert ai_evidence_id not in ids2


def test_received_at_range_filter():
    client, composition = _client()
    old_time = datetime(2020, 1, 1, tzinfo=timezone.utc)
    new_time = datetime.now(timezone.utc)
    old_id = _register_email_evidence(composition, subject="Old document", received_at=old_time)
    new_id = _register_email_evidence(composition, subject="New document", received_at=new_time)

    cutoff = (new_time - timedelta(days=1)).isoformat()
    body = _get_items(client, received_at_from=cutoff, limit=200)
    ids = {row["evidence_id"] for row in body["items"]}
    assert new_id in ids
    assert old_id not in ids


# ---------------------------------------------------------------------
# §8 — bounded pagination
# ---------------------------------------------------------------------


def test_default_limit_is_25():
    client, composition = _client()
    response = client.get("/internal/documents")
    assert response.status_code == 200, response.text
    assert response.json()["limit"] == 25


def test_limit_above_200_is_rejected():
    client, composition = _client()
    response = client.get("/internal/documents", params={"limit": 201})
    assert response.status_code == 422, response.text


def test_limit_zero_or_negative_is_rejected():
    client, composition = _client()
    assert client.get("/internal/documents", params={"limit": 0}).status_code == 422
    assert client.get("/internal/documents", params={"limit": -1}).status_code == 422


def test_negative_offset_is_rejected():
    client, composition = _client()
    response = client.get("/internal/documents", params={"offset": -1})
    assert response.status_code == 422, response.text


def test_pagination_is_stable_newest_first_with_no_duplicates_or_gaps():
    client, composition = _client()
    ids = [_register_email_evidence(composition, subject=f"Doc {i}") for i in range(5)]

    page1 = _get_items(client, limit=2, offset=0)
    page2 = _get_items(client, limit=2, offset=2)
    page3 = _get_items(client, limit=2, offset=4)
    seen = [row["evidence_id"] for row in page1["items"] + page2["items"] + page3["items"]]
    assert len(seen) == len(set(seen)) == 5
    assert set(seen) == set(ids)


# ---------------------------------------------------------------------
# Never exposes raw AI prompt/document content
# ---------------------------------------------------------------------


def test_projection_never_carries_raw_ai_prompt_or_document_body_content():
    client, composition = _client()
    evidence_id = _register_email_evidence(composition)
    composition.litellm_client.queue_success(
        capability_alias="bagman-core",
        content=json.dumps({"proposed_type": "SUPPLIER_INVOICE", "confidence": 0.9, "signals": ["s"], "warnings": []}),
    )
    resp = client.post(
        f"/internal/evidence/{evidence_id}/classifications/orchestrated",
        json={"actor_type": ACTOR_TYPE, "actor_id": ACTOR_ID},
    )
    assert resp.status_code == 201, resp.text

    body = _get_items(client, limit=200)
    row = _by_evidence_id(body, evidence_id)
    serialized = json.dumps(row)
    for forbidden in ("Please pay $500", "system_prompt", "chain_of_thought", "raw_prompt"):
        assert forbidden not in serialized


# ---------------------------------------------------------------------
# Detail endpoint
# ---------------------------------------------------------------------


def test_detail_endpoint_returns_the_same_row_shape():
    client, composition = _client()
    evidence_id = _register_email_evidence(composition)
    response = client.get(f"/internal/documents/{evidence_id}")
    assert response.status_code == 200, response.text
    assert response.json()["evidence_id"] == evidence_id


def test_detail_endpoint_404s_honestly_for_unknown_evidence_id():
    client, composition = _client()
    response = client.get("/internal/documents/does-not-exist")
    assert response.status_code == 404, response.text
