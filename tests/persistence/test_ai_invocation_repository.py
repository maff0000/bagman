"""PID §26-30/§73-74 'AI invocation persistence' proofs (CD-5 WI-1).

The "genuine concurrent-create race" proof below deliberately does NOT
use real threads/true concurrency (that full proof is WI-5's job,
mirroring the same deferral CD-4 WI-1 documented for intake's
idempotency-key race) — it simulates a race between two repository
instances by making the SECOND instance's optimistic pre-check
(`find_active_invocation`) report "nothing active yet" (via
`unittest.mock.patch.object`), forcing it down the same
insert-then-translate-the-real-constraint-violation code path a
genuine concurrent caller would hit, deterministically and without
flakiness.

Unlike CD-4 WI-1's intake idempotency-key race (which has a
replay-vs-conflict ambiguity to resolve, requiring a second read of the
winning row), AIInvocation's concurrency guard has no such ambiguity: a
real unique-violation on `uq_ai_invocations_active_subject` always means
exactly one thing — "an active invocation already exists for this
subject" — so the repository never needs to re-read anything to decide
the outcome; see `persistence/postgres/ai_invocation_repository.py`.
"""
from __future__ import annotations

from unittest import mock

import pytest
import sqlalchemy as sa

from core import identity
from core.errors import (
    ActiveInvocationConflictError,
    InvalidStateTransitionError,
    NotFoundError,
    ValidationError,
)
from persistence.postgres.ai_invocation_models import AIInvocationRow
from persistence.postgres.ai_invocation_repository import PostgresAIInvocationRepository
from persistence.postgres.session import get_engine


def _create_background(repo=None, evidence_id=None, **overrides):
    repo = repo or PostgresAIInvocationRepository()
    evidence_id = evidence_id if evidence_id is not None else identity.generate_id()
    kwargs = dict(
        task_id="DOCUMENT_TYPE_PROPOSAL",
        task_version=1,
        role="BACKGROUND",
        provider="LITELLM",
        capability_alias="bagman-fast",
        input_references={"evidence_id": evidence_id},
        actor_type="SYSTEM",
        actor_id="bagman-test-harness",
    )
    kwargs.update(overrides)
    return repo.create_invocation(**kwargs)


def _create_operator(repo=None, evidence_id=None, **overrides):
    repo = repo or PostgresAIInvocationRepository()
    evidence_id = evidence_id if evidence_id is not None else identity.generate_id()
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


def _invocation_count_for_subject(task_id: str, task_version: int, primary_input_reference: str) -> int:
    with get_engine().connect() as conn:
        result = conn.execute(
            sa.select(sa.func.count())
            .select_from(AIInvocationRow)
            .where(
                AIInvocationRow.task_id == task_id,
                AIInvocationRow.task_version == task_version,
                AIInvocationRow.primary_input_reference == primary_input_reference,
            )
        )
        return result.scalar_one()


# ---------------------------------------------------------------------
# Create / get round trip
# ---------------------------------------------------------------------


def test_create_and_get_round_trip():
    created = _create_background()
    fetched = PostgresAIInvocationRepository().get_invocation(created.ai_invocation_id)
    assert fetched == created


def test_operator_invocation_round_trips_with_null_capability_alias():
    created = _create_operator()
    fetched = PostgresAIInvocationRepository().get_invocation(created.ai_invocation_id)
    assert fetched.role == "OPERATOR"
    assert fetched.capability_alias is None
    assert fetched.provider == "ANTHROPIC"


def test_get_invocation_not_found_raises():
    with pytest.raises(NotFoundError):
        PostgresAIInvocationRepository().get_invocation(identity.generate_id())


def test_get_invocation_malformed_id_raises_not_found_not_persistence_error():
    with pytest.raises(NotFoundError):
        PostgresAIInvocationRepository().get_invocation("not-a-valid-uuid")


def test_invalid_role_provider_pairing_is_rejected_before_reaching_the_database():
    with pytest.raises(ValidationError):
        _create_background(capability_alias=None)


# ---------------------------------------------------------------------
# Transitions persist durably (fresh repository instance each time)
# ---------------------------------------------------------------------


def test_transition_to_running_then_succeeded_persists_every_field():
    created = _create_background()
    repo_a = PostgresAIInvocationRepository()
    repo_a.transition_status(created.ai_invocation_id, "RUNNING")

    repo_b = PostgresAIInvocationRepository()
    succeeded = repo_b.transition_status(
        created.ai_invocation_id,
        "SUCCEEDED",
        output={"proposed_type": "INVOICE", "confidence": 0.91, "signals": [], "warnings": []},
        confidence=0.91,
        validation_result={"valid": True, "errors": []},
        latency_ms=642,
        provider_model="gemma-3-12b-it",
        usage_metadata={"input_tokens": 500, "output_tokens": 40},
    )
    assert succeeded.status == "SUCCEEDED"
    assert succeeded.completed_at is not None

    repo_c = PostgresAIInvocationRepository()
    reread = repo_c.get_invocation(created.ai_invocation_id)
    assert reread.status == "SUCCEEDED"
    assert reread.output == {"proposed_type": "INVOICE", "confidence": 0.91, "signals": [], "warnings": []}
    assert reread.confidence == 0.91
    assert reread.validation_result == {"valid": True, "errors": []}
    assert reread.latency_ms == 642
    assert reread.provider_model == "gemma-3-12b-it"
    assert reread.usage_metadata == {"input_tokens": 500, "output_tokens": 40}
    assert reread.completed_at is not None


def test_transition_invalid_edge_raises_and_leaves_state_unchanged():
    created = _create_background()
    with pytest.raises(InvalidStateTransitionError):
        PostgresAIInvocationRepository().transition_status(created.ai_invocation_id, "SUCCEEDED")

    still_requested = PostgresAIInvocationRepository().get_invocation(created.ai_invocation_id)
    assert still_requested.status == "REQUESTED"


def test_transition_status_not_found_raises():
    with pytest.raises(NotFoundError):
        PostgresAIInvocationRepository().transition_status(identity.generate_id(), "RUNNING")


# ---------------------------------------------------------------------
# The real, database-enforced concurrency guard (PID §73)
# ---------------------------------------------------------------------


def test_partial_unique_index_exists_at_the_database_level():
    inspector = sa.inspect(get_engine())
    indexes = inspector.get_indexes("ai_invocations")
    match = next((ix for ix in indexes if ix["name"] == "uq_ai_invocations_active_subject"), None)
    assert match is not None, f"expected a partial unique index, found indexes: {indexes}"
    assert match["unique"] is True
    assert match["column_names"] == ["task_id", "task_version", "primary_input_reference"]


def test_generated_column_derives_evidence_id_when_present():
    evidence_id = identity.generate_id()
    _create_background(evidence_id=evidence_id)
    with get_engine().connect() as conn:
        value = conn.execute(
            sa.select(AIInvocationRow.primary_input_reference).where(
                AIInvocationRow.input_references["evidence_id"].astext == evidence_id
            )
        ).scalar_one()
    assert value == evidence_id


def test_second_create_for_same_subject_conflicts_via_the_ordinary_pre_check():
    evidence_id = identity.generate_id()
    _create_background(evidence_id=evidence_id)
    with pytest.raises(ActiveInvocationConflictError):
        _create_background(evidence_id=evidence_id)
    assert _invocation_count_for_subject("DOCUMENT_TYPE_PROPOSAL", 1, evidence_id) == 1


def test_retry_after_terminal_creates_a_new_distinct_row_preserving_the_old_one():
    evidence_id = identity.generate_id()
    first = _create_background(evidence_id=evidence_id)
    PostgresAIInvocationRepository().transition_status(first.ai_invocation_id, "FAILED", error_code="TIMEOUT")

    retry = _create_background(evidence_id=evidence_id)
    assert retry.ai_invocation_id != first.ai_invocation_id

    reread_first = PostgresAIInvocationRepository().get_invocation(first.ai_invocation_id)
    assert reread_first.status == "FAILED"
    assert reread_first.error_code == "TIMEOUT"

    assert _invocation_count_for_subject("DOCUMENT_TYPE_PROPOSAL", 1, evidence_id) == 2


def test_find_active_invocation_returns_none_when_absent():
    evidence_id = identity.generate_id()
    found = PostgresAIInvocationRepository().find_active_invocation(
        task_id="DOCUMENT_TYPE_PROPOSAL", task_version=1, primary_input_reference=evidence_id
    )
    assert found is None


def test_find_active_invocation_finds_the_persisted_active_row():
    evidence_id = identity.generate_id()
    created = _create_background(evidence_id=evidence_id)
    found = PostgresAIInvocationRepository().find_active_invocation(
        task_id="DOCUMENT_TYPE_PROPOSAL", task_version=1, primary_input_reference=evidence_id
    )
    assert found is not None
    assert found.ai_invocation_id == created.ai_invocation_id


def test_find_active_invocation_returns_none_once_terminal():
    evidence_id = identity.generate_id()
    created = _create_background(evidence_id=evidence_id)
    PostgresAIInvocationRepository().transition_status(created.ai_invocation_id, "REJECTED")
    found = PostgresAIInvocationRepository().find_active_invocation(
        task_id="DOCUMENT_TYPE_PROPOSAL", task_version=1, primary_input_reference=evidence_id
    )
    assert found is None


# ---------------------------------------------------------------------
# Simulated insert race (two repository instances, not true concurrency
# — see module docstring; true concurrency proof is WI-5's job)
# ---------------------------------------------------------------------


def test_simulated_race_resolves_via_the_real_constraint_not_the_pre_check():
    evidence_id = identity.generate_id()
    winner_repo = PostgresAIInvocationRepository()
    winner = _create_background(winner_repo, evidence_id)

    racer_repo = PostgresAIInvocationRepository()
    call_count = {"n": 0}

    def _pre_check_always_misses(**kwargs):
        call_count["n"] += 1
        return None

    with mock.patch.object(racer_repo, "find_active_invocation", side_effect=_pre_check_always_misses):
        with pytest.raises(ActiveInvocationConflictError):
            _create_background(racer_repo, evidence_id)

    assert call_count["n"] == 1  # the racer's pre-check DID run and DID miss
    # ...yet the real database constraint still caught it — exactly one
    # active row exists for this subject, and the racer's rejected
    # insert left no orphan row behind.
    assert _invocation_count_for_subject("DOCUMENT_TYPE_PROPOSAL", 1, evidence_id) == 1

    still_the_winner = PostgresAIInvocationRepository().get_invocation(winner.ai_invocation_id)
    assert still_the_winner.status == "REQUESTED"


# ---------------------------------------------------------------------
# Ordering
# ---------------------------------------------------------------------


def test_list_invocations_orders_by_started_at_desc_with_id_tiebreak():
    created = [_create_background() for _ in range(3)]
    newest_first_ids = [inv.ai_invocation_id for inv in reversed(created)]

    listed = PostgresAIInvocationRepository().list_invocations(task_id="DOCUMENT_TYPE_PROPOSAL", task_version=1)
    listed_ids = [inv.ai_invocation_id for inv in listed if inv.ai_invocation_id in newest_first_ids]
    assert listed_ids == newest_first_ids


def test_list_invocations_limit_and_offset_page_correctly():
    created = [_create_background() for _ in range(5)]
    newest_first_ids = [inv.ai_invocation_id for inv in reversed(created)]

    page1 = PostgresAIInvocationRepository().list_invocations(
        task_id="DOCUMENT_TYPE_PROPOSAL", task_version=1, limit=2, offset=0
    )
    page2 = PostgresAIInvocationRepository().list_invocations(
        task_id="DOCUMENT_TYPE_PROPOSAL", task_version=1, limit=2, offset=2
    )
    assert [inv.ai_invocation_id for inv in page1] == newest_first_ids[0:2]
    assert [inv.ai_invocation_id for inv in page2] == newest_first_ids[2:4]


# ---------------------------------------------------------------------
# list_invocations(primary_input_reference=...) (WI-4, PID §45) — the
# REAL stored-generated column, not a computed-in-Python comparison
# (contrast the in-memory repository's own equivalent test).
# ---------------------------------------------------------------------


def test_list_invocations_filters_by_primary_input_reference():
    evidence_id = identity.generate_id()
    a = _create_background(evidence_id=evidence_id, task_id="DOCUMENT_TYPE_PROPOSAL")
    b = _create_background(evidence_id=evidence_id, task_id="DOCUMENT_SUMMARY")
    _create_background()  # unrelated evidence_id — must not match

    found = PostgresAIInvocationRepository().list_invocations(primary_input_reference=evidence_id)
    assert {inv.ai_invocation_id for inv in found} == {a.ai_invocation_id, b.ai_invocation_id}


def test_list_invocations_by_primary_input_reference_includes_terminal_rows():
    evidence_id = identity.generate_id()
    created = _create_background(evidence_id=evidence_id)
    PostgresAIInvocationRepository().transition_status(created.ai_invocation_id, "REJECTED")

    found = PostgresAIInvocationRepository().list_invocations(primary_input_reference=evidence_id)
    assert [inv.ai_invocation_id for inv in found] == [created.ai_invocation_id]
    assert found[0].status == "REJECTED"


def test_list_invocations_primary_input_reference_with_no_matches_returns_empty():
    found = PostgresAIInvocationRepository().list_invocations(primary_input_reference=identity.generate_id())
    assert found == []
