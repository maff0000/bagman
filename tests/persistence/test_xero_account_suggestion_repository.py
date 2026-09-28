"""`xero/account-suggestion-producer` WO — PostgreSQL persistence proofs
for `persistence/postgres/xero_account_suggestion_repository.py` against
a REAL, disposable PostgreSQL container. Mirrors
`tests/persistence/test_xero_repository.py`/
`tests/persistence/test_evidence_classification_repository.py`'s own
style and rigor for the closest structural precedents.
"""
from __future__ import annotations

import hashlib
from datetime import datetime, timezone

import pytest
from sqlalchemy.exc import IntegrityError

from core import identity
from core.errors import ConflictError, NotFoundError
from persistence.postgres.ai_invocation_repository import PostgresAIInvocationRepository
from persistence.postgres.entity_repository import PostgresEntityRepository
from persistence.postgres.evidence_repository import PostgresEvidenceRepository
from persistence.postgres.external_reference_repository import PostgresExternalReferenceRepository
from persistence.postgres.session import get_engine, session_scope
from persistence.postgres.source_repository import PostgresSourceRepository
from persistence.postgres.xero_account_suggestion_models import (
    XeroAccountAssignmentRow,
    XeroAccountSuggestionRow,
)
from persistence.postgres.xero_account_suggestion_repository import (
    PostgresXeroAccountAssignmentRepository,
    PostgresXeroAccountSuggestionRepository,
)
from services.xero.account_assignment import SOURCE_AI_ACCEPTED, SOURCE_OPERATOR_SELECTED


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _fabricated_content_hash(tag: str) -> str:
    return hashlib.sha256(tag.encode("utf-8")).hexdigest()


def _make_entity(**overrides) -> str:
    kwargs = dict(
        entity_type="COMPANY",
        canonical_name=f"TEST_ENTITY_{identity.generate_id().replace('-', '').upper()}",
        display_name="Test Entity",
        status="ACTIVE",
    )
    kwargs.update(overrides)
    entity = PostgresEntityRepository().register_entity(**kwargs)
    return entity.entity_id


def _make_evidence(entity_id: str | None = None) -> str:
    source = PostgresSourceRepository().register_source(
        source_type="MANUAL_UPLOAD", provider="INTERNAL", status="ACTIVE"
    )
    now = _utc_now()
    evidence = PostgresEvidenceRepository(PostgresExternalReferenceRepository()).register_evidence(
        entity_id=entity_id, evidence_type="INVOICE", source_id=source.source_id, observed_at=now,
        received_at=now, content_hash=_fabricated_content_hash(identity.generate_id()),
        mime_type="text/plain", size_bytes=64,
    )
    return evidence.evidence_id


def _make_succeeded_invocation(evidence_id: str) -> str:
    repo = PostgresAIInvocationRepository()
    created = repo.create_invocation(
        task_id="XERO_ACCOUNT_SUGGESTION", task_version=1, role="BACKGROUND", provider="LITELLM",
        capability_alias="bagman-fast", input_references={"evidence_id": evidence_id},
        actor_type="SYSTEM", actor_id="bagman-test-harness",
    )
    repo.transition_status(created.ai_invocation_id, "RUNNING")
    repo.transition_status(created.ai_invocation_id, "SUCCEEDED")
    return created.ai_invocation_id


def _setup(fresh_engine=None):
    entity_id = _make_entity()
    evidence_id = _make_evidence(entity_id)
    ai_invocation_id = _make_succeeded_invocation(evidence_id)
    return entity_id, evidence_id, ai_invocation_id


# ---------------------------------------------------------------------
# XeroAccountSuggestion — creation, round trip, provenance
# ---------------------------------------------------------------------


def test_suggestion_persists_and_round_trips_with_full_provenance(fresh_engine):
    entity_id, evidence_id, ai_invocation_id = _setup()
    repo = PostgresXeroAccountSuggestionRepository()

    created = repo.create_suggestion(
        evidence_id=evidence_id, entity_id=entity_id, tenant_id="tenant-abc",
        suggested_account_id="XERO-ACC-1", confidence=0.87, signals=["matches invoice wording"],
        ai_invocation_id=ai_invocation_id,
    )
    assert created.status == "REVIEW_REQUIRED"

    fresh_repo = PostgresXeroAccountSuggestionRepository(engine=fresh_engine)
    fetched = fresh_repo.get_suggestion(created.suggestion_id)
    assert fetched.evidence_id == evidence_id
    assert fetched.entity_id == entity_id
    assert fetched.tenant_id == "tenant-abc"
    assert fetched.suggested_account_id == "XERO-ACC-1"
    assert fetched.confidence == pytest.approx(0.87)
    assert fetched.signals == ("matches invoice wording",)
    assert fetched.ai_invocation_id == ai_invocation_id

    by_evidence = fresh_repo.get_by_evidence(evidence_id)
    assert by_evidence is not None
    assert by_evidence.suggestion_id == created.suggestion_id


def test_get_suggestion_raises_not_found_for_unknown_id():
    with pytest.raises(NotFoundError):
        PostgresXeroAccountSuggestionRepository().get_suggestion(identity.generate_id())


def test_get_by_evidence_returns_none_when_no_suggestion_exists():
    _, evidence_id, _ = _setup()
    assert PostgresXeroAccountSuggestionRepository().get_by_evidence(evidence_id) is None


def test_suggestion_evidence_id_foreign_key_is_enforced_at_the_database_level():
    """The suggestion's `evidence_id` FK is a REAL constraint, proven
    directly at the ORM/table level — bypassing the repository's own
    creation path entirely — so this cannot pass merely because the
    repository happens to check first."""
    engine = get_engine()
    with pytest.raises(IntegrityError):
        with session_scope(engine) as session:
            session.add(
                XeroAccountSuggestionRow(
                    suggestion_id=identity.generate_id(),
                    evidence_id=identity.generate_id(),  # does not exist
                    entity_id=_make_entity(),
                    tenant_id="tenant-x",
                    suggested_account_id="ACC-1",
                    confidence=None,
                    signals=[],
                    ai_invocation_id=_make_succeeded_invocation(_make_evidence()),
                    status="REVIEW_REQUIRED",
                    created_at=_utc_now(),
                )
            )


def test_suggestion_evidence_id_uniqueness_is_enforced_at_the_database_level_not_only_in_application_code():
    """CORRECTED (post-merge concurrency finding, migration
    `f1a2b3c4d5e6`): this table was ORIGINALLY PK-only, permitting two
    rows per `evidence_id` — a real, disposable-PostgreSQL concurrency
    test (`tests/persistence/test_xero_account_suggestion_concurrency.py`)
    proved that gap reachable under genuine concurrency, not merely
    theoretical. A real unique constraint now backs "at most one
    suggestion per evidence_id", mirroring
    `test_assignment_evidence_id_uniqueness_is_enforced_at_the_database_level_not_only_in_application_code`'s
    own proof shape exactly: the application-level pre-check is
    bypassed by writing the SECOND row directly via a raw session, so
    only the real database constraint — never `create_suggestion`'s own
    pre-check — is what is actually proven here."""
    entity_id, evidence_id, ai_invocation_id = _setup()
    repo = PostgresXeroAccountSuggestionRepository()
    first = repo.create_suggestion(
        evidence_id=evidence_id, entity_id=entity_id, tenant_id="tenant-a",
        suggested_account_id="ACC-1", confidence=0.5, signals=[], ai_invocation_id=ai_invocation_id,
    )

    with pytest.raises(IntegrityError):
        with session_scope(get_engine()) as session:
            session.add(
                XeroAccountSuggestionRow(
                    suggestion_id=identity.generate_id(), evidence_id=evidence_id, entity_id=entity_id,
                    tenant_id="tenant-a", suggested_account_id="ACC-2", confidence=0.6, signals=[],
                    ai_invocation_id=ai_invocation_id, status="REVIEW_REQUIRED", created_at=_utc_now(),
                )
            )

    # The real, application-level contract: a second create_suggestion
    # call for the same evidence_id is idempotent-under-race, returning
    # the FIRST (winning) row unchanged — never raising, never creating
    # a second row.
    second = repo.create_suggestion(
        evidence_id=evidence_id, entity_id=entity_id, tenant_id="tenant-a",
        suggested_account_id="ACC-2", confidence=0.6, signals=[], ai_invocation_id=ai_invocation_id,
    )
    assert second.suggestion_id == first.suggestion_id
    assert second.suggested_account_id == "ACC-1"


# ---------------------------------------------------------------------
# XeroAccountAssignment — creation, round trip, write-once discipline
# ---------------------------------------------------------------------


def test_assignment_persists_and_round_trips(fresh_engine):
    entity_id, evidence_id, ai_invocation_id = _setup()
    suggestion = PostgresXeroAccountSuggestionRepository().create_suggestion(
        evidence_id=evidence_id, entity_id=entity_id, tenant_id="tenant-abc",
        suggested_account_id="ACC-1", confidence=0.9, signals=[], ai_invocation_id=ai_invocation_id,
    )
    repo = PostgresXeroAccountAssignmentRepository()
    created = repo.create_assignment(
        evidence_id=evidence_id, entity_id=entity_id, tenant_id="tenant-abc", account_id="ACC-1",
        source=SOURCE_AI_ACCEPTED, suggestion_id=suggestion.suggestion_id,
        assigned_by_actor_type="USER", assigned_by_actor_id="matt",
    )

    fresh_repo = PostgresXeroAccountAssignmentRepository(engine=fresh_engine)
    fetched = fresh_repo.get_assignment(created.assignment_id)
    assert fetched.evidence_id == evidence_id
    assert fetched.account_id == "ACC-1"
    assert fetched.source == SOURCE_AI_ACCEPTED
    assert fetched.suggestion_id == suggestion.suggestion_id

    by_evidence = fresh_repo.get_by_evidence(evidence_id)
    assert by_evidence is not None
    assert by_evidence.assignment_id == created.assignment_id


def test_assignment_same_account_id_replay_is_idempotent_no_second_row():
    entity_id, evidence_id, _ = _setup()
    repo = PostgresXeroAccountAssignmentRepository()
    first = repo.create_assignment(
        evidence_id=evidence_id, entity_id=entity_id, tenant_id="tenant-a", account_id="ACC-1",
        source=SOURCE_OPERATOR_SELECTED, suggestion_id=None,
        assigned_by_actor_type="USER", assigned_by_actor_id="matt",
    )
    second = repo.create_assignment(
        evidence_id=evidence_id, entity_id=entity_id, tenant_id="tenant-a", account_id="ACC-1",
        source=SOURCE_OPERATOR_SELECTED, suggestion_id=None,
        assigned_by_actor_type="USER", assigned_by_actor_id="matt",
    )
    assert first.assignment_id == second.assignment_id


def test_assignment_different_account_id_raises_conflict_no_silent_overwrite():
    entity_id, evidence_id, _ = _setup()
    repo = PostgresXeroAccountAssignmentRepository()
    repo.create_assignment(
        evidence_id=evidence_id, entity_id=entity_id, tenant_id="tenant-a", account_id="ACC-1",
        source=SOURCE_OPERATOR_SELECTED, suggestion_id=None,
        assigned_by_actor_type="USER", assigned_by_actor_id="matt",
    )
    with pytest.raises(ConflictError):
        repo.create_assignment(
            evidence_id=evidence_id, entity_id=entity_id, tenant_id="tenant-a", account_id="ACC-2",
            source=SOURCE_OPERATOR_SELECTED, suggestion_id=None,
            assigned_by_actor_type="USER", assigned_by_actor_id="matt",
        )


def test_assignment_evidence_id_uniqueness_is_enforced_at_the_database_level_not_only_in_application_code():
    """architect instruction: 'exactly one row per evidence_id (real
    unique constraint)'. Proven directly at the ORM/table level —
    bypassing `create_assignment`'s own resolve-or-conflict logic
    entirely."""
    entity_id, evidence_id, _ = _setup()
    engine = get_engine()
    with session_scope(engine) as session:
        session.add(
            XeroAccountAssignmentRow(
                assignment_id=identity.generate_id(), evidence_id=evidence_id, entity_id=entity_id,
                tenant_id="tenant-a", account_id="ACC-1", source=SOURCE_OPERATOR_SELECTED,
                suggestion_id=None, assigned_by_actor_type="USER", assigned_by_actor_id="matt",
                assigned_at=_utc_now(),
            )
        )
    with pytest.raises(IntegrityError):
        with session_scope(engine) as session:
            session.add(
                XeroAccountAssignmentRow(
                    assignment_id=identity.generate_id(), evidence_id=evidence_id, entity_id=entity_id,
                    tenant_id="tenant-a", account_id="ACC-2", source=SOURCE_OPERATOR_SELECTED,
                    suggestion_id=None, assigned_by_actor_type="USER", assigned_by_actor_id="matt",
                    assigned_at=_utc_now(),
                )
            )


def test_get_by_evidence_returns_none_when_no_assignment_exists():
    _, evidence_id, _ = _setup()
    assert PostgresXeroAccountAssignmentRepository().get_by_evidence(evidence_id) is None


def test_get_assignment_raises_not_found_for_unknown_id():
    with pytest.raises(NotFoundError):
        PostgresXeroAccountAssignmentRepository().get_assignment(identity.generate_id())
