"""HTTP-level tests for `/internal/ai/*` (CD-5 WI-2, PID §48/§68-71).

Runs against DEVELOPMENT/TEST composition only — in-memory
`AIInvocationRepository` + `FakeLiteLLMClient` (PID §61: no live LLM,
no real database needed for this WI's own ordinary tests). Unlike
`tests/app_api/test_intake_endpoint.py`/`test_malformed_id_mapping.py`
(which need a real disposable Postgres/MinIO/ClamAV stack via
`tests/app_api/conftest.py`'s `client` fixture), this module builds its
own lightweight development-mode `TestClient` — no Docker required.
"""
from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone

import pytest
from fastapi.testclient import TestClient

from ai.providers.litellm.client import LiteLLMOutcomeStatus
from app.api.composition import get_composition, reset_composition_for_tests
from app.api.main import app

ACTOR_TYPE = "SYSTEM"
ACTOR_ID = "bagman-ai-endpoint-tests"


@pytest.fixture
def dev_client(monkeypatch):
    monkeypatch.setenv("BAGMAN_RUNTIME_ENV", "development")
    reset_composition_for_tests()
    with TestClient(app) as test_client:
        yield test_client
    reset_composition_for_tests()


def _register_text_evidence(composition, content: bytes = b"Synthetic AI-endpoint test evidence.") -> str:
    """Register a real, minimal `EvidenceItem` with real stored bytes
    (via the in-memory object store), the same way
    `app/api/routers/intake.py` does after a real intake — returns its
    `evidence_id`."""
    from core import identity

    content_hash = {"algorithm": "SHA-256", "value": hashlib.sha256(content).hexdigest()}
    source = composition.api.register_source(
        source_type="MANUAL_UPLOAD",
        provider="bagman-ai-endpoint-tests",
        status="ACTIVE",
        actor_type=ACTOR_TYPE,
        actor_id=ACTOR_ID,
    )
    storage_reference = composition.object_store.put(identity.generate_id(), content_hash, content)
    evidence = composition.api.register_evidence(
        entity_id=None,
        evidence_type="INVOICE",
        source_id=source.source_id,
        observed_at=datetime.now(timezone.utc),
        received_at=datetime.now(timezone.utc),
        content_hash=content_hash,
        mime_type="text/plain",
        size_bytes=len(content),
        actor_type=ACTOR_TYPE,
        actor_id=ACTOR_ID,
        storage_reference=storage_reference,
        status="OBSERVED",
    )
    return evidence.evidence_id


def test_post_tasks_success_returns_succeeded_invocation(dev_client):
    composition = get_composition()
    evidence_id = _register_text_evidence(composition)
    composition.litellm_client.queue_success(
        capability_alias="bagman-fast",
        content=json.dumps({"proposed_type": "INVOICE", "confidence": 0.9, "signals": ["totals present"], "warnings": []}),
    )

    response = dev_client.post(
        "/internal/ai/tasks",
        json={
            "task_id": "DOCUMENT_TYPE_PROPOSAL",
            "task_version": 1,
            "input_references": {"evidence_id": evidence_id},
            "actor_type": ACTOR_TYPE,
            "actor_id": ACTOR_ID,
        },
    )

    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "SUCCEEDED"
    assert body["output"]["proposed_type"] == "INVOICE"
    assert body["capability_alias"] == "bagman-fast"
    assert body["role"] == "BACKGROUND"
    assert body["provider"] == "LITELLM"


def test_post_tasks_transport_failure_returns_200_with_failed_status(dev_client):
    """The HTTP REQUEST succeeded (BAGMAN accepted and processed it) —
    it is the underlying AI task that failed. That is a 200 with a
    FAILED invocation body, not an HTTP error status."""
    composition = get_composition()
    evidence_id = _register_text_evidence(composition)
    composition.litellm_client.queue_failure(
        capability_alias="bagman-fast", status=LiteLLMOutcomeStatus.TRANSPORT_ERROR
    )

    response = dev_client.post(
        "/internal/ai/tasks",
        json={
            "task_id": "DOCUMENT_TYPE_PROPOSAL",
            "task_version": 1,
            "input_references": {"evidence_id": evidence_id},
            "actor_type": ACTOR_TYPE,
            "actor_id": ACTOR_ID,
        },
    )
    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "FAILED"
    assert body["error_code"] == "LITELLM_TRANSPORT_ERROR"


def test_post_tasks_conflict_returns_409(dev_client):
    composition = get_composition()
    evidence_id = _register_text_evidence(composition)
    # Pre-seed a non-terminal invocation for the exact same subject,
    # directly through the repository (bypassing HTTP), so the HTTP
    # call below is guaranteed to race into PID §73's concurrency guard.
    composition.ai_invocation_repository.create_invocation(
        task_id="DOCUMENT_TYPE_PROPOSAL",
        task_version=1,
        role="BACKGROUND",
        provider="LITELLM",
        capability_alias="bagman-fast",
        input_references={"evidence_id": evidence_id},
        actor_type=ACTOR_TYPE,
        actor_id=ACTOR_ID,
    )

    response = dev_client.post(
        "/internal/ai/tasks",
        json={
            "task_id": "DOCUMENT_TYPE_PROPOSAL",
            "task_version": 1,
            "input_references": {"evidence_id": evidence_id},
            "actor_type": ACTOR_TYPE,
            "actor_id": ACTOR_ID,
        },
    )
    assert response.status_code == 409
    assert response.json()["error_code"] == "ACTIVE_INVOCATION_CONFLICT"


def test_post_tasks_unknown_task_returns_404(dev_client):
    # A real, registered evidence_id — so this genuinely exercises "task
    # unknown", not incidentally "evidence not found" (both would 404,
    # but for different, undistinguished reasons if evidence_id were
    # also bogus).
    composition = get_composition()
    evidence_id = _register_text_evidence(composition)
    response = dev_client.post(
        "/internal/ai/tasks",
        json={
            "task_id": "SOME_MADE_UP_TASK",
            "task_version": 1,
            "input_references": {"evidence_id": evidence_id},
            "actor_type": ACTOR_TYPE,
            "actor_id": ACTOR_ID,
        },
    )
    assert response.status_code == 404


def test_post_tasks_operator_role_task_returns_422(dev_client):
    composition = get_composition()
    evidence_id = _register_text_evidence(composition)
    response = dev_client.post(
        "/internal/ai/tasks",
        json={
            "task_id": "OPERATOR_DOCUMENT_REVIEW",
            "task_version": 1,
            "input_references": {"evidence_id": evidence_id, "operator_question": None},
            "actor_type": ACTOR_TYPE,
            "actor_id": ACTOR_ID,
        },
    )
    assert response.status_code == 422


def test_get_invocations_list_and_detail_round_trip(dev_client):
    composition = get_composition()
    evidence_id = _register_text_evidence(composition)
    composition.litellm_client.queue_success(
        capability_alias="bagman-fast",
        content=json.dumps({"proposed_type": "RECEIPT", "confidence": 0.6, "signals": [], "warnings": []}),
    )
    created = dev_client.post(
        "/internal/ai/tasks",
        json={
            "task_id": "DOCUMENT_TYPE_PROPOSAL",
            "task_version": 1,
            "input_references": {"evidence_id": evidence_id},
            "actor_type": ACTOR_TYPE,
            "actor_id": ACTOR_ID,
        },
    ).json()

    listed = dev_client.get("/internal/ai/invocations", params={"task_id": "DOCUMENT_TYPE_PROPOSAL"})
    assert listed.status_code == 200
    listed_body = listed.json()
    assert listed_body["count"] == 1
    assert listed_body["items"][0]["ai_invocation_id"] == created["ai_invocation_id"]

    detail = dev_client.get(f"/internal/ai/invocations/{created['ai_invocation_id']}")
    assert detail.status_code == 200
    assert detail.json() == created

    missing = dev_client.get("/internal/ai/invocations/00000000-0000-7000-8000-000000000000")
    assert missing.status_code == 404


def test_ai_health_reports_gateway_wide_checks_for_every_alias(dev_client):
    composition = get_composition()
    composition.litellm_client.set_available(True)
    composition.claude_code_operator_runner.set_available(True)
    response = dev_client.get("/internal/ai/health")
    assert response.status_code == 200
    body = response.json()
    # CD-6 §103 Inference Architecture Ruling: `bagman-deep` is retired
    # and `trinity-core` is deliberately NOT in this live-checked tier
    # at all (backlog/overflow only — see `trinity_core_overflow` key).
    assert body["checks"] == {
        "bagman_fast": "ok",
        "bagman_core": "ok",
        "claude_code": "ok",
    }
    assert "gateway-wide" in body["granularity"]
    assert "backlog" in body["trinity_core_overflow"]

    composition.litellm_client.set_available(False)
    response = dev_client.get("/internal/ai/health")
    assert response.json()["checks"] == {
        "bagman_fast": "unreachable",
        "bagman_core": "unreachable",
        "claude_code": "ok",
    }


def test_ai_health_claude_code_key_is_independent_of_the_litellm_gateway(dev_client):
    """`claude_code` must be a genuinely separate signal, not folded
    into the gateway-wide bagman-* caveat (PID §46-48)."""
    composition = get_composition()
    composition.litellm_client.set_available(True)
    composition.claude_code_operator_runner.set_available(False)
    response = dev_client.get("/internal/ai/health")
    body = response.json()
    assert body["checks"]["claude_code"] == "unreachable"
    assert body["checks"]["bagman_fast"] == "ok"


def test_list_invocations_filters_by_primary_input_reference_query_param(dev_client):
    composition = get_composition()
    evidence_id = _register_text_evidence(composition)
    other_evidence_id = _register_text_evidence(composition, content=b"other doc")

    composition.litellm_client.queue_success(
        capability_alias="bagman-fast",
        content=json.dumps({"proposed_type": "INVOICE", "confidence": 0.9, "signals": [], "warnings": []}),
    )
    created = dev_client.post(
        "/internal/ai/tasks",
        json={
            "task_id": "DOCUMENT_TYPE_PROPOSAL",
            "task_version": 1,
            "input_references": {"evidence_id": evidence_id},
            "actor_type": ACTOR_TYPE,
            "actor_id": ACTOR_ID,
        },
    ).json()

    composition.litellm_client.queue_success(
        capability_alias="bagman-fast",
        content=json.dumps({"proposed_type": "RECEIPT", "confidence": 0.5, "signals": [], "warnings": []}),
    )
    dev_client.post(
        "/internal/ai/tasks",
        json={
            "task_id": "DOCUMENT_TYPE_PROPOSAL",
            "task_version": 1,
            "input_references": {"evidence_id": other_evidence_id},
            "actor_type": ACTOR_TYPE,
            "actor_id": ACTOR_ID,
        },
    )

    listed = dev_client.get("/internal/ai/invocations", params={"primary_input_reference": evidence_id})
    assert listed.status_code == 200
    body = listed.json()
    assert body["count"] == 1
    assert body["items"][0]["ai_invocation_id"] == created["ai_invocation_id"]


def test_request_body_has_no_model_or_alias_override_field(dev_client):
    """PID §77 — no prompt/caller-controlled provider/model/alias name.
    An extra unexpected field is silently ignored by pydantic (not
    rejected), but proves the field is inert: the invocation's own
    capability_alias still comes from the task's preferred_capability,
    never from anything the caller supplied."""
    composition = get_composition()
    evidence_id = _register_text_evidence(composition)
    composition.litellm_client.queue_success(
        capability_alias="bagman-fast",
        content=json.dumps({"proposed_type": "INVOICE", "confidence": 0.9, "signals": [], "warnings": []}),
    )
    response = dev_client.post(
        "/internal/ai/tasks",
        json={
            "task_id": "DOCUMENT_TYPE_PROPOSAL",
            "task_version": 1,
            "input_references": {"evidence_id": evidence_id},
            "actor_type": ACTOR_TYPE,
            "actor_id": ACTOR_ID,
            "model": "gemma-3-12b",  # an attempted override — must be ignored entirely
            "capability_alias": "trinity-deep",  # ditto
        },
    )
    assert response.status_code == 200
    assert response.json()["capability_alias"] == "bagman-fast"
