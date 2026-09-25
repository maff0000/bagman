"""CD-6 Slice 5 WI-4 §45-57 — the human-control loop
(`services.evidence.classification_review`) against in-memory
repositories. Mirrors `tests/integration/test_classification_orchestrator.py`'s
own conventions exactly (same fixture shapes, same
`_register_email_evidence`/succeeded-invocation helper style).

§50 (exact retry), §51 (conflicting resubmission), and §58 (DISMISSED
rejection) are HTTP-router-level behaviours (the generic idempotent-
replay/conflict logic already lives in
`app/api/routers/needs_you.py::resolve_needs_you_item`, not in this
service module) — proven separately in
`tests/app_api/test_classification_review_endpoints.py`, never here.
§59 (genuine concurrency) is proven against real Postgres in
`tests/persistence/test_classification_review_postgres.py`.
"""
from __future__ import annotations

import hashlib
from datetime import datetime, timezone

import pytest

from ai.invocation import InMemoryAIInvocationRepository
from core import actor, identity
from core.audit import InMemoryAuditRepository
from core.errors import ConflictError, ValidationError
from core.external_reference import InMemoryExternalReferenceRepository
from services.evidence.classification import (
    CLASSIFICATION_TYPE_DOCUMENT_TYPE,
    SOURCE_AI_PROPOSAL,
    SOURCE_OPERATOR_ASSIGNED,
    STATUS_CLASSIFIED,
    STATUS_REVIEW_REQUIRED,
    STATUS_UNCLASSIFIABLE,
    InMemoryEvidenceClassificationRepository,
)
from services.evidence.classification_review import (
    DECISION_CONFIRMED,
    DECISION_CORRECTED,
    EVIDENCE_CLASSIFICATION_CONFIRMED,
    EVIDENCE_CLASSIFICATION_CORRECTED,
    ensure_classification_review_item,
    resolve_classification_review,
)
from services.evidence.classification_rule import InMemoryEvidenceClassificationRuleRepository
from services.evidence.evidence import InMemoryEvidenceRepository
from services.needs_you.needs_you import (
    ALLOWED_ACTION_CLASSIFICATION_REVIEW,
    ITEM_TYPE_CLASSIFICATION_REVIEW,
    InMemoryNeedsYouRepository,
)

ACTOR_ID = "wi4-review-tests"


@pytest.fixture
def evidence_repository():
    return InMemoryEvidenceRepository(InMemoryExternalReferenceRepository())


@pytest.fixture
def rule_repository():
    return InMemoryEvidenceClassificationRuleRepository()


@pytest.fixture
def ai_invocation_repository():
    return InMemoryAIInvocationRepository()


@pytest.fixture
def classification_repository(evidence_repository, rule_repository, ai_invocation_repository):
    return InMemoryEvidenceClassificationRepository(
        evidence_repository=evidence_repository, rule_repository=rule_repository,
        ai_invocation_repository=ai_invocation_repository,
    )


@pytest.fixture
def audit_repository():
    return InMemoryAuditRepository()


@pytest.fixture
def needs_you_repository():
    return InMemoryNeedsYouRepository()


def _register_evidence(evidence_repository, *, sender_address=None, subject=None) -> str:
    content = f"synthetic-{identity.generate_id()}".encode("utf-8")
    content_hash = {"algorithm": "SHA-256", "value": hashlib.sha256(content).hexdigest()}
    metadata = {}
    if sender_address is not None:
        metadata["sender_address"] = sender_address
    if subject is not None:
        metadata["subject"] = subject
    now = datetime.now(timezone.utc)
    item = evidence_repository.register_evidence(
        entity_id=None, evidence_type="EMAIL", source_id=identity.generate_id(),
        observed_at=now, received_at=now, content_hash=content_hash, mime_type="message/rfc822",
        size_bytes=len(content), metadata=metadata,
    )
    return item.evidence_id


def _succeeded_invocation(ai_invocation_repository, evidence_id: str) -> str:
    created = ai_invocation_repository.create_invocation(
        task_id="DOCUMENT_TYPE_PROPOSAL", task_version=2, role="BACKGROUND", provider="LITELLM",
        capability_alias="bagman-core", input_references={"evidence_id": evidence_id},
        actor_type=actor.SYSTEM, actor_id=ACTOR_ID,
    )
    ai_invocation_repository.transition_status(created.ai_invocation_id, "RUNNING")
    ai_invocation_repository.transition_status(created.ai_invocation_id, "SUCCEEDED")
    return created.ai_invocation_id


def _make_ai_proposal(
    classification_repository, ai_invocation_repository, evidence_repository, *,
    sender_address=None, subject=None, document_type="BROKER_STATEMENT", status=STATUS_REVIEW_REQUIRED,
    confidence=0.8,
):
    evidence_id = _register_evidence(evidence_repository, sender_address=sender_address, subject=subject)
    ai_invocation_id = _succeeded_invocation(ai_invocation_repository, evidence_id)
    classification = classification_repository.create_classification(
        evidence_id=evidence_id, classification_type=CLASSIFICATION_TYPE_DOCUMENT_TYPE,
        document_type=document_type, status=status, source=SOURCE_AI_PROPOSAL,
        confidence=confidence, ai_invocation_id=ai_invocation_id, expected_current_classification_id=None,
    )
    evidence = evidence_repository.get_evidence(evidence_id)
    return evidence, classification, ai_invocation_id


def _resolve(
    needs_you_item, resolution, *, evidence_repository, classification_repository, rule_repository,
    audit_repository,
):
    return resolve_classification_review(
        needs_you_item=needs_you_item, resolution=resolution, actor_type=actor.USER, actor_id="matt",
        evidence_repository=evidence_repository, classification_repository=classification_repository,
        rule_repository=rule_repository, audit_repository=audit_repository,
        record_audit_event=audit_repository.record_audit_event,
    )


# ---------------------------------------------------------------------
# §45 — review producer
# ---------------------------------------------------------------------


def test_ensure_review_item_for_concrete_proposal_creates_exactly_one_open_item(
    evidence_repository, classification_repository, ai_invocation_repository, needs_you_repository,
):
    evidence, classification, ai_invocation_id = _make_ai_proposal(
        classification_repository, ai_invocation_repository, evidence_repository,
        document_type="BROKER_STATEMENT", status=STATUS_REVIEW_REQUIRED,
    )
    item = ensure_classification_review_item(
        classification=classification, evidence=evidence, ai_invocation_id=ai_invocation_id,
        needs_you_repository=needs_you_repository,
    )
    assert item.item_type == ITEM_TYPE_CLASSIFICATION_REVIEW
    assert item.allowed_action_type == ALLOWED_ACTION_CLASSIFICATION_REVIEW
    assert item.status == "OPEN"
    assert item.source_object_reference == classification.classification_id
    assert "BROKER_STATEMENT" in item.question
    open_items = needs_you_repository.list_needs_you_items(status="OPEN", item_type=ITEM_TYPE_CLASSIFICATION_REVIEW)
    assert len(open_items) == 1


def test_ensure_review_item_for_unknown_proposal_creates_exactly_one_open_item(
    evidence_repository, classification_repository, ai_invocation_repository, needs_you_repository,
):
    evidence, classification, ai_invocation_id = _make_ai_proposal(
        classification_repository, ai_invocation_repository, evidence_repository,
        document_type="UNKNOWN", status=STATUS_UNCLASSIFIABLE, confidence=0.05,
    )
    item = ensure_classification_review_item(
        classification=classification, evidence=evidence, ai_invocation_id=ai_invocation_id,
        needs_you_repository=needs_you_repository,
    )
    assert item.status == "OPEN"
    assert "could not determine" in item.question
    assert item.metadata["proposed_type"] == "UNKNOWN"


def test_ensure_review_item_replay_returns_same_item_not_a_second_one(
    evidence_repository, classification_repository, ai_invocation_repository, needs_you_repository,
):
    evidence, classification, ai_invocation_id = _make_ai_proposal(
        classification_repository, ai_invocation_repository, evidence_repository,
    )
    first = ensure_classification_review_item(
        classification=classification, evidence=evidence, ai_invocation_id=ai_invocation_id,
        needs_you_repository=needs_you_repository,
    )
    second = ensure_classification_review_item(
        classification=classification, evidence=evidence, ai_invocation_id=ai_invocation_id,
        needs_you_repository=needs_you_repository,
    )
    assert first.item_id == second.item_id
    assert len(needs_you_repository.list_needs_you_items(item_type=ITEM_TYPE_CLASSIFICATION_REVIEW)) == 1


def test_ensure_review_item_recovers_after_missed_producer_call(
    evidence_repository, classification_repository, ai_invocation_repository, needs_you_repository,
):
    """Simulates §8's crash-recovery case: the classification was
    persisted (by a prior, now-vanished call) but the review item was
    never created because the process died in between — a later
    (re-entrant) call must still create it."""
    evidence, classification, ai_invocation_id = _make_ai_proposal(
        classification_repository, ai_invocation_repository, evidence_repository,
    )
    assert needs_you_repository.list_needs_you_items(item_type=ITEM_TYPE_CLASSIFICATION_REVIEW) == []
    recovered = ensure_classification_review_item(
        classification=classification, evidence=evidence, ai_invocation_id=ai_invocation_id,
        needs_you_repository=needs_you_repository,
    )
    assert recovered.status == "OPEN"
    assert recovered.source_object_reference == classification.classification_id


def test_deterministic_and_operator_current_classifications_never_get_a_review_item(
    evidence_repository, classification_repository, needs_you_repository,
):
    """§9/§40 — this module's producer is only ever invoked by the
    orchestrator for an AI_PROPOSAL current classification; a
    DETERMINISTIC_RULE or OPERATOR_ASSIGNED current classification is
    proven (in `test_classification_orchestrator.py`'s own existing
    suite, re-run above) to never even reach a call to
    `ensure_classification_review_item` at all — this test proves the
    negative directly: nothing in this module's own list of open items
    is created for those sources by construction, since the only way an
    item is created is a real call to `ensure_classification_review_item`,
    which this test never makes."""
    evidence_id = _register_evidence(evidence_repository)
    classification_repository.create_classification(
        evidence_id=evidence_id, classification_type=CLASSIFICATION_TYPE_DOCUMENT_TYPE,
        document_type="RECEIPT", status=STATUS_CLASSIFIED, source=SOURCE_OPERATOR_ASSIGNED,
        operator_action_id="manual-op-1", expected_current_classification_id=None,
    )
    assert needs_you_repository.list_needs_you_items(item_type=ITEM_TYPE_CLASSIFICATION_REVIEW) == []


# ---------------------------------------------------------------------
# §47 — confirmation
# ---------------------------------------------------------------------


def test_confirmation_creates_operator_row_superseding_ai_proposal_leaves_ai_row_untouched(
    evidence_repository, classification_repository, ai_invocation_repository, rule_repository, audit_repository,
    needs_you_repository,
):
    evidence, ai_classification, ai_invocation_id = _make_ai_proposal(
        classification_repository, ai_invocation_repository, evidence_repository,
        document_type="BROKER_STATEMENT",
    )
    ai_before = ai_classification.to_dict()
    item = ensure_classification_review_item(
        classification=ai_classification, evidence=evidence, ai_invocation_id=ai_invocation_id,
        needs_you_repository=needs_you_repository,
    )

    result = _resolve(
        item, {"document_type": "BROKER_STATEMENT", "teach_rule": None},
        evidence_repository=evidence_repository, classification_repository=classification_repository,
        rule_repository=rule_repository, audit_repository=audit_repository,
    )

    assert result.decision == DECISION_CONFIRMED
    assert result.classification_was_created is True
    operator_classification = result.classification
    assert operator_classification.source == SOURCE_OPERATOR_ASSIGNED
    assert operator_classification.document_type == "BROKER_STATEMENT"
    assert operator_classification.status == STATUS_CLASSIFIED
    assert operator_classification.supersedes_classification_id == ai_classification.classification_id
    assert operator_classification.operator_action_id == item.item_id

    current = classification_repository.get_current_classification(
        evidence.evidence_id, CLASSIFICATION_TYPE_DOCUMENT_TYPE
    )
    assert current.classification_id == operator_classification.classification_id

    # AI row byte-identical before/after.
    ai_after = classification_repository.get_classification(ai_classification.classification_id).to_dict()
    assert ai_before == ai_after

    confirmed_events = audit_repository.list_by_subject("EvidenceClassification", operator_classification.classification_id)
    confirmed = [e for e in confirmed_events if e.event_type == EVIDENCE_CLASSIFICATION_CONFIRMED]
    assert len(confirmed) == 1


# ---------------------------------------------------------------------
# §48 — correction
# ---------------------------------------------------------------------


def test_correction_creates_immutable_two_row_lineage_with_one_corrected_audit(
    evidence_repository, classification_repository, ai_invocation_repository, rule_repository, audit_repository,
    needs_you_repository,
):
    evidence, ai_classification, ai_invocation_id = _make_ai_proposal(
        classification_repository, ai_invocation_repository, evidence_repository, document_type="RECEIPT",
    )
    item = ensure_classification_review_item(
        classification=ai_classification, evidence=evidence, ai_invocation_id=ai_invocation_id,
        needs_you_repository=needs_you_repository,
    )
    result = _resolve(
        item, {"document_type": "SUPPLIER_INVOICE", "teach_rule": None},
        evidence_repository=evidence_repository, classification_repository=classification_repository,
        rule_repository=rule_repository, audit_repository=audit_repository,
    )
    assert result.decision == DECISION_CORRECTED
    assert result.classification.document_type == "SUPPLIER_INVOICE"

    history = classification_repository.list_classification_history(
        evidence.evidence_id, CLASSIFICATION_TYPE_DOCUMENT_TYPE
    )
    assert [c.classification_id for c in history] == [
        ai_classification.classification_id, result.classification.classification_id,
    ]

    corrected_events = [
        e for e in audit_repository.list_by_subject("EvidenceClassification", result.classification.classification_id)
        if e.event_type == EVIDENCE_CLASSIFICATION_CORRECTED
    ]
    assert len(corrected_events) == 1


# ---------------------------------------------------------------------
# §49 — UNKNOWN
# ---------------------------------------------------------------------


def test_unknown_proposal_operator_picks_real_type_is_classified(
    evidence_repository, classification_repository, ai_invocation_repository, rule_repository, audit_repository,
    needs_you_repository,
):
    evidence, ai_classification, ai_invocation_id = _make_ai_proposal(
        classification_repository, ai_invocation_repository, evidence_repository,
        document_type="UNKNOWN", status=STATUS_UNCLASSIFIABLE, confidence=0.05,
    )
    item = ensure_classification_review_item(
        classification=ai_classification, evidence=evidence, ai_invocation_id=ai_invocation_id,
        needs_you_repository=needs_you_repository,
    )
    result = _resolve(
        item, {"document_type": "RECEIPT", "teach_rule": None},
        evidence_repository=evidence_repository, classification_repository=classification_repository,
        rule_repository=rule_repository, audit_repository=audit_repository,
    )
    assert result.decision == DECISION_CORRECTED
    assert result.classification.status == STATUS_CLASSIFIED
    assert result.classification.document_type == "RECEIPT"


def test_unknown_proposal_operator_confirms_unknown_stays_unclassifiable(
    evidence_repository, classification_repository, ai_invocation_repository, rule_repository, audit_repository,
    needs_you_repository,
):
    evidence, ai_classification, ai_invocation_id = _make_ai_proposal(
        classification_repository, ai_invocation_repository, evidence_repository,
        document_type="UNKNOWN", status=STATUS_UNCLASSIFIABLE, confidence=0.05,
    )
    item = ensure_classification_review_item(
        classification=ai_classification, evidence=evidence, ai_invocation_id=ai_invocation_id,
        needs_you_repository=needs_you_repository,
    )
    result = _resolve(
        item, {"document_type": "UNKNOWN", "teach_rule": None},
        evidence_repository=evidence_repository, classification_repository=classification_repository,
        rule_repository=rule_repository, audit_repository=audit_repository,
    )
    assert result.decision == DECISION_CONFIRMED
    assert result.classification.source == SOURCE_OPERATOR_ASSIGNED
    assert result.classification.document_type == "UNKNOWN"
    assert result.classification.status == STATUS_UNCLASSIFIABLE


# ---------------------------------------------------------------------
# §52 — current changed before resolution
# ---------------------------------------------------------------------


def test_resolution_fails_conflict_if_another_producer_superseded_ai_proposal_first(
    evidence_repository, classification_repository, ai_invocation_repository, rule_repository, audit_repository,
    needs_you_repository,
):
    evidence, ai_classification, ai_invocation_id = _make_ai_proposal(
        classification_repository, ai_invocation_repository, evidence_repository, document_type="RECEIPT",
    )
    item = ensure_classification_review_item(
        classification=ai_classification, evidence=evidence, ai_invocation_id=ai_invocation_id,
        needs_you_repository=needs_you_repository,
    )
    # A different producer races in and supersedes the AI proposal first.
    classification_repository.create_classification(
        evidence_id=evidence.evidence_id, classification_type=CLASSIFICATION_TYPE_DOCUMENT_TYPE,
        document_type="SUPPLIER_INVOICE", status=STATUS_CLASSIFIED, source=SOURCE_OPERATOR_ASSIGNED,
        operator_action_id="some-other-operator-action", supersedes_classification_id=ai_classification.classification_id,
        expected_current_classification_id=ai_classification.classification_id,
    )

    with pytest.raises(ConflictError):
        _resolve(
            item, {"document_type": "RECEIPT", "teach_rule": None},
            evidence_repository=evidence_repository, classification_repository=classification_repository,
            rule_repository=rule_repository, audit_repository=audit_repository,
        )

    assert item.status == "OPEN"  # the in-memory NeedsYouItem passed in was never mutated by this call
    fetched = needs_you_repository.get_needs_you_item(item.item_id)
    assert fetched.status == "OPEN"


# ---------------------------------------------------------------------
# §53 — partial-failure recovery
# ---------------------------------------------------------------------


def test_retry_after_operator_classification_created_reuses_same_row_no_duplicate_audit(
    evidence_repository, classification_repository, ai_invocation_repository, rule_repository, audit_repository,
    needs_you_repository,
):
    evidence, ai_classification, ai_invocation_id = _make_ai_proposal(
        classification_repository, ai_invocation_repository, evidence_repository, document_type="RECEIPT",
    )
    item = ensure_classification_review_item(
        classification=ai_classification, evidence=evidence, ai_invocation_id=ai_invocation_id,
        needs_you_repository=needs_you_repository,
    )
    resolution = {"document_type": "SUPPLIER_INVOICE", "teach_rule": None}
    first = _resolve(
        item, resolution, evidence_repository=evidence_repository, classification_repository=classification_repository,
        rule_repository=rule_repository, audit_repository=audit_repository,
    )
    # Simulate the process dying BEFORE the Needs You item was ever
    # resolved (the router would normally do that next) — the NeedsYouItem
    # is still OPEN, so a retry re-enters this same service function.
    second = _resolve(
        item, resolution, evidence_repository=evidence_repository, classification_repository=classification_repository,
        rule_repository=rule_repository, audit_repository=audit_repository,
    )
    assert second.classification.classification_id == first.classification.classification_id
    assert second.classification_was_created is False

    confirmed_events = [
        e for e in audit_repository.list_by_subject("EvidenceClassification", first.classification.classification_id)
        if e.event_type in (EVIDENCE_CLASSIFICATION_CONFIRMED, EVIDENCE_CLASSIFICATION_CORRECTED)
    ]
    assert len(confirmed_events) == 1


# ---------------------------------------------------------------------
# §54 — teaching happy path
# ---------------------------------------------------------------------


def test_confirmation_with_matching_teach_rule_creates_active_operator_rule(
    evidence_repository, classification_repository, ai_invocation_repository, rule_repository, audit_repository,
    needs_you_repository,
):
    evidence, ai_classification, ai_invocation_id = _make_ai_proposal(
        classification_repository, ai_invocation_repository, evidence_repository,
        sender_address="orders@ebay.com", subject="Order confirmed: #12345",
        document_type="ORDER_CONFIRMATION",
    )
    item = ensure_classification_review_item(
        classification=ai_classification, evidence=evidence, ai_invocation_id=ai_invocation_id,
        needs_you_repository=needs_you_repository,
    )
    result = _resolve(
        item,
        {
            "document_type": "ORDER_CONFIRMATION",
            "teach_rule": {
                "sender_scope_type": "EXACT_SENDER_DOMAIN", "sender_scope_value": "ebay.com",
                "subject_predicate_type": "STARTS_WITH", "subject_predicate_value": "Order confirmed:",
            },
        },
        evidence_repository=evidence_repository, classification_repository=classification_repository,
        rule_repository=rule_repository, audit_repository=audit_repository,
    )
    assert result.rule is not None
    assert result.rule_was_created is True
    assert result.rule.document_type == "ORDER_CONFIRMATION"
    assert result.rule.status == "ACTIVE"

    rule_created_events = [
        e for e in audit_repository.list_by_subject("EvidenceClassificationRule", result.rule.rule_id)
        if e.event_type == "EVIDENCE_CLASSIFICATION_RULE_CREATED"
    ]
    assert len(rule_created_events) == 1


# ---------------------------------------------------------------------
# §55 — teach-must-match-reviewed-evidence
# ---------------------------------------------------------------------


def test_teach_rule_not_matching_reviewed_evidence_is_rejected_but_classification_still_succeeds(
    evidence_repository, classification_repository, ai_invocation_repository, rule_repository, audit_repository,
    needs_you_repository,
):
    evidence, ai_classification, ai_invocation_id = _make_ai_proposal(
        classification_repository, ai_invocation_repository, evidence_repository,
        sender_address="orders@ebay.com", subject="Order confirmed: #12345",
        document_type="ORDER_CONFIRMATION",
    )
    item = ensure_classification_review_item(
        classification=ai_classification, evidence=evidence, ai_invocation_id=ai_invocation_id,
        needs_you_repository=needs_you_repository,
    )
    with pytest.raises(ValidationError):
        _resolve(
            item,
            {
                "document_type": "ORDER_CONFIRMATION",
                "teach_rule": {
                    "sender_scope_type": "EXACT_SENDER_DOMAIN", "sender_scope_value": "totally-different-domain.com",
                    "subject_predicate_type": "EXACT", "subject_predicate_value": "Order confirmed: #12345",
                },
            },
            evidence_repository=evidence_repository, classification_repository=classification_repository,
            rule_repository=rule_repository, audit_repository=audit_repository,
        )
    # §35 — the operator classification MAY already exist even though
    # the rule attempt failed.
    current = classification_repository.get_current_classification(
        evidence.evidence_id, CLASSIFICATION_TYPE_DOCUMENT_TYPE
    )
    assert current is not None
    assert current.source == SOURCE_OPERATOR_ASSIGNED
    assert rule_repository.list_rules() == []


# ---------------------------------------------------------------------
# §56 — teach replay
# ---------------------------------------------------------------------


def test_teach_rule_replay_before_needs_you_resolution_reuses_same_rule_no_duplicate_audits(
    evidence_repository, classification_repository, ai_invocation_repository, rule_repository, audit_repository,
    needs_you_repository,
):
    evidence, ai_classification, ai_invocation_id = _make_ai_proposal(
        classification_repository, ai_invocation_repository, evidence_repository,
        sender_address="orders@ebay.com", subject="Order confirmed: #12345",
        document_type="ORDER_CONFIRMATION",
    )
    item = ensure_classification_review_item(
        classification=ai_classification, evidence=evidence, ai_invocation_id=ai_invocation_id,
        needs_you_repository=needs_you_repository,
    )
    resolution = {
        "document_type": "ORDER_CONFIRMATION",
        "teach_rule": {
            "sender_scope_type": "EXACT_SENDER_DOMAIN", "sender_scope_value": "ebay.com",
            "subject_predicate_type": "STARTS_WITH", "subject_predicate_value": "Order confirmed:",
        },
    }
    first = _resolve(
        item, resolution, evidence_repository=evidence_repository, classification_repository=classification_repository,
        rule_repository=rule_repository, audit_repository=audit_repository,
    )
    second = _resolve(
        item, resolution, evidence_repository=evidence_repository, classification_repository=classification_repository,
        rule_repository=rule_repository, audit_repository=audit_repository,
    )
    assert second.classification.classification_id == first.classification.classification_id
    assert second.classification_was_created is False
    assert second.rule.rule_id == first.rule.rule_id
    assert second.rule_was_created is False

    rule_created_events = [
        e for e in audit_repository.list_by_subject("EvidenceClassificationRule", first.rule.rule_id)
        if e.event_type == "EVIDENCE_CLASSIFICATION_RULE_CREATED"
    ]
    assert len(rule_created_events) == 1
    classification_events = [
        e for e in audit_repository.list_by_subject("EvidenceClassification", first.classification.classification_id)
        if e.event_type in (EVIDENCE_CLASSIFICATION_CONFIRMED, EVIDENCE_CLASSIFICATION_CORRECTED)
    ]
    assert len(classification_events) == 1


# ---------------------------------------------------------------------
# §57 — teach conflict
# ---------------------------------------------------------------------


def test_teach_rule_conflicting_with_existing_active_rule_raises_conflict_needs_you_stays_open(
    evidence_repository, classification_repository, ai_invocation_repository, rule_repository, audit_repository,
    needs_you_repository,
):
    rule_repository.create_rule(
        sender_scope_type="EXACT_SENDER_DOMAIN", sender_scope_value="ebay.com",
        subject_predicate_type="STARTS_WITH", subject_predicate_value="Order confirmed:",
        document_type="RECEIPT", source="OPERATOR",
    )
    evidence, ai_classification, ai_invocation_id = _make_ai_proposal(
        classification_repository, ai_invocation_repository, evidence_repository,
        sender_address="orders@ebay.com", subject="Order confirmed: #99999",
        document_type="ORDER_CONFIRMATION",
    )
    item = ensure_classification_review_item(
        classification=ai_classification, evidence=evidence, ai_invocation_id=ai_invocation_id,
        needs_you_repository=needs_you_repository,
    )
    with pytest.raises(ConflictError):
        _resolve(
            item,
            {
                "document_type": "ORDER_CONFIRMATION",
                "teach_rule": {
                    "sender_scope_type": "EXACT_SENDER_DOMAIN", "sender_scope_value": "ebay.com",
                    "subject_predicate_type": "STARTS_WITH", "subject_predicate_value": "Order confirmed:",
                },
            },
            evidence_repository=evidence_repository, classification_repository=classification_repository,
            rule_repository=rule_repository, audit_repository=audit_repository,
        )
    # operator classification MAY already exist (§35); the pre-existing
    # rule must remain unchanged (no replacement/retirement).
    active_rules = rule_repository.list_rules(status="ACTIVE")
    assert len(active_rules) == 1
    assert active_rules[0].document_type == "RECEIPT"


# ---------------------------------------------------------------------
# Payload validation
# ---------------------------------------------------------------------


def test_resolution_missing_document_type_raises_validation_error(
    evidence_repository, classification_repository, ai_invocation_repository, rule_repository, audit_repository,
    needs_you_repository,
):
    evidence, ai_classification, ai_invocation_id = _make_ai_proposal(
        classification_repository, ai_invocation_repository, evidence_repository,
    )
    item = ensure_classification_review_item(
        classification=ai_classification, evidence=evidence, ai_invocation_id=ai_invocation_id,
        needs_you_repository=needs_you_repository,
    )
    with pytest.raises(ValidationError):
        _resolve(
            item, {"teach_rule": None},
            evidence_repository=evidence_repository, classification_repository=classification_repository,
            rule_repository=rule_repository, audit_repository=audit_repository,
        )


def test_resolution_with_invalid_document_type_raises_validation_error(
    evidence_repository, classification_repository, ai_invocation_repository, rule_repository, audit_repository,
    needs_you_repository,
):
    evidence, ai_classification, ai_invocation_id = _make_ai_proposal(
        classification_repository, ai_invocation_repository, evidence_repository,
    )
    item = ensure_classification_review_item(
        classification=ai_classification, evidence=evidence, ai_invocation_id=ai_invocation_id,
        needs_you_repository=needs_you_repository,
    )
    with pytest.raises(ValidationError):
        _resolve(
            item, {"document_type": "SOMETHING_MADE_UP", "teach_rule": None},
            evidence_repository=evidence_repository, classification_repository=classification_repository,
            rule_repository=rule_repository, audit_repository=audit_repository,
        )


def test_teach_rule_targeting_unknown_is_rejected_before_any_mutation(
    evidence_repository, classification_repository, ai_invocation_repository, rule_repository, audit_repository,
    needs_you_repository,
):
    evidence, ai_classification, ai_invocation_id = _make_ai_proposal(
        classification_repository, ai_invocation_repository, evidence_repository,
        document_type="UNKNOWN", status=STATUS_UNCLASSIFIABLE, confidence=0.05,
    )
    item = ensure_classification_review_item(
        classification=ai_classification, evidence=evidence, ai_invocation_id=ai_invocation_id,
        needs_you_repository=needs_you_repository,
    )
    with pytest.raises(ValidationError):
        _resolve(
            item,
            {
                "document_type": "UNKNOWN",
                "teach_rule": {
                    "sender_scope_type": "EXACT_SENDER_DOMAIN", "sender_scope_value": "vendor.com",
                    "subject_predicate_type": "EXACT", "subject_predicate_value": "whatever",
                },
            },
            evidence_repository=evidence_repository, classification_repository=classification_repository,
            rule_repository=rule_repository, audit_repository=audit_repository,
        )
    # §30 — no mutation at all: rejected during upfront payload
    # validation, before the operator classification is ever created.
    current = classification_repository.get_current_classification(
        evidence.evidence_id, CLASSIFICATION_TYPE_DOCUMENT_TYPE
    )
    assert current.classification_id == ai_classification.classification_id
    assert rule_repository.list_rules() == []
