"""HTTP-level tests for ``/internal/evidence-classification/*`` and
``/internal/evidence/{evidence_id}/classifications/deterministic`` (CD-6
Slice 5 WI-2). Mirrors ``tests/app_api/test_mailbox_policy_rules_endpoint.py``'s
own lightweight ``TestClient``-only style (in-memory composition — no
Docker required)."""
from __future__ import annotations

from datetime import datetime, timezone

import pytest
from fastapi.testclient import TestClient

from app.api.composition import get_composition, reset_composition_for_tests
from app.api.main import app
from core import actor, identity

ACTOR_ID = "bagman-evidence-classification-endpoint-tests"


@pytest.fixture
def client():
    reset_composition_for_tests()
    return TestClient(app)


def _register_email_evidence(composition, *, sender_address, subject) -> str:
    source = composition.api.register_source(
        source_type="MAILBOX_TEST", provider="test", status="ACTIVE", actor_type=actor.SYSTEM, actor_id=ACTOR_ID
    )
    now = datetime.now(timezone.utc)
    evidence = composition.api.register_evidence(
        entity_id=None, evidence_type="EMAIL", source_id=source.source_id,
        observed_at=now, received_at=now, content_hash=identity.generate_id().replace("-", "") * 2,
        mime_type="message/rfc822", size_bytes=10, actor_type=actor.SYSTEM, actor_id=ACTOR_ID,
        metadata={"sender_address": sender_address, "subject": subject},
    )
    return evidence.evidence_id


_RULE_BODY = {
    "sender_scope_type": "EXACT_SENDER_DOMAIN",
    "sender_scope_value": "vendor.com",
    "subject_predicate_type": "EXACT",
    "subject_predicate_value": "Monthly Statement",
    "document_type": "SUPPLIER_INVOICE",
}


def test_preview_read_only(client: TestClient):
    composition = get_composition()
    ev = _register_email_evidence(composition, sender_address="billing@vendor.com", subject="Monthly Statement")

    response = client.post("/internal/evidence-classification/rules/preview", json=_RULE_BODY)
    assert response.status_code == 200
    body = response.json()
    assert body["match_count"] == 1
    assert body["representative_evidence_ids"] == [ev]
    # Never persists anything.
    assert composition.classification_rule_repository.list_rules() == []


def test_create_rule_then_replay(client: TestClient):
    composition = get_composition()
    _register_email_evidence(composition, sender_address="billing@vendor.com", subject="Monthly Statement")

    payload = {**_RULE_BODY, "source": "OPERATOR", "actor_type": actor.SYSTEM, "actor_id": ACTOR_ID}
    created = client.post("/internal/evidence-classification/rules", json=payload)
    assert created.status_code == 201
    assert created.json()["was_created"] is True

    replay = client.post("/internal/evidence-classification/rules", json=payload)
    assert replay.status_code == 200
    assert replay.json()["was_created"] is False
    assert replay.json()["evidence_classification_rule"]["rule_id"] == created.json()["evidence_classification_rule"]["rule_id"]


def test_create_rule_zero_matches_rejected(client: TestClient):
    payload = {**_RULE_BODY, "source": "OPERATOR", "actor_type": actor.SYSTEM, "actor_id": ACTOR_ID}
    response = client.post("/internal/evidence-classification/rules", json=payload)
    assert response.status_code == 422


def test_create_rule_bagman_proposed_source_rejected(client: TestClient):
    composition = get_composition()
    _register_email_evidence(composition, sender_address="billing@vendor.com", subject="Monthly Statement")
    payload = {**_RULE_BODY, "source": "BAGMAN_PROPOSED", "actor_type": actor.SYSTEM, "actor_id": ACTOR_ID}
    response = client.post("/internal/evidence-classification/rules", json=payload)
    assert response.status_code == 422


def test_create_rule_semantic_conflict_is_409(client: TestClient):
    composition = get_composition()
    _register_email_evidence(composition, sender_address="billing@vendor.com", subject="Monthly Statement")
    payload = {**_RULE_BODY, "source": "OPERATOR", "actor_type": actor.SYSTEM, "actor_id": ACTOR_ID}
    first = client.post("/internal/evidence-classification/rules", json=payload)
    assert first.status_code == 201

    conflicting = {**payload, "document_type": "RECEIPT"}
    response = client.post("/internal/evidence-classification/rules", json=conflicting)
    assert response.status_code == 409


def test_list_and_detail(client: TestClient):
    composition = get_composition()
    _register_email_evidence(composition, sender_address="billing@vendor.com", subject="Monthly Statement")
    payload = {**_RULE_BODY, "source": "OPERATOR", "actor_type": actor.SYSTEM, "actor_id": ACTOR_ID}
    created = client.post("/internal/evidence-classification/rules", json=payload).json()
    rule_id = created["evidence_classification_rule"]["rule_id"]

    listing = client.get("/internal/evidence-classification/rules")
    assert listing.status_code == 200
    assert listing.json()["count"] == 1

    detail = client.get(f"/internal/evidence-classification/rules/{rule_id}")
    assert detail.status_code == 200
    assert detail.json()["rule_id"] == rule_id


def test_detail_unknown_rule_id_is_404(client: TestClient):
    response = client.get(f"/internal/evidence-classification/rules/{identity.generate_id()}")
    assert response.status_code == 404


def test_detail_malformed_rule_id_is_404(client: TestClient):
    response = client.get("/internal/evidence-classification/rules/not-a-real-id")
    assert response.status_code == 404


def test_list_pagination_bounds(client: TestClient):
    too_big = client.get("/internal/evidence-classification/rules", params={"limit": 99999})
    assert too_big.status_code == 422
    negative_offset = client.get("/internal/evidence-classification/rules", params={"offset": -1})
    assert negative_offset.status_code == 422


def test_retire_then_replay(client: TestClient):
    composition = get_composition()
    _register_email_evidence(composition, sender_address="billing@vendor.com", subject="Monthly Statement")
    payload = {**_RULE_BODY, "source": "OPERATOR", "actor_type": actor.SYSTEM, "actor_id": ACTOR_ID}
    created = client.post("/internal/evidence-classification/rules", json=payload).json()
    rule_id = created["evidence_classification_rule"]["rule_id"]

    retire_payload = {"actor_type": actor.SYSTEM, "actor_id": ACTOR_ID}
    first = client.post(f"/internal/evidence-classification/rules/{rule_id}/retire", json=retire_payload)
    assert first.status_code == 200
    assert first.json()["was_retired_now"] is True

    second = client.post(f"/internal/evidence-classification/rules/{rule_id}/retire", json=retire_payload)
    assert second.status_code == 200
    assert second.json()["was_retired_now"] is False


def test_retire_unknown_rule_id_is_404(client: TestClient):
    response = client.post(
        f"/internal/evidence-classification/rules/{identity.generate_id()}/retire",
        json={"actor_type": actor.SYSTEM, "actor_id": ACTOR_ID},
    )
    assert response.status_code == 404


def test_deterministic_classify_full_outcome_matrix(client: TestClient):
    composition = get_composition()
    ev = _register_email_evidence(composition, sender_address="billing@vendor.com", subject="Monthly Statement")

    payload = {**_RULE_BODY, "source": "OPERATOR", "actor_type": actor.SYSTEM, "actor_id": ACTOR_ID}
    client.post("/internal/evidence-classification/rules", json=payload)

    classify_payload = {"actor_type": actor.SYSTEM, "actor_id": ACTOR_ID}

    classified = client.post(f"/internal/evidence/{ev}/classifications/deterministic", json=classify_payload)
    assert classified.status_code == 201
    assert classified.json()["outcome"] == "CLASSIFIED"

    existing = client.post(f"/internal/evidence/{ev}/classifications/deterministic", json=classify_payload)
    assert existing.status_code == 200
    assert existing.json()["outcome"] == "EXISTING"


def test_deterministic_classify_no_match(client: TestClient):
    composition = get_composition()
    ev = _register_email_evidence(composition, sender_address="billing@other.com", subject="Unrelated")
    response = client.post(
        f"/internal/evidence/{ev}/classifications/deterministic", json={"actor_type": actor.SYSTEM, "actor_id": ACTOR_ID}
    )
    assert response.status_code == 200
    assert response.json()["outcome"] == "NO_MATCH"


def test_deterministic_classify_no_applicable_rule_input_for_manual_upload(client: TestClient):
    composition = get_composition()
    source = composition.api.register_source(
        source_type="MANUAL_UPLOAD_TEST", provider="test", status="ACTIVE", actor_type=actor.SYSTEM, actor_id=ACTOR_ID
    )
    now = datetime.now(timezone.utc)
    evidence = composition.api.register_evidence(
        entity_id=None, evidence_type="INVOICE", source_id=source.source_id, observed_at=now, received_at=now,
        content_hash=identity.generate_id().replace("-", "") * 2, mime_type="application/pdf", size_bytes=10,
        actor_type=actor.SYSTEM, actor_id=ACTOR_ID, original_name="invoice.pdf",
    )
    response = client.post(
        f"/internal/evidence/{evidence.evidence_id}/classifications/deterministic",
        json={"actor_type": actor.SYSTEM, "actor_id": ACTOR_ID},
    )
    assert response.status_code == 200
    assert response.json()["outcome"] == "NO_APPLICABLE_RULE_INPUT"


def test_deterministic_classify_current_classification_exists_is_409(client: TestClient):
    composition = get_composition()
    ev = _register_email_evidence(composition, sender_address="billing@vendor.com", subject="Monthly Statement")
    composition.classification_repository.create_classification(
        evidence_id=ev, classification_type="DOCUMENT_TYPE", document_type="RECEIPT", status="CLASSIFIED",
        source="OPERATOR_ASSIGNED", operator_action_id="op-1",
    )
    payload = {**_RULE_BODY, "source": "OPERATOR", "actor_type": actor.SYSTEM, "actor_id": ACTOR_ID}
    client.post("/internal/evidence-classification/rules", json=payload)

    response = client.post(
        f"/internal/evidence/{ev}/classifications/deterministic", json={"actor_type": actor.SYSTEM, "actor_id": ACTOR_ID}
    )
    assert response.status_code == 409


def test_deterministic_classify_unknown_evidence_id_is_404(client: TestClient):
    response = client.post(
        f"/internal/evidence/{identity.generate_id()}/classifications/deterministic",
        json={"actor_type": actor.SYSTEM, "actor_id": ACTOR_ID},
    )
    assert response.status_code == 404


def test_deterministic_classify_malformed_evidence_id_is_404(client: TestClient):
    response = client.post(
        "/internal/evidence/not-a-real-id/classifications/deterministic",
        json={"actor_type": actor.SYSTEM, "actor_id": ACTOR_ID},
    )
    assert response.status_code == 404
