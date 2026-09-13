"""PID §8/§25/§52/§53 'Intake persistence' proofs (CD-4 WI-1).

`content_hash` values here are fabricated SHA-256-shaped hex strings —
this work item does not integrate with the real object store; only the
shape needs to be contract-valid (same convention as
`tests/persistence/test_evidence_persistence.py`).

The "genuine duplicate-idempotency-key insert race" proof below
deliberately does NOT use real threads/true concurrency (that full
proof is WI-5's job, per the CD-4 WI-1 dispatch) — it simulates a race
between two repository instances by making the SECOND instance's
optimistic pre-check report "not seen yet" exactly once (via
`unittest.mock.patch.object`), forcing it down the same insert-then-
resolve-from-the-real-constraint-violation code path a genuine
concurrent caller would hit, deterministically and without flakiness.
"""
from __future__ import annotations

from datetime import datetime, timezone
from unittest import mock

import pytest
import sqlalchemy as sa
from sqlalchemy import func, select

from core import identity
from core.errors import (
    IdempotencyConflictError,
    InvalidStateTransitionError,
    NotFoundError,
    ValidationError,
)
from persistence.postgres.evidence_repository import PostgresEvidenceRepository
from persistence.postgres.external_reference_repository import PostgresExternalReferenceRepository
from persistence.postgres.intake_models import IntakeRecordRow
from persistence.postgres.intake_repository import PostgresIntakeRepository
from persistence.postgres.session import get_engine
from persistence.postgres.source_repository import PostgresSourceRepository


def _make_source():
    return PostgresSourceRepository().register_source(
        source_type="MANUAL_UPLOAD", provider="INTERNAL", status="ACTIVE"
    )


def _make_evidence(source_id: str):
    now = datetime.now(timezone.utc)
    return PostgresEvidenceRepository(PostgresExternalReferenceRepository()).register_evidence(
        entity_id=None,
        evidence_type="DOCUMENT",
        source_id=source_id,
        observed_at=now,
        received_at=now,
        content_hash="b" * 64,
        mime_type="application/pdf",
        size_bytes=10,
    )


def _intake_count_for_key(idempotency_key: str) -> int:
    with get_engine().connect() as conn:
        return conn.execute(
            select(func.count())
            .select_from(IntakeRecordRow)
            .where(IntakeRecordRow.idempotency_key == idempotency_key)
        ).scalar_one()


# ---------------------------------------------------------------------
# Create / get round trip
# ---------------------------------------------------------------------


def test_intake_record_persists_and_is_retrievable_via_a_fresh_repository_instance(fresh_engine):
    source = _make_source()
    repo = PostgresIntakeRepository()

    created = repo.create_intake_record(
        source_id=source.source_id,
        entity_hint="UNRESOLVED",
        original_filename="synthetic-invoice.pdf",
        reported_mime_type="application/pdf",
    )
    assert created.status == "RECEIVED"
    assert created.completed_at is None
    assert created.evidence_id is None

    fresh_repo = PostgresIntakeRepository(engine=fresh_engine)
    fetched = fresh_repo.get_intake_record(created.intake_id)
    assert fetched == created


def test_unknown_intake_id_raises_not_found():
    repo = PostgresIntakeRepository()
    with pytest.raises(NotFoundError):
        repo.get_intake_record(identity.generate_id())


def test_malformed_intake_id_raises_not_found_not_persistence_error():
    repo = PostgresIntakeRepository()
    with pytest.raises(NotFoundError):
        repo.get_intake_record("not-a-valid-uuid")


def test_malformed_intake_id_in_transition_status_raises_not_found():
    repo = PostgresIntakeRepository()
    with pytest.raises(NotFoundError):
        repo.transition_status("not-a-valid-uuid", "VALIDATING")


# ---------------------------------------------------------------------
# Foreign keys
# ---------------------------------------------------------------------


def test_bogus_source_id_is_rejected_by_the_real_foreign_key():
    repo = PostgresIntakeRepository()
    with pytest.raises(NotFoundError):
        repo.create_intake_record(source_id=identity.generate_id())


def test_malformed_source_id_is_rejected_as_a_validation_error_before_any_db_round_trip():
    """Unlike a LOOKUP (`get_intake_record`), where a malformed id can
    never correspond to an existing row and so is honestly translated
    to `NotFoundError`, a malformed `source_id` given to a CREATE call
    is simply bad caller input — it never even reaches the database:
    `core.contract_validation.validate_against_contract` already
    rejects it against `bagman.identifier.v1`'s UUID pattern before any
    row is constructed, exactly like every other `register_*` call in
    this codebase (e.g. `PostgresSourceRepository.register_source` with
    a malformed `source_type` raises the same way)."""
    repo = PostgresIntakeRepository()
    with pytest.raises(ValidationError):
        repo.create_intake_record(source_id="not-a-valid-uuid")


def test_bogus_evidence_id_on_registration_is_rejected_and_row_is_unchanged():
    source = _make_source()
    repo = PostgresIntakeRepository()
    record = repo.create_intake_record(source_id=source.source_id)
    repo.transition_status(record.intake_id, "VALIDATING")
    repo.transition_status(record.intake_id, "ACCEPTED")

    with pytest.raises(NotFoundError):
        repo.transition_status(record.intake_id, "REGISTERED", evidence_id=identity.generate_id())

    # The rejected transaction must not have left the row half-updated
    # (PID §55-style atomicity expectation, applied here to intake).
    unchanged = repo.get_intake_record(record.intake_id)
    assert unchanged.status == "ACCEPTED"
    assert unchanged.evidence_id is None


def test_registration_with_a_real_evidence_id_succeeds():
    source = _make_source()
    evidence = _make_evidence(source.source_id)
    repo = PostgresIntakeRepository()
    record = repo.create_intake_record(source_id=source.source_id)
    repo.transition_status(record.intake_id, "VALIDATING")
    repo.transition_status(record.intake_id, "ACCEPTED")

    registered = repo.transition_status(record.intake_id, "REGISTERED", evidence_id=evidence.evidence_id)
    assert registered.status == "REGISTERED"
    assert registered.evidence_id == evidence.evidence_id
    assert registered.completed_at is not None


# ---------------------------------------------------------------------
# State transitions persist durably
# ---------------------------------------------------------------------


def test_state_transition_persists_and_rejects_an_invalid_next_state(fresh_engine):
    source = _make_source()
    repo = PostgresIntakeRepository()
    record = repo.create_intake_record(source_id=source.source_id)

    repo.transition_status(record.intake_id, "VALIDATING")

    fresh_repo = PostgresIntakeRepository(engine=fresh_engine)
    fetched = fresh_repo.get_intake_record(record.intake_id)
    assert fetched.status == "VALIDATING"

    with pytest.raises(InvalidStateTransitionError):
        fresh_repo.transition_status(record.intake_id, "REGISTERED", evidence_id=identity.generate_id())

    # Rejected transition must not have changed persisted state.
    assert repo.get_intake_record(record.intake_id).status == "VALIDATING"


def test_quarantine_transition_persists_reason_and_completed_at():
    source = _make_source()
    repo = PostgresIntakeRepository()
    record = repo.create_intake_record(source_id=source.source_id)
    repo.transition_status(record.intake_id, "VALIDATING")

    quarantined = repo.transition_status(
        record.intake_id, "QUARANTINED", quarantine_reason="unsupported executable content"
    )
    assert quarantined.quarantine_reason == "unsupported executable content"
    assert quarantined.completed_at is not None

    refetched = PostgresIntakeRepository().get_intake_record(record.intake_id)
    assert refetched.quarantine_reason == "unsupported executable content"
    assert refetched.status == "QUARANTINED"


# ---------------------------------------------------------------------
# Idempotency — durable across a fresh repository instance ("restart")
# ---------------------------------------------------------------------


def test_bare_retry_of_same_idempotency_key_is_idempotent_via_a_brand_new_repository_instance():
    source = _make_source()
    key = "synthetic-idempotency-durable-0001"  # gitleaks:allow
    repo = PostgresIntakeRepository()

    first = repo.create_intake_record(
        source_id=source.source_id,
        original_filename="invoice.pdf",
        reported_mime_type="application/pdf",
        idempotency_key=key,
    )

    # Brand new repository instance — no Python-level cache anywhere in
    # PostgresIntakeRepository, so this is what actually proves
    # durability across a "process restart" (PID §52), not in-process
    # memoization.
    retry_repo = PostgresIntakeRepository()
    second = retry_repo.create_intake_record(
        source_id=source.source_id,
        original_filename="invoice.pdf",
        reported_mime_type="application/pdf",
        idempotency_key=key,
    )

    assert first.intake_id == second.intake_id
    assert _intake_count_for_key(key) == 1


def test_reused_key_with_different_identifying_content_raises_conflict():
    source = _make_source()
    key = "synthetic-idempotency-conflict-0001"
    repo = PostgresIntakeRepository()
    repo.create_intake_record(source_id=source.source_id, original_filename="invoice-a.pdf", idempotency_key=key)

    with pytest.raises(IdempotencyConflictError):
        PostgresIntakeRepository().create_intake_record(
            source_id=source.source_id, original_filename="invoice-b.pdf", idempotency_key=key
        )

    assert _intake_count_for_key(key) == 1  # no duplicate/second row created


def test_find_by_idempotency_key_returns_none_when_absent():
    assert PostgresIntakeRepository().find_by_idempotency_key("never-used-durable-key") is None


def test_find_by_idempotency_key_finds_the_persisted_record():
    source = _make_source()
    key = "synthetic-idempotency-lookup-0001"
    created = PostgresIntakeRepository().create_intake_record(source_id=source.source_id, idempotency_key=key)
    found = PostgresIntakeRepository().find_by_idempotency_key(key)
    assert found is not None
    assert found.intake_id == created.intake_id


def test_many_records_with_no_idempotency_key_never_collide():
    """The partial unique index is unique only where `idempotency_key
    IS NOT NULL` — many keyless records must coexist freely (PID §25)."""
    source = _make_source()
    repo = PostgresIntakeRepository()
    created = [repo.create_intake_record(source_id=source.source_id) for _ in range(5)]
    assert len({r.intake_id for r in created}) == 5


def test_partial_unique_index_exists_at_the_database_level():
    inspector = sa.inspect(get_engine())
    indexes = inspector.get_indexes("intake_records")
    match = next((ix for ix in indexes if ix["name"] == "uq_intake_records_idempotency_key"), None)
    assert match is not None, f"expected a partial unique index, found indexes: {indexes}"
    assert match["unique"] is True
    assert match["column_names"] == ["idempotency_key"]


# ---------------------------------------------------------------------
# Simulated insert race (two repository instances, not true concurrency
# — see module docstring; true concurrency proof is WI-5's job)
# ---------------------------------------------------------------------


def test_simulated_race_with_same_identifying_content_resolves_via_the_real_constraint_not_the_pre_check():
    source = _make_source()
    key = "synthetic-idempotency-race-replay-0001"
    winner_repo = PostgresIntakeRepository()
    winner = winner_repo.create_intake_record(
        source_id=source.source_id, original_filename="race.pdf", idempotency_key=key
    )

    racer_repo = PostgresIntakeRepository()
    real_find = racer_repo.find_by_idempotency_key
    call_count = {"n": 0}

    def _pre_check_misses_once(idempotency_key: str):
        call_count["n"] += 1
        if call_count["n"] == 1:
            return None  # simulate: racer's pre-check ran before winner committed
        return real_find(idempotency_key)

    with mock.patch.object(racer_repo, "find_by_idempotency_key", side_effect=_pre_check_misses_once):
        replay = racer_repo.create_intake_record(
            source_id=source.source_id, original_filename="race.pdf", idempotency_key=key
        )

    assert replay.intake_id == winner.intake_id  # same identifying content -> resolved to the real winner
    assert _intake_count_for_key(key) == 1  # the racer's insert attempt never left a second row
    assert call_count["n"] == 2  # proves the pre-check MISS was followed by a real re-read after the DB conflict


def test_simulated_race_with_different_identifying_content_raises_conflict_via_the_real_constraint():
    source = _make_source()
    key = "synthetic-idempotency-race-conflict-0001"
    winner_repo = PostgresIntakeRepository()
    winner_repo.create_intake_record(
        source_id=source.source_id, original_filename="race-a.pdf", idempotency_key=key
    )

    racer_repo = PostgresIntakeRepository()
    real_find = racer_repo.find_by_idempotency_key
    call_count = {"n": 0}

    def _pre_check_misses_once(idempotency_key: str):
        call_count["n"] += 1
        if call_count["n"] == 1:
            return None
        return real_find(idempotency_key)

    with mock.patch.object(racer_repo, "find_by_idempotency_key", side_effect=_pre_check_misses_once):
        with pytest.raises(IdempotencyConflictError):
            racer_repo.create_intake_record(
                source_id=source.source_id, original_filename="race-b.pdf", idempotency_key=key
            )

    assert _intake_count_for_key(key) == 1  # the racer's rejected insert left no orphan row
    assert call_count["n"] == 2


# ---------------------------------------------------------------------
# Ordering
# ---------------------------------------------------------------------


def test_list_intake_records_orders_received_at_desc():
    source = _make_source()
    repo = PostgresIntakeRepository()
    first = repo.create_intake_record(source_id=source.source_id)
    second = repo.create_intake_record(source_id=source.source_id)

    records = repo.list_intake_records()
    ids_in_order = [r.intake_id for r in records if r.intake_id in {first.intake_id, second.intake_id}]
    assert ids_in_order == [second.intake_id, first.intake_id]
