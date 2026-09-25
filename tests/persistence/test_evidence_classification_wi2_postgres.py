"""CD-6 Slice 5 WI-2 PostgreSQL persistence/parity proofs — against a
REAL, disposable PostgreSQL container (the same session-scoped
`postgres_container`/`_clean_tables` fixtures every other file in this
directory shares). Mirrors `tests/persistence/test_evidence_classification_repository.py`'s
own style.

Covers:
* `PostgresEvidenceRepository.find_candidate_evidence_for_sender` —
  bounded, correct narrowing (domain, exact address, key-presence,
  never returns a manual-upload item).
* The full WI-2 governed create/retire/deterministic-classify flow
  running against REAL Postgres-backed repositories end to end (parity
  with the in-memory proofs in `tests/integration/`).
"""
from __future__ import annotations

import hashlib
from datetime import datetime, timezone

import pytest

from core import actor, identity
from core.audit import AuditRepository
from persistence.postgres.audit_repository import PostgresAuditRepository
from persistence.postgres.evidence_classification_repository import PostgresEvidenceClassificationRepository
from persistence.postgres.evidence_classification_rule_repository import PostgresEvidenceClassificationRuleRepository
from persistence.postgres.evidence_repository import PostgresEvidenceRepository
from persistence.postgres.external_reference_repository import PostgresExternalReferenceRepository
from persistence.postgres.source_repository import PostgresSourceRepository
from services.evidence.classification_rule_service import create_classification_rule, retire_classification_rule
from services.evidence.classification_service import (
    OUTCOME_CLASSIFIED,
    OUTCOME_CURRENT_CLASSIFICATION_EXISTS,
    OUTCOME_EXISTING,
    OUTCOME_NO_MATCH,
    classify_evidence_deterministically,
)


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _fabricated_content_hash(tag: str) -> str:
    return hashlib.sha256(tag.encode("utf-8")).hexdigest()


def _make_source():
    return PostgresSourceRepository().register_source(source_type="MAILBOX_TEST", provider="test", status="ACTIVE")


def _make_email_evidence(evidence_repository, *, sender_address=None, subject=None, evidence_type="EMAIL") -> str:
    source = _make_source()
    now = _utc_now()
    metadata = {}
    if sender_address is not None:
        metadata["sender_address"] = sender_address
    if subject is not None:
        metadata["subject"] = subject
    evidence = evidence_repository.register_evidence(
        entity_id=None, evidence_type=evidence_type, source_id=source.source_id, observed_at=now, received_at=now,
        content_hash=_fabricated_content_hash(identity.generate_id()), mime_type="message/rfc822", size_bytes=100,
        metadata=metadata,
    )
    return evidence.evidence_id


@pytest.fixture
def evidence_repository(fresh_engine):
    return PostgresEvidenceRepository(PostgresExternalReferenceRepository(engine=fresh_engine), engine=fresh_engine)


@pytest.fixture
def rule_repository(fresh_engine):
    return PostgresEvidenceClassificationRuleRepository(engine=fresh_engine)


@pytest.fixture
def classification_repository(fresh_engine, evidence_repository, rule_repository):
    from ai.invocation import InMemoryAIInvocationRepository

    return PostgresEvidenceClassificationRepository(
        evidence_repository=evidence_repository, rule_repository=rule_repository,
        ai_invocation_repository=InMemoryAIInvocationRepository(), engine=fresh_engine,
    )


@pytest.fixture
def audit_repository(fresh_engine) -> AuditRepository:
    return PostgresAuditRepository(engine=fresh_engine)


# ---------------------------------------------------------------------
# find_candidate_evidence_for_sender
# ---------------------------------------------------------------------


def test_candidate_query_matches_by_domain(evidence_repository):
    ev = _make_email_evidence(evidence_repository, sender_address="billing@vendor.com", subject="Monthly Statement")
    candidates = evidence_repository.find_candidate_evidence_for_sender(sender_domain="vendor.com")
    assert ev in {c.evidence_id for c in candidates}


def test_candidate_query_matches_by_exact_address_only(evidence_repository):
    match_ev = _make_email_evidence(evidence_repository, sender_address="ap@vendor.com", subject="Invoice 1")
    other_ev = _make_email_evidence(evidence_repository, sender_address="noreply@vendor.com", subject="Invoice 2")
    candidates = evidence_repository.find_candidate_evidence_for_sender(
        sender_domain="vendor.com", sender_address="ap@vendor.com"
    )
    ids = {c.evidence_id for c in candidates}
    assert match_ev in ids
    assert other_ev not in ids


def test_candidate_query_never_returns_manual_upload_evidence(evidence_repository):
    source = _make_source()
    now = _utc_now()
    manual = evidence_repository.register_evidence(
        entity_id=None, evidence_type="INVOICE", source_id=source.source_id, observed_at=now, received_at=now,
        content_hash=_fabricated_content_hash(identity.generate_id()), mime_type="application/pdf", size_bytes=200,
        metadata={}, original_name="invoice.pdf",
    )
    email_ev = _make_email_evidence(evidence_repository, sender_address="billing@vendor.com", subject="Monthly Statement")

    candidates = evidence_repository.find_candidate_evidence_for_sender(sender_domain="vendor.com")
    ids = {c.evidence_id for c in candidates}
    assert manual.evidence_id not in ids
    assert email_ev in ids


def test_candidate_query_is_bounded_by_limit(evidence_repository):
    for _ in range(5):
        _make_email_evidence(evidence_repository, sender_address="billing@vendor.com", subject="Monthly Statement")
    candidates = evidence_repository.find_candidate_evidence_for_sender(sender_domain="vendor.com", limit=2)
    assert len(candidates) == 2


def test_candidate_query_domain_narrowing_excludes_other_domains(evidence_repository):
    ev = _make_email_evidence(evidence_repository, sender_address="billing@vendor.com", subject="Monthly Statement")
    other = _make_email_evidence(evidence_repository, sender_address="billing@othercompany.com", subject="Monthly Statement")
    candidates = evidence_repository.find_candidate_evidence_for_sender(sender_domain="vendor.com")
    ids = {c.evidence_id for c in candidates}
    assert ev in ids
    assert other not in ids


# ---------------------------------------------------------------------
# End-to-end governed flow against real Postgres — parity with the
# in-memory proofs in tests/integration/.
# ---------------------------------------------------------------------


def test_full_governed_flow_against_postgres(evidence_repository, rule_repository, classification_repository, audit_repository):
    ev = _make_email_evidence(evidence_repository, sender_address="billing@vendor.com", subject="Monthly Statement")

    create_kwargs = dict(
        sender_scope_type="EXACT_SENDER_DOMAIN", sender_scope_value="vendor.com",
        subject_predicate_type="EXACT", subject_predicate_value="Monthly Statement",
        document_type="SUPPLIER_INVOICE", source="OPERATOR", actor_type=actor.SYSTEM, actor_id="pg-test",
        rule_repository=rule_repository, evidence_repository=evidence_repository, audit_repository=audit_repository,
    )
    created = create_classification_rule(**create_kwargs)
    assert created.was_created is True
    assert created.match_count == 1

    replay = create_classification_rule(**create_kwargs)
    assert replay.was_created is False
    assert replay.rule.rule_id == created.rule.rule_id

    events = audit_repository.list_by_subject("EvidenceClassificationRule", created.rule.rule_id)
    assert len(events) == 1

    classify_kwargs = dict(
        evidence_id=ev, evidence_repository=evidence_repository, rule_repository=rule_repository,
        classification_repository=classification_repository, audit_repository=audit_repository,
        actor_type=actor.SYSTEM, actor_id="pg-test",
    )
    classified = classify_evidence_deterministically(**classify_kwargs)
    assert classified.outcome == OUTCOME_CLASSIFIED
    assert classified.classification.rule_id == created.rule.rule_id

    replay_classify = classify_evidence_deterministically(**classify_kwargs)
    assert replay_classify.outcome == OUTCOME_EXISTING

    classification_events = audit_repository.list_by_subject(
        "EvidenceClassification", classified.classification.classification_id
    )
    assert len(classification_events) == 1

    retire_result = retire_classification_rule(
        rule_id=created.rule.rule_id, actor_type=actor.SYSTEM, actor_id="pg-test",
        rule_repository=rule_repository, audit_repository=audit_repository,
    )
    assert retire_result.was_retired_now is True
    retire_replay = retire_classification_rule(
        rule_id=created.rule.rule_id, actor_type=actor.SYSTEM, actor_id="pg-test",
        rule_repository=rule_repository, audit_repository=audit_repository,
    )
    assert retire_replay.was_retired_now is False
    retire_events = audit_repository.list_by_subject("EvidenceClassificationRule", created.rule.rule_id)
    retire_only = [e for e in retire_events if e.event_type == "EVIDENCE_CLASSIFICATION_RULE_RETIRED"]
    assert len(retire_only) == 1

    # entity_id untouched throughout.
    final_evidence = evidence_repository.get_evidence(ev)
    assert final_evidence.entity_id is None


def test_current_classification_exists_blocks_deterministic_classify_on_postgres(
    evidence_repository, rule_repository, classification_repository, audit_repository
):
    ev = _make_email_evidence(evidence_repository, sender_address="billing@vendor.com", subject="Monthly Statement")
    create_classification_rule(
        sender_scope_type="EXACT_SENDER_DOMAIN", sender_scope_value="vendor.com",
        subject_predicate_type="EXACT", subject_predicate_value="Monthly Statement",
        document_type="SUPPLIER_INVOICE", source="OPERATOR", actor_type=actor.SYSTEM, actor_id="pg-test",
        rule_repository=rule_repository, evidence_repository=evidence_repository, audit_repository=audit_repository,
    )
    classification_repository.create_classification(
        evidence_id=ev, classification_type="DOCUMENT_TYPE", document_type="RECEIPT", status="CLASSIFIED",
        source="OPERATOR_ASSIGNED", operator_action_id="op-pg-1",
    )
    result = classify_evidence_deterministically(
        evidence_id=ev, evidence_repository=evidence_repository, rule_repository=rule_repository,
        classification_repository=classification_repository, audit_repository=audit_repository,
        actor_type=actor.SYSTEM, actor_id="pg-test",
    )
    assert result.outcome == OUTCOME_CURRENT_CLASSIFICATION_EXISTS


def test_no_match_on_postgres(evidence_repository, rule_repository, classification_repository, audit_repository):
    ev = _make_email_evidence(evidence_repository, sender_address="billing@other.com", subject="Unrelated")
    result = classify_evidence_deterministically(
        evidence_id=ev, evidence_repository=evidence_repository, rule_repository=rule_repository,
        classification_repository=classification_repository, audit_repository=audit_repository,
        actor_type=actor.SYSTEM, actor_id="pg-test",
    )
    assert result.outcome == OUTCOME_NO_MATCH
