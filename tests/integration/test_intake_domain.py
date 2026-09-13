"""Domain-behaviour tests for `services.evidence.intake.intake` (CD-4
WI-1, PID §5-8/§25/§53/§68).

Exercises `InMemoryIntakeRepository` and the pure `transition()`
state-machine helper directly — there is no `core.api.BagmanCanonicalAPI`
integration for intake yet (that composition is WI-3's "governed
intake API & canonical integration" scope, out of WI-1's bounds), so
these tests go straight at `services.evidence.intake.intake`, the way
`tests/contract/` goes straight at raw contracts and
`tests/integration/test_domain_and_lineage.py` goes through the facade
for the domains that already have one.

`source_id`/`entity_hint`/`evidence_id` values used here are
well-formed synthetic canonical identifiers (`core.identity.generate_id()`)
— `InMemoryIntakeRepository`, like every other CD-2/CD-3 in-memory
reference repository, does not itself enforce cross-object referential
integrity (that is the real PostgreSQL foreign key's job, proven in
`tests/persistence/test_intake_repository.py`).
"""
from __future__ import annotations

import pytest

from core import identity
from core.errors import (
    IdempotencyConflictError,
    InvalidStateTransitionError,
    ValidationError,
)
from services.evidence.intake.intake import (
    ALLOWED_TRANSITIONS,
    STATUSES,
    TERMINAL_STATUSES,
    InMemoryIntakeRepository,
    transition,
)

TERMINAL = TERMINAL_STATUSES


@pytest.fixture
def repo() -> InMemoryIntakeRepository:
    return InMemoryIntakeRepository()


@pytest.fixture
def source_id() -> str:
    return identity.generate_id()


# ---------------------------------------------------------------------
# Creation
# ---------------------------------------------------------------------


def test_create_intake_record_starts_in_received_with_no_completed_at(repo, source_id):
    record = repo.create_intake_record(source_id=source_id, entity_hint="UNRESOLVED")
    assert record.status == "RECEIVED"
    assert record.completed_at is None
    assert record.evidence_id is None
    assert record.correlation_id is not None  # generated automatically


def test_create_intake_record_without_correlation_id_generates_a_fresh_one(repo, source_id):
    a = repo.create_intake_record(source_id=source_id)
    b = repo.create_intake_record(source_id=source_id)
    assert a.correlation_id != b.correlation_id
    assert a.intake_id != b.intake_id


def test_create_intake_record_honours_a_supplied_correlation_id(repo, source_id):
    correlation_id = identity.generate_id()
    record = repo.create_intake_record(source_id=source_id, correlation_id=correlation_id)
    assert record.correlation_id == correlation_id


def test_get_intake_record_round_trips(repo, source_id):
    created = repo.create_intake_record(source_id=source_id)
    fetched = repo.get_intake_record(created.intake_id)
    assert fetched == created


# ---------------------------------------------------------------------
# State machine — valid paths
# ---------------------------------------------------------------------


def test_full_success_path_stamps_completed_at_only_at_registered(repo, source_id):
    record = repo.create_intake_record(source_id=source_id)

    validating = repo.transition_status(record.intake_id, "VALIDATING")
    assert validating.status == "VALIDATING"
    assert validating.completed_at is None

    accepted = repo.transition_status(
        validating.intake_id,
        "ACCEPTED",
        detected_mime_type="application/pdf",
        size_bytes=1024,
        content_hash={"algorithm": "SHA-256", "value": "a" * 64},
    )
    assert accepted.status == "ACCEPTED"
    assert accepted.completed_at is None
    assert accepted.detected_mime_type == "application/pdf"

    evidence_id = identity.generate_id()
    registered = repo.transition_status(accepted.intake_id, "REGISTERED", evidence_id=evidence_id)
    assert registered.status == "REGISTERED"
    assert registered.completed_at is not None
    assert registered.evidence_id == evidence_id


def test_quarantine_path_from_validating(repo, source_id):
    record = repo.create_intake_record(source_id=source_id)
    repo.transition_status(record.intake_id, "VALIDATING")
    quarantined = repo.transition_status(
        record.intake_id, "QUARANTINED", quarantine_reason="unsupported executable content"
    )
    assert quarantined.status == "QUARANTINED"
    assert quarantined.completed_at is not None
    assert quarantined.quarantine_reason == "unsupported executable content"
    assert quarantined.evidence_id is None


def test_reject_path_directly_from_received(repo, source_id):
    record = repo.create_intake_record(source_id=source_id)
    rejected = repo.transition_status(record.intake_id, "REJECTED", failure_code="FILE_TOO_LARGE")
    assert rejected.status == "REJECTED"
    assert rejected.completed_at is not None
    assert rejected.failure_code == "FILE_TOO_LARGE"


@pytest.mark.parametrize("from_status", ["RECEIVED", "VALIDATING", "ACCEPTED"])
def test_failed_path_from_every_non_terminal_state(repo, source_id, from_status):
    record = repo.create_intake_record(source_id=source_id)
    if from_status in ("VALIDATING", "ACCEPTED"):
        repo.transition_status(record.intake_id, "VALIDATING")
    if from_status == "ACCEPTED":
        repo.transition_status(record.intake_id, "ACCEPTED")

    failed = repo.transition_status(record.intake_id, "FAILED", failure_code="INTAKE_PERSISTENCE_ERROR")
    assert failed.status == "FAILED"
    assert failed.completed_at is not None


# ---------------------------------------------------------------------
# State machine — invalid paths
# ---------------------------------------------------------------------


def test_skipping_validating_straight_to_accepted_is_rejected(repo, source_id):
    record = repo.create_intake_record(source_id=source_id)
    with pytest.raises(InvalidStateTransitionError):
        repo.transition_status(record.intake_id, "ACCEPTED")


def test_skipping_straight_to_registered_is_rejected(repo, source_id):
    record = repo.create_intake_record(source_id=source_id)
    with pytest.raises(InvalidStateTransitionError):
        repo.transition_status(record.intake_id, "REGISTERED", evidence_id=identity.generate_id())


@pytest.mark.parametrize("terminal_status", sorted(TERMINAL))
def test_every_terminal_state_rejects_any_further_transition(repo, source_id, terminal_status):
    record = repo.create_intake_record(source_id=source_id)
    repo.transition_status(record.intake_id, "VALIDATING")

    # Drive the record into the terminal state under test.
    if terminal_status == "REGISTERED":
        repo.transition_status(record.intake_id, "ACCEPTED")
        repo.transition_status(record.intake_id, "REGISTERED", evidence_id=identity.generate_id())
    elif terminal_status == "QUARANTINED":
        repo.transition_status(record.intake_id, "QUARANTINED", quarantine_reason="synthetic")
    elif terminal_status == "REJECTED":
        repo.transition_status(record.intake_id, "REJECTED", failure_code="synthetic")
    elif terminal_status == "FAILED":
        repo.transition_status(record.intake_id, "FAILED", failure_code="synthetic")

    for attempted_next in sorted(STATUSES):
        with pytest.raises(InvalidStateTransitionError):
            repo.transition_status(record.intake_id, attempted_next)


def test_allowed_transitions_table_matches_pid_section_7_exactly():
    assert ALLOWED_TRANSITIONS == {
        "RECEIVED": frozenset({"VALIDATING", "REJECTED", "FAILED"}),
        "VALIDATING": frozenset({"QUARANTINED", "ACCEPTED", "REJECTED", "FAILED"}),
        "ACCEPTED": frozenset({"REGISTERED", "FAILED"}),
        "QUARANTINED": frozenset(),
        "REJECTED": frozenset(),
        "REGISTERED": frozenset(),
        "FAILED": frozenset(),
    }


def test_transition_rejects_completed_at_supplied_directly(repo, source_id):
    record = repo.create_intake_record(source_id=source_id)
    with pytest.raises(ValidationError):
        transition(record, "VALIDATING", completed_at=None)


def test_transition_rejects_unknown_field_name(repo, source_id):
    record = repo.create_intake_record(source_id=source_id)
    with pytest.raises(ValidationError):
        transition(record, "VALIDATING", not_a_real_field="oops")


# ---------------------------------------------------------------------
# Idempotency (PID §25/§53) — see services/evidence/intake/intake.py's
# module docstring for the exact "same identifying request" doctrine.
# ---------------------------------------------------------------------


def test_bare_retry_with_same_key_and_same_identifying_content_is_idempotent(repo, source_id):
    key = "synthetic-idempotency-key-0001"  # gitleaks:allow
    first = repo.create_intake_record(
        source_id=source_id,
        entity_hint="UNRESOLVED",
        original_filename="invoice.pdf",
        reported_mime_type="application/pdf",
        idempotency_key=key,
    )
    second = repo.create_intake_record(
        source_id=source_id,
        entity_hint="UNRESOLVED",
        original_filename="invoice.pdf",
        reported_mime_type="application/pdf",
        idempotency_key=key,
    )
    assert first.intake_id == second.intake_id
    assert len(repo.list_intake_records()) == 1  # no duplicate created


def test_bare_retry_after_the_record_progressed_still_replays_the_same_outcome(repo, source_id):
    """PID §25: 'the same valid key replay must resolve to the same
    IntakeRecord and final result' — even once the first attempt has
    already moved past RECEIVED."""
    key = "synthetic-idempotency-key-progressed"
    first = repo.create_intake_record(source_id=source_id, idempotency_key=key)
    repo.transition_status(first.intake_id, "VALIDATING")
    quarantined = repo.transition_status(first.intake_id, "QUARANTINED", quarantine_reason="synthetic")

    replay = repo.create_intake_record(source_id=source_id, idempotency_key=key)
    assert replay.intake_id == quarantined.intake_id
    assert replay.status == "QUARANTINED"


def test_reused_key_with_different_identifying_content_raises_conflict(repo, source_id):
    key = "synthetic-idempotency-key-conflict"
    repo.create_intake_record(
        source_id=source_id, original_filename="invoice-a.pdf", idempotency_key=key
    )
    with pytest.raises(IdempotencyConflictError):
        repo.create_intake_record(
            source_id=source_id, original_filename="invoice-b.pdf", idempotency_key=key
        )


def test_reused_key_with_different_source_raises_conflict(repo, source_id):
    key = "synthetic-idempotency-key-conflict-source"
    other_source_id = identity.generate_id()
    repo.create_intake_record(source_id=source_id, idempotency_key=key)
    with pytest.raises(IdempotencyConflictError):
        repo.create_intake_record(source_id=other_source_id, idempotency_key=key)


def test_no_idempotency_key_never_collides(repo, source_id):
    """Many records may have no idempotency key at all — they must
    never be treated as conflicting with each other or with a keyed
    record (PID §25's 'a manual upload may not always supply one')."""
    a = repo.create_intake_record(source_id=source_id)
    b = repo.create_intake_record(source_id=source_id)
    assert a.intake_id != b.intake_id
    assert len(repo.list_intake_records()) == 2


def test_find_by_idempotency_key_returns_none_when_absent(repo):
    assert repo.find_by_idempotency_key("never-used-key") is None


def test_find_by_idempotency_key_finds_the_created_record(repo, source_id):
    key = "synthetic-idempotency-key-lookup"
    created = repo.create_intake_record(source_id=source_id, idempotency_key=key)
    found = repo.find_by_idempotency_key(key)
    assert found is not None
    assert found.intake_id == created.intake_id


def test_list_intake_records_returns_every_record(repo, source_id):
    first = repo.create_intake_record(source_id=source_id)
    second = repo.create_intake_record(source_id=source_id)
    ids = {r.intake_id for r in repo.list_intake_records()}
    assert ids == {first.intake_id, second.intake_id}
