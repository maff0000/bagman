"""``POST /internal/operator/chat`` HTTP-level tests (CD-5 PID §42-44/
§68/§97).

CD-5 Gate-2 closure (2026-09-16): this endpoint now calls
``agent.claude_code.orchestrator.handle_operator_message`` (the bounded
headless Claude Code runner), not the superseded direct-Anthropic
``agent.bagman.orchestrator`` — see ``app/api/routers/operator.py``'s
own module docstring for the full correction history. Tests below
script ``composition.claude_code_operator_runner``
(`FakeClaudeCodeOperatorRunner`), not `composition.claude_client`.

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


def test_chat_with_only_a_conversation_id_succeeds(dev_client):
    """CD-6 reliability delta (PID §98/§100) — the real, previously-live
    defect this delivery fixes: a genuinely contextless "hi bagman"
    message (no evidence/intake/entity_id) now succeeds via
    `conversation_id`, the architect's own ruling that "an
    operator-originated conversational message is itself a valid
    traceable input"."""
    from app.api.composition import get_composition

    composition = get_composition()
    composition.claude_code_operator_runner.queue_success(text="Hi Matt — how can I help?")

    resp = dev_client.post(
        "/internal/operator/chat",
        json={
            "message": "hi bagman",
            "actor_type": "USER",
            "actor_id": "matt",
            "conversation_id": "conv-http-1",
            "source": "ask_bagman_drawer",
        },
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "SUCCEEDED"
    assert body["response_text"] == "Hi Matt — how can I help?"
    assert body["input_references"]["conversation_id"] == "conv-http-1"
    assert body["input_references"]["source"] == "ask_bagman_drawer"


def test_two_turns_same_conversation_id_conflict_via_http(dev_client):
    from app.api.composition import get_composition

    composition = get_composition()
    from core import actor as actor_module

    composition.ai_invocation_repository.create_invocation(
        task_id="ASK_BAGMAN", task_version=1, role="OPERATOR", provider="ANTHROPIC",
        capability_alias=None,
        input_references={"message": "first", "conversation_id": "conv-http-2"},
        actor_type=actor_module.USER, actor_id="matt",
    )

    resp = dev_client.post(
        "/internal/operator/chat",
        json={
            "message": "second, concurrent",
            "actor_type": "USER",
            "actor_id": "matt",
            "conversation_id": "conv-http-2",
        },
    )
    assert resp.status_code == 409
    assert resp.json()["error_code"] == "ACTIVE_INVOCATION_CONFLICT"


def test_chat_with_an_empty_message_returns_422(dev_client):
    resp = dev_client.post(
        "/internal/operator/chat",
        json={"message": "", "actor_type": "USER", "actor_id": "matt"},
    )
    assert resp.status_code == 422


def test_chat_about_a_document_succeeds_against_the_fake_claude_code_runner(dev_client):
    composition, evidence = _register_evidence(dev_client)
    composition.claude_code_operator_runner.queue_success(text="This appears to be an invoice.")

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
    assert body["output"]["tool_calls"] == []  # PID §97 — no live tool-calling loop in this design


def test_chat_sends_bounded_governed_context_to_the_runner_not_a_live_tool_call(dev_client):
    """PID §97: BAGMAN's own application layer assembles context BEFORE
    the one bounded invocation — no live tool call happens. Proves the
    trusted evidence summary and the evidence's own content both
    reached the runner's `user_prompt`, and the fixed operator
    instructions reached `system_prompt`."""
    composition, evidence = _register_evidence(dev_client)
    composition.claude_code_operator_runner.queue_success(text="It is an invoice.")

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

    calls = composition.claude_code_operator_runner.calls
    assert len(calls) == 1
    assert evidence.evidence_id in calls[0].user_prompt  # governed context reached the prompt
    assert "What is this?" in calls[0].user_prompt  # the operator's own question, last
    assert "BAGMAN's operator intelligence" in calls[0].system_prompt
    assert "NO tools available" in calls[0].system_prompt


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


def test_claude_code_timeout_surfaces_as_a_timed_out_invocation_not_a_500(dev_client):
    """CD-6 reliability delta (PID §100.14/§100.16): a genuine runner
    timeout now reaches TIMED_OUT specifically, not FAILED."""
    composition, evidence = _register_evidence(dev_client)
    from agent.claude_code.runner import ClaudeCodeOutcomeStatus

    composition.claude_code_operator_runner.queue_failure(
        status=ClaudeCodeOutcomeStatus.TIMEOUT, error_detail="simulated timeout"
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
    assert resp.status_code == 200  # the HTTP call itself succeeded — it returns a TIMED_OUT AIInvocation
    body = resp.json()
    assert body["status"] == "TIMED_OUT"
    assert body["error_code"] == "CLAUDE_CODE_TIMEOUT"
    assert body["response_text"] is None


def test_claude_code_process_error_surfaces_as_a_failed_invocation_not_a_500(dev_client):
    composition, evidence = _register_evidence(dev_client)
    from agent.claude_code.runner import ClaudeCodeOutcomeStatus

    composition.claude_code_operator_runner.queue_failure(
        status=ClaudeCodeOutcomeStatus.PROCESS_ERROR, error_detail="simulated process failure"
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
    assert body["status"] == "FAILED"
    assert body["error_code"] == "CLAUDE_CODE_PROCESS_ERROR"
    assert body["response_text"] is None
