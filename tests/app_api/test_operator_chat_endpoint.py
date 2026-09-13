"""``POST /internal/operator/chat`` HTTP-level tests (CD-5 PID §42-44/
§68, WI-3).

Deliberately independent of ``tests/app_api/conftest.py``'s
``runtime_stack``/``client`` fixtures (real disposable Postgres/MinIO/
ClamAV containers) — this endpoint's own logic needs none of that; it
runs against development/test (in-memory) composition, exactly like
``tests/app_api/test_static_ui.py``.
"""
from __future__ import annotations

import datetime

import pytest


@pytest.fixture
def dev_client(monkeypatch):
    monkeypatch.setenv("BAGMAN_RUNTIME_ENV", "test")

    from app.api.composition import reset_composition_for_tests

    reset_composition_for_tests()

    from fastapi.testclient import TestClient

    from app.api.main import app

    with TestClient(app) as test_client:
        yield test_client

    reset_composition_for_tests()


def _register_evidence(dev_client):
    from app.api.composition import get_composition
    from core import actor

    composition = get_composition()
    source = composition.api.register_source(
        source_type="MANUAL_UPLOAD",
        provider="test-operator-chat",
        status="ACTIVE",
        actor_type=actor.SYSTEM,
        actor_id="t",
    )
    now = datetime.datetime.now(datetime.timezone.utc)
    evidence = composition.api.register_evidence(
        entity_id=None,
        evidence_type="INVOICE",
        source_id=source.source_id,
        observed_at=now,
        received_at=now,
        content_hash={"algorithm": "sha256", "value": "e" * 64},
        mime_type="application/pdf",
        size_bytes=42,
        actor_type=actor.SYSTEM,
        actor_id="t",
    )
    return composition, evidence


def test_chat_without_a_canonical_subject_reference_returns_422(dev_client):
    resp = dev_client.post(
        "/internal/operator/chat",
        json={"message": "What needs my attention?", "actor_type": "USER", "actor_id": "matt"},
    )
    assert resp.status_code == 422
    assert resp.json()["error_code"] == "VALIDATION_ERROR"


def test_chat_with_an_empty_message_returns_422(dev_client):
    resp = dev_client.post(
        "/internal/operator/chat",
        json={"message": "", "actor_type": "USER", "actor_id": "matt"},
    )
    assert resp.status_code == 422


def test_chat_about_a_document_succeeds_against_the_fake_claude_client(dev_client):
    composition, evidence = _register_evidence(dev_client)
    from ai.providers.claude.fake import text_turn

    composition.claude_client._queue.append(text_turn("This appears to be an invoice."))

    resp = dev_client.post(
        "/internal/operator/chat",
        json={
            "message": "What is this document?",
            "actor_type": "USER",
            "actor_id": "matt",
            "evidence_id": evidence.evidence_id,
        },
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "SUCCEEDED"
    assert body["task_id"] == "ASK_BAGMAN"
    assert body["role"] == "OPERATOR"
    assert body["provider"] == "ANTHROPIC"
    assert body["capability_alias"] is None
    assert body["response_text"] == "This appears to be an invoice."
    assert evidence.evidence_id in body["referenced_evidence_ids"]
    assert body["schema_version"] == "bagman.ai_invocation.v1"


def test_chat_with_a_tool_call_returns_the_tool_call_record(dev_client):
    composition, evidence = _register_evidence(dev_client)
    from ai.providers.claude.fake import text_turn, tool_use_turn

    composition.claude_client._queue.extend(
        [
            tool_use_turn(("c1", "get_document", {"evidence_id": evidence.evidence_id})),
            text_turn("It is an invoice, confirmed via get_document."),
        ]
    )

    resp = dev_client.post(
        "/internal/operator/chat",
        json={
            "message": "What is this?",
            "actor_type": "USER",
            "actor_id": "matt",
            "evidence_id": evidence.evidence_id,
        },
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["output"]["tool_calls"][0]["tool"] == "get_document"


def test_a_duplicate_concurrent_chat_about_the_same_subject_returns_409(dev_client):
    composition, evidence = _register_evidence(dev_client)
    from core import actor as actor_module

    composition.ai_invocation_repository.create_invocation(
        task_id="ASK_BAGMAN",
        task_version=1,
        role="OPERATOR",
        provider="ANTHROPIC",
        capability_alias=None,
        input_references={
            "message": "first",
            "evidence_id": evidence.evidence_id,
            "intake_id": None,
            "entity_id": None,
        },
        actor_type=actor_module.USER,
        actor_id="matt",
    )

    resp = dev_client.post(
        "/internal/operator/chat",
        json={
            "message": "second, concurrent",
            "actor_type": "USER",
            "actor_id": "matt",
            "evidence_id": evidence.evidence_id,
        },
    )
    assert resp.status_code == 409
    assert resp.json()["error_code"] == "ACTIVE_INVOCATION_CONFLICT"


def test_claude_auth_failure_surfaces_as_a_failed_invocation_not_a_500(dev_client):
    composition, evidence = _register_evidence(dev_client)
    from ai.providers.claude.client import ClaudeAuthenticationError

    composition.claude_client._queue.append(ClaudeAuthenticationError("simulated"))

    resp = dev_client.post(
        "/internal/operator/chat",
        json={
            "message": "What is this?",
            "actor_type": "USER",
            "actor_id": "matt",
            "evidence_id": evidence.evidence_id,
        },
    )
    assert resp.status_code == 200  # the HTTP call itself succeeded — it returns a FAILED AIInvocation
    body = resp.json()
    assert body["status"] == "FAILED"
    assert body["error_code"] == "CLAUDE_AUTHENTICATION_FAILED"
    assert body["response_text"] is None
