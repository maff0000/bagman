"""Domain-behaviour tests for `ai.invocation` (CD-5 WI-1, PID §26-30/§73-76).

Exercises `InMemoryAIInvocationRepository` and the pure `transition()`
state-machine helper directly — mirrors
`tests/integration/test_intake_domain.py`'s own structure for
`IntakeRecord`, adapted for `AIInvocation`'s different state machine and
its one-active-invocation-per-subject concurrency doctrine (a
DIFFERENT doctrine from intake's idempotency-key replay — see
`ai.invocation`'s module docstring for the contrast).
"""
from __future__ import annotations

import pytest

from core import identity
from core.errors import (
    ActiveInvocationConflictError,
    InvalidStateTransitionError,
    ValidationError,
)
from ai.invocation import (
    ALLOWED_TRANSITIONS,
    STATUSES,
    TERMINAL_STATUSES,
    InMemoryAIInvocationRepository,
    derive_primary_input_reference,
    transition,
)

TERMINAL = TERMINAL_STATUSES


@pytest.fixture
def repo() -> InMemoryAIInvocationRepository:
    return InMemoryAIInvocationRepository()


@pytest.fixture
def evidence_id() -> str:
    return identity.generate_id()


def _create_background(repo, evidence_id, *, task_id="DOCUMENT_TYPE_PROPOSAL", task_version=1, **overrides):
    kwargs = dict(
        task_id=task_id,
        task_version=task_version,
        role="BACKGROUND",
        provider="LITELLM",
        capability_alias="bagman-fast",
        input_references={"evidence_id": evidence_id},
        actor_type="SYSTEM",
        actor_id="bagman-test-harness",
    )
    kwargs.update(overrides)
    return repo.create_invocation(**kwargs)


def _create_operator(repo, evidence_id, **overrides):
    kwargs = dict(
        task_id="OPERATOR_DOCUMENT_REVIEW",
        task_version=1,
        role="OPERATOR",
        provider="ANTHROPIC",
        capability_alias=None,
        input_references={"evidence_id": evidence_id},
        actor_type="USER",
        actor_id="matt",
    )
    kwargs.update(overrides)
    return repo.create_invocation(**kwargs)


# ---------------------------------------------------------------------
# Creation
# ---------------------------------------------------------------------


def test_create_invocation_starts_in_requested_with_no_completed_at(repo, evidence_id):
    invocation = _create_background(repo, evidence_id)
    assert invocation.status == "REQUESTED"
    assert invocation.completed_at is None
    assert invocation.output is None
    assert invocation.correlation_id is not None  # generated automatically


def test_create_invocation_without_correlation_id_generates_a_fresh_one(repo, evidence_id):
    a = _create_background(repo, evidence_id)
    b = _create_background(repo, identity.generate_id())
    assert a.correlation_id != b.correlation_id
    assert a.ai_invocation_id != b.ai_invocation_id


def test_create_invocation_honours_a_supplied_correlation_id(repo, evidence_id):
    correlation_id = identity.generate_id()
    invocation = _create_background(repo, evidence_id, correlation_id=correlation_id)
    assert invocation.correlation_id == correlation_id


def test_get_invocation_round_trips(repo, evidence_id):
    created = _create_background(repo, evidence_id)
    fetched = repo.get_invocation(created.ai_invocation_id)
    assert fetched == created


def test_operator_invocation_is_created_with_null_capability_alias(repo, evidence_id):
    invocation = _create_operator(repo, evidence_id)
    assert invocation.role == "OPERATOR"
    assert invocation.capability_alias is None


# ---------------------------------------------------------------------
# Role / provider / capability_alias pairing (PID §2/§8/§9)
# ---------------------------------------------------------------------


def test_background_with_null_capability_alias_is_rejected(repo, evidence_id):
    with pytest.raises(ValidationError):
        _create_background(repo, evidence_id, capability_alias=None)


def test_background_with_a_trinity_alias_is_rejected(repo, evidence_id):
    with pytest.raises(ValidationError):
        _create_background(repo, evidence_id, capability_alias="trinity-fast")


def test_operator_with_a_non_null_capability_alias_is_rejected(repo, evidence_id):
    with pytest.raises(ValidationError):
        _create_operator(repo, evidence_id, capability_alias="bagman-fast")


def test_background_role_with_anthropic_provider_is_rejected(repo, evidence_id):
    with pytest.raises(ValidationError):
        _create_background(repo, evidence_id, provider="ANTHROPIC")


def test_operator_role_with_litellm_provider_is_rejected(repo, evidence_id):
    with pytest.raises(ValidationError):
        _create_operator(repo, evidence_id, provider="LITELLM")


def test_invalid_actor_type_is_rejected(repo, evidence_id):
    with pytest.raises(ValidationError):
        _create_background(repo, evidence_id, actor_type="NOT_A_REAL_TYPE")


# ---------------------------------------------------------------------
# derive_primary_input_reference (PID §29/§73)
# ---------------------------------------------------------------------


def test_derive_primary_input_reference_prefers_evidence_id():
    ref = derive_primary_input_reference({"evidence_id": "e1", "intake_id": "i1", "entity_id": "n1"})
    assert ref == "e1"


def test_derive_primary_input_reference_falls_back_to_intake_id():
    ref = derive_primary_input_reference({"intake_id": "i1", "entity_id": "n1"})
    assert ref == "i1"


def test_derive_primary_input_reference_falls_back_to_entity_id():
    ref = derive_primary_input_reference({"entity_id": "n1"})
    assert ref == "n1"


def test_derive_primary_input_reference_raises_when_no_recognised_key_present():
    with pytest.raises(ValidationError):
        derive_primary_input_reference({"some_other_key": "x"})


def test_create_invocation_with_no_recognised_primary_reference_is_rejected(repo):
    with pytest.raises(ValidationError):
        repo.create_invocation(
            task_id="DOCUMENT_TYPE_PROPOSAL",
            task_version=1,
            role="BACKGROUND",
            provider="LITELLM",
            capability_alias="bagman-fast",
            input_references={"nonsense_key": "x"},
            actor_type="SYSTEM",
            actor_id="bagman-test-harness",
        )


# ---------------------------------------------------------------------
# State machine — valid paths
# ---------------------------------------------------------------------


def test_success_path_stamps_completed_at_only_at_terminal(repo, evidence_id):
    invocation = _create_background(repo, evidence_id)

    running = repo.transition_status(invocation.ai_invocation_id, "RUNNING")
    assert running.status == "RUNNING"
    assert running.completed_at is None

    succeeded = repo.transition_status(
        running.ai_invocation_id,
        "SUCCEEDED",
        output={"proposed_type": "INVOICE", "confidence": 0.9, "signals": [], "warnings": []},
        confidence=0.9,
        validation_result={"valid": True, "errors": []},
        latency_ms=850,
        provider_model="gemma-3-12b-it",
    )
    assert succeeded.status == "SUCCEEDED"
    assert succeeded.completed_at is not None
    assert succeeded.output["proposed_type"] == "INVOICE"
    assert succeeded.provider_model == "gemma-3-12b-it"


def test_requested_can_fail_directly_without_ever_running(repo, evidence_id):
    invocation = _create_background(repo, evidence_id)
    failed = repo.transition_status(invocation.ai_invocation_id, "FAILED", error_code="INFRA_UNAVAILABLE")
    assert failed.status == "FAILED"
    assert failed.completed_at is not None
    assert failed.error_code == "INFRA_UNAVAILABLE"


def test_requested_can_be_rejected_before_ever_running(repo, evidence_id):
    invocation = _create_background(repo, evidence_id)
    rejected = repo.transition_status(invocation.ai_invocation_id, "REJECTED", error_code="DATA_POLICY_REFUSED")
    assert rejected.status == "REJECTED"
    assert rejected.completed_at is not None
    assert rejected.output is None


def test_running_can_fail(repo, evidence_id):
    invocation = _create_background(repo, evidence_id)
    repo.transition_status(invocation.ai_invocation_id, "RUNNING")
    failed = repo.transition_status(invocation.ai_invocation_id, "FAILED", error_code="TIMEOUT")
    assert failed.status == "FAILED"
    assert failed.completed_at is not None


# ---------------------------------------------------------------------
# State machine — invalid paths
# ---------------------------------------------------------------------


def test_running_cannot_be_rejected(repo, evidence_id):
    """REJECTED is reachable only from REQUESTED (module docstring) —
    once RUNNING, a negative outcome is FAILED, never REJECTED."""
    invocation = _create_background(repo, evidence_id)
    repo.transition_status(invocation.ai_invocation_id, "RUNNING")
    with pytest.raises(InvalidStateTransitionError):
        repo.transition_status(invocation.ai_invocation_id, "REJECTED")


def test_skipping_running_straight_to_succeeded_is_rejected(repo, evidence_id):
    invocation = _create_background(repo, evidence_id)
    with pytest.raises(InvalidStateTransitionError):
        repo.transition_status(invocation.ai_invocation_id, "SUCCEEDED")


@pytest.mark.parametrize("terminal_status", sorted(TERMINAL))
def test_every_terminal_state_rejects_any_further_transition(repo, evidence_id, terminal_status):
    invocation = _create_background(repo, evidence_id)

    if terminal_status == "SUCCEEDED":
        repo.transition_status(invocation.ai_invocation_id, "RUNNING")
        repo.transition_status(invocation.ai_invocation_id, "SUCCEEDED")
    elif terminal_status == "FAILED":
        repo.transition_status(invocation.ai_invocation_id, "FAILED")
    elif terminal_status == "REJECTED":
        repo.transition_status(invocation.ai_invocation_id, "REJECTED")

    for attempted_next in sorted(STATUSES):
        with pytest.raises(InvalidStateTransitionError):
            repo.transition_status(invocation.ai_invocation_id, attempted_next)


def test_allowed_transitions_table_matches_pid_section_28_exactly():
    assert ALLOWED_TRANSITIONS == {
        "REQUESTED": frozenset({"RUNNING", "FAILED", "REJECTED"}),
        "RUNNING": frozenset({"SUCCEEDED", "FAILED"}),
        "SUCCEEDED": frozenset(),
        "FAILED": frozenset(),
        "REJECTED": frozenset(),
    }


def test_transition_rejects_completed_at_supplied_directly(repo, evidence_id):
    invocation = _create_background(repo, evidence_id)
    with pytest.raises(ValidationError):
        transition(invocation, "RUNNING", completed_at=None)


def test_transition_rejects_unknown_field_name(repo, evidence_id):
    invocation = _create_background(repo, evidence_id)
    with pytest.raises(ValidationError):
        transition(invocation, "RUNNING", not_a_real_field="oops")


# ---------------------------------------------------------------------
# Concurrency guard — one active invocation per subject (PID §73)
# ---------------------------------------------------------------------


def test_second_create_for_same_subject_while_first_is_active_conflicts(repo, evidence_id):
    _create_background(repo, evidence_id)
    with pytest.raises(ActiveInvocationConflictError):
        _create_background(repo, evidence_id)


def test_second_create_for_same_subject_conflicts_even_while_first_is_running(repo, evidence_id):
    first = _create_background(repo, evidence_id)
    repo.transition_status(first.ai_invocation_id, "RUNNING")
    with pytest.raises(ActiveInvocationConflictError):
        _create_background(repo, evidence_id)


def test_a_different_task_id_for_the_same_evidence_does_not_conflict(repo, evidence_id):
    _create_background(repo, evidence_id, task_id="DOCUMENT_TYPE_PROPOSAL")
    # Different task_id -> different subject -> no conflict.
    second = _create_background(repo, evidence_id, task_id="DOCUMENT_SUMMARY")
    assert second.task_id == "DOCUMENT_SUMMARY"


def test_a_different_evidence_id_does_not_conflict(repo, evidence_id):
    _create_background(repo, evidence_id)
    other_evidence_id = identity.generate_id()
    second = _create_background(repo, other_evidence_id)
    assert second.input_references["evidence_id"] == other_evidence_id


def test_retry_after_terminal_state_creates_a_new_distinct_row_preserving_the_old_one(repo, evidence_id):
    first = _create_background(repo, evidence_id)
    failed = repo.transition_status(first.ai_invocation_id, "FAILED", error_code="TIMEOUT")

    retry = _create_background(repo, evidence_id)
    assert retry.ai_invocation_id != failed.ai_invocation_id
    assert retry.status == "REQUESTED"

    # The old, failed attempt's history is preserved unchanged, not
    # overwritten (PID §74).
    still_there = repo.get_invocation(failed.ai_invocation_id)
    assert still_there.status == "FAILED"
    assert still_there.error_code == "TIMEOUT"

    all_for_subject = [
        inv for inv in repo.list_invocations() if inv.input_references.get("evidence_id") == evidence_id
    ]
    assert len(all_for_subject) == 2


def test_find_active_invocation_returns_none_when_none_active(repo, evidence_id):
    assert repo.find_active_invocation(
        task_id="DOCUMENT_TYPE_PROPOSAL", task_version=1, primary_input_reference=evidence_id
    ) is None


def test_find_active_invocation_finds_the_requested_invocation(repo, evidence_id):
    created = _create_background(repo, evidence_id)
    found = repo.find_active_invocation(
        task_id="DOCUMENT_TYPE_PROPOSAL", task_version=1, primary_input_reference=evidence_id
    )
    assert found is not None
    assert found.ai_invocation_id == created.ai_invocation_id


def test_find_active_invocation_returns_none_once_terminal(repo, evidence_id):
    created = _create_background(repo, evidence_id)
    repo.transition_status(created.ai_invocation_id, "REJECTED")
    found = repo.find_active_invocation(
        task_id="DOCUMENT_TYPE_PROPOSAL", task_version=1, primary_input_reference=evidence_id
    )
    assert found is None


# ---------------------------------------------------------------------
# Listing
# ---------------------------------------------------------------------


def test_list_invocations_returns_every_record(repo, evidence_id):
    a = _create_background(repo, evidence_id)
    b = _create_background(repo, identity.generate_id())
    ids = {inv.ai_invocation_id for inv in repo.list_invocations()}
    assert ids == {a.ai_invocation_id, b.ai_invocation_id}


def test_list_invocations_filters_by_status(repo, evidence_id):
    a = _create_background(repo, evidence_id)
    b = _create_background(repo, identity.generate_id())
    repo.transition_status(a.ai_invocation_id, "REJECTED")

    rejected = repo.list_invocations(status="REJECTED")
    assert {inv.ai_invocation_id for inv in rejected} == {a.ai_invocation_id}

    requested = repo.list_invocations(status="REQUESTED")
    assert {inv.ai_invocation_id for inv in requested} == {b.ai_invocation_id}


def test_list_invocations_filters_by_role(repo, evidence_id):
    background = _create_background(repo, evidence_id)
    operator = _create_operator(repo, identity.generate_id())

    assert {inv.ai_invocation_id for inv in repo.list_invocations(role="BACKGROUND")} == {
        background.ai_invocation_id
    }
    assert {inv.ai_invocation_id for inv in repo.list_invocations(role="OPERATOR")} == {
        operator.ai_invocation_id
    }


def test_list_invocations_respects_limit_and_offset_with_deterministic_ordering(repo):
    created = [_create_background(repo, identity.generate_id()) for _ in range(5)]
    created_ids_newest_first = [inv.ai_invocation_id for inv in reversed(created)]

    page1 = repo.list_invocations(limit=2, offset=0)
    page2 = repo.list_invocations(limit=2, offset=2)

    assert [inv.ai_invocation_id for inv in page1] == created_ids_newest_first[0:2]
    assert [inv.ai_invocation_id for inv in page2] == created_ids_newest_first[2:4]
