"""CD-6 Slice 5 WI-3 PostgreSQL parity proofs — the full governed
AI-fallback classification orchestrator
(`services.evidence.classification_orchestrator.classify_evidence`)
running end to end against REAL, disposable PostgreSQL-backed
repositories (the same session-scoped `postgres_container`/`fresh_engine`
fixtures every other file in this directory shares). Mirrors
`tests/persistence/test_evidence_classification_wi2_postgres.py`'s own
style: a focused subset of proofs re-run against real Postgres for
parity with the exhaustive in-memory suite in `tests/integration/`, not
an exhaustive re-duplication of every case.

`FakeLiteLLMClient` only (PID §61) — no real/live model call.
"""
from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone

import pytest

from ai.providers.litellm.fake import FakeLiteLLMClient
from core import actor, identity
from persistence.postgres.ai_invocation_repository import PostgresAIInvocationRepository
from persistence.postgres.audit_repository import PostgresAuditRepository
from persistence.postgres.evidence_classification_repository import PostgresEvidenceClassificationRepository
from persistence.postgres.evidence_classification_rule_repository import PostgresEvidenceClassificationRuleRepository
from persistence.postgres.evidence_repository import PostgresEvidenceRepository
from persistence.postgres.external_reference_repository import PostgresExternalReferenceRepository
from persistence.postgres.source_repository import PostgresSourceRepository
from persistence.objects.memory_store import InMemoryObjectStore
from services.evidence.classification_orchestrator import (
    OUTCOME_AI_PROPOSAL_REVIEW_REQUIRED,
    OUTCOME_CURRENT_CLASSIFICATION_EXISTS,
    OUTCOME_DETERMINISTIC_CLASSIFIED,
    classify_evidence,
)

ACTOR_ID = "wi3-postgres-parity-tests"


def _utc_now():
    return datetime.now(timezone.utc)


@pytest.fixture
def evidence_repository(fresh_engine):
    return PostgresEvidenceRepository(PostgresExternalReferenceRepository(engine=fresh_engine), engine=fresh_engine)


@pytest.fixture
def rule_repository(fresh_engine):
    return PostgresEvidenceClassificationRuleRepository(engine=fresh_engine)


@pytest.fixture
def audit_repository(fresh_engine):
    return PostgresAuditRepository(engine=fresh_engine)


@pytest.fixture
def ai_invocation_repository(fresh_engine, audit_repository):
    return PostgresAIInvocationRepository(fresh_engine, audit_repository=audit_repository)


@pytest.fixture
def classification_repository(fresh_engine, evidence_repository, rule_repository, ai_invocation_repository):
    return PostgresEvidenceClassificationRepository(
        evidence_repository=evidence_repository, rule_repository=rule_repository,
        ai_invocation_repository=ai_invocation_repository, engine=fresh_engine,
    )


@pytest.fixture
def object_store():
    return InMemoryObjectStore()


@pytest.fixture
def litellm_client():
    return FakeLiteLLMClient()


def _register_email_evidence(evidence_repository, object_store, *, sender_address=None, subject=None) -> str:
    import email.message

    msg = email.message.EmailMessage()
    msg["From"] = sender_address or "billing@vendor.com"
    msg["Subject"] = subject or "Invoice 42"
    msg.set_content("Please pay $500 for services rendered.")
    content = bytes(msg)
    content_hash = {"algorithm": "SHA-256", "value": hashlib.sha256(content).hexdigest()}
    storage_reference = object_store.put(identity.generate_id(), content_hash, content)
    source = PostgresSourceRepository().register_source(source_type="MAILBOX_TEST", provider="test", status="ACTIVE")
    metadata = {}
    if sender_address is not None:
        metadata["sender_address"] = sender_address
    if subject is not None:
        metadata["subject"] = subject
    now = _utc_now()
    evidence = evidence_repository.register_evidence(
        entity_id=None, evidence_type="EMAIL", source_id=source.source_id, observed_at=now, received_at=now,
        content_hash=content_hash, mime_type="message/rfc822", size_bytes=len(content),
        storage_reference=storage_reference, metadata=metadata,
    )
    return evidence.evidence_id


def _classify(evidence_id, *, persist, evidence_repository, rule_repository, classification_repository,
              ai_invocation_repository, litellm_client, object_store, audit_repository):
    return classify_evidence(
        evidence_id=evidence_id, persist=persist,
        evidence_repository=evidence_repository, rule_repository=rule_repository,
        classification_repository=classification_repository, ai_invocation_repository=ai_invocation_repository,
        litellm_client=litellm_client, object_store=object_store, audit_repository=audit_repository,
        record_audit_event=audit_repository.record_audit_event,
        actor_type=actor.SYSTEM, actor_id=ACTOR_ID,
    )


def test_full_orchestrated_flow_persists_review_required_on_real_postgres(
    fresh_engine, evidence_repository, rule_repository, classification_repository, ai_invocation_repository,
    litellm_client, object_store, audit_repository,
):
    evidence_id = _register_email_evidence(evidence_repository, object_store)
    litellm_client.queue_success(
        capability_alias="bagman-core",
        content=json.dumps({"proposed_type": "SUPPLIER_INVOICE", "confidence": 0.92, "signals": [], "warnings": []}),
    )
    result = _classify(
        evidence_id, persist=True, evidence_repository=evidence_repository, rule_repository=rule_repository,
        classification_repository=classification_repository, ai_invocation_repository=ai_invocation_repository,
        litellm_client=litellm_client, object_store=object_store, audit_repository=audit_repository,
    )
    assert result.outcome == OUTCOME_AI_PROPOSAL_REVIEW_REQUIRED
    assert result.was_created is True
    assert result.classification.source == "AI_PROPOSAL"
    assert result.classification.status == "REVIEW_REQUIRED"

    # Re-read via a FRESH repository instance against the same engine —
    # proves the write is durable, not merely cached in-process.
    reread_classification_repository = PostgresEvidenceClassificationRepository(
        evidence_repository=evidence_repository, rule_repository=rule_repository,
        ai_invocation_repository=ai_invocation_repository, engine=fresh_engine,
    )
    current = reread_classification_repository.get_current_classification(evidence_id, "DOCUMENT_TYPE")
    assert current is not None
    assert current.classification_id == result.classification.classification_id


def test_deterministic_match_takes_precedence_over_ai_on_real_postgres(
    evidence_repository, rule_repository, classification_repository, ai_invocation_repository,
    litellm_client, object_store, audit_repository,
):
    rule_repository.create_rule(
        sender_scope_type="EXACT_SENDER_DOMAIN", sender_scope_value="vendor.com",
        subject_predicate_type="EXACT", subject_predicate_value="Invoice 42",
        document_type="SUPPLIER_INVOICE", source="OPERATOR",
    )
    evidence_id = _register_email_evidence(
        evidence_repository, object_store, sender_address="billing@vendor.com", subject="Invoice 42",
    )
    result = _classify(
        evidence_id, persist=True, evidence_repository=evidence_repository, rule_repository=rule_repository,
        classification_repository=classification_repository, ai_invocation_repository=ai_invocation_repository,
        litellm_client=litellm_client, object_store=object_store, audit_repository=audit_repository,
    )
    assert result.outcome == OUTCOME_DETERMINISTIC_CLASSIFIED
    assert litellm_client.calls == []


def test_idempotent_reuse_across_two_calls_on_real_postgres(
    evidence_repository, rule_repository, classification_repository, ai_invocation_repository,
    litellm_client, object_store, audit_repository,
):
    evidence_id = _register_email_evidence(evidence_repository, object_store)
    litellm_client.queue_success(
        capability_alias="bagman-core",
        content=json.dumps({"proposed_type": "SUPPLIER_INVOICE", "confidence": 0.8, "signals": [], "warnings": []}),
    )
    first = _classify(
        evidence_id, persist=False, evidence_repository=evidence_repository, rule_repository=rule_repository,
        classification_repository=classification_repository, ai_invocation_repository=ai_invocation_repository,
        litellm_client=litellm_client, object_store=object_store, audit_repository=audit_repository,
    )
    second = _classify(
        evidence_id, persist=False, evidence_repository=evidence_repository, rule_repository=rule_repository,
        classification_repository=classification_repository, ai_invocation_repository=ai_invocation_repository,
        litellm_client=litellm_client, object_store=object_store, audit_repository=audit_repository,
    )
    assert len(litellm_client.calls) == 1
    assert first.ai_invocation_id == second.ai_invocation_id


def test_current_classification_guard_against_real_postgres(
    evidence_repository, rule_repository, classification_repository, ai_invocation_repository,
    litellm_client, object_store, audit_repository,
):
    evidence_id = _register_email_evidence(evidence_repository, object_store)
    classification_repository.create_classification(
        evidence_id=evidence_id, classification_type="DOCUMENT_TYPE", document_type="RECEIPT",
        status="CLASSIFIED", source="OPERATOR_ASSIGNED", operator_action_id="op-pg-1",
        expected_current_classification_id=None,
    )
    result = _classify(
        evidence_id, persist=True, evidence_repository=evidence_repository, rule_repository=rule_repository,
        classification_repository=classification_repository, ai_invocation_repository=ai_invocation_repository,
        litellm_client=litellm_client, object_store=object_store, audit_repository=audit_repository,
    )
    assert result.outcome == OUTCOME_CURRENT_CLASSIFICATION_EXISTS
    assert litellm_client.calls == []
