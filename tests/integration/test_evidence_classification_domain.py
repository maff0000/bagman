"""CD-6 Slice 5 WI-1 tests for `services.evidence.classification` — the
in-memory reference implementation. Mirrors
`tests/integration/test_mailbox_domain_rule_domain.py`'s own style.

Covers the WO's own §39-§44 scenario list: initial classification,
current-lookup, supersession, current-after-supersession, a linear
three-deep chain, branch-rejection, stale-expected-current rejection,
producer-replay idempotency for all three sources, source-reference
validation failures, evidence-missing rejection, AIInvocation-not-
SUCCEEDED rejection, cross-evidence-supersession rejection, and
cross-classification-type-supersession rejection.
"""
from __future__ import annotations

from datetime import datetime, timezone

import pytest

from ai.invocation import InMemoryAIInvocationRepository
from core import identity
from core.errors import ConflictError, NotFoundError, ValidationError
from core.external_reference import InMemoryExternalReferenceRepository
from services.evidence.classification import (
    CLASSIFICATION_TYPE_DOCUMENT_TYPE,
    SOURCE_AI_PROPOSAL,
    SOURCE_DETERMINISTIC_RULE,
    SOURCE_OPERATOR_ASSIGNED,
    STATUS_CLASSIFIED,
    STATUS_REVIEW_REQUIRED,
    STATUS_UNCLASSIFIABLE,
    InMemoryEvidenceClassificationRepository,
    validate_classification_fields_or_raise,
)
from services.evidence.classification_rule import InMemoryEvidenceClassificationRuleRepository
from services.evidence.evidence import InMemoryEvidenceRepository


def _register_evidence(evidence_repository, entity_id=None) -> str:
    now = datetime.now(timezone.utc)
    item = evidence_repository.register_evidence(
        entity_id=entity_id,
        evidence_type="INVOICE",
        source_id=identity.generate_id(),
        observed_at=now,
        received_at=now,
        content_hash="a" * 64,
        mime_type="application/pdf",
        size_bytes=1024,
    )
    return item.evidence_id


def _active_rule(rule_repository) -> str:
    rule = rule_repository.create_rule(
        sender_scope_type="EXACT_SENDER_DOMAIN", sender_scope_value="vendor.com",
        subject_predicate_type="EXACT", subject_predicate_value="monthly statement",
        document_type="SUPPLIER_INVOICE", source="OPERATOR",
    )
    return rule.rule_id


def _succeeded_invocation(ai_invocation_repository, evidence_id) -> str:
    created = ai_invocation_repository.create_invocation(
        task_id="DOCUMENT_TYPE_PROPOSAL", task_version=1, role="BACKGROUND", provider="LITELLM",
        capability_alias="bagman-fast", input_references={"evidence_id": evidence_id},
        actor_type="SYSTEM", actor_id="bagman-test-harness",
    )
    ai_invocation_repository.transition_status(created.ai_invocation_id, "RUNNING")
    ai_invocation_repository.transition_status(created.ai_invocation_id, "SUCCEEDED")
    return created.ai_invocation_id


def _failed_invocation(ai_invocation_repository, evidence_id) -> str:
    created = ai_invocation_repository.create_invocation(
        task_id="DOCUMENT_TYPE_PROPOSAL", task_version=1, role="BACKGROUND", provider="LITELLM",
        capability_alias="bagman-fast", input_references={"evidence_id": evidence_id},
        actor_type="SYSTEM", actor_id="bagman-test-harness",
    )
    ai_invocation_repository.transition_status(created.ai_invocation_id, "RUNNING")
    ai_invocation_repository.transition_status(created.ai_invocation_id, "FAILED", error_code="PROVIDER_ERROR")
    return created.ai_invocation_id


@pytest.fixture
def evidence_repository() -> InMemoryEvidenceRepository:
    return InMemoryEvidenceRepository(InMemoryExternalReferenceRepository())


@pytest.fixture
def rule_repository() -> InMemoryEvidenceClassificationRuleRepository:
    return InMemoryEvidenceClassificationRuleRepository()


@pytest.fixture
def ai_invocation_repository() -> InMemoryAIInvocationRepository:
    return InMemoryAIInvocationRepository()


@pytest.fixture
def repo(evidence_repository, rule_repository, ai_invocation_repository) -> InMemoryEvidenceClassificationRepository:
    return InMemoryEvidenceClassificationRepository(
        evidence_repository=evidence_repository, rule_repository=rule_repository,
        ai_invocation_repository=ai_invocation_repository,
    )


# ---------------------------------------------------------------------
# validate_classification_fields_or_raise — field-shape invariants
# ---------------------------------------------------------------------


def test_status_document_type_classified_requires_not_unknown():
    with pytest.raises(ValidationError):
        validate_classification_fields_or_raise(
            classification_type=CLASSIFICATION_TYPE_DOCUMENT_TYPE, document_type="UNKNOWN", status=STATUS_CLASSIFIED,
            source=SOURCE_OPERATOR_ASSIGNED, confidence=None, rule_id=None, ai_invocation_id=None,
            operator_action_id="op-1",
        )


def test_status_document_type_unclassifiable_requires_unknown():
    with pytest.raises(ValidationError):
        validate_classification_fields_or_raise(
            classification_type=CLASSIFICATION_TYPE_DOCUMENT_TYPE, document_type="RECEIPT", status=STATUS_UNCLASSIFIABLE,
            source=SOURCE_OPERATOR_ASSIGNED, confidence=None, rule_id=None, ai_invocation_id=None,
            operator_action_id="op-1",
        )


def test_review_required_allows_any_document_type():
    validate_classification_fields_or_raise(
        classification_type=CLASSIFICATION_TYPE_DOCUMENT_TYPE, document_type="UNKNOWN", status=STATUS_REVIEW_REQUIRED,
        source=SOURCE_OPERATOR_ASSIGNED, confidence=None, rule_id=None, ai_invocation_id=None,
        operator_action_id="op-1",
    )


def test_non_document_type_classification_type_rejected():
    with pytest.raises(ValidationError):
        validate_classification_fields_or_raise(
            classification_type="SOMETHING_ELSE", document_type="RECEIPT", status=STATUS_CLASSIFIED,
            source=SOURCE_OPERATOR_ASSIGNED, confidence=None, rule_id=None, ai_invocation_id=None,
            operator_action_id="op-1",
        )


def test_deterministic_rule_never_manufactures_confidence():
    with pytest.raises(ValidationError):
        validate_classification_fields_or_raise(
            classification_type=CLASSIFICATION_TYPE_DOCUMENT_TYPE, document_type="RECEIPT", status=STATUS_CLASSIFIED,
            source=SOURCE_DETERMINISTIC_RULE, confidence=1.0, rule_id=identity.generate_id(), ai_invocation_id=None,
            operator_action_id=None,
        )


def test_ai_proposal_confidence_out_of_range_rejected():
    with pytest.raises(ValidationError):
        validate_classification_fields_or_raise(
            classification_type=CLASSIFICATION_TYPE_DOCUMENT_TYPE, document_type="RECEIPT", status=STATUS_CLASSIFIED,
            source=SOURCE_AI_PROPOSAL, confidence=1.2, rule_id=None, ai_invocation_id=identity.generate_id(),
            operator_action_id=None,
        )


# ---------------------------------------------------------------------
# initial classification / current-lookup
# ---------------------------------------------------------------------


def test_initial_classification_with_expected_current_none(repo, evidence_repository, rule_repository):
    evidence_id = _register_evidence(evidence_repository)
    rule_id = _active_rule(rule_repository)
    created = repo.create_classification(
        evidence_id=evidence_id, classification_type=CLASSIFICATION_TYPE_DOCUMENT_TYPE,
        document_type="SUPPLIER_INVOICE", status=STATUS_CLASSIFIED, source=SOURCE_DETERMINISTIC_RULE,
        rule_id=rule_id, expected_current_classification_id=None,
    )
    assert created.supersedes_classification_id is None
    assert created.reason_codes == ()


def test_current_lookup_returns_the_only_row(repo, evidence_repository, rule_repository):
    evidence_id = _register_evidence(evidence_repository)
    rule_id = _active_rule(rule_repository)
    created = repo.create_classification(
        evidence_id=evidence_id, classification_type=CLASSIFICATION_TYPE_DOCUMENT_TYPE,
        document_type="SUPPLIER_INVOICE", status=STATUS_CLASSIFIED, source=SOURCE_DETERMINISTIC_RULE,
        rule_id=rule_id, expected_current_classification_id=None,
    )
    current = repo.get_current_classification(evidence_id, CLASSIFICATION_TYPE_DOCUMENT_TYPE)
    assert current.classification_id == created.classification_id


def test_current_lookup_with_no_classifications_returns_none(repo, evidence_repository):
    evidence_id = _register_evidence(evidence_repository)
    assert repo.get_current_classification(evidence_id, CLASSIFICATION_TYPE_DOCUMENT_TYPE) is None


# ---------------------------------------------------------------------
# supersession / current-after-supersession / linear chain
# ---------------------------------------------------------------------


def test_supersession_moves_the_current_tip(repo, evidence_repository, rule_repository):
    evidence_id = _register_evidence(evidence_repository)
    rule_id = _active_rule(rule_repository)
    first = repo.create_classification(
        evidence_id=evidence_id, classification_type=CLASSIFICATION_TYPE_DOCUMENT_TYPE,
        document_type="SUPPLIER_INVOICE", status=STATUS_CLASSIFIED, source=SOURCE_DETERMINISTIC_RULE,
        rule_id=rule_id, expected_current_classification_id=None,
    )
    second = repo.create_classification(
        evidence_id=evidence_id, classification_type=CLASSIFICATION_TYPE_DOCUMENT_TYPE,
        document_type="RECEIPT", status=STATUS_CLASSIFIED, source=SOURCE_OPERATOR_ASSIGNED,
        operator_action_id="op-correction-1", supersedes_classification_id=first.classification_id,
        expected_current_classification_id=first.classification_id,
    )
    current = repo.get_current_classification(evidence_id, CLASSIFICATION_TYPE_DOCUMENT_TYPE)
    assert current.classification_id == second.classification_id
    # The superseded row is unchanged — still whatever it was created with.
    original = repo.get_classification(first.classification_id)
    assert original.document_type == "SUPPLIER_INVOICE"
    assert original.status == STATUS_CLASSIFIED


def test_linear_three_deep_chain(repo, evidence_repository, rule_repository):
    evidence_id = _register_evidence(evidence_repository)
    rule_id = _active_rule(rule_repository)
    first = repo.create_classification(
        evidence_id=evidence_id, classification_type=CLASSIFICATION_TYPE_DOCUMENT_TYPE,
        document_type="SUPPLIER_INVOICE", status=STATUS_CLASSIFIED, source=SOURCE_DETERMINISTIC_RULE,
        rule_id=rule_id, expected_current_classification_id=None,
    )
    second = repo.create_classification(
        evidence_id=evidence_id, classification_type=CLASSIFICATION_TYPE_DOCUMENT_TYPE,
        document_type="RECEIPT", status=STATUS_CLASSIFIED, source=SOURCE_OPERATOR_ASSIGNED,
        operator_action_id="op-correction-1", supersedes_classification_id=first.classification_id,
        expected_current_classification_id=first.classification_id,
    )
    third = repo.create_classification(
        evidence_id=evidence_id, classification_type=CLASSIFICATION_TYPE_DOCUMENT_TYPE,
        document_type="ORDER_CONFIRMATION", status=STATUS_CLASSIFIED, source=SOURCE_OPERATOR_ASSIGNED,
        operator_action_id="op-correction-2", supersedes_classification_id=second.classification_id,
        expected_current_classification_id=second.classification_id,
    )
    history = repo.list_classification_history(evidence_id, CLASSIFICATION_TYPE_DOCUMENT_TYPE)
    assert [c.classification_id for c in history] == [first.classification_id, second.classification_id, third.classification_id]
    current = repo.get_current_classification(evidence_id, CLASSIFICATION_TYPE_DOCUMENT_TYPE)
    assert current.classification_id == third.classification_id


def test_branch_rejected_with_conflict_error(repo, evidence_repository, rule_repository):
    evidence_id = _register_evidence(evidence_repository)
    rule_id = _active_rule(rule_repository)
    first = repo.create_classification(
        evidence_id=evidence_id, classification_type=CLASSIFICATION_TYPE_DOCUMENT_TYPE,
        document_type="SUPPLIER_INVOICE", status=STATUS_CLASSIFIED, source=SOURCE_DETERMINISTIC_RULE,
        rule_id=rule_id, expected_current_classification_id=None,
    )
    repo.create_classification(
        evidence_id=evidence_id, classification_type=CLASSIFICATION_TYPE_DOCUMENT_TYPE,
        document_type="RECEIPT", status=STATUS_CLASSIFIED, source=SOURCE_OPERATOR_ASSIGNED,
        operator_action_id="op-correction-1", supersedes_classification_id=first.classification_id,
        expected_current_classification_id=first.classification_id,
    )
    # A SECOND attempt to supersede the SAME (already-superseded) row —
    # branch prevention must reject this.
    with pytest.raises(ConflictError):
        repo.create_classification(
            evidence_id=evidence_id, classification_type=CLASSIFICATION_TYPE_DOCUMENT_TYPE,
            document_type="BROKER_STATEMENT", status=STATUS_CLASSIFIED, source=SOURCE_OPERATOR_ASSIGNED,
            operator_action_id="op-correction-branch", supersedes_classification_id=first.classification_id,
            expected_current_classification_id=first.classification_id,
        )


def test_stale_expected_current_rejected_with_conflict_error(repo, evidence_repository, rule_repository):
    evidence_id = _register_evidence(evidence_repository)
    rule_id = _active_rule(rule_repository)
    repo.create_classification(
        evidence_id=evidence_id, classification_type=CLASSIFICATION_TYPE_DOCUMENT_TYPE,
        document_type="SUPPLIER_INVOICE", status=STATUS_CLASSIFIED, source=SOURCE_DETERMINISTIC_RULE,
        rule_id=rule_id, expected_current_classification_id=None,
    )
    # Caller believes there is no current classification yet (None) —
    # but one already exists.
    with pytest.raises(ConflictError):
        repo.create_classification(
            evidence_id=evidence_id, classification_type=CLASSIFICATION_TYPE_DOCUMENT_TYPE,
            document_type="RECEIPT", status=STATUS_CLASSIFIED, source=SOURCE_OPERATOR_ASSIGNED,
            operator_action_id="op-stale", expected_current_classification_id=None,
        )


def test_cross_evidence_supersession_rejected(repo, evidence_repository, rule_repository):
    evidence_a = _register_evidence(evidence_repository)
    evidence_b = _register_evidence(evidence_repository)
    rule_id = _active_rule(rule_repository)
    first = repo.create_classification(
        evidence_id=evidence_a, classification_type=CLASSIFICATION_TYPE_DOCUMENT_TYPE,
        document_type="SUPPLIER_INVOICE", status=STATUS_CLASSIFIED, source=SOURCE_DETERMINISTIC_RULE,
        rule_id=rule_id, expected_current_classification_id=None,
    )
    with pytest.raises(ValidationError):
        repo.create_classification(
            evidence_id=evidence_b, classification_type=CLASSIFICATION_TYPE_DOCUMENT_TYPE,
            document_type="RECEIPT", status=STATUS_CLASSIFIED, source=SOURCE_OPERATOR_ASSIGNED,
            operator_action_id="op-cross-evidence", supersedes_classification_id=first.classification_id,
            expected_current_classification_id=None,
        )


def test_cross_classification_type_supersession_rejected(repo, evidence_repository, rule_repository):
    evidence_id = _register_evidence(evidence_repository)
    rule_id = _active_rule(rule_repository)
    first = repo.create_classification(
        evidence_id=evidence_id, classification_type=CLASSIFICATION_TYPE_DOCUMENT_TYPE,
        document_type="SUPPLIER_INVOICE", status=STATUS_CLASSIFIED, source=SOURCE_DETERMINISTIC_RULE,
        rule_id=rule_id, expected_current_classification_id=None,
    )
    with pytest.raises(ValidationError):
        repo.create_classification(
            evidence_id=evidence_id, classification_type="A_DIFFERENT_DIMENSION",
            document_type="RECEIPT", status=STATUS_CLASSIFIED, source=SOURCE_OPERATOR_ASSIGNED,
            operator_action_id="op-cross-type", supersedes_classification_id=first.classification_id,
            expected_current_classification_id=None,
        )


# ---------------------------------------------------------------------
# producer-replay idempotency — all three sources
# ---------------------------------------------------------------------


def test_deterministic_rule_producer_replay_returns_the_same_row(repo, evidence_repository, rule_repository):
    evidence_id = _register_evidence(evidence_repository)
    rule_id = _active_rule(rule_repository)
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
    assert len(repo.list_classification_history(evidence_id, CLASSIFICATION_TYPE_DOCUMENT_TYPE)) == 1


def test_ai_proposal_producer_replay_returns_the_same_row(repo, evidence_repository, ai_invocation_repository):
    evidence_id = _register_evidence(evidence_repository)
    invocation_id = _succeeded_invocation(ai_invocation_repository, evidence_id)
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


def test_operator_assigned_producer_replay_returns_the_same_row(repo, evidence_repository):
    evidence_id = _register_evidence(evidence_repository)
    first = repo.create_classification(
        evidence_id=evidence_id, classification_type=CLASSIFICATION_TYPE_DOCUMENT_TYPE,
        document_type="RECEIPT", status=STATUS_CLASSIFIED, source=SOURCE_OPERATOR_ASSIGNED,
        operator_action_id="op-idempotent-1", expected_current_classification_id=None,
    )
    replay = repo.create_classification(
        evidence_id=evidence_id, classification_type=CLASSIFICATION_TYPE_DOCUMENT_TYPE,
        document_type="RECEIPT", status=STATUS_CLASSIFIED, source=SOURCE_OPERATOR_ASSIGNED,
        operator_action_id="op-idempotent-1", expected_current_classification_id=None,
    )
    assert replay.classification_id == first.classification_id


# ---------------------------------------------------------------------
# CD-6 cross-WI concurrency correction (2026-09-25) —
# `create_classification_with_result` / `ClassificationCreationResult`
# InMemory parity: identical create/replay `was_created` semantics to
# the Postgres implementation, re-proving all three WI-1 producer
# identities (§14) even though WI-4 does not yet produce operator
# classifications.
# ---------------------------------------------------------------------


def test_create_classification_with_result_fresh_insert_reports_was_created_true(
    repo, evidence_repository, rule_repository
):
    evidence_id = _register_evidence(evidence_repository)
    rule_id = _active_rule(rule_repository)
    result = repo.create_classification_with_result(
        evidence_id=evidence_id, classification_type=CLASSIFICATION_TYPE_DOCUMENT_TYPE,
        document_type="SUPPLIER_INVOICE", status=STATUS_CLASSIFIED, source=SOURCE_DETERMINISTIC_RULE,
        rule_id=rule_id, expected_current_classification_id=None,
    )
    assert result.was_created is True
    assert result.classification.document_type == "SUPPLIER_INVOICE"


def test_create_classification_with_result_sequential_replay_reports_was_created_false(
    repo, evidence_repository, rule_repository
):
    evidence_id = _register_evidence(evidence_repository)
    rule_id = _active_rule(rule_repository)
    first = repo.create_classification_with_result(
        evidence_id=evidence_id, classification_type=CLASSIFICATION_TYPE_DOCUMENT_TYPE,
        document_type="SUPPLIER_INVOICE", status=STATUS_CLASSIFIED, source=SOURCE_DETERMINISTIC_RULE,
        rule_id=rule_id, expected_current_classification_id=None,
    )
    replay = repo.create_classification_with_result(
        evidence_id=evidence_id, classification_type=CLASSIFICATION_TYPE_DOCUMENT_TYPE,
        document_type="SUPPLIER_INVOICE", status=STATUS_CLASSIFIED, source=SOURCE_DETERMINISTIC_RULE,
        rule_id=rule_id, expected_current_classification_id=None,
    )
    assert first.was_created is True
    assert replay.was_created is False
    assert replay.classification.classification_id == first.classification.classification_id
    assert len(repo.list_classification_history(evidence_id, CLASSIFICATION_TYPE_DOCUMENT_TYPE)) == 1


def test_create_classification_with_result_ai_proposal_producer_identity(
    repo, evidence_repository, ai_invocation_repository
):
    evidence_id = _register_evidence(evidence_repository)
    invocation_id = _succeeded_invocation(ai_invocation_repository, evidence_id)
    first = repo.create_classification_with_result(
        evidence_id=evidence_id, classification_type=CLASSIFICATION_TYPE_DOCUMENT_TYPE,
        document_type="RECEIPT", status=STATUS_CLASSIFIED, source=SOURCE_AI_PROPOSAL,
        ai_invocation_id=invocation_id, confidence=0.9, expected_current_classification_id=None,
    )
    replay = repo.create_classification_with_result(
        evidence_id=evidence_id, classification_type=CLASSIFICATION_TYPE_DOCUMENT_TYPE,
        document_type="RECEIPT", status=STATUS_CLASSIFIED, source=SOURCE_AI_PROPOSAL,
        ai_invocation_id=invocation_id, confidence=0.9, expected_current_classification_id=None,
    )
    assert first.was_created is True
    assert replay.was_created is False
    assert replay.classification.classification_id == first.classification.classification_id


def test_create_classification_with_result_operator_assigned_producer_identity(repo, evidence_repository):
    """WI-4 does not yet produce operator classifications, but
    repository result semantics must already be correct for that
    source (WI-3-correction §14's own explicit instruction)."""
    evidence_id = _register_evidence(evidence_repository)
    first = repo.create_classification_with_result(
        evidence_id=evidence_id, classification_type=CLASSIFICATION_TYPE_DOCUMENT_TYPE,
        document_type="RECEIPT", status=STATUS_CLASSIFIED, source=SOURCE_OPERATOR_ASSIGNED,
        operator_action_id="op-with-result-1", expected_current_classification_id=None,
    )
    replay = repo.create_classification_with_result(
        evidence_id=evidence_id, classification_type=CLASSIFICATION_TYPE_DOCUMENT_TYPE,
        document_type="RECEIPT", status=STATUS_CLASSIFIED, source=SOURCE_OPERATOR_ASSIGNED,
        operator_action_id="op-with-result-1", expected_current_classification_id=None,
    )
    assert first.was_created is True
    assert replay.was_created is False
    assert replay.classification.classification_id == first.classification.classification_id


def test_create_classification_and_create_classification_with_result_share_one_impl(
    repo, evidence_repository, rule_repository
):
    """`create_classification` (bare `EvidenceClassification`) and
    `create_classification_with_result` (`ClassificationCreationResult`)
    must observe/produce the exact same underlying row for the exact
    same producer identity — proving they share one implementation,
    never two independent copies that could drift apart."""
    evidence_id = _register_evidence(evidence_repository)
    rule_id = _active_rule(rule_repository)
    bare = repo.create_classification(
        evidence_id=evidence_id, classification_type=CLASSIFICATION_TYPE_DOCUMENT_TYPE,
        document_type="SUPPLIER_INVOICE", status=STATUS_CLASSIFIED, source=SOURCE_DETERMINISTIC_RULE,
        rule_id=rule_id, expected_current_classification_id=None,
    )
    with_result = repo.create_classification_with_result(
        evidence_id=evidence_id, classification_type=CLASSIFICATION_TYPE_DOCUMENT_TYPE,
        document_type="SUPPLIER_INVOICE", status=STATUS_CLASSIFIED, source=SOURCE_DETERMINISTIC_RULE,
        rule_id=rule_id, expected_current_classification_id=None,
    )
    assert with_result.was_created is False
    assert with_result.classification.classification_id == bare.classification_id


def test_different_producer_same_subject_is_not_idempotent(repo, evidence_repository, rule_repository):
    """A DIFFERENT rule_id creating a classification for the same
    (evidence_id, classification_type) is a genuinely different producer
    — not a replay — so it must go through the normal
    expected-current-check, not silently short-circuit."""
    evidence_id = _register_evidence(evidence_repository)
    rule_a = _active_rule(rule_repository)
    rule_b = rule_repository.create_rule(
        sender_scope_type="EXACT_SENDER_DOMAIN", sender_scope_value="other.com",
        subject_predicate_type="EXACT", subject_predicate_value="receipt", document_type="RECEIPT", source="OPERATOR",
    ).rule_id
    first = repo.create_classification(
        evidence_id=evidence_id, classification_type=CLASSIFICATION_TYPE_DOCUMENT_TYPE,
        document_type="SUPPLIER_INVOICE", status=STATUS_CLASSIFIED, source=SOURCE_DETERMINISTIC_RULE,
        rule_id=rule_a, expected_current_classification_id=None,
    )
    with pytest.raises(ConflictError):
        repo.create_classification(
            evidence_id=evidence_id, classification_type=CLASSIFICATION_TYPE_DOCUMENT_TYPE,
            document_type="RECEIPT", status=STATUS_CLASSIFIED, source=SOURCE_DETERMINISTIC_RULE,
            rule_id=rule_b, expected_current_classification_id=None,
        )
    assert repo.get_current_classification(evidence_id, CLASSIFICATION_TYPE_DOCUMENT_TYPE).classification_id == first.classification_id


# ---------------------------------------------------------------------
# source-reference validation failures — existence checks
# ---------------------------------------------------------------------


def test_evidence_missing_rejected_with_not_found(repo, rule_repository):
    rule_id = _active_rule(rule_repository)
    with pytest.raises(NotFoundError):
        repo.create_classification(
            evidence_id=identity.generate_id(), classification_type=CLASSIFICATION_TYPE_DOCUMENT_TYPE,
            document_type="SUPPLIER_INVOICE", status=STATUS_CLASSIFIED, source=SOURCE_DETERMINISTIC_RULE,
            rule_id=rule_id, expected_current_classification_id=None,
        )


def test_rule_missing_rejected_with_not_found(repo, evidence_repository):
    evidence_id = _register_evidence(evidence_repository)
    with pytest.raises(NotFoundError):
        repo.create_classification(
            evidence_id=evidence_id, classification_type=CLASSIFICATION_TYPE_DOCUMENT_TYPE,
            document_type="SUPPLIER_INVOICE", status=STATUS_CLASSIFIED, source=SOURCE_DETERMINISTIC_RULE,
            rule_id=identity.generate_id(), expected_current_classification_id=None,
        )


def test_ai_invocation_missing_rejected_with_not_found(repo, evidence_repository):
    evidence_id = _register_evidence(evidence_repository)
    with pytest.raises(NotFoundError):
        repo.create_classification(
            evidence_id=evidence_id, classification_type=CLASSIFICATION_TYPE_DOCUMENT_TYPE,
            document_type="RECEIPT", status=STATUS_CLASSIFIED, source=SOURCE_AI_PROPOSAL,
            ai_invocation_id=identity.generate_id(), confidence=0.5, expected_current_classification_id=None,
        )


def test_ai_invocation_not_succeeded_rejected(repo, evidence_repository, ai_invocation_repository):
    evidence_id = _register_evidence(evidence_repository)
    invocation_id = _failed_invocation(ai_invocation_repository, evidence_id)
    with pytest.raises(ValidationError):
        repo.create_classification(
            evidence_id=evidence_id, classification_type=CLASSIFICATION_TYPE_DOCUMENT_TYPE,
            document_type="RECEIPT", status=STATUS_CLASSIFIED, source=SOURCE_AI_PROPOSAL,
            ai_invocation_id=invocation_id, confidence=0.5, expected_current_classification_id=None,
        )


def test_supersedes_missing_classification_id_rejected_with_not_found(repo, evidence_repository, rule_repository):
    evidence_id = _register_evidence(evidence_repository)
    rule_id = _active_rule(rule_repository)
    with pytest.raises(NotFoundError):
        repo.create_classification(
            evidence_id=evidence_id, classification_type=CLASSIFICATION_TYPE_DOCUMENT_TYPE,
            document_type="SUPPLIER_INVOICE", status=STATUS_CLASSIFIED, source=SOURCE_DETERMINISTIC_RULE,
            rule_id=rule_id, supersedes_classification_id=identity.generate_id(),
            expected_current_classification_id=None,
        )
