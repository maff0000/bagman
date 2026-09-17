"""PID §26-30/§73-74 'AI invocation persistence' proofs (CD-5 WI-1;
genuine-threaded-race proof added CD-5 WI-5).

The "genuine concurrent-create race" proof immediately below deliberately
does NOT use real threads/true concurrency (that full proof is WI-5's
job — see `test_genuinely_concurrent_threads_racing_the_same_subject_...`
near the bottom of this file — mirroring the same deferral CD-4 WI-1
documented for intake's idempotency-key race) — it simulates a race
between two repository instances by making the SECOND instance's
optimistic pre-check (`find_active_invocation`) report "nothing active
yet" (via `unittest.mock.patch.object`), forcing it down the same
insert-then-translate-the-real-constraint-violation code path a genuine
concurrent caller would hit, deterministically and without flakiness.

Unlike CD-4 WI-1's intake idempotency-key race (which has a
replay-vs-conflict ambiguity to resolve, requiring a second read of the
winning row), AIInvocation's concurrency guard has no such ambiguity: a
real unique-violation on `uq_ai_invocations_active_subject` always means
exactly one thing — "an active invocation already exists for this
subject" — so the repository never needs to re-read anything to decide
the outcome; see `persistence/postgres/ai_invocation_repository.py`.

CD-5 WI-5's own real-threaded proof (PID §73/§91's "Persistence" bullet:
"retries/history durable; restart safe" and the WI-5 dispatch's own
"a genuine race between multiple attempts to create/analyse the same
subject, resolved cleanly via the real database constraint, never a
silent duplicate")
------------------------------------------------------------------------
Unlike CD-4's own `idempotency_and_concurrency_proof.py` (which needed a
`docker compose exec`-into-the-running-container trick to force genuine
interleaving, because `bagman-api` itself runs as a single Uvicorn
worker with no `await` in its own handler body — see that script's own
module docstring), this proof does not need that: `pytest tests/
persistence/` already talks to a REAL, disposable PostgreSQL container
over a REAL TCP socket (`127.0.0.1:55432`, `tests/persistence/conftest.py`).
Real Python `threading.Thread`s, each with their OWN
`PostgresAIInvocationRepository`/SQLAlchemy `Engine`, calling
`create_invocation` for the exact same subject at (as near as a
`threading.Barrier` can arrange) the same instant, genuinely release the
GIL during that socket I/O — so this IS a true concurrent race against
the real database, not a simulation, proven by inspecting the actual
wall-clock overlap of each thread's own call window below.
"""
from __future__ import annotations

import datetime
import threading
import time
from unittest import mock

import pytest
import sqlalchemy as sa

from ai.invocation import STALE_RECOVERY_ERROR_CODE, STALE_RUNNING_THRESHOLD_SECONDS
from core import identity
from core.audit import InMemoryAuditRepository
from core.errors import (
    ActiveInvocationConflictError,
    InvalidStateTransitionError,
    NotFoundError,
    ValidationError,
)
from core.timestamps import utc_now
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
# CD-6 reliability delta — TIMED_OUT / CANCELLED durably persist
# (PID §100.14/§100.16)
# ---------------------------------------------------------------------


def test_running_can_time_out_and_persists_durably():
    created = _create_operator()
    PostgresAIInvocationRepository().transition_status(created.ai_invocation_id, "RUNNING")
    timed_out = PostgresAIInvocationRepository().transition_status(
        created.ai_invocation_id, "TIMED_OUT", error_code="CLAUDE_CODE_TIMEOUT"
    )
    assert timed_out.status == "TIMED_OUT"
    assert timed_out.completed_at is not None

    reread = PostgresAIInvocationRepository().get_invocation(created.ai_invocation_id)
    assert reread.status == "TIMED_OUT"
    assert reread.error_code == "CLAUDE_CODE_TIMEOUT"


def test_requested_cannot_time_out_directly():
    created = _create_operator()
    with pytest.raises(InvalidStateTransitionError):
        PostgresAIInvocationRepository().transition_status(created.ai_invocation_id, "TIMED_OUT")


def test_requested_can_be_cancelled_and_persists_durably():
    created = _create_operator()
    cancelled = PostgresAIInvocationRepository().transition_status(created.ai_invocation_id, "CANCELLED")
    assert cancelled.status == "CANCELLED"

    reread = PostgresAIInvocationRepository().get_invocation(created.ai_invocation_id)
    assert reread.status == "CANCELLED"


def test_running_can_be_cancelled_and_persists_durably():
    created = _create_operator()
    PostgresAIInvocationRepository().transition_status(created.ai_invocation_id, "RUNNING")
    cancelled = PostgresAIInvocationRepository().transition_status(created.ai_invocation_id, "CANCELLED")
    assert cancelled.status == "CANCELLED"


@pytest.mark.parametrize("terminal_status", ["SUCCEEDED", "FAILED", "REJECTED", "TIMED_OUT"])
def test_no_other_terminal_state_can_be_cancelled(terminal_status):
    created = _create_operator()
    if terminal_status == "SUCCEEDED":
        PostgresAIInvocationRepository().transition_status(created.ai_invocation_id, "RUNNING")
        PostgresAIInvocationRepository().transition_status(created.ai_invocation_id, "SUCCEEDED")
    elif terminal_status == "TIMED_OUT":
        PostgresAIInvocationRepository().transition_status(created.ai_invocation_id, "RUNNING")
        PostgresAIInvocationRepository().transition_status(created.ai_invocation_id, "TIMED_OUT")
    else:
        PostgresAIInvocationRepository().transition_status(created.ai_invocation_id, terminal_status)
    with pytest.raises(InvalidStateTransitionError):
        PostgresAIInvocationRepository().transition_status(created.ai_invocation_id, "CANCELLED")


# ---------------------------------------------------------------------
# CD-6 reliability delta — conversation_id primary reference precedence
# (PID §98/§100), at the database level (the REAL generated column)
# ---------------------------------------------------------------------


def test_generated_column_derives_conversation_id_when_nothing_else_present():
    conversation_id = identity.generate_id()
    created = PostgresAIInvocationRepository().create_invocation(
        task_id="ASK_BAGMAN", task_version=1, role="OPERATOR", provider="ANTHROPIC",
        capability_alias=None,
        input_references={"message": "hi", "conversation_id": conversation_id},
        actor_type="USER", actor_id="matt",
    )
    with get_engine().connect() as conn:
        value = conn.execute(
            sa.select(AIInvocationRow.primary_input_reference).where(
                AIInvocationRow.ai_invocation_id == created.ai_invocation_id
            )
        ).scalar_one()
    assert value == conversation_id


def test_generated_column_prefers_evidence_id_over_conversation_id():
    evidence_id = identity.generate_id()
    conversation_id = identity.generate_id()
    created = PostgresAIInvocationRepository().create_invocation(
        task_id="ASK_BAGMAN", task_version=1, role="OPERATOR", provider="ANTHROPIC",
        capability_alias=None,
        input_references={"message": "hi", "evidence_id": evidence_id, "conversation_id": conversation_id},
        actor_type="USER", actor_id="matt",
    )
    with get_engine().connect() as conn:
        value = conn.execute(
            sa.select(AIInvocationRow.primary_input_reference).where(
                AIInvocationRow.ai_invocation_id == created.ai_invocation_id
            )
        ).scalar_one()
    assert value == evidence_id


def test_two_contextless_turns_in_the_same_conversation_conflict_via_the_real_constraint():
    conversation_id = identity.generate_id()
    PostgresAIInvocationRepository().create_invocation(
        task_id="ASK_BAGMAN", task_version=1, role="OPERATOR", provider="ANTHROPIC",
        capability_alias=None,
        input_references={"message": "first", "conversation_id": conversation_id},
        actor_type="USER", actor_id="matt",
    )
    with pytest.raises(ActiveInvocationConflictError):
        PostgresAIInvocationRepository().create_invocation(
            task_id="ASK_BAGMAN", task_version=1, role="OPERATOR", provider="ANTHROPIC",
            capability_alias=None,
            input_references={"message": "second", "conversation_id": conversation_id},
            actor_type="USER", actor_id="matt",
        )


def test_two_contextless_turns_in_different_conversations_do_not_conflict():
    first = PostgresAIInvocationRepository().create_invocation(
        task_id="ASK_BAGMAN", task_version=1, role="OPERATOR", provider="ANTHROPIC",
        capability_alias=None,
        input_references={"message": "first", "conversation_id": identity.generate_id()},
        actor_type="USER", actor_id="matt",
    )
    second = PostgresAIInvocationRepository().create_invocation(
        task_id="ASK_BAGMAN", task_version=1, role="OPERATOR", provider="ANTHROPIC",
        capability_alias=None,
        input_references={"message": "second", "conversation_id": identity.generate_id()},
        actor_type="USER", actor_id="matt",
    )
    assert first.ai_invocation_id != second.ai_invocation_id


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


# ---------------------------------------------------------------------
# CD-5 WI-5 — genuine real-threaded race against the real database (see
# module docstring for why real threads talking to a real remote
# PostgreSQL genuinely interleave here, unlike CD-4's own single-worker
# HTTP-layer concurrency proof).
# ---------------------------------------------------------------------


def test_genuinely_concurrent_threads_racing_the_same_subject_resolve_to_exactly_one_active_invocation():
    evidence_id = identity.generate_id()
    n_workers = 8
    barrier = threading.Barrier(n_workers)

    results: list[dict] = [{} for _ in range(n_workers)]

    def _worker(index: int) -> None:
        # Each thread gets its OWN repository/engine — never shared —
        # so this is genuinely n independent callers, not n threads
        # sharing one connection (which would prove nothing about a
        # real multi-caller race).
        repo = PostgresAIInvocationRepository()
        barrier.wait()  # all n_workers threads attempt create_invocation as close to simultaneously as possible
        start = time.monotonic()
        try:
            invocation = _create_background(repo, evidence_id, task_id="DOCUMENT_TYPE_PROPOSAL")
            results[index] = {
                "outcome": "created",
                "ai_invocation_id": invocation.ai_invocation_id,
                "start": start,
                "end": time.monotonic(),
            }
        except ActiveInvocationConflictError:
            results[index] = {"outcome": "conflict", "start": start, "end": time.monotonic()}

    threads = [threading.Thread(target=_worker, args=(i,)) for i in range(n_workers)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=30)

    # Every thread must have finished and reported an outcome — none
    # silently hung or crashed unhandled.
    assert all(r for r in results), f"one or more worker threads did not complete: {results}"

    created = [r for r in results if r["outcome"] == "created"]
    conflicted = [r for r in results if r["outcome"] == "conflict"]

    # This is the actual proof this test exists for: EXACTLY one winner,
    # never zero (a bug that let the constraint block every attempt) and
    # never more than one (a bug that let a duplicate active row
    # through) — resolved by the real database constraint
    # (`uq_ai_invocations_active_subject`), not merely by an
    # application-level pre-check that could itself race.
    assert len(created) == 1, (
        f"expected exactly ONE winning create_invocation call for the same subject under a genuine "
        f"concurrent race, got {len(created)}: {results}"
    )
    assert len(conflicted) == n_workers - 1, f"expected every other thread to receive a real conflict: {results}"

    # Confirm genuine wall-clock overlap actually occurred — i.e. this
    # was a real race, not an accidental serialisation where thread 2
    # never even started until thread 1 had already finished (which
    # would make the "conflict" outcomes trivial/meaningless).
    windows = [(r["start"], r["end"]) for r in results]
    overlap_found = any(
        a_start < b_end and b_start < a_end
        for i, (a_start, a_end) in enumerate(windows)
        for j, (b_start, b_end) in enumerate(windows)
        if i < j
    )
    assert overlap_found, f"no genuine wall-clock overlap detected between worker call windows: {windows}"

    # Exactly one row exists for this subject at the database level —
    # no orphaned duplicate, no silently-vanished row.
    assert _invocation_count_for_subject("DOCUMENT_TYPE_PROPOSAL", 1, evidence_id) == 1

    winner = PostgresAIInvocationRepository().get_invocation(created[0]["ai_invocation_id"])
    assert winner.status == "REQUESTED"


# ---------------------------------------------------------------------
# CD-6 reliability delta — bounded, deterministic stale-RUNNING
# recovery backstop (PID §100.14/§100.16), against the REAL database.
# ---------------------------------------------------------------------


def _force_started_at(ai_invocation_id: str, started_at: datetime.datetime) -> None:
    """Test-only: directly overwrite a row's `started_at` via a real,
    safe, targeted UPDATE — the deterministic way this suite proves
    staleness without ever actually waiting `STALE_RUNNING_THRESHOLD_SECONDS`
    for real (per the PID's own "artificially old started_at, not by
    actually waiting" instruction)."""
    with get_engine().begin() as conn:
        conn.execute(
            sa.update(AIInvocationRow)
            .where(AIInvocationRow.ai_invocation_id == ai_invocation_id)
            .values(started_at=started_at)
        )


def test_create_invocation_recovers_a_stale_running_row_then_succeeds():
    audit = InMemoryAuditRepository()
    repo = PostgresAIInvocationRepository(audit_repository=audit)
    conversation_id = identity.generate_id()

    stuck = repo.create_invocation(
        task_id="ASK_BAGMAN", task_version=1, role="OPERATOR", provider="ANTHROPIC",
        capability_alias=None,
        input_references={"message": "first", "conversation_id": conversation_id},
        actor_type="USER", actor_id="matt",
    )
    repo.transition_status(stuck.ai_invocation_id, "RUNNING")
    _force_started_at(
        stuck.ai_invocation_id, utc_now() - datetime.timedelta(seconds=STALE_RUNNING_THRESHOLD_SECONDS + 5)
    )

    # A brand-new invocation for the SAME subject succeeds instead of
    # raising ActiveInvocationConflictError — the real partial unique
    # index would otherwise refuse this insert outright, proving the
    # stale row was genuinely transitioned to a terminal state FIRST
    # (not merely ignored) before this insert was even attempted.
    new = repo.create_invocation(
        task_id="ASK_BAGMAN", task_version=1, role="OPERATOR", provider="ANTHROPIC",
        capability_alias=None,
        input_references={"message": "second", "conversation_id": conversation_id},
        actor_type="USER", actor_id="matt",
    )
    assert new.ai_invocation_id != stuck.ai_invocation_id
    assert new.status == "REQUESTED"

    recovered = PostgresAIInvocationRepository().get_invocation(stuck.ai_invocation_id)
    assert recovered.status == "TIMED_OUT"
    assert recovered.error_code == STALE_RECOVERY_ERROR_CODE
    assert recovered.completed_at is not None

    events = audit.list_by_subject("AIInvocation", stuck.ai_invocation_id)
    assert [e.event_type for e in events] == ["AI_INVOCATION_STALE_RECOVERED"]

    # Exactly two rows for this subject now — the recovered original
    # plus the new one — never a silent delete of the old row.
    assert _invocation_count_for_subject("ASK_BAGMAN", 1, conversation_id) == 2


def test_find_active_invocation_recovers_a_stale_row_and_reports_none():
    audit = InMemoryAuditRepository()
    repo = PostgresAIInvocationRepository(audit_repository=audit)
    conversation_id = identity.generate_id()

    stuck = repo.create_invocation(
        task_id="ASK_BAGMAN", task_version=1, role="OPERATOR", provider="ANTHROPIC",
        capability_alias=None,
        input_references={"message": "first", "conversation_id": conversation_id},
        actor_type="USER", actor_id="matt",
    )
    repo.transition_status(stuck.ai_invocation_id, "RUNNING")
    _force_started_at(
        stuck.ai_invocation_id, utc_now() - datetime.timedelta(seconds=STALE_RUNNING_THRESHOLD_SECONDS + 5)
    )

    found = repo.find_active_invocation(
        task_id="ASK_BAGMAN", task_version=1, primary_input_reference=conversation_id
    )
    assert found is None

    recovered = PostgresAIInvocationRepository().get_invocation(stuck.ai_invocation_id)
    assert recovered.status == "TIMED_OUT"
    assert recovered.error_code == STALE_RECOVERY_ERROR_CODE

    events = audit.list_by_subject("AIInvocation", stuck.ai_invocation_id)
    assert [e.event_type for e in events] == ["AI_INVOCATION_STALE_RECOVERED"]


def test_a_genuinely_recent_running_row_is_not_recovered_and_still_blocks():
    repo = PostgresAIInvocationRepository()
    conversation_id = identity.generate_id()

    stuck = repo.create_invocation(
        task_id="ASK_BAGMAN", task_version=1, role="OPERATOR", provider="ANTHROPIC",
        capability_alias=None,
        input_references={"message": "first", "conversation_id": conversation_id},
        actor_type="USER", actor_id="matt",
    )
    repo.transition_status(stuck.ai_invocation_id, "RUNNING")

    # No time manipulation — this row is genuinely fresh.
    found = repo.find_active_invocation(
        task_id="ASK_BAGMAN", task_version=1, primary_input_reference=conversation_id
    )
    assert found is not None
    assert found.status == "RUNNING"

    with pytest.raises(ActiveInvocationConflictError):
        repo.create_invocation(
            task_id="ASK_BAGMAN", task_version=1, role="OPERATOR", provider="ANTHROPIC",
            capability_alias=None,
            input_references={"message": "second", "conversation_id": conversation_id},
            actor_type="USER", actor_id="matt",
        )

    still_running = PostgresAIInvocationRepository().get_invocation(stuck.ai_invocation_id)
    assert still_running.status == "RUNNING"
    assert _invocation_count_for_subject("ASK_BAGMAN", 1, conversation_id) == 1


def test_stale_recovery_default_audit_repository_shares_the_same_database():
    """No `audit_repository` explicitly supplied — the default
    (`PostgresAuditRepository(engine)`, see the repository's own
    `__init__`) still durably records the stale-recovery event, in the
    SAME `audit_events` table every other production audit event
    writes to."""
    repo = PostgresAIInvocationRepository()
    conversation_id = identity.generate_id()

    stuck = repo.create_invocation(
        task_id="ASK_BAGMAN", task_version=1, role="OPERATOR", provider="ANTHROPIC",
        capability_alias=None,
        input_references={"message": "first", "conversation_id": conversation_id},
        actor_type="USER", actor_id="matt",
    )
    repo.transition_status(stuck.ai_invocation_id, "RUNNING")
    _force_started_at(
        stuck.ai_invocation_id, utc_now() - datetime.timedelta(seconds=STALE_RUNNING_THRESHOLD_SECONDS + 5)
    )

    repo.create_invocation(
        task_id="ASK_BAGMAN", task_version=1, role="OPERATOR", provider="ANTHROPIC",
        capability_alias=None,
        input_references={"message": "second", "conversation_id": conversation_id},
        actor_type="USER", actor_id="matt",
    )

    from persistence.postgres.audit_repository import PostgresAuditRepository

    events = PostgresAuditRepository(get_engine()).list_by_subject("AIInvocation", stuck.ai_invocation_id)
    assert [e.event_type for e in events] == ["AI_INVOCATION_STALE_RECOVERED"]
