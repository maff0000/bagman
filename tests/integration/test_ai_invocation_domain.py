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

import datetime

import pytest

from ai.invocation import (
    ALLOWED_TRANSITIONS,
    STALE_RECOVERY_ERROR_CODE,
    STALE_RECOVERY_NEVER_DISPATCHED_ERROR_CODE,
    STALE_RUNNING_THRESHOLD_SECONDS,
    STATUSES,
    TERMINAL_STATUSES,
    InMemoryAIInvocationRepository,
    derive_primary_input_reference,
    is_stale_running,
    transition,
)
from core import identity
from core.audit import InMemoryAuditRepository
from core.errors import (
    ActiveInvocationConflictError,
    InvalidStateTransitionError,
    ValidationError,
)
from core.timestamps import utc_now

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
    elif terminal_status == "TIMED_OUT":
        repo.transition_status(invocation.ai_invocation_id, "RUNNING")
        repo.transition_status(invocation.ai_invocation_id, "TIMED_OUT")
    elif terminal_status == "CANCELLED":
        repo.transition_status(invocation.ai_invocation_id, "CANCELLED")

    for attempted_next in sorted(STATUSES):
        with pytest.raises(InvalidStateTransitionError):
            repo.transition_status(invocation.ai_invocation_id, attempted_next)


def test_allowed_transitions_table_matches_pid_section_28_exactly():
    """CD-6 reliability delta (PID §100.14/§100.16) added `TIMED_OUT`
    (reachable only from `RUNNING`) and `CANCELLED` (reachable from
    both `REQUESTED` and `RUNNING`) — see `ai.invocation`'s own module
    docstring for the full reasoning behind each edge."""
    assert ALLOWED_TRANSITIONS == {
        "REQUESTED": frozenset({"RUNNING", "FAILED", "REJECTED", "CANCELLED"}),
        "RUNNING": frozenset({"SUCCEEDED", "FAILED", "TIMED_OUT", "CANCELLED"}),
        "SUCCEEDED": frozenset(),
        "FAILED": frozenset(),
        "REJECTED": frozenset(),
        "TIMED_OUT": frozenset(),
        "CANCELLED": frozenset(),
    }


# ---------------------------------------------------------------------
# CD-6 reliability delta — TIMED_OUT / CANCELLED (PID §100.14/§100.16)
# ---------------------------------------------------------------------


def test_running_can_time_out(repo, evidence_id):
    invocation = _create_background(repo, evidence_id)
    repo.transition_status(invocation.ai_invocation_id, "RUNNING")
    timed_out = repo.transition_status(invocation.ai_invocation_id, "TIMED_OUT", error_code="CLAUDE_CODE_TIMEOUT")
    assert timed_out.status == "TIMED_OUT"
    assert timed_out.completed_at is not None
    assert timed_out.error_code == "CLAUDE_CODE_TIMEOUT"


def test_requested_cannot_time_out_directly(repo, evidence_id):
    """TIMED_OUT is reachable only from RUNNING — a request cannot time
    out before it was ever dispatched (module docstring)."""
    invocation = _create_background(repo, evidence_id)
    with pytest.raises(InvalidStateTransitionError):
        repo.transition_status(invocation.ai_invocation_id, "TIMED_OUT")


def test_requested_can_be_cancelled_before_ever_running(repo, evidence_id):
    invocation = _create_background(repo, evidence_id)
    cancelled = repo.transition_status(invocation.ai_invocation_id, "CANCELLED")
    assert cancelled.status == "CANCELLED"
    assert cancelled.completed_at is not None


def test_running_can_be_cancelled(repo, evidence_id):
    invocation = _create_background(repo, evidence_id)
    repo.transition_status(invocation.ai_invocation_id, "RUNNING")
    cancelled = repo.transition_status(invocation.ai_invocation_id, "CANCELLED")
    assert cancelled.status == "CANCELLED"


@pytest.mark.parametrize("terminal_status", ["SUCCEEDED", "FAILED", "REJECTED", "TIMED_OUT"])
def test_no_terminal_state_can_be_cancelled(repo, evidence_id, terminal_status):
    """Cancellation is never a way to retroactively un-fail or
    un-succeed something — every OTHER terminal state stays terminal."""
    invocation = _create_background(repo, evidence_id)
    if terminal_status == "SUCCEEDED":
        repo.transition_status(invocation.ai_invocation_id, "RUNNING")
        repo.transition_status(invocation.ai_invocation_id, "SUCCEEDED")
    elif terminal_status == "TIMED_OUT":
        repo.transition_status(invocation.ai_invocation_id, "RUNNING")
        repo.transition_status(invocation.ai_invocation_id, "TIMED_OUT")
    else:
        repo.transition_status(invocation.ai_invocation_id, terminal_status)
    with pytest.raises(InvalidStateTransitionError):
        repo.transition_status(invocation.ai_invocation_id, "CANCELLED")


# ---------------------------------------------------------------------
# CD-6 reliability delta — conversation_id primary reference (PID §98/§100)
# ---------------------------------------------------------------------


def test_derive_primary_input_reference_falls_back_to_conversation_id_when_contextless():
    ref = derive_primary_input_reference({"conversation_id": "conv-1"})
    assert ref == "conv-1"


def test_derive_primary_input_reference_prefers_evidence_id_over_conversation_id():
    ref = derive_primary_input_reference({"evidence_id": "e1", "conversation_id": "conv-1"})
    assert ref == "e1"


def test_derive_primary_input_reference_prefers_entity_id_over_conversation_id():
    ref = derive_primary_input_reference({"entity_id": "n1", "conversation_id": "conv-1"})
    assert ref == "n1"


def test_derive_primary_input_reference_still_raises_when_truly_nothing_present():
    """Even with `conversation_id` recognised, a call with NONE of the
    four recognised keys is still rejected — never opaque, unbounded AI
    analysis (PID §29)."""
    with pytest.raises(ValidationError):
        derive_primary_input_reference({"message": "hi", "evidence_id": None, "conversation_id": None})


def test_two_contextless_turns_in_the_same_conversation_conflict(repo):
    repo.create_invocation(
        task_id="ASK_BAGMAN", task_version=1, role="OPERATOR", provider="ANTHROPIC",
        capability_alias=None,
        input_references={"message": "hi", "conversation_id": "conv-1"},
        actor_type="USER", actor_id="matt",
    )
    with pytest.raises(ActiveInvocationConflictError):
        repo.create_invocation(
            task_id="ASK_BAGMAN", task_version=1, role="OPERATOR", provider="ANTHROPIC",
            capability_alias=None,
            input_references={"message": "second", "conversation_id": "conv-1"},
            actor_type="USER", actor_id="matt",
        )


def test_two_contextless_turns_in_different_conversations_do_not_conflict(repo):
    first = repo.create_invocation(
        task_id="ASK_BAGMAN", task_version=1, role="OPERATOR", provider="ANTHROPIC",
        capability_alias=None,
        input_references={"message": "hi", "conversation_id": "conv-1"},
        actor_type="USER", actor_id="matt",
    )
    second = repo.create_invocation(
        task_id="ASK_BAGMAN", task_version=1, role="OPERATOR", provider="ANTHROPIC",
        capability_alias=None,
        input_references={"message": "hi", "conversation_id": "conv-2"},
        actor_type="USER", actor_id="matt",
    )
    assert first.ai_invocation_id != second.ai_invocation_id


# ---------------------------------------------------------------------
# CD-6 reliability delta — bounded stale-RUNNING recovery (PID §100)
# ---------------------------------------------------------------------


def _force_started_at(repo: InMemoryAIInvocationRepository, ai_invocation_id: str, started_at) -> None:
    """Test-only: directly overwrite a stored record's `started_at` — a
    genuine race/real elapsed time is never actually waited for; this
    is the documented, deterministic way this suite proves staleness
    without a real sleep (mirrors the PID's own "artificially old
    started_at" instruction)."""
    import dataclasses

    current = repo._by_id[ai_invocation_id]
    repo._by_id[ai_invocation_id] = dataclasses.replace(current, started_at=started_at)


def test_is_stale_running_true_past_the_threshold():
    invocation = InMemoryAIInvocationRepository().create_invocation(
        task_id="ASK_BAGMAN", task_version=1, role="OPERATOR", provider="ANTHROPIC",
        capability_alias=None, input_references={"conversation_id": "c"},
        actor_type="USER", actor_id="matt",
    )
    old_enough = invocation.started_at - datetime.timedelta(seconds=STALE_RUNNING_THRESHOLD_SECONDS + 1)
    import dataclasses

    stale = dataclasses.replace(invocation, started_at=old_enough)
    assert is_stale_running(stale) is True


def test_is_stale_running_false_when_recent():
    invocation = InMemoryAIInvocationRepository().create_invocation(
        task_id="ASK_BAGMAN", task_version=1, role="OPERATOR", provider="ANTHROPIC",
        capability_alias=None, input_references={"conversation_id": "c"},
        actor_type="USER", actor_id="matt",
    )
    assert is_stale_running(invocation) is False


def test_is_stale_running_false_once_terminal(repo, evidence_id):
    invocation = _create_background(repo, evidence_id)
    failed = repo.transition_status(invocation.ai_invocation_id, "FAILED")
    import dataclasses

    ancient = dataclasses.replace(
        failed, started_at=utc_now() - datetime.timedelta(seconds=STALE_RUNNING_THRESHOLD_SECONDS * 10)
    )
    assert is_stale_running(ancient) is False


def test_create_invocation_recovers_a_stale_running_row_for_the_same_subject_then_succeeds():
    audit = InMemoryAuditRepository()
    repo = InMemoryAIInvocationRepository(audit_repository=audit)

    stuck = repo.create_invocation(
        task_id="ASK_BAGMAN", task_version=1, role="OPERATOR", provider="ANTHROPIC",
        capability_alias=None, input_references={"conversation_id": "conv-stuck"},
        actor_type="USER", actor_id="matt",
    )
    repo.transition_status(stuck.ai_invocation_id, "RUNNING")
    _force_started_at(
        repo, stuck.ai_invocation_id, utc_now() - datetime.timedelta(seconds=STALE_RUNNING_THRESHOLD_SECONDS + 5)
    )

    # A brand-new invocation for the SAME subject now succeeds instead
    # of raising ActiveInvocationConflictError — the stale row is
    # recovered first, audibly, THEN the new one is created.
    new = repo.create_invocation(
        task_id="ASK_BAGMAN", task_version=1, role="OPERATOR", provider="ANTHROPIC",
        capability_alias=None, input_references={"conversation_id": "conv-stuck"},
        actor_type="USER", actor_id="matt",
    )
    assert new.ai_invocation_id != stuck.ai_invocation_id
    assert new.status == "REQUESTED"

    recovered = repo.get_invocation(stuck.ai_invocation_id)
    assert recovered.status == "TIMED_OUT"
    assert recovered.error_code == STALE_RECOVERY_ERROR_CODE
    assert recovered.completed_at is not None

    events = audit.list_by_subject("AIInvocation", stuck.ai_invocation_id)
    assert [e.event_type for e in events] == ["AI_INVOCATION_STALE_RECOVERED"]


def test_find_active_invocation_recovers_a_stale_row_and_reports_none():
    audit = InMemoryAuditRepository()
    repo = InMemoryAIInvocationRepository(audit_repository=audit)

    stuck = repo.create_invocation(
        task_id="ASK_BAGMAN", task_version=1, role="OPERATOR", provider="ANTHROPIC",
        capability_alias=None, input_references={"conversation_id": "conv-stuck-2"},
        actor_type="USER", actor_id="matt",
    )
    repo.transition_status(stuck.ai_invocation_id, "RUNNING")
    _force_started_at(
        repo, stuck.ai_invocation_id, utc_now() - datetime.timedelta(seconds=STALE_RUNNING_THRESHOLD_SECONDS + 5)
    )

    found = repo.find_active_invocation(
        task_id="ASK_BAGMAN", task_version=1, primary_input_reference="conv-stuck-2"
    )
    assert found is None

    recovered = repo.get_invocation(stuck.ai_invocation_id)
    assert recovered.status == "TIMED_OUT"
    events = audit.list_by_subject("AIInvocation", stuck.ai_invocation_id)
    assert [e.event_type for e in events] == ["AI_INVOCATION_STALE_RECOVERED"]


def test_create_invocation_recovers_a_stale_requested_row_to_failed_then_succeeds():
    """The exact gap a fresh Auditor found and reproduced live against
    the real Postgres-backed repository: a row abandoned while still
    `REQUESTED` (the process died in the real, reachable window between
    `create_invocation` returning `REQUESTED` and a caller's own
    subsequent `transition_status(..., "RUNNING")` a few lines later —
    exactly `agent.claude_code.orchestrator.handle_operator_message`'s
    own call shape) must NOT be targeted at `TIMED_OUT`
    (`ALLOWED_TRANSITIONS["REQUESTED"]` does not include it — see
    `ai.invocation.recover_stale_invocation`'s own docstring) — doing so
    made the subject permanently, unrecoverably stuck: every recovery
    attempt raised `InvalidStateTransitionError`, a real HTTP 500, worse
    than the original bug. Deliberately never calls
    `transition_status(..., "RUNNING")` — that is exactly what every
    OTHER stale-recovery test in this suite does, which is why none of
    them caught this."""
    audit = InMemoryAuditRepository()
    repo = InMemoryAIInvocationRepository(audit_repository=audit)

    stuck = repo.create_invocation(
        task_id="ASK_BAGMAN", task_version=1, role="OPERATOR", provider="ANTHROPIC",
        capability_alias=None, input_references={"conversation_id": "conv-never-dispatched"},
        actor_type="USER", actor_id="matt",
    )
    assert stuck.status == "REQUESTED"  # never transitioned to RUNNING — the exact gap
    _force_started_at(
        repo, stuck.ai_invocation_id, utc_now() - datetime.timedelta(seconds=STALE_RUNNING_THRESHOLD_SECONDS + 5)
    )

    # Must NOT raise InvalidStateTransitionError — this is the live-
    # reproduced defect: recovery previously crashed here instead of
    # succeeding.
    new = repo.create_invocation(
        task_id="ASK_BAGMAN", task_version=1, role="OPERATOR", provider="ANTHROPIC",
        capability_alias=None, input_references={"conversation_id": "conv-never-dispatched"},
        actor_type="USER", actor_id="matt",
    )
    assert new.ai_invocation_id != stuck.ai_invocation_id
    assert new.status == "REQUESTED"

    recovered = repo.get_invocation(stuck.ai_invocation_id)
    assert recovered.status == "FAILED"  # NOT TIMED_OUT — REQUESTED can never reach it
    assert recovered.error_code == STALE_RECOVERY_NEVER_DISPATCHED_ERROR_CODE
    assert recovered.completed_at is not None

    events = audit.list_by_subject("AIInvocation", stuck.ai_invocation_id)
    assert [e.event_type for e in events] == ["AI_INVOCATION_STALE_RECOVERED"]


def test_find_active_invocation_recovers_a_stale_requested_row_to_failed():
    """Same gap as the `create_invocation` test above, exercised via
    `find_active_invocation` instead (the other real call site
    `_recover_if_stale` backs)."""
    audit = InMemoryAuditRepository()
    repo = InMemoryAIInvocationRepository(audit_repository=audit)

    stuck = repo.create_invocation(
        task_id="ASK_BAGMAN", task_version=1, role="OPERATOR", provider="ANTHROPIC",
        capability_alias=None, input_references={"conversation_id": "conv-never-dispatched-2"},
        actor_type="USER", actor_id="matt",
    )
    assert stuck.status == "REQUESTED"
    _force_started_at(
        repo, stuck.ai_invocation_id, utc_now() - datetime.timedelta(seconds=STALE_RUNNING_THRESHOLD_SECONDS + 5)
    )

    found = repo.find_active_invocation(
        task_id="ASK_BAGMAN", task_version=1, primary_input_reference="conv-never-dispatched-2"
    )
    assert found is None

    recovered = repo.get_invocation(stuck.ai_invocation_id)
    assert recovered.status == "FAILED"
    assert recovered.error_code == STALE_RECOVERY_NEVER_DISPATCHED_ERROR_CODE


def test_a_genuinely_recent_running_row_is_not_touched_and_still_blocks(repo):
    stuck = repo.create_invocation(
        task_id="ASK_BAGMAN", task_version=1, role="OPERATOR", provider="ANTHROPIC",
        capability_alias=None, input_references={"conversation_id": "conv-fresh"},
        actor_type="USER", actor_id="matt",
    )
    repo.transition_status(stuck.ai_invocation_id, "RUNNING")

    # No time manipulation — this row is genuinely fresh.
    found = repo.find_active_invocation(
        task_id="ASK_BAGMAN", task_version=1, primary_input_reference="conv-fresh"
    )
    assert found is not None
    assert found.status == "RUNNING"

    with pytest.raises(ActiveInvocationConflictError):
        repo.create_invocation(
            task_id="ASK_BAGMAN", task_version=1, role="OPERATOR", provider="ANTHROPIC",
            capability_alias=None, input_references={"conversation_id": "conv-fresh"},
            actor_type="USER", actor_id="matt",
        )

    still_running = repo.get_invocation(stuck.ai_invocation_id)
    assert still_running.status == "RUNNING"


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


# ---------------------------------------------------------------------
# list_invocations(primary_input_reference=...) (WI-4, PID §45)
# ---------------------------------------------------------------------


def test_list_invocations_filters_by_primary_input_reference(repo, evidence_id):
    a = _create_background(repo, evidence_id, task_id="DOCUMENT_TYPE_PROPOSAL")
    b = _create_background(repo, evidence_id, task_id="DOCUMENT_SUMMARY")
    other_evidence_id = identity.generate_id()
    _create_background(repo, other_evidence_id, task_id="DOCUMENT_TYPE_PROPOSAL")

    found = repo.list_invocations(primary_input_reference=evidence_id)
    assert {inv.ai_invocation_id for inv in found} == {a.ai_invocation_id, b.ai_invocation_id}


def test_list_invocations_by_primary_input_reference_spans_every_status_including_terminal(repo, evidence_id):
    """Deliberately proves this is genuinely different from
    `find_active_invocation` — it must include terminal invocations too
    (module docstring's own "terminal or not" contract)."""
    a = _create_background(repo, evidence_id, task_id="DOCUMENT_TYPE_PROPOSAL")
    repo.transition_status(a.ai_invocation_id, "REJECTED")
    b = _create_background(repo, evidence_id, task_id="DOCUMENT_SUMMARY")
    repo.transition_status(b.ai_invocation_id, "RUNNING")
    repo.transition_status(b.ai_invocation_id, "SUCCEEDED", output={"summary": "s", "confidence": 0.5, "signals": [], "warnings": []})

    found = {inv.ai_invocation_id: inv.status for inv in repo.list_invocations(primary_input_reference=evidence_id)}
    assert found == {a.ai_invocation_id: "REJECTED", b.ai_invocation_id: "SUCCEEDED"}


def test_list_invocations_filters_by_primary_input_reference_for_intake_and_entity_subjects(repo):
    """`primary_input_reference` is a generalised subject, not literally
    `evidence_id` — an invocation keyed on `intake_id` alone must match
    too (see `derive_primary_input_reference`'s precedence order)."""
    intake_id = identity.generate_id()
    matching = repo.create_invocation(
        task_id="ASK_BAGMAN",
        task_version=1,
        role="OPERATOR",
        provider="ANTHROPIC",
        capability_alias=None,
        input_references={"intake_id": intake_id},
        actor_type="USER",
        actor_id="matt",
    )
    found = repo.list_invocations(primary_input_reference=intake_id)
    assert [inv.ai_invocation_id for inv in found] == [matching.ai_invocation_id]


def test_list_invocations_primary_input_reference_with_no_matches_returns_empty(repo, evidence_id):
    _create_background(repo, evidence_id)
    assert repo.list_invocations(primary_input_reference=identity.generate_id()) == []
