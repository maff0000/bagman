"""Tests for `agent.bagman.orchestrator.handle_operator_message` — the
Ask BAGMAN tool-calling loop, AIInvocation lifecycle, audit emission,
and failure handling (CD-5 PID §17/§35/§53/§56-58, WI-3).
"""
from __future__ import annotations

import datetime

import pytest

from agent.bagman.orchestrator import DEFAULT_MAX_TOOL_ITERATIONS, handle_operator_message
from agent.tools.background import DeterministicFakeBackgroundTaskRunner
from agent.tools.handlers import ToolDependencies, build_default_tool_registry
from ai.invocation import InMemoryAIInvocationRepository
from ai.providers.claude.client import ClaudeAuthenticationError, ClaudeUnavailableError
from ai.providers.claude.fake import FakeClaudeClient, text_turn, tool_use_turn
from core import actor
from core.api import BagmanCanonicalAPI
from core.errors import ActiveInvocationConflictError, ValidationError
from services.evidence.intake.intake import InMemoryIntakeRepository


@pytest.fixture
def wired():
    api = BagmanCanonicalAPI()
    intake_repository = InMemoryIntakeRepository()
    ai_invocation_repository = InMemoryAIInvocationRepository()
    background_task_runner = DeterministicFakeBackgroundTaskRunner(ai_invocation_repository)
    tool_registry = build_default_tool_registry(
        ToolDependencies(
            api=api,
            intake_repository=intake_repository,
            ai_invocation_repository=ai_invocation_repository,
            background_task_runner=background_task_runner,
            runtime_health_check=lambda: {"runtime_environment": "test", "checks": {}},
        )
    )
    source = api.register_source(
        source_type="MANUAL_UPLOAD", provider="test", status="ACTIVE", actor_type=actor.SYSTEM, actor_id="t"
    )
    now = datetime.datetime.now(datetime.timezone.utc)
    evidence = api.register_evidence(
        entity_id=None,
        evidence_type="INVOICE",
        source_id=source.source_id,
        observed_at=now,
        received_at=now,
        content_hash={"algorithm": "sha256", "value": "d" * 64},
        mime_type="application/pdf",
        size_bytes=123,
        actor_type=actor.SYSTEM,
        actor_id="t",
    )
    return {
        "api": api,
        "ai_invocation_repository": ai_invocation_repository,
        "tool_registry": tool_registry,
        "evidence": evidence,
    }


def _call(wired, claude_client, **overrides):
    kwargs = dict(
        message="What is this?",
        actor_type=actor.USER,
        actor_id="matt",
        evidence_id=wired["evidence"].evidence_id,
        repository=wired["ai_invocation_repository"],
        claude_client=claude_client,
        tool_registry=wired["tool_registry"],
        record_audit_event=wired["api"].record_audit_event,
    )
    kwargs.update(overrides)
    return handle_operator_message(**kwargs)


# ---------------------------------------------------------------------
# happy path, no tools
# ---------------------------------------------------------------------


def test_plain_text_answer_succeeds_with_no_tool_calls(wired):
    claude_client = FakeClaudeClient(scripted_responses=[text_turn("It is an invoice.")])
    result = _call(wired, claude_client)

    assert result.invocation.status == "SUCCEEDED"
    assert result.invocation.role == "OPERATOR"
    assert result.invocation.provider == "ANTHROPIC"
    assert result.invocation.capability_alias is None
    assert result.invocation.task_id == "ASK_BAGMAN"
    assert result.response_text == "It is an invoice."
    assert result.tool_call_records == ()
    assert result.invocation.validation_result == {"valid": True, "errors": []}
    assert result.invocation.output["response_text"] == "It is an invoice."


def test_evidence_id_is_always_in_referenced_evidence_ids_even_with_no_tool_calls(wired):
    claude_client = FakeClaudeClient(scripted_responses=[text_turn("hi")])
    result = _call(wired, claude_client)
    assert wired["evidence"].evidence_id in result.referenced_evidence_ids


# ---------------------------------------------------------------------
# tool-calling loop
# ---------------------------------------------------------------------


def test_single_tool_call_then_final_answer(wired):
    claude_client = FakeClaudeClient(
        scripted_responses=[
            tool_use_turn(("c1", "get_document", {"evidence_id": wired["evidence"].evidence_id})),
            text_turn("It is an INVOICE."),
        ]
    )
    result = _call(wired, claude_client)
    assert result.invocation.status == "SUCCEEDED"
    assert len(result.tool_call_records) == 1
    assert result.tool_call_records[0]["tool"] == "get_document"
    assert not result.tool_call_records[0]["summary"].startswith("error:")


def test_multiple_sequential_tool_calls_across_turns(wired):
    claude_client = FakeClaudeClient(
        scripted_responses=[
            tool_use_turn(("c1", "get_document", {"evidence_id": wired["evidence"].evidence_id})),
            tool_use_turn(("c2", "get_runtime_status", {})),
            text_turn("Done."),
        ]
    )
    result = _call(wired, claude_client)
    assert result.invocation.status == "SUCCEEDED"
    assert [r["tool"] for r in result.tool_call_records] == ["get_document", "get_runtime_status"]


def test_multiple_tool_calls_in_one_turn(wired):
    claude_client = FakeClaudeClient(
        scripted_responses=[
            tool_use_turn(
                ("c1", "get_document", {"evidence_id": wired["evidence"].evidence_id}),
                ("c2", "get_runtime_status", {}),
            ),
            text_turn("Done."),
        ]
    )
    result = _call(wired, claude_client)
    assert result.invocation.status == "SUCCEEDED"
    assert {r["tool"] for r in result.tool_call_records} == {"get_document", "get_runtime_status"}


def test_run_background_analysis_tool_result_flows_through_to_final_answer(wired):
    claude_client = FakeClaudeClient(
        scripted_responses=[
            tool_use_turn(
                (
                    "c1",
                    "run_background_analysis",
                    {"task_id": "DOCUMENT_TYPE_PROPOSAL", "input_references": {"evidence_id": wired["evidence"].evidence_id}},
                )
            ),
            text_turn("Background analysis proposes UNKNOWN (stub)."),
        ]
    )
    result = _call(wired, claude_client)
    assert result.invocation.status == "SUCCEEDED"
    assert result.tool_call_records[0]["tool"] == "run_background_analysis"
    # a genuine second AIInvocation (the BACKGROUND one) now exists
    background_invocations = wired["ai_invocation_repository"].list_invocations(role="BACKGROUND")
    assert len(background_invocations) == 1
    assert background_invocations[0].status == "SUCCEEDED"


def test_an_unregistered_tool_call_is_reported_as_an_error_but_conversation_continues(wired):
    claude_client = FakeClaudeClient(
        scripted_responses=[
            tool_use_turn(("c1", "some_made_up_tool", {})),
            text_turn("I could not do that."),
        ]
    )
    result = _call(wired, claude_client)
    assert result.invocation.status == "SUCCEEDED"
    assert result.tool_call_records[0]["summary"].startswith("error:")


# ---------------------------------------------------------------------
# bounded tool-call loop (PID §35/§75)
# ---------------------------------------------------------------------


def test_max_tool_iterations_is_enforced_and_fails_closed(wired):
    scripted = [
        tool_use_turn(("c", "get_runtime_status", {})) for _ in range(DEFAULT_MAX_TOOL_ITERATIONS + 2)
    ]
    claude_client = FakeClaudeClient(scripted_responses=scripted)
    result = _call(wired, claude_client, max_tool_iterations=3)

    assert result.invocation.status == "FAILED"
    assert result.invocation.error_code == "MAX_TOOL_ITERATIONS_EXCEEDED"
    assert result.invocation.output is None
    # never looped beyond the bound
    assert len(claude_client.calls) == 3


# ---------------------------------------------------------------------
# Claude provider failure (PID §84)
# ---------------------------------------------------------------------


def test_claude_authentication_failure_marks_invocation_failed_clearly(wired):
    claude_client = FakeClaudeClient(scripted_responses=[ClaudeAuthenticationError("bad key")])
    result = _call(wired, claude_client)
    assert result.invocation.status == "FAILED"
    assert result.invocation.error_code == "CLAUDE_AUTHENTICATION_FAILED"
    assert result.response_text is None


def test_claude_unavailable_marks_invocation_failed_clearly(wired):
    claude_client = FakeClaudeClient(scripted_responses=[ClaudeUnavailableError("down")])
    result = _call(wired, claude_client)
    assert result.invocation.status == "FAILED"
    assert result.invocation.error_code == "CLAUDE_UNAVAILABLE"


def test_claude_failure_never_mutates_canonical_evidence(wired):
    before = wired["api"].get_evidence(wired["evidence"].evidence_id)
    claude_client = FakeClaudeClient(scripted_responses=[ClaudeAuthenticationError("bad key")])
    _call(wired, claude_client)
    after = wired["api"].get_evidence(wired["evidence"].evidence_id)
    assert before == after


# ---------------------------------------------------------------------
# the "general chat has no evidence_id" tension (module docstring)
# ---------------------------------------------------------------------


def test_a_message_with_no_canonical_subject_reference_is_rejected(wired):
    claude_client = FakeClaudeClient(scripted_responses=[text_turn("hi")])
    with pytest.raises(ValidationError):
        handle_operator_message(
            message="What needs my attention?",
            actor_type=actor.USER,
            actor_id="matt",
            repository=wired["ai_invocation_repository"],
            claude_client=claude_client,
            tool_registry=wired["tool_registry"],
            record_audit_event=wired["api"].record_audit_event,
        )


def test_an_intake_id_alone_is_an_acceptable_subject_reference(wired):
    intake = wired["api"]  # not used; just documents intake_id path also works
    claude_client = FakeClaudeClient(scripted_responses=[text_turn("about this intake")])
    result = handle_operator_message(
        message="Why was this quarantined?",
        actor_type=actor.USER,
        actor_id="matt",
        intake_id="01a09c85-98a9-73b0-abbb-2209bd5cd0d5",
        repository=wired["ai_invocation_repository"],
        claude_client=claude_client,
        tool_registry=wired["tool_registry"],
        record_audit_event=wired["api"].record_audit_event,
    )
    assert result.invocation.status == "SUCCEEDED"


def test_empty_message_is_rejected(wired):
    claude_client = FakeClaudeClient(scripted_responses=[text_turn("hi")])
    with pytest.raises(ValidationError):
        _call(wired, claude_client, message="   ")


# ---------------------------------------------------------------------
# concurrency guard (PID §73) — reused from WI-1, exercised here
# ---------------------------------------------------------------------


def test_a_second_concurrent_ask_bagman_call_about_the_same_subject_conflicts(wired, monkeypatch):
    """`create_invocation` (WI-1) enforces one-active-invocation-per-
    subject; a caller that tries to start a second ASK_BAGMAN
    conversation about the same evidence_id while the first is still
    non-terminal gets a clear conflict, never a silently-duplicated
    invocation."""
    from ai.invocation import InMemoryAIInvocationRepository as Repo

    repo = wired["ai_invocation_repository"]
    repo.create_invocation(
        task_id="ASK_BAGMAN",
        task_version=1,
        role="OPERATOR",
        provider="ANTHROPIC",
        capability_alias=None,
        input_references={"message": "first", "evidence_id": wired["evidence"].evidence_id, "intake_id": None, "entity_id": None},
        actor_type=actor.USER,
        actor_id="matt",
    )
    claude_client = FakeClaudeClient(scripted_responses=[text_turn("hi")])
    with pytest.raises(ActiveInvocationConflictError):
        _call(wired, claude_client)


# ---------------------------------------------------------------------
# audit emission (PID §57-58)
# ---------------------------------------------------------------------


def test_audit_events_are_emitted_for_a_successful_conversation(wired):
    claude_client = FakeClaudeClient(scripted_responses=[text_turn("ok")])
    result = _call(wired, claude_client)

    events = wired["api"].audit_repository.list_by_subject("AIInvocation", result.invocation.ai_invocation_id)
    event_types = [e.event_type for e in events]
    assert event_types == ["AI_INVOCATION_REQUESTED", "AI_INVOCATION_SUCCEEDED"]
    # correlation/causation chain (PID §58)
    assert events[0].correlation_id == result.invocation.correlation_id
    assert events[1].causation_id == events[0].audit_event_id


def test_audit_events_are_emitted_for_a_claude_failure(wired):
    claude_client = FakeClaudeClient(scripted_responses=[ClaudeAuthenticationError("bad key")])
    result = _call(wired, claude_client)
    events = wired["api"].audit_repository.list_by_subject("AIInvocation", result.invocation.ai_invocation_id)
    event_types = [e.event_type for e in events]
    assert event_types == ["AI_INVOCATION_REQUESTED", "AI_INVOCATION_FAILED"]


def test_audit_events_for_max_iterations_failure(wired):
    scripted = [tool_use_turn(("c", "get_runtime_status", {})) for _ in range(5)]
    claude_client = FakeClaudeClient(scripted_responses=scripted)
    result = _call(wired, claude_client, max_tool_iterations=2)
    events = wired["api"].audit_repository.list_by_subject("AIInvocation", result.invocation.ai_invocation_id)
    event_types = [e.event_type for e in events]
    assert event_types == ["AI_INVOCATION_REQUESTED", "AI_INVOCATION_FAILED"]


# ---------------------------------------------------------------------
# no hidden chain-of-thought persistence (PID §25)
# ---------------------------------------------------------------------


def test_persisted_output_never_contains_a_raw_chain_of_thought_field(wired):
    claude_client = FakeClaudeClient(
        scripted_responses=[
            tool_use_turn(("c1", "get_document", {"evidence_id": wired["evidence"].evidence_id})),
            text_turn("It is an invoice."),
        ]
    )
    result = _call(wired, claude_client)
    output = result.invocation.output
    assert output is not None
    forbidden_keys = {"chain_of_thought", "reasoning", "scratchpad", "raw_chain_of_thought", "thinking"}
    assert forbidden_keys.isdisjoint(output.keys())
    for tool_call in output["tool_calls"]:
        assert set(tool_call.keys()) == {"tool", "input", "summary"}
