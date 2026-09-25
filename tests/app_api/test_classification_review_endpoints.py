"""CD-6 Slice 5 WI-4 — HTTP-level proofs for the parts of the
human-control loop that are genuinely router behaviour (never service-
module behaviour): the generic idempotent-replay/conflict logic already
built into ``app/api/routers/needs_you.py::resolve_needs_you_item``
(§50/§51), the DISMISSED rejection guard (§26/§58), and the new
classification-history read endpoint (§43/§44).

Runs against the DEV-MODE in-memory composition (never real Postgres —
mirrors `tests/integration/test_architecture_boundaries.py
::test_wi3_document_type_proposal_v2_rejected_by_generic_ai_tasks_endpoint`'s
own exact pattern: `reset_composition_for_tests()` + a fresh
`TestClient(app)`), since this module tests HTTP/router wiring, not
PostgreSQL durability (that is `tests/persistence/`'s job — see
`tests/persistence/test_classification_review_postgres.py` for the real
concurrency proof)."""
from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone

from fastapi.testclient import TestClient

from app.api.composition import get_composition, reset_composition_for_tests
from app.api.main import app
from core import identity

ACTOR_TYPE = "SYSTEM"
ACTOR_ID = "wi4-http-test"


def _client():
    reset_composition_for_tests()
    return TestClient(app), get_composition()


def _rfc822_email(*, sender: str, subject: str, body: str) -> bytes:
    import email.message

    msg = email.message.EmailMessage()
    msg["From"] = sender
    msg["Subject"] = subject
    msg.set_content(body)
    return bytes(msg)


def _register_evidence(composition, *, sender_address="billing@vendor.com", subject="Invoice 42"):
    source = composition.api.register_source(
        source_type="MANUAL_UPLOAD", provider="wi4-http-test", status="ACTIVE",
        actor_type=ACTOR_TYPE, actor_id=ACTOR_ID,
    )
    content = _rfc822_email(sender=sender_address, subject=subject, body="Please pay $500.")
    content_hash = {"algorithm": "SHA-256", "value": hashlib.sha256(content).hexdigest()}
    storage_reference = composition.object_store.put(identity.generate_id(), content_hash, content)
    evidence = composition.api.register_evidence(
        entity_id=None, evidence_type="EMAIL", source_id=source.source_id,
        observed_at=datetime.now(timezone.utc), received_at=datetime.now(timezone.utc),
        content_hash=content_hash, mime_type="message/rfc822", size_bytes=len(content),
        actor_type=ACTOR_TYPE, actor_id=ACTOR_ID, storage_reference=storage_reference,
        metadata={"sender_address": sender_address, "subject": subject},
    )
    return evidence.evidence_id


def _create_ai_proposal(client, composition, *, sender_address="billing@vendor.com", subject="Invoice 42",
                         proposed_type="SUPPLIER_INVOICE", confidence=0.9):
    evidence_id = _register_evidence(composition, sender_address=sender_address, subject=subject)
    composition.litellm_client.queue_success(
        capability_alias="bagman-core",
        content=json.dumps({"proposed_type": proposed_type, "confidence": confidence, "signals": [], "warnings": []}),
    )
    response = client.post(
        f"/internal/evidence/{evidence_id}/classifications/orchestrated",
        json={"actor_type": ACTOR_TYPE, "actor_id": ACTOR_ID},
    )
    assert response.status_code == 201, response.text
    body = response.json()
    classification_id = body["evidence_classification"]["classification_id"]
    return evidence_id, classification_id


def _find_open_review_item(client, classification_id):
    listing = client.get("/internal/needs-you", params={"status": "OPEN", "item_type": "CLASSIFICATION_REVIEW"}).json()
    matching = [i for i in listing["items"] if i.get("source_object_reference") == classification_id]
    assert len(matching) == 1, f"expected exactly one OPEN review item, found {len(matching)}: {matching}"
    return matching[0]


# ---------------------------------------------------------------------
# End-to-end wiring: orchestrated persist -> review item -> resolve
# ---------------------------------------------------------------------


def test_orchestrated_persist_creates_exactly_one_open_classification_review_item():
    client, composition = _client()
    evidence_id, classification_id = _create_ai_proposal(client, composition)
    item = _find_open_review_item(client, classification_id)
    assert item["item_type"] == "CLASSIFICATION_REVIEW"
    assert item["allowed_action_type"] == "CLASSIFICATION_REVIEW"
    assert item["domain"] == "EVIDENCE"
    assert item["metadata"]["evidence_id"] == evidence_id
    assert item["metadata"]["proposed_type"] == "SUPPLIER_INVOICE"


def test_resolve_classification_review_confirms_and_resolves_needs_you_item():
    client, composition = _client()
    _evidence_id, classification_id = _create_ai_proposal(client, composition)
    item = _find_open_review_item(client, classification_id)

    response = client.post(
        f"/internal/needs-you/{item['item_id']}/resolve",
        json={
            "new_status": "RESOLVED", "actor_type": "USER", "actor_id": "matt",
            "resolution": {"document_type": "SUPPLIER_INVOICE", "teach_rule": None},
        },
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["status"] == "RESOLVED"
    assert body["resolution"] == {"document_type": "SUPPLIER_INVOICE", "teach_rule": None}


# ---------------------------------------------------------------------
# §50 — exact retry
# ---------------------------------------------------------------------


def test_exact_retry_of_same_resolution_is_idempotent_no_conflict():
    client, composition = _client()
    _evidence_id, classification_id = _create_ai_proposal(client, composition)
    item = _find_open_review_item(client, classification_id)
    resolution = {"document_type": "SUPPLIER_INVOICE", "teach_rule": None}
    payload = {"new_status": "RESOLVED", "actor_type": "USER", "actor_id": "matt", "resolution": resolution}

    first = client.post(f"/internal/needs-you/{item['item_id']}/resolve", json=payload)
    assert first.status_code == 200, first.text
    second = client.post(f"/internal/needs-you/{item['item_id']}/resolve", json=payload)
    assert second.status_code == 200, second.text
    assert first.json() == second.json()


# ---------------------------------------------------------------------
# §51 — conflicting second resolution
# ---------------------------------------------------------------------


def test_conflicting_second_resolution_after_resolved_returns_409():
    client, composition = _client()
    _evidence_id, classification_id = _create_ai_proposal(client, composition)
    item = _find_open_review_item(client, classification_id)

    first = client.post(
        f"/internal/needs-you/{item['item_id']}/resolve",
        json={
            "new_status": "RESOLVED", "actor_type": "USER", "actor_id": "matt",
            "resolution": {"document_type": "SUPPLIER_INVOICE", "teach_rule": None},
        },
    )
    assert first.status_code == 200, first.text

    second = client.post(
        f"/internal/needs-you/{item['item_id']}/resolve",
        json={
            "new_status": "RESOLVED", "actor_type": "USER", "actor_id": "matt",
            "resolution": {"document_type": "RECEIPT", "teach_rule": None},
        },
    )
    assert second.status_code == 409, second.text


# ---------------------------------------------------------------------
# §26/§58 — DISMISSED rejection
# ---------------------------------------------------------------------


def test_dismissed_is_rejected_for_classification_review_with_no_mutation():
    client, composition = _client()
    _evidence_id, classification_id = _create_ai_proposal(client, composition)
    item = _find_open_review_item(client, classification_id)

    response = client.post(
        f"/internal/needs-you/{item['item_id']}/resolve",
        json={"new_status": "DISMISSED", "actor_type": "USER", "actor_id": "matt", "resolution": {}},
    )
    assert response.status_code == 422, response.text

    fetched = client.get(f"/internal/needs-you/{item['item_id']}").json()
    assert fetched["status"] == "OPEN"

    classification_history = client.get(f"/internal/evidence/{_evidence_id}/classifications").json()
    assert len(classification_history["items"]) == 1  # only the original AI proposal — no operator row


# ---------------------------------------------------------------------
# WI-4-correction §18 — closed resolution-shape validation
# ---------------------------------------------------------------------


def test_resolution_with_unsupported_extra_key_is_rejected_via_http():
    client, composition = _client()
    _evidence_id, classification_id = _create_ai_proposal(client, composition)
    item = _find_open_review_item(client, classification_id)

    response = client.post(
        f"/internal/needs-you/{item['item_id']}/resolve",
        json={
            "new_status": "RESOLVED", "actor_type": "USER", "actor_id": "matt",
            "resolution": {"document_type": "SUPPLIER_INVOICE", "teach_rule": None, "confidence": 0.99},
        },
    )
    assert response.status_code == 422, response.text

    fetched = client.get(f"/internal/needs-you/{item['item_id']}").json()
    assert fetched["status"] == "OPEN"
    classification_history = client.get(f"/internal/evidence/{_evidence_id}/classifications").json()
    assert len(classification_history["items"]) == 1


# ---------------------------------------------------------------------
# WI-4-correction §11 — different document_type after a partial
# failure must never silently resolve with the wrong canonical truth.
# ---------------------------------------------------------------------


def test_different_document_type_retry_via_http_returns_409_and_preserves_committed_classification():
    client, composition = _client()
    _evidence_id, classification_id = _create_ai_proposal(client, composition, proposed_type="RECEIPT")
    item = _find_open_review_item(client, classification_id)

    # First request commits SUPPLIER_INVOICE at the SERVICE layer only
    # (never reaching the router's own final `resolve_needs_you_item`
    # call) — simulating "operator classification committed, then a
    # downstream failure before Needs You resolution" exactly like the
    # service-level tests do, but here proven through the real
    # composition's own classification repository directly, then the
    # retry goes through the REAL HTTP endpoint.
    from services.evidence.classification_review import resolve_classification_review

    needs_you_item = composition.needs_you_repository.get_needs_you_item(item["item_id"])
    first = resolve_classification_review(
        needs_you_item=needs_you_item, resolution={"document_type": "SUPPLIER_INVOICE", "teach_rule": None},
        actor_type="USER", actor_id="matt",
        evidence_repository=composition.api.evidence_repository,
        classification_repository=composition.classification_repository,
        rule_repository=composition.classification_rule_repository,
        audit_repository=composition.api.audit_repository,
        record_audit_event=composition.api.record_audit_event,
    )
    assert first.classification_was_created is True
    assert first.classification.document_type == "SUPPLIER_INVOICE"

    response = client.post(
        f"/internal/needs-you/{item['item_id']}/resolve",
        json={
            "new_status": "RESOLVED", "actor_type": "USER", "actor_id": "matt",
            "resolution": {"document_type": "RECEIPT", "teach_rule": None},
        },
    )
    assert response.status_code == 409, response.text

    fetched = client.get(f"/internal/needs-you/{item['item_id']}").json()
    assert fetched["status"] == "OPEN"
    classification_history = client.get(f"/internal/evidence/{_evidence_id}/classifications").json()
    assert len(classification_history["items"]) == 2
    current = [c for c in classification_history["items"] if c["is_current"]][0]
    assert current["document_type"] == "SUPPLIER_INVOICE"
    assert current["classification_id"] == first.classification.classification_id


# ---------------------------------------------------------------------
# §43/§44 — classification history read endpoint
# ---------------------------------------------------------------------


def test_classification_history_endpoint_reflects_correction_and_current_tip():
    client, composition = _client()
    evidence_id, classification_id = _create_ai_proposal(client, composition, proposed_type="RECEIPT")
    item = _find_open_review_item(client, classification_id)

    resolve_response = client.post(
        f"/internal/needs-you/{item['item_id']}/resolve",
        json={
            "new_status": "RESOLVED", "actor_type": "USER", "actor_id": "matt",
            "resolution": {"document_type": "SUPPLIER_INVOICE", "teach_rule": None},
        },
    )
    assert resolve_response.status_code == 200, resolve_response.text

    history_response = client.get(f"/internal/evidence/{evidence_id}/classifications")
    assert history_response.status_code == 200, history_response.text
    body = history_response.json()
    assert body["evidence_id"] == evidence_id
    assert body["classification_type"] == "DOCUMENT_TYPE"
    items = body["items"]
    assert len(items) == 2
    assert items[0]["classification_id"] == classification_id
    assert items[0]["source"] == "AI_PROPOSAL"
    assert items[0]["is_current"] is False
    assert items[1]["source"] == "OPERATOR_ASSIGNED"
    assert items[1]["document_type"] == "SUPPLIER_INVOICE"
    assert items[1]["is_current"] is True
    assert items[1]["supersedes_classification_id"] == classification_id


def test_classification_history_endpoint_404s_for_unknown_evidence():
    client, _composition = _client()
    response = client.get(f"/internal/evidence/{identity.generate_id()}/classifications")
    assert response.status_code == 404
