"""CD-6 Slice 5 WI-1 PostgreSQL persistence proofs —
`persistence/postgres/evidence_classification_repository.py` against a
REAL, disposable PostgreSQL container. Mirrors
`tests/persistence/test_mailbox_domain_rule_repository.py`'s own style,
and `tests/persistence/test_ai_invocation_repository.py`'s own
genuine-threaded-race proof for the concurrency test at the bottom of
this file (real `threading.Thread`s, each with their OWN repository/
engine, released simultaneously via a `threading.Barrier` — never a
single-worker simulated race).
"""
from __future__ import annotations

import hashlib
import threading
import time
from datetime import datetime, timezone

import pytest
from sqlalchemy.exc import IntegrityError

from core import identity
from core.contract_validation import validate_against_contract
from core.errors import ConflictError, NotFoundError, ValidationError
from core.timestamps import utc_now
from persistence.postgres.ai_invocation_repository import PostgresAIInvocationRepository
from persistence.postgres.evidence_classification_models import EvidenceClassificationRow
from persistence.postgres.evidence_classification_repository import PostgresEvidenceClassificationRepository
from persistence.postgres.evidence_classification_rule_repository import PostgresEvidenceClassificationRuleRepository
from persistence.postgres.evidence_repository import PostgresEvidenceRepository
from persistence.postgres.external_reference_repository import PostgresExternalReferenceRepository
from persistence.postgres.session import get_engine, session_scope
from persistence.postgres.source_repository import PostgresSourceRepository
from services.evidence.classification import (
    CLASSIFICATION_TYPE_DOCUMENT_TYPE,
    SOURCE_AI_PROPOSAL,
    SOURCE_DETERMINISTIC_RULE,
    SOURCE_OPERATOR_ASSIGNED,
    STATUS_CLASSIFIED,
)
from services.evidence.classification_rule import RULE_SOURCE_OPERATOR, SENDER_SCOPE_EXACT_SENDER_DOMAIN, SUBJECT_PREDICATE_EXACT

_SCHEMA = "evidence/bagman.evidence_classification.v1.schema.json"


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _fabricated_content_hash(tag: str) -> str:
    return hashlib.sha256(tag.encode("utf-8")).hexdigest()


def _make_source():
    return PostgresSourceRepository().register_source(source_type="MANUAL_UPLOAD", provider="INTERNAL", status="ACTIVE")


def _make_evidence() -> str:
    source = _make_source()
    now = _utc_now()
    evidence = PostgresEvidenceRepository(PostgresExternalReferenceRepository()).register_evidence(
        entity_id=None, evidence_type="INVOICE", source_id=source.source_id, observed_at=now, received_at=now,
        content_hash=_fabricated_content_hash(identity.generate_id()), mime_type="application/pdf", size_bytes=1024,
    )
    return evidence.evidence_id


def _make_rule(**overrides) -> str:
    kwargs = dict(
        sender_scope_type=SENDER_SCOPE_EXACT_SENDER_DOMAIN, sender_scope_value=f"vendor-{identity.generate_id()}.example",
        subject_predicate_type=SUBJECT_PREDICATE_EXACT, subject_predicate_value="monthly statement",
        document_type="SUPPLIER_INVOICE", source=RULE_SOURCE_OPERATOR,
    )
    kwargs.update(overrides)
    rule = PostgresEvidenceClassificationRuleRepository().create_rule(**kwargs)
    return rule.rule_id


def _make_succeeded_invocation(evidence_id: str) -> str:
    repo = PostgresAIInvocationRepository()
    created = repo.create_invocation(
        task_id="DOCUMENT_TYPE_PROPOSAL", task_version=1, role="BACKGROUND", provider="LITELLM",
        capability_alias="bagman-fast", input_references={"evidence_id": evidence_id},
        actor_type="SYSTEM", actor_id="bagman-test-harness",
    )
    repo.transition_status(created.ai_invocation_id, "RUNNING")
    repo.transition_status(created.ai_invocation_id, "SUCCEEDED")
    return created.ai_invocation_id


def _make_failed_invocation(evidence_id: str) -> str:
    repo = PostgresAIInvocationRepository()
    created = repo.create_invocation(
        task_id="DOCUMENT_TYPE_PROPOSAL", task_version=1, role="BACKGROUND", provider="LITELLM",
        capability_alias="bagman-fast", input_references={"evidence_id": evidence_id},
        actor_type="SYSTEM", actor_id="bagman-test-harness",
    )
    repo.transition_status(created.ai_invocation_id, "RUNNING")
    repo.transition_status(created.ai_invocation_id, "FAILED", error_code="PROVIDER_ERROR")
    return created.ai_invocation_id


def _repo() -> PostgresEvidenceClassificationRepository:
    return PostgresEvidenceClassificationRepository(
        evidence_repository=PostgresEvidenceRepository(PostgresExternalReferenceRepository()),
        rule_repository=PostgresEvidenceClassificationRuleRepository(),
        ai_invocation_repository=PostgresAIInvocationRepository(),
    )


# ---------------------------------------------------------------------
# round-trip / initial classification / current-lookup
# ---------------------------------------------------------------------


def test_classification_persists_and_is_retrievable_via_a_fresh_repository_instance(fresh_engine):
    evidence_id = _make_evidence()
    rule_id = _make_rule()
    repo = _repo()
    created = repo.create_classification(
        evidence_id=evidence_id, classification_type=CLASSIFICATION_TYPE_DOCUMENT_TYPE,
        document_type="SUPPLIER_INVOICE", status=STATUS_CLASSIFIED, source=SOURCE_DETERMINISTIC_RULE,
        rule_id=rule_id, expected_current_classification_id=None,
    )
    fresh_repo = PostgresEvidenceClassificationRepository(
        evidence_repository=PostgresEvidenceRepository(PostgresExternalReferenceRepository()),
        rule_repository=PostgresEvidenceClassificationRuleRepository(),
        ai_invocation_repository=PostgresAIInvocationRepository(),
        engine=fresh_engine,
    )
    fetched = fresh_repo.get_classification(created.classification_id)
    assert fetched.document_type == "SUPPLIER_INVOICE"
    assert fetched.rule_id == rule_id


def test_get_unknown_classification_id_raises_not_found():
    with pytest.raises(NotFoundError):
        _repo().get_classification(identity.generate_id())


def test_current_lookup_with_no_classifications_returns_none():
    evidence_id = _make_evidence()
    assert _repo().get_current_classification(evidence_id, CLASSIFICATION_TYPE_DOCUMENT_TYPE) is None


# ---------------------------------------------------------------------
# supersession / current-after-supersession / linear chain / branch
# ---------------------------------------------------------------------


def test_supersession_moves_the_current_tip():
    evidence_id = _make_evidence()
    rule_id = _make_rule()
    repo = _repo()
    first = repo.create_classification(
        evidence_id=evidence_id, classification_type=CLASSIFICATION_TYPE_DOCUMENT_TYPE,
        document_type="SUPPLIER_INVOICE", status=STATUS_CLASSIFIED, source=SOURCE_DETERMINISTIC_RULE,
        rule_id=rule_id, expected_current_classification_id=None,
    )
    second = repo.create_classification(
        evidence_id=evidence_id, classification_type=CLASSIFICATION_TYPE_DOCUMENT_TYPE,
        document_type="RECEIPT", status=STATUS_CLASSIFIED, source=SOURCE_OPERATOR_ASSIGNED,
        operator_action_id="op-supersede-1", supersedes_classification_id=first.classification_id,
        expected_current_classification_id=first.classification_id,
    )
    current = repo.get_current_classification(evidence_id, CLASSIFICATION_TYPE_DOCUMENT_TYPE)
    assert current.classification_id == second.classification_id
    original = repo.get_classification(first.classification_id)
    assert original.document_type == "SUPPLIER_INVOICE"


def test_linear_three_deep_chain_and_history_ordering():
    evidence_id = _make_evidence()
    rule_id = _make_rule()
    repo = _repo()
    first = repo.create_classification(
        evidence_id=evidence_id, classification_type=CLASSIFICATION_TYPE_DOCUMENT_TYPE,
        document_type="SUPPLIER_INVOICE", status=STATUS_CLASSIFIED, source=SOURCE_DETERMINISTIC_RULE,
        rule_id=rule_id, expected_current_classification_id=None,
    )
    second = repo.create_classification(
        evidence_id=evidence_id, classification_type=CLASSIFICATION_TYPE_DOCUMENT_TYPE,
        document_type="RECEIPT", status=STATUS_CLASSIFIED, source=SOURCE_OPERATOR_ASSIGNED,
        operator_action_id="op-chain-1", supersedes_classification_id=first.classification_id,
        expected_current_classification_id=first.classification_id,
    )
    third = repo.create_classification(
        evidence_id=evidence_id, classification_type=CLASSIFICATION_TYPE_DOCUMENT_TYPE,
        document_type="ORDER_CONFIRMATION", status=STATUS_CLASSIFIED, source=SOURCE_OPERATOR_ASSIGNED,
        operator_action_id="op-chain-2", supersedes_classification_id=second.classification_id,
        expected_current_classification_id=second.classification_id,
    )
    history = repo.list_classification_history(evidence_id, CLASSIFICATION_TYPE_DOCUMENT_TYPE)
    assert [c.classification_id for c in history] == [first.classification_id, second.classification_id, third.classification_id]
    current = repo.get_current_classification(evidence_id, CLASSIFICATION_TYPE_DOCUMENT_TYPE)
    assert current.classification_id == third.classification_id


def test_branch_rejected_with_conflict_error():
    evidence_id = _make_evidence()
    rule_id = _make_rule()
    repo = _repo()
    first = repo.create_classification(
        evidence_id=evidence_id, classification_type=CLASSIFICATION_TYPE_DOCUMENT_TYPE,
        document_type="SUPPLIER_INVOICE", status=STATUS_CLASSIFIED, source=SOURCE_DETERMINISTIC_RULE,
        rule_id=rule_id, expected_current_classification_id=None,
    )
    repo.create_classification(
        evidence_id=evidence_id, classification_type=CLASSIFICATION_TYPE_DOCUMENT_TYPE,
        document_type="RECEIPT", status=STATUS_CLASSIFIED, source=SOURCE_OPERATOR_ASSIGNED,
        operator_action_id="op-branch-1", supersedes_classification_id=first.classification_id,
        expected_current_classification_id=first.classification_id,
    )
    with pytest.raises(ConflictError):
        repo.create_classification(
            evidence_id=evidence_id, classification_type=CLASSIFICATION_TYPE_DOCUMENT_TYPE,
            document_type="BROKER_STATEMENT", status=STATUS_CLASSIFIED, source=SOURCE_OPERATOR_ASSIGNED,
            operator_action_id="op-branch-2", supersedes_classification_id=first.classification_id,
            expected_current_classification_id=first.classification_id,
        )


def test_stale_expected_current_rejected_with_conflict_error():
    evidence_id = _make_evidence()
    rule_id = _make_rule()
    repo = _repo()
    repo.create_classification(
        evidence_id=evidence_id, classification_type=CLASSIFICATION_TYPE_DOCUMENT_TYPE,
        document_type="SUPPLIER_INVOICE", status=STATUS_CLASSIFIED, source=SOURCE_DETERMINISTIC_RULE,
        rule_id=rule_id, expected_current_classification_id=None,
    )
    with pytest.raises(ConflictError):
        repo.create_classification(
            evidence_id=evidence_id, classification_type=CLASSIFICATION_TYPE_DOCUMENT_TYPE,
            document_type="RECEIPT", status=STATUS_CLASSIFIED, source=SOURCE_OPERATOR_ASSIGNED,
            operator_action_id="op-stale-1", expected_current_classification_id=None,
        )


def test_cross_evidence_supersession_rejected():
    evidence_a = _make_evidence()
    evidence_b = _make_evidence()
    rule_id = _make_rule()
    repo = _repo()
    first = repo.create_classification(
        evidence_id=evidence_a, classification_type=CLASSIFICATION_TYPE_DOCUMENT_TYPE,
        document_type="SUPPLIER_INVOICE", status=STATUS_CLASSIFIED, source=SOURCE_DETERMINISTIC_RULE,
        rule_id=rule_id, expected_current_classification_id=None,
    )
    with pytest.raises(ValidationError):
        repo.create_classification(
            evidence_id=evidence_b, classification_type=CLASSIFICATION_TYPE_DOCUMENT_TYPE,
            document_type="RECEIPT", status=STATUS_CLASSIFIED, source=SOURCE_OPERATOR_ASSIGNED,
            operator_action_id="op-cross-evidence-1", supersedes_classification_id=first.classification_id,
            expected_current_classification_id=None,
        )


def test_cross_classification_type_supersession_rejected():
    evidence_id = _make_evidence()
    rule_id = _make_rule()
    repo = _repo()
    first = repo.create_classification(
        evidence_id=evidence_id, classification_type=CLASSIFICATION_TYPE_DOCUMENT_TYPE,
        document_type="SUPPLIER_INVOICE", status=STATUS_CLASSIFIED, source=SOURCE_DETERMINISTIC_RULE,
        rule_id=rule_id, expected_current_classification_id=None,
    )
    with pytest.raises(ValidationError):
        repo.create_classification(
            evidence_id=evidence_id, classification_type="A_DIFFERENT_DIMENSION",
            document_type="RECEIPT", status=STATUS_CLASSIFIED, source=SOURCE_OPERATOR_ASSIGNED,
            operator_action_id="op-cross-type-1", supersedes_classification_id=first.classification_id,
            expected_current_classification_id=None,
        )


def test_database_level_branch_prevention_unique_index_exists():
    evidence_id = _make_evidence()
    rule_id = _make_rule()
    repo = _repo()
    first = repo.create_classification(
        evidence_id=evidence_id, classification_type=CLASSIFICATION_TYPE_DOCUMENT_TYPE,
        document_type="SUPPLIER_INVOICE", status=STATUS_CLASSIFIED, source=SOURCE_DETERMINISTIC_RULE,
        rule_id=rule_id, expected_current_classification_id=None,
    )
    now = utc_now()
    with pytest.raises(IntegrityError):
        with session_scope(get_engine()) as session:
            for op_id in ("op-db-branch-a", "op-db-branch-b"):
                session.add(
                    EvidenceClassificationRow(
                        classification_id=identity.generate_id(), evidence_id=evidence_id,
                        classification_type=CLASSIFICATION_TYPE_DOCUMENT_TYPE, document_type="RECEIPT",
                        status=STATUS_CLASSIFIED, source=SOURCE_OPERATOR_ASSIGNED, confidence=None, rule_id=None,
                        ai_invocation_id=None, operator_action_id=op_id, reason_codes=[],
                        supersedes_classification_id=first.classification_id, created_at=now,
                    )
                )
                session.flush()


# ---------------------------------------------------------------------
# producer-replay idempotency — all three sources
# ---------------------------------------------------------------------


def test_deterministic_rule_producer_replay_returns_the_same_row():
    evidence_id = _make_evidence()
    rule_id = _make_rule()
    repo = _repo()
    first = repo.create_classification(
        evidence_id=evidence_id, classification_type=CLASSIFICATION_TYPE_DOCUMENT_TYPE,
        document_type="SUPPLIER_INVOICE", status=STATUS_CLASSIFIED, source=SOURCE_DETERMINISTIC_RULE,
        rule_id=rule_id, expected_current_classification_id=None,
    )
    replay = repo.create_classification(
        evidence_id=evidence_id, classification_type=CLASSIFICATION_TYPE_DOCUMENT_TYPE,
        document_type="SUPPLIER_INVOICE", status=STATUS_CLASSIFIED, source=SOURCE_DETERMINISTIC_RULE,
        rule_id=rule_id, expected_current_classification_id=None,
    )
    assert replay.classification_id == first.classification_id
    with session_scope(get_engine()) as session:
        count = session.query(EvidenceClassificationRow).filter_by(evidence_id=evidence_id).count()
        assert count == 1


def test_ai_proposal_producer_replay_returns_the_same_row():
    evidence_id = _make_evidence()
    invocation_id = _make_succeeded_invocation(evidence_id)
    repo = _repo()
    first = repo.create_classification(
        evidence_id=evidence_id, classification_type=CLASSIFICATION_TYPE_DOCUMENT_TYPE,
        document_type="RECEIPT", status=STATUS_CLASSIFIED, source=SOURCE_AI_PROPOSAL,
        ai_invocation_id=invocation_id, confidence=0.9, expected_current_classification_id=None,
    )
    replay = repo.create_classification(
        evidence_id=evidence_id, classification_type=CLASSIFICATION_TYPE_DOCUMENT_TYPE,
        document_type="RECEIPT", status=STATUS_CLASSIFIED, source=SOURCE_AI_PROPOSAL,
        ai_invocation_id=invocation_id, confidence=0.9, expected_current_classification_id=None,
    )
    assert replay.classification_id == first.classification_id


def test_operator_assigned_producer_replay_returns_the_same_row():
    evidence_id = _make_evidence()
    repo = _repo()
    first = repo.create_classification(
        evidence_id=evidence_id, classification_type=CLASSIFICATION_TYPE_DOCUMENT_TYPE,
        document_type="RECEIPT", status=STATUS_CLASSIFIED, source=SOURCE_OPERATOR_ASSIGNED,
        operator_action_id="op-replay-1", expected_current_classification_id=None,
    )
    replay = repo.create_classification(
        evidence_id=evidence_id, classification_type=CLASSIFICATION_TYPE_DOCUMENT_TYPE,
        document_type="RECEIPT", status=STATUS_CLASSIFIED, source=SOURCE_OPERATOR_ASSIGNED,
        operator_action_id="op-replay-1", expected_current_classification_id=None,
    )
    assert replay.classification_id == first.classification_id


def test_database_level_producer_idempotency_indexes_exist():
    """A raw duplicate insert at the same (evidence_id, classification_type,
    rule_id) identity under source=DETERMINISTIC_RULE must be rejected
    by the database itself."""
    evidence_id = _make_evidence()
    rule_id = _make_rule()
    now = utc_now()
    with pytest.raises(IntegrityError):
        with session_scope(get_engine()) as session:
            for _ in range(2):
                session.add(
                    EvidenceClassificationRow(
                        classification_id=identity.generate_id(), evidence_id=evidence_id,
                        classification_type=CLASSIFICATION_TYPE_DOCUMENT_TYPE, document_type="SUPPLIER_INVOICE",
                        status=STATUS_CLASSIFIED, source=SOURCE_DETERMINISTIC_RULE, confidence=None, rule_id=rule_id,
                        ai_invocation_id=None, operator_action_id=None, reason_codes=[],
                        supersedes_classification_id=None, created_at=now,
                    )
                )
                session.flush()


# ---------------------------------------------------------------------
# source-reference validation failures — existence checks
# ---------------------------------------------------------------------


def test_evidence_missing_rejected_with_not_found():
    rule_id = _make_rule()
    with pytest.raises(NotFoundError):
        _repo().create_classification(
            evidence_id=identity.generate_id(), classification_type=CLASSIFICATION_TYPE_DOCUMENT_TYPE,
            document_type="SUPPLIER_INVOICE", status=STATUS_CLASSIFIED, source=SOURCE_DETERMINISTIC_RULE,
            rule_id=rule_id, expected_current_classification_id=None,
        )


def test_rule_missing_rejected_with_not_found():
    evidence_id = _make_evidence()
    with pytest.raises(NotFoundError):
        _repo().create_classification(
            evidence_id=evidence_id, classification_type=CLASSIFICATION_TYPE_DOCUMENT_TYPE,
            document_type="SUPPLIER_INVOICE", status=STATUS_CLASSIFIED, source=SOURCE_DETERMINISTIC_RULE,
            rule_id=identity.generate_id(), expected_current_classification_id=None,
        )


def test_ai_invocation_missing_rejected_with_not_found():
    evidence_id = _make_evidence()
    with pytest.raises(NotFoundError):
        _repo().create_classification(
            evidence_id=evidence_id, classification_type=CLASSIFICATION_TYPE_DOCUMENT_TYPE,
            document_type="RECEIPT", status=STATUS_CLASSIFIED, source=SOURCE_AI_PROPOSAL,
            ai_invocation_id=identity.generate_id(), confidence=0.5, expected_current_classification_id=None,
        )


def test_ai_invocation_not_succeeded_rejected():
    evidence_id = _make_evidence()
    invocation_id = _make_failed_invocation(evidence_id)
    with pytest.raises(ValidationError):
        _repo().create_classification(
            evidence_id=evidence_id, classification_type=CLASSIFICATION_TYPE_DOCUMENT_TYPE,
            document_type="RECEIPT", status=STATUS_CLASSIFIED, source=SOURCE_AI_PROPOSAL,
            ai_invocation_id=invocation_id, confidence=0.5, expected_current_classification_id=None,
        )


# ---------------------------------------------------------------------
# Contract-layer conformance — the REAL code path.
# ---------------------------------------------------------------------


def test_postgres_repository_create_classification_produces_a_schema_valid_snapshot():
    evidence_id = _make_evidence()
    rule_id = _make_rule()
    classification = _repo().create_classification(
        evidence_id=evidence_id, classification_type=CLASSIFICATION_TYPE_DOCUMENT_TYPE,
        document_type="SUPPLIER_INVOICE", status=STATUS_CLASSIFIED, source=SOURCE_DETERMINISTIC_RULE,
        rule_id=rule_id, expected_current_classification_id=None,
    )
    validate_against_contract(classification.to_dict(), _SCHEMA)


# ---------------------------------------------------------------------
# CD-6 Slice 5 WI-1 — genuine real-threaded concurrency race against the
# real database: two independent writers targeting the SAME
# (evidence_id, DOCUMENT_TYPE) identity with expected_current_classification_id=None
# must resolve to EXACTLY one winner, never a fork, never zero.
# ---------------------------------------------------------------------


def test_genuinely_concurrent_writers_racing_the_same_identity_resolve_to_exactly_one_current_classification():
    evidence_id = _make_evidence()
    n_workers = 8
    barrier = threading.Barrier(n_workers)
    results: list[dict] = [{} for _ in range(n_workers)]

    def _worker(index: int) -> None:
        # Each thread gets its OWN repository set (own engine-backed
        # PostgresEvidenceRepository/PostgresEvidenceClassificationRuleRepository/
        # PostgresAIInvocationRepository instances) — never shared —
        # so this is genuinely n independent callers racing the real
        # database, not n threads sharing one connection.
        repo = _repo()
        barrier.wait()
        start = time.monotonic()
        try:
            created = repo.create_classification(
                evidence_id=evidence_id, classification_type=CLASSIFICATION_TYPE_DOCUMENT_TYPE,
                document_type="SUPPLIER_INVOICE", status=STATUS_CLASSIFIED, source=SOURCE_OPERATOR_ASSIGNED,
                operator_action_id=f"op-race-{index}", expected_current_classification_id=None,
            )
            results[index] = {
                "outcome": "created", "classification_id": created.classification_id,
                "start": start, "end": time.monotonic(),
            }
        except ConflictError:
            results[index] = {"outcome": "conflict", "start": start, "end": time.monotonic()}

    threads = [threading.Thread(target=_worker, args=(i,)) for i in range(n_workers)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=30)

    assert all(r for r in results), f"one or more worker threads did not complete: {results}"

    created = [r for r in results if r["outcome"] == "created"]
    conflicted = [r for r in results if r["outcome"] == "conflict"]

    assert len(created) == 1, (
        f"expected exactly ONE winning create_classification call for the same identity under a genuine "
        f"concurrent race, got {len(created)}: {results}"
    )
    assert len(conflicted) == n_workers - 1, f"expected every other thread to receive a real conflict: {results}"

    windows = [(r["start"], r["end"]) for r in results]
    overlap_found = any(
        a_start < b_end and b_start < a_end
        for i, (a_start, a_end) in enumerate(windows)
        for j, (b_start, b_end) in enumerate(windows)
        if i < j
    )
    assert overlap_found, f"no genuine wall-clock overlap detected between worker call windows: {windows}"

    # Exactly one row exists for this identity — no fork, no orphan.
    with session_scope(get_engine()) as session:
        count = (
            session.query(EvidenceClassificationRow)
            .filter_by(evidence_id=evidence_id, classification_type=CLASSIFICATION_TYPE_DOCUMENT_TYPE)
            .count()
        )
        assert count == 1

    current = _repo().get_current_classification(evidence_id, CLASSIFICATION_TYPE_DOCUMENT_TYPE)
    assert current is not None
    assert current.classification_id == created[0]["classification_id"]
