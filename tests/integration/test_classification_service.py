"""CD-6 Slice 5 WI-2 tests for
`services.evidence.classification_service.classify_evidence_deterministically`
— against in-memory repositories."""
from __future__ import annotations

from datetime import datetime, timezone

import pytest

from ai.invocation import InMemoryAIInvocationRepository
from core import actor, identity
from core.audit import InMemoryAuditRepository
from core.errors import NotFoundError
from core.external_reference import InMemoryExternalReferenceRepository
from services.evidence.classification import InMemoryEvidenceClassificationRepository
from services.evidence.classification_rule import InMemoryEvidenceClassificationRuleRepository
from services.evidence.classification_service import (
    OUTCOME_CLASSIFIED,
    OUTCOME_CONFLICT,
    OUTCOME_CURRENT_CLASSIFICATION_EXISTS,
    OUTCOME_EXISTING,
    OUTCOME_NO_APPLICABLE_RULE_INPUT,
    OUTCOME_NO_MATCH,
    classify_evidence_deterministically,
)
from services.evidence.evidence import InMemoryEvidenceRepository


@pytest.fixture
def evidence_repository():
    return InMemoryEvidenceRepository(InMemoryExternalReferenceRepository())


@pytest.fixture
def rule_repository():
    return InMemoryEvidenceClassificationRuleRepository()


@pytest.fixture
def classification_repository(evidence_repository, rule_repository):
    return InMemoryEvidenceClassificationRepository(
        evidence_repository=evidence_repository, rule_repository=rule_repository,
        ai_invocation_repository=InMemoryAIInvocationRepository(),
    )


@pytest.fixture
def audit_repository():
    return InMemoryAuditRepository()


def _register_email_evidence(evidence_repository, *, sender_address=None, subject=None, evidence_type="EMAIL") -> str:
    now = datetime.now(timezone.utc)
    metadata = {}
    if sender_address is not None:
        metadata["sender_address"] = sender_address
    if subject is not None:
        metadata["subject"] = subject
    item = evidence_repository.register_evidence(
        entity_id=None, evidence_type=evidence_type, source_id=identity.generate_id(),
        observed_at=now, received_at=now, content_hash="a" * 64, mime_type="message/rfc822", size_bytes=100,
        metadata=metadata,
    )
    return item.evidence_id


def _classify(evidence_id, evidence_repository, rule_repository, classification_repository, audit_repository):
    return classify_evidence_deterministically(
        evidence_id=evidence_id, evidence_repository=evidence_repository, rule_repository=rule_repository,
        classification_repository=classification_repository, audit_repository=audit_repository,
        actor_type=actor.SYSTEM, actor_id="test",
    )


def _active_rule(rule_repository, **overrides):
    kwargs = dict(
        sender_scope_type="EXACT_SENDER_DOMAIN", sender_scope_value="vendor.com",
        subject_predicate_type="EXACT", subject_predicate_value="Monthly Statement",
        document_type="SUPPLIER_INVOICE", source="OPERATOR",
    )
    kwargs.update(overrides)
    return rule_repository.create_rule(**kwargs)


# ---------------------------------------------------------------------
# No-current + match -> CLASSIFIED
# ---------------------------------------------------------------------


def test_no_current_plus_match_creates_classified_row_with_audit(
    evidence_repository, rule_repository, classification_repository, audit_repository
):
    rule = _active_rule(rule_repository)
    ev = _register_email_evidence(evidence_repository, sender_address="billing@vendor.com", subject="Monthly Statement")

    result = _classify(ev, evidence_repository, rule_repository, classification_repository, audit_repository)

    assert result.outcome == OUTCOME_CLASSIFIED
    assert result.classification.confidence is None
    assert result.classification.rule_id == rule.rule_id
    assert result.classification.source == "DETERMINISTIC_RULE"
    assert result.classification.status == "CLASSIFIED"

    events = audit_repository.list_by_subject("EvidenceClassification", result.classification.classification_id)
    assert len(events) == 1
    assert events[0].event_type == "EVIDENCE_CLASSIFIED"


# ---------------------------------------------------------------------
# Replay of the same rule -> EXISTING
# ---------------------------------------------------------------------


def test_replay_of_the_same_rule_is_existing_no_new_row_no_new_audit(
    evidence_repository, rule_repository, classification_repository, audit_repository
):
    _active_rule(rule_repository)
    ev = _register_email_evidence(evidence_repository, sender_address="billing@vendor.com", subject="Monthly Statement")

    first = _classify(ev, evidence_repository, rule_repository, classification_repository, audit_repository)
    second = _classify(ev, evidence_repository, rule_repository, classification_repository, audit_repository)

    assert first.outcome == OUTCOME_CLASSIFIED
    assert second.outcome == OUTCOME_EXISTING
    assert second.classification.classification_id == first.classification.classification_id

    events = audit_repository.list_by_subject("EvidenceClassification", first.classification.classification_id)
    assert len(events) == 1  # NOT 2


# ---------------------------------------------------------------------
# No-match / no-applicable-input / conflict -> nothing written
# ---------------------------------------------------------------------


def test_no_match_writes_nothing(evidence_repository, rule_repository, classification_repository, audit_repository):
    _active_rule(rule_repository, subject_predicate_value="Something Else")
    ev = _register_email_evidence(evidence_repository, sender_address="billing@vendor.com", subject="Monthly Statement")

    result = _classify(ev, evidence_repository, rule_repository, classification_repository, audit_repository)
    assert result.outcome == OUTCOME_NO_MATCH
    assert classification_repository.get_current_classification(ev, "DOCUMENT_TYPE") is None
    assert audit_repository.list_recent(limit=50) == []


def test_no_applicable_rule_input_writes_nothing(
    evidence_repository, rule_repository, classification_repository, audit_repository
):
    ev = _register_email_evidence(evidence_repository)  # no sender/subject at all — manual-upload-shaped
    result = _classify(ev, evidence_repository, rule_repository, classification_repository, audit_repository)
    assert result.outcome == OUTCOME_NO_APPLICABLE_RULE_INPUT
    assert audit_repository.list_recent(limit=50) == []


def test_conflict_writes_nothing(evidence_repository, rule_repository, classification_repository, audit_repository):
    from datetime import datetime, timezone as _tz

    now = datetime.now(_tz.utc)
    # Bypass the repository's own uniqueness (see
    # test_classification_matcher.py's own "CONFLICT reachability" note)
    # by inserting two identically-identitied ACTIVE rows directly.
    from services.evidence.classification_rule import EvidenceClassificationRule

    for rid in ("tie-a", "tie-b"):
        rule = EvidenceClassificationRule(
            rule_id=rid, sender_scope_type="EXACT_SENDER_DOMAIN", sender_scope_value="vendor.com",
            subject_predicate_type="EXACT", subject_predicate_value="monthly statement",
            document_type="SUPPLIER_INVOICE", status="ACTIVE", source="OPERATOR", created_at=now, approved_at=now,
        )
        rule_repository._by_id[rid] = rule  # noqa: SLF001 - deliberate corruption-scenario setup

    ev = _register_email_evidence(evidence_repository, sender_address="billing@vendor.com", subject="Monthly Statement")
    result = _classify(ev, evidence_repository, rule_repository, classification_repository, audit_repository)
    assert result.outcome == OUTCOME_CONFLICT
    assert set(result.conflicting_rule_ids) == {"tie-a", "tie-b"}
    assert classification_repository.get_current_classification(ev, "DOCUMENT_TYPE") is None
    assert audit_repository.list_recent(limit=50) == []


# ---------------------------------------------------------------------
# An existing classification from a DIFFERENT producer is never
# overwritten.
# ---------------------------------------------------------------------


def test_existing_classification_from_different_deterministic_rule_never_overwritten(
    evidence_repository, rule_repository, classification_repository, audit_repository
):
    other_rule = rule_repository.create_rule(
        sender_scope_type="EXACT_SENDER_DOMAIN", sender_scope_value="other.example",
        subject_predicate_type="EXACT", subject_predicate_value="whatever", document_type="RECEIPT", source="OPERATOR",
    )
    _active_rule(rule_repository)
    ev = _register_email_evidence(evidence_repository, sender_address="billing@vendor.com", subject="Monthly Statement")

    existing = classification_repository.create_classification(
        evidence_id=ev, classification_type="DOCUMENT_TYPE", document_type="RECEIPT", status="CLASSIFIED",
        source="DETERMINISTIC_RULE", rule_id=other_rule.rule_id,
    )

    result = _classify(ev, evidence_repository, rule_repository, classification_repository, audit_repository)
    assert result.outcome == OUTCOME_CURRENT_CLASSIFICATION_EXISTS
    assert result.existing_classification_id == existing.classification_id
    assert result.existing_source == "DETERMINISTIC_RULE"
    assert result.existing_document_type == "RECEIPT"
    # Unchanged.
    current = classification_repository.get_current_classification(ev, "DOCUMENT_TYPE")
    assert current.classification_id == existing.classification_id
    assert current.rule_id == other_rule.rule_id


def test_existing_classification_from_ai_proposal_never_overwritten(
    evidence_repository, rule_repository, classification_repository, audit_repository
):
    """Construct a synthetic AI_PROPOSAL-shaped current row directly
    (no real AI producer exists yet in this codebase) to prove a FUTURE
    producer's classification is equally protected."""
    ai_repo = InMemoryAIInvocationRepository()
    _active_rule(rule_repository)
    ev = _register_email_evidence(evidence_repository, sender_address="billing@vendor.com", subject="Monthly Statement")

    invocation = ai_repo.create_invocation(
        task_id="DOCUMENT_TYPE_PROPOSAL", task_version=1, role="BACKGROUND", provider="LITELLM",
        capability_alias="bagman-fast", input_references={"evidence_id": ev}, actor_type=actor.SYSTEM, actor_id="test",
    )
    ai_repo.transition_status(invocation.ai_invocation_id, "RUNNING")
    ai_repo.transition_status(invocation.ai_invocation_id, "SUCCEEDED")

    classification_repository_with_ai = InMemoryEvidenceClassificationRepository(
        evidence_repository=evidence_repository, rule_repository=rule_repository, ai_invocation_repository=ai_repo
    )

    existing = classification_repository_with_ai.create_classification(
        evidence_id=ev, classification_type="DOCUMENT_TYPE", document_type="RECEIPT", status="CLASSIFIED",
        source="AI_PROPOSAL", confidence=0.9, ai_invocation_id=invocation.ai_invocation_id,
    )

    result = _classify(ev, evidence_repository, rule_repository, classification_repository_with_ai, audit_repository)
    assert result.outcome == OUTCOME_CURRENT_CLASSIFICATION_EXISTS
    assert result.existing_classification_id == existing.classification_id
    assert result.existing_source == "AI_PROPOSAL"


def test_existing_classification_from_operator_assigned_never_overwritten(
    evidence_repository, rule_repository, classification_repository, audit_repository
):
    _active_rule(rule_repository)
    ev = _register_email_evidence(evidence_repository, sender_address="billing@vendor.com", subject="Monthly Statement")

    existing = classification_repository.create_classification(
        evidence_id=ev, classification_type="DOCUMENT_TYPE", document_type="RECEIPT", status="CLASSIFIED",
        source="OPERATOR_ASSIGNED", operator_action_id="op-action-1",
    )

    result = _classify(ev, evidence_repository, rule_repository, classification_repository, audit_repository)
    assert result.outcome == OUTCOME_CURRENT_CLASSIFICATION_EXISTS
    assert result.existing_classification_id == existing.classification_id
    assert result.existing_source == "OPERATOR_ASSIGNED"


# ---------------------------------------------------------------------
# EvidenceItem.entity_id is never touched.
# ---------------------------------------------------------------------


def test_entity_id_is_never_touched(evidence_repository, rule_repository, classification_repository, audit_repository):
    _active_rule(rule_repository)
    ev = _register_email_evidence(evidence_repository, sender_address="billing@vendor.com", subject="Monthly Statement")
    before = evidence_repository.get_evidence(ev)
    assert before.entity_id is None

    _classify(ev, evidence_repository, rule_repository, classification_repository, audit_repository)

    after = evidence_repository.get_evidence(ev)
    assert after.entity_id == before.entity_id
    assert after.entity_id is None


def test_unknown_evidence_id_raises_not_found(evidence_repository, rule_repository, classification_repository, audit_repository):
    with pytest.raises(NotFoundError):
        _classify(identity.generate_id(), evidence_repository, rule_repository, classification_repository, audit_repository)
