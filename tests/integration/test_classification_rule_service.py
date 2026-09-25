"""CD-6 Slice 5 WI-2 tests for
`services.evidence.classification_rule_service` — governed create/retire
lifecycle, against in-memory repositories."""
from __future__ import annotations

from datetime import datetime, timezone

import pytest

from core import actor, identity
from core.audit import InMemoryAuditRepository
from core.errors import ConflictError, NotFoundError, ValidationError
from core.external_reference import InMemoryExternalReferenceRepository
from services.evidence.classification_rule import InMemoryEvidenceClassificationRuleRepository
from services.evidence.classification_rule_service import (
    create_classification_rule,
    retire_classification_rule,
)
from services.evidence.evidence import InMemoryEvidenceRepository


@pytest.fixture
def evidence_repository():
    return InMemoryEvidenceRepository(InMemoryExternalReferenceRepository())


@pytest.fixture
def rule_repository():
    return InMemoryEvidenceClassificationRuleRepository()


@pytest.fixture
def audit_repository():
    return InMemoryAuditRepository()


def _register_email_evidence(evidence_repository, *, sender_address, subject) -> str:
    now = datetime.now(timezone.utc)
    item = evidence_repository.register_evidence(
        entity_id=None, evidence_type="EMAIL", source_id=identity.generate_id(),
        observed_at=now, received_at=now, content_hash="a" * 64, mime_type="message/rfc822", size_bytes=100,
        metadata={"sender_address": sender_address, "subject": subject},
    )
    return item.evidence_id


def _create(evidence_repository, rule_repository, audit_repository, **overrides):
    kwargs = dict(
        sender_scope_type="EXACT_SENDER_DOMAIN", sender_scope_value="vendor.com",
        subject_predicate_type="EXACT", subject_predicate_value="Monthly Statement",
        document_type="SUPPLIER_INVOICE", source="OPERATOR", actor_type=actor.SYSTEM, actor_id="test",
        rule_repository=rule_repository, evidence_repository=evidence_repository, audit_repository=audit_repository,
    )
    kwargs.update(overrides)
    return create_classification_rule(**kwargs)


# ---------------------------------------------------------------------
# Create
# ---------------------------------------------------------------------


def test_create_active_rule_with_observed_evidence(evidence_repository, rule_repository, audit_repository):
    _register_email_evidence(evidence_repository, sender_address="billing@vendor.com", subject="Monthly Statement")
    result = _create(evidence_repository, rule_repository, audit_repository)
    assert result.was_created is True
    assert result.match_count == 1
    assert result.rule.status == "ACTIVE"
    events = audit_repository.list_by_subject("EvidenceClassificationRule", result.rule.rule_id)
    assert len(events) == 1
    assert events[0].event_type == "EVIDENCE_CLASSIFICATION_RULE_CREATED"


def test_create_rejects_zero_observed_matches(evidence_repository, rule_repository, audit_repository):
    with pytest.raises(ValidationError):
        _create(evidence_repository, rule_repository, audit_repository)
    assert audit_repository.list_recent(limit=50) == []


def test_create_rejects_bagman_proposed_source(evidence_repository, rule_repository, audit_repository):
    _register_email_evidence(evidence_repository, sender_address="billing@vendor.com", subject="Monthly Statement")
    with pytest.raises(ValidationError):
        _create(evidence_repository, rule_repository, audit_repository, source="BAGMAN_PROPOSED")


def test_identical_replay_is_a_no_op_no_duplicate_audit(evidence_repository, rule_repository, audit_repository):
    _register_email_evidence(evidence_repository, sender_address="billing@vendor.com", subject="Monthly Statement")
    first = _create(evidence_repository, rule_repository, audit_repository)
    second = _create(evidence_repository, rule_repository, audit_repository)

    assert first.rule.rule_id == second.rule.rule_id
    assert first.was_created is True
    assert second.was_created is False

    events = audit_repository.list_by_subject("EvidenceClassificationRule", first.rule.rule_id)
    assert len(events) == 1  # NOT 2 — no duplicate audit event on replay


def test_semantic_conflict_same_identity_different_document_type_rejected(
    evidence_repository, rule_repository, audit_repository
):
    _register_email_evidence(evidence_repository, sender_address="billing@vendor.com", subject="Monthly Statement")
    _create(evidence_repository, rule_repository, audit_repository, document_type="SUPPLIER_INVOICE")
    with pytest.raises(ConflictError):
        _create(evidence_repository, rule_repository, audit_repository, document_type="RECEIPT")


# ---------------------------------------------------------------------
# Retire
# ---------------------------------------------------------------------


def test_retire_transitions_active_to_retired_with_one_audit_event(evidence_repository, rule_repository, audit_repository):
    _register_email_evidence(evidence_repository, sender_address="billing@vendor.com", subject="Monthly Statement")
    created = _create(evidence_repository, rule_repository, audit_repository)

    result = retire_classification_rule(
        rule_id=created.rule.rule_id, actor_type=actor.SYSTEM, actor_id="test",
        rule_repository=rule_repository, audit_repository=audit_repository,
    )
    assert result.was_retired_now is True
    assert result.rule.status == "RETIRED"
    events = audit_repository.list_by_subject("EvidenceClassificationRule", created.rule.rule_id)
    retire_events = [e for e in events if e.event_type == "EVIDENCE_CLASSIFICATION_RULE_RETIRED"]
    assert len(retire_events) == 1


def test_retire_replay_is_safe_no_duplicate_audit(evidence_repository, rule_repository, audit_repository):
    _register_email_evidence(evidence_repository, sender_address="billing@vendor.com", subject="Monthly Statement")
    created = _create(evidence_repository, rule_repository, audit_repository)

    first = retire_classification_rule(
        rule_id=created.rule.rule_id, actor_type=actor.SYSTEM, actor_id="test",
        rule_repository=rule_repository, audit_repository=audit_repository,
    )
    second = retire_classification_rule(
        rule_id=created.rule.rule_id, actor_type=actor.SYSTEM, actor_id="test",
        rule_repository=rule_repository, audit_repository=audit_repository,
    )
    assert first.was_retired_now is True
    assert second.was_retired_now is False

    events = audit_repository.list_by_subject("EvidenceClassificationRule", created.rule.rule_id)
    retire_events = [e for e in events if e.event_type == "EVIDENCE_CLASSIFICATION_RULE_RETIRED"]
    assert len(retire_events) == 1  # NOT 2


def test_retire_unknown_rule_raises_not_found(rule_repository, audit_repository):
    with pytest.raises(NotFoundError):
        retire_classification_rule(
            rule_id=identity.generate_id(), actor_type=actor.SYSTEM, actor_id="test",
            rule_repository=rule_repository, audit_repository=audit_repository,
        )


def test_retired_rule_excluded_from_active_listing(evidence_repository, rule_repository, audit_repository):
    _register_email_evidence(evidence_repository, sender_address="billing@vendor.com", subject="Monthly Statement")
    created = _create(evidence_repository, rule_repository, audit_repository)
    retire_classification_rule(
        rule_id=created.rule.rule_id, actor_type=actor.SYSTEM, actor_id="test",
        rule_repository=rule_repository, audit_repository=audit_repository,
    )
    active_rules = rule_repository.list_rules(status="ACTIVE")
    assert created.rule.rule_id not in {r.rule_id for r in active_rules}


def test_replacement_rule_after_retirement_can_be_created(evidence_repository, rule_repository, audit_repository):
    _register_email_evidence(evidence_repository, sender_address="billing@vendor.com", subject="Monthly Statement")
    created = _create(evidence_repository, rule_repository, audit_repository, document_type="SUPPLIER_INVOICE")
    retire_classification_rule(
        rule_id=created.rule.rule_id, actor_type=actor.SYSTEM, actor_id="test",
        rule_repository=rule_repository, audit_repository=audit_repository,
    )
    replacement = _create(evidence_repository, rule_repository, audit_repository, document_type="RECEIPT")
    assert replacement.was_created is True
    assert replacement.rule.rule_id != created.rule.rule_id
    assert replacement.rule.document_type == "RECEIPT"
