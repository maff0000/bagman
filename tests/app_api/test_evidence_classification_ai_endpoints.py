"""HTTP-level tests for CD-6 Slice 5 WI-3's governed AI-fallback
classification endpoints:

* ``POST /internal/evidence/{evidence_id}/classifications/ai-preview``
* ``POST /internal/evidence/{evidence_id}/classifications/orchestrated``

Runs against DEVELOPMENT/TEST composition only — in-memory repositories
+ `FakeLiteLLMClient` (PID §61: no live LLM). Mirrors
`tests/app_api/test_ai_endpoints.py`'s own lightweight `dev_client`
style and `tests/app_api/test_evidence_classification_endpoints.py`'s
own evidence-registration helper conventions.
"""
from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone

import pytest
from fastapi.testclient import TestClient

from app.api.composition import get_composition, reset_composition_for_tests
from app.api.main import app
from core import actor, identity

ACTOR_TYPE = actor.SYSTEM
ACTOR_ID = "bagman-evidence-classification-ai-endpoint-tests"


@pytest.fixture
def dev_client(monkeypatch):
    monkeypatch.setenv("BAGMAN_RUNTIME_ENV", "development")
    reset_composition_for_tests()
    with TestClient(app) as test_client:
        yield test_client
    reset_composition_for_tests()


def _rfc822_bytes(*, sender: str, subject: str, body: str) -> bytes:
    import email.message

    msg = email.message.EmailMessage()
    msg["From"] = sender
    msg["Subject"] = subject
    msg.set_content(body)
    return bytes(msg)


def _register_email_evidence(composition, *, sender_address=None, subject=None, content: bytes = None) -> str:
    content = content or _rfc822_bytes(
        sender=sender_address or "billing@vendor.com", subject=subject or "Invoice 42",
        body="Please pay $500 for services rendered.",
    )
    content_hash = {"algorithm": "SHA-256", "value": hashlib.sha256(content).hexdigest()}
    storage_reference = composition.object_store.put(identity.generate_id(), content_hash, content)
    source = composition.api.register_source(
        source_type="MAILBOX_TEST", provider="test", status="ACTIVE", actor_type=ACTOR_TYPE, actor_id=ACTOR_ID,
    )
    metadata = {}
    if sender_address is not None:
        metadata["sender_address"] = sender_address
    if subject is not None:
        metadata["subject"] = subject
    now = datetime.now(timezone.utc)
    evidence = composition.api.register_evidence(
        entity_id=None, evidence_type="EMAIL", source_id=source.source_id,
        observed_at=now, received_at=now, content_hash=content_hash, mime_type="message/rfc822",
        size_bytes=len(content), storage_reference=storage_reference, actor_type=ACTOR_TYPE, actor_id=ACTOR_ID,
        metadata=metadata,
    )
    return evidence.evidence_id


def _queue_proposal(composition, *, proposed_type: str, confidence: float = 0.85):
    composition.litellm_client.queue_success(
        capability_alias="bagman-core",
        content=json.dumps({"proposed_type": proposed_type, "confidence": confidence, "signals": [], "warnings": []}),
    )


# ---------------------------------------------------------------------
# ai-preview — creates no EvidenceClassification row
# ---------------------------------------------------------------------


def test_ai_preview_returns_proposal_and_creates_no_classification(dev_client):
    composition = get_composition()
    evidence_id = _register_email_evidence(composition)
    _queue_proposal(composition, proposed_type="SUPPLIER_INVOICE", confidence=0.9)

    response = dev_client.post(
        f"/internal/evidence/{evidence_id}/classifications/ai-preview",
        json={"actor_type": ACTOR_TYPE, "actor_id": ACTOR_ID},
    )
    assert response.status_code == 200
    body = response.json()
    assert body["outcome"] == "AI_PROPOSAL_REVIEW_REQUIRED"
    assert body["proposed_type"] == "SUPPLIER_INVOICE"
    assert "evidence_classification" not in body
    assert composition.classification_repository.get_current_classification(evidence_id, "DOCUMENT_TYPE") is None

    # A real AIInvocation was created and is independently inspectable.
    invocation = composition.ai_invocation_repository.get_invocation(body["ai_invocation_id"])
    assert invocation.status == "SUCCEEDED"
    assert invocation.task_id == "DOCUMENT_TYPE_PROPOSAL"
    assert invocation.task_version == 2
    assert invocation.capability_alias == "bagman-core"


def test_ai_preview_unknown_proposal(dev_client):
    composition = get_composition()
    evidence_id = _register_email_evidence(composition)
    _queue_proposal(composition, proposed_type="UNKNOWN", confidence=0.1)

    response = dev_client.post(
        f"/internal/evidence/{evidence_id}/classifications/ai-preview",
        json={"actor_type": ACTOR_TYPE, "actor_id": ACTOR_ID},
    )
    assert response.status_code == 200
    assert response.json()["outcome"] == "AI_PROPOSAL_UNCLASSIFIABLE"


# ---------------------------------------------------------------------
# orchestrated — persists AI proposals
# ---------------------------------------------------------------------


def test_orchestrated_persists_review_required_classification(dev_client):
    composition = get_composition()
    evidence_id = _register_email_evidence(composition)
    _queue_proposal(composition, proposed_type="SUPPLIER_INVOICE", confidence=0.88)

    response = dev_client.post(
        f"/internal/evidence/{evidence_id}/classifications/orchestrated",
        json={"actor_type": ACTOR_TYPE, "actor_id": ACTOR_ID},
    )
    assert response.status_code == 201
    body = response.json()
    assert body["outcome"] == "AI_PROPOSAL_REVIEW_REQUIRED"
    assert body["was_created"] is True
    classification = body["evidence_classification"]
    assert classification["status"] == "REVIEW_REQUIRED"
    assert classification["source"] == "AI_PROPOSAL"
    assert classification["document_type"] == "SUPPLIER_INVOICE"

    current = composition.classification_repository.get_current_classification(evidence_id, "DOCUMENT_TYPE")
    assert current is not None
    assert current.classification_id == classification["classification_id"]


def test_orchestrated_current_classification_exists_returns_409(dev_client):
    composition = get_composition()
    evidence_id = _register_email_evidence(composition)
    composition.classification_repository.create_classification(
        evidence_id=evidence_id, classification_type="DOCUMENT_TYPE", document_type="RECEIPT",
        status="CLASSIFIED", source="OPERATOR_ASSIGNED", operator_action_id="op-1",
        expected_current_classification_id=None,
    )
    response = dev_client.post(
        f"/internal/evidence/{evidence_id}/classifications/orchestrated",
        json={"actor_type": ACTOR_TYPE, "actor_id": ACTOR_ID},
    )
    assert response.status_code == 409


def test_orchestrated_deterministic_match_short_circuits(dev_client):
    composition = get_composition()
    composition.classification_rule_repository.create_rule(
        sender_scope_type="EXACT_SENDER_DOMAIN", sender_scope_value="vendor.com",
        subject_predicate_type="EXACT", subject_predicate_value="Invoice 42",
        document_type="SUPPLIER_INVOICE", source="OPERATOR",
    )
    evidence_id = _register_email_evidence(composition, sender_address="billing@vendor.com", subject="Invoice 42")

    response = dev_client.post(
        f"/internal/evidence/{evidence_id}/classifications/orchestrated",
        json={"actor_type": ACTOR_TYPE, "actor_id": ACTOR_ID},
    )
    assert response.status_code == 201
    body = response.json()
    assert body["outcome"] == "DETERMINISTIC_CLASSIFIED"
    assert body["evidence_classification"]["source"] == "DETERMINISTIC_RULE"
    assert composition.litellm_client.calls == []


def test_orchestrated_context_unsupported_returns_200(dev_client):
    composition = get_composition()
    content = b"%PDF-1.4 not really a pdf"
    content_hash = {"algorithm": "SHA-256", "value": hashlib.sha256(content).hexdigest()}
    storage_reference = composition.object_store.put(identity.generate_id(), content_hash, content)
    source = composition.api.register_source(
        source_type="MANUAL_UPLOAD", provider="test", status="ACTIVE", actor_type=ACTOR_TYPE, actor_id=ACTOR_ID,
    )
    now = datetime.now(timezone.utc)
    evidence = composition.api.register_evidence(
        entity_id=None, evidence_type="INVOICE", source_id=source.source_id,
        observed_at=now, received_at=now, content_hash=content_hash, mime_type="application/pdf",
        size_bytes=len(content), storage_reference=storage_reference, actor_type=ACTOR_TYPE, actor_id=ACTOR_ID,
    )
    response = dev_client.post(
        f"/internal/evidence/{evidence.evidence_id}/classifications/orchestrated",
        json={"actor_type": ACTOR_TYPE, "actor_id": ACTOR_ID},
    )
    assert response.status_code == 200
    body = response.json()
    assert body["outcome"] == "CONTEXT_UNSUPPORTED"
    assert composition.litellm_client.calls == []


def test_orchestrated_evidence_not_found_returns_404(dev_client):
    response = dev_client.post(
        "/internal/evidence/does-not-exist/classifications/orchestrated",
        json={"actor_type": ACTOR_TYPE, "actor_id": ACTOR_ID},
    )
    assert response.status_code == 404


def test_ai_preview_evidence_not_found_returns_404(dev_client):
    response = dev_client.post(
        "/internal/evidence/does-not-exist/classifications/ai-preview",
        json={"actor_type": ACTOR_TYPE, "actor_id": ACTOR_ID},
    )
    assert response.status_code == 404


# ---------------------------------------------------------------------
# Generic /internal/ai/tasks path rejects v2 (WI-3 §22) — HTTP-level
# re-confirmation alongside the static/request-level proof in
# tests/integration/test_architecture_boundaries.py.
# ---------------------------------------------------------------------


def test_generic_ai_tasks_endpoint_rejects_document_type_proposal_v2(dev_client):
    composition = get_composition()
    evidence_id = _register_email_evidence(composition)
    response = dev_client.post(
        "/internal/ai/tasks",
        json={
            "task_id": "DOCUMENT_TYPE_PROPOSAL", "task_version": 2,
            "input_references": {"evidence_id": evidence_id},
            "actor_type": ACTOR_TYPE, "actor_id": ACTOR_ID,
        },
    )
    assert response.status_code == 422
    assert composition.litellm_client.calls == []


def test_generic_ai_tasks_endpoint_still_accepts_document_type_proposal_v1(dev_client):
    composition = get_composition()
    evidence_id = _register_email_evidence(composition)
    composition.litellm_client.queue_success(
        capability_alias="bagman-fast",
        content=json.dumps({"proposed_type": "INVOICE", "confidence": 0.5, "signals": [], "warnings": []}),
    )
    response = dev_client.post(
        "/internal/ai/tasks",
        json={
            "task_id": "DOCUMENT_TYPE_PROPOSAL", "task_version": 1,
            "input_references": {"evidence_id": evidence_id},
            "actor_type": ACTOR_TYPE, "actor_id": ACTOR_ID,
        },
    )
    assert response.status_code == 200
    assert response.json()["task_version"] == 1
