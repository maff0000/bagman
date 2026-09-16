"""Tests for `ai.gateway.background.run_background_task` (CD-5 WI-2,
PID §57-58/§73-76). No real network I/O, no real database —
`ai.invocation.InMemoryAIInvocationRepository`,
`ai.providers.litellm.fake.FakeLiteLLMClient`, and a bare
`core.api.BagmanCanonicalAPI()`'s own `record_audit_event` (PID §61).
"""
from __future__ import annotations

import json

import pytest

from ai.gateway.background import run_background_task
from ai.invocation import InMemoryAIInvocationRepository
from ai.providers.litellm.client import LiteLLMOutcomeStatus
from ai.providers.litellm.fake import FakeLiteLLMClient
from ai.tasks import get_task_contract
from core.api import BagmanCanonicalAPI
from core.errors import ActiveInvocationConflictError, ValidationError

ACTOR_TYPE = "SYSTEM"
ACTOR_ID = "bagman-gateway-tests"


@pytest.fixture
def repository() -> InMemoryAIInvocationRepository:
    return InMemoryAIInvocationRepository()


@pytest.fixture
def litellm() -> FakeLiteLLMClient:
    return FakeLiteLLMClient()


@pytest.fixture
def api() -> BagmanCanonicalAPI:
    return BagmanCanonicalAPI()


def _run(*, api, repository, litellm, task_id="DOCUMENT_TYPE_PROPOSAL", task_version=1, evidence_id="ev-1", evidence_content="synthetic content"):
    return run_background_task(
        task_id=task_id,
        task_version=task_version,
        input_references={"evidence_id": evidence_id},
        evidence_content=evidence_content,
        actor_type=ACTOR_TYPE,
        actor_id=ACTOR_ID,
        correlation_id=None,
        repository=repository,
        litellm_client=litellm,
        record_audit_event=api.record_audit_event,
    )


# ---------------------------------------------------------------------
# outcome 1: success
# ---------------------------------------------------------------------


def test_success_populates_every_expected_field_and_emits_two_audit_events(api, repository, litellm):
    litellm.queue_success(
        capability_alias="bagman-fast",
        content=json.dumps({"proposed_type": "INVOICE", "confidence": 0.87, "signals": ["has line items"], "warnings": []}),
        provider_model="fake-mac-mini-model-v1",
        usage_metadata={"input_tokens": 42, "output_tokens": 7},
        latency_ms=123,
    )

    invocation = _run(api=api, repository=repository, litellm=litellm)

    assert invocation.status == "SUCCEEDED"
    assert invocation.role == "BACKGROUND"
    assert invocation.provider == "LITELLM"
    assert invocation.capability_alias == "bagman-fast"
    assert invocation.output == {
        "proposed_type": "INVOICE",
        "confidence": 0.87,
        "signals": ["has line items"],
        "warnings": [],
    }
    assert invocation.confidence == 0.87
    assert invocation.validation_result == {"valid": True, "errors": []}
    assert invocation.provider_model == "fake-mac-mini-model-v1"
    assert invocation.usage_metadata == {"input_tokens": 42, "output_tokens": 7}
    assert invocation.latency_ms == 123
    assert invocation.prompt_contract_version == "v1"
    assert invocation.error_code is None

    events = api.audit_repository.list_by_subject("AIInvocation", invocation.ai_invocation_id)
    event_types = [e.event_type for e in events]
    assert event_types == ["AI_INVOCATION_REQUESTED", "AI_INVOCATION_SUCCEEDED"]
    # causation chain: SUCCEEDED caused by REQUESTED
    assert events[1].causation_id == events[0].audit_event_id
    assert events[0].correlation_id == events[1].correlation_id == invocation.correlation_id


def test_success_sends_task_preferred_capability_not_a_caller_supplied_value(api, repository, litellm):
    litellm.queue_success(
        capability_alias="bagman-core",
        content=json.dumps({"proposed_entity_hint": "NOUSTAI_LIMITED", "confidence": 0.5, "signals": [], "warnings": []}),
    )
    invocation = _run(api=api, repository=repository, litellm=litellm, task_id="ENTITY_PROPOSAL")
    assert invocation.capability_alias == "bagman-core"  # ENTITY_PROPOSAL_V1.preferred_capability
    assert litellm.calls[0].capability_alias == "bagman-core"


# ---------------------------------------------------------------------
# structured-output request (CD-5 Gate-1 closure delta, 2026-09-16) —
# the exact task_contract.output_schema must reach the provider call,
# and different tasks sharing the same alias must each get THEIR OWN
# schema, never a shared/generic one.
# ---------------------------------------------------------------------


def test_output_schema_sent_is_exactly_the_resolved_task_contracts_schema(api, repository, litellm):
    litellm.queue_success(
        capability_alias="bagman-fast",
        content=json.dumps({"proposed_type": "INVOICE", "confidence": 0.9, "signals": [], "warnings": []}),
    )
    _run(api=api, repository=repository, litellm=litellm, task_id="DOCUMENT_TYPE_PROPOSAL")
    expected = get_task_contract("DOCUMENT_TYPE_PROPOSAL", 1).output_schema
    assert litellm.calls[0].output_schema == expected


def test_document_type_proposal_and_document_summary_share_bagman_fast_but_get_different_schemas(api, repository, litellm):
    """PID §21-22: the alias is a routing decision only — the schema is
    an entirely separate, per-task decision. Two tasks preferring the
    SAME capability alias must still each receive THEIR OWN
    output_schema, never a shared/generic/most-recently-used one."""
    litellm.queue_success(
        capability_alias="bagman-fast",
        content=json.dumps({"proposed_type": "INVOICE", "confidence": 0.9, "signals": [], "warnings": []}),
    )
    _run(api=api, repository=repository, litellm=litellm, task_id="DOCUMENT_TYPE_PROPOSAL", evidence_id="ev-a")

    litellm.queue_success(
        capability_alias="bagman-fast",
        content=json.dumps({"summary": "An invoice.", "confidence": 0.9, "signals": [], "warnings": []}),
    )
    _run(api=api, repository=repository, litellm=litellm, task_id="DOCUMENT_SUMMARY", evidence_id="ev-b")

    assert len(litellm.calls) == 2
    assert litellm.calls[0].capability_alias == litellm.calls[1].capability_alias == "bagman-fast"
    type_proposal_schema = get_task_contract("DOCUMENT_TYPE_PROPOSAL", 1).output_schema
    summary_schema = get_task_contract("DOCUMENT_SUMMARY", 1).output_schema
    assert litellm.calls[0].output_schema == type_proposal_schema
    assert litellm.calls[1].output_schema == summary_schema
    assert litellm.calls[0].output_schema != litellm.calls[1].output_schema


def test_entity_proposal_gets_its_own_schema_distinct_from_document_tasks(api, repository, litellm):
    litellm.queue_success(
        capability_alias="bagman-core",
        content=json.dumps({"proposed_entity_hint": "NOUSTAI_LIMITED", "confidence": 0.5, "signals": [], "warnings": []}),
    )
    _run(api=api, repository=repository, litellm=litellm, task_id="ENTITY_PROPOSAL")
    entity_schema = get_task_contract("ENTITY_PROPOSAL", 1).output_schema
    type_proposal_schema = get_task_contract("DOCUMENT_TYPE_PROPOSAL", 1).output_schema
    assert litellm.calls[0].output_schema == entity_schema
    assert litellm.calls[0].output_schema != type_proposal_schema


def test_malformed_output_still_rejected_even_though_structured_output_was_requested(api, repository, litellm):
    """Provider-side structured-output support is never treated as
    sufficient proof of a conforming response (Matt's explicit ruling)
    — BAGMAN's own validate_task_output remains the canonical safety
    boundary regardless of what response_format was sent."""
    litellm.queue_success(capability_alias="bagman-fast", content=json.dumps({"nonsense": True}))
    invocation = _run(api=api, repository=repository, litellm=litellm, task_id="DOCUMENT_TYPE_PROPOSAL")
    assert invocation.status == "FAILED"
    assert invocation.error_code == "OUTPUT_SCHEMA_INVALID"
    # The request DID carry the real schema — the provider just didn't honour it.
    assert litellm.calls[0].output_schema == get_task_contract("DOCUMENT_TYPE_PROPOSAL", 1).output_schema


# ---------------------------------------------------------------------
# outcome 2: malformed output (not JSON, and valid-JSON-but-schema-invalid)
# ---------------------------------------------------------------------


def test_output_not_json_fails_with_distinct_error_code_and_three_audit_events(api, repository, litellm):
    litellm.queue_success(capability_alias="bagman-fast", content="not json at all { { {")

    invocation = _run(api=api, repository=repository, litellm=litellm)

    assert invocation.status == "FAILED"
    assert invocation.error_code == "OUTPUT_NOT_JSON"
    assert invocation.validation_result["valid"] is False
    assert invocation.validation_result["errors"]

    events = api.audit_repository.list_by_subject("AIInvocation", invocation.ai_invocation_id)
    assert [e.event_type for e in events] == [
        "AI_INVOCATION_REQUESTED",
        "AI_OUTPUT_REJECTED",
        "AI_INVOCATION_FAILED",
    ]
    assert events[1].causation_id == events[0].audit_event_id
    assert events[2].causation_id == events[1].audit_event_id


def test_output_valid_json_but_schema_invalid_fails_and_preserves_raw_output(api, repository, litellm):
    # Missing every required field ("proposed_type", "confidence", ...).
    litellm.queue_success(capability_alias="bagman-fast", content=json.dumps({"unexpected": "shape"}))

    invocation = _run(api=api, repository=repository, litellm=litellm)

    assert invocation.status == "FAILED"
    assert invocation.error_code == "OUTPUT_SCHEMA_INVALID"
    assert invocation.validation_result["valid"] is False
    assert invocation.output == {"unexpected": "shape"}  # preserved per PID §76, not discarded

    events = api.audit_repository.list_by_subject("AIInvocation", invocation.ai_invocation_id)
    assert [e.event_type for e in events] == [
        "AI_INVOCATION_REQUESTED",
        "AI_OUTPUT_REJECTED",
        "AI_INVOCATION_FAILED",
    ]


# ---------------------------------------------------------------------
# outcome 3: transport/timeout/auth/provider failure
# ---------------------------------------------------------------------


@pytest.mark.parametrize(
    "outcome_status",
    [
        LiteLLMOutcomeStatus.TRANSPORT_ERROR,
        LiteLLMOutcomeStatus.TIMEOUT,
        LiteLLMOutcomeStatus.AUTH_ERROR,
        LiteLLMOutcomeStatus.RATE_LIMITED,
        LiteLLMOutcomeStatus.PROVIDER_ERROR,
        LiteLLMOutcomeStatus.CONFIG_ERROR,
    ],
)
def test_provider_failure_fails_directly_with_litellm_prefixed_error_code(api, repository, litellm, outcome_status):
    litellm.queue_failure(capability_alias="bagman-fast", status=outcome_status, error_detail="synthetic failure")

    invocation = _run(api=api, repository=repository, litellm=litellm)

    assert invocation.status == "FAILED"
    assert invocation.error_code == f"LITELLM_{outcome_status.value}"
    assert invocation.output is None  # never fabricated

    events = api.audit_repository.list_by_subject("AIInvocation", invocation.ai_invocation_id)
    # Transport-level failure never goes through the AI_OUTPUT_REJECTED
    # path — it never produced any content to reject in the first place.
    assert [e.event_type for e in events] == ["AI_INVOCATION_REQUESTED", "AI_INVOCATION_FAILED"]


# ---------------------------------------------------------------------
# outcome 4: concurrency conflict
# ---------------------------------------------------------------------


def test_conflicting_concurrent_request_raises_and_creates_no_second_invocation(api, repository, litellm):
    existing = repository.create_invocation(
        task_id="DOCUMENT_TYPE_PROPOSAL",
        task_version=1,
        role="BACKGROUND",
        provider="LITELLM",
        capability_alias="bagman-fast",
        input_references={"evidence_id": "ev-shared"},
        actor_type=ACTOR_TYPE,
        actor_id=ACTOR_ID,
    )  # left in REQUESTED — non-terminal

    with pytest.raises(ActiveInvocationConflictError):
        _run(api=api, repository=repository, litellm=litellm, evidence_id="ev-shared")

    all_invocations = repository.list_invocations()
    assert [inv.ai_invocation_id for inv in all_invocations] == [existing.ai_invocation_id]
    # No audit events at all for the rejected attempt — nothing to attach one to.
    assert litellm.calls == []


def test_retry_after_terminal_state_is_allowed_and_creates_a_new_row(api, repository, litellm):
    litellm.queue_failure(capability_alias="bagman-fast", status=LiteLLMOutcomeStatus.TRANSPORT_ERROR)
    first = _run(api=api, repository=repository, litellm=litellm, evidence_id="ev-retry")
    assert first.status == "FAILED"

    litellm.queue_success(
        capability_alias="bagman-fast",
        content=json.dumps({"proposed_type": "INVOICE", "confidence": 0.9, "signals": [], "warnings": []}),
    )
    second = _run(api=api, repository=repository, litellm=litellm, evidence_id="ev-retry")
    assert second.status == "SUCCEEDED"
    assert second.ai_invocation_id != first.ai_invocation_id  # a genuinely new, distinct row (PID §74)


# ---------------------------------------------------------------------
# input validation / role enforcement
# ---------------------------------------------------------------------


def test_operator_role_task_id_is_rejected(api, repository, litellm):
    with pytest.raises(ValidationError):
        _run(api=api, repository=repository, litellm=litellm, task_id="OPERATOR_DOCUMENT_REVIEW")
    assert repository.list_invocations() == []


def test_input_references_missing_required_key_is_rejected(api, repository, litellm):
    with pytest.raises(ValidationError):
        run_background_task(
            task_id="DOCUMENT_TYPE_PROPOSAL",
            task_version=1,
            input_references={},  # no evidence_id at all
            evidence_content="content",
            actor_type=ACTOR_TYPE,
            actor_id=ACTOR_ID,
            correlation_id=None,
            repository=repository,
            litellm_client=litellm,
            record_audit_event=api.record_audit_event,
        )
    assert repository.list_invocations() == []
