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
import threading
import time
from datetime import datetime, timezone

import pytest

from ai.providers.litellm.fake import FakeLiteLLMClient
from core import actor, identity
from persistence.postgres.ai_invocation_repository import PostgresAIInvocationRepository
from persistence.postgres.audit_repository import PostgresAuditRepository
from persistence.postgres.evidence_classification_models import EvidenceClassificationRow
from persistence.postgres.evidence_classification_repository import PostgresEvidenceClassificationRepository
from persistence.postgres.evidence_classification_rule_repository import PostgresEvidenceClassificationRuleRepository
from persistence.postgres.evidence_repository import PostgresEvidenceRepository
from persistence.postgres.external_reference_repository import PostgresExternalReferenceRepository
from persistence.postgres.session import get_engine, session_scope
from persistence.postgres.source_repository import PostgresSourceRepository
from persistence.objects.memory_store import InMemoryObjectStore
from services.evidence.classification_orchestrator import (
    OUTCOME_AI_PROPOSAL_REVIEW_REQUIRED,
    OUTCOME_CURRENT_CLASSIFICATION_EXISTS,
    OUTCOME_DETERMINISTIC_CLASSIFIED,
    classify_evidence,
)
from services.evidence.classification_service import (
    OUTCOME_CLASSIFIED as DET_OUTCOME_CLASSIFIED,
    OUTCOME_EXISTING as DET_OUTCOME_EXISTING,
    classify_evidence_deterministically,
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


# ---------------------------------------------------------------------
# CD-6 cross-WI concurrency correction (2026-09-25) — genuine
# real-threaded concurrency proofs against the real database, at both
# the WI-2 service layer (§10) and the full public WI-3 orchestration
# service (§12: "prove the public persistent orchestration service
# itself... do not test only repository internals"). Each worker uses
# its OWN default-engine-backed repository instances (never a shared
# `fresh_engine` fixture across threads) — the same discipline
# `tests/persistence/test_evidence_classification_repository.py`'s own
# genuine-threaded-race tests establish.
# ---------------------------------------------------------------------


def _default_evidence_repository() -> PostgresEvidenceRepository:
    return PostgresEvidenceRepository(PostgresExternalReferenceRepository())


def _default_rule_repository() -> PostgresEvidenceClassificationRuleRepository:
    return PostgresEvidenceClassificationRuleRepository()


def _default_audit_repository() -> PostgresAuditRepository:
    return PostgresAuditRepository()


def _default_ai_invocation_repository() -> PostgresAIInvocationRepository:
    return PostgresAIInvocationRepository(get_engine(), audit_repository=_default_audit_repository())


def _default_classification_repository() -> PostgresEvidenceClassificationRepository:
    return PostgresEvidenceClassificationRepository(
        evidence_repository=_default_evidence_repository(), rule_repository=_default_rule_repository(),
        ai_invocation_repository=_default_ai_invocation_repository(),
    )


def test_concurrent_deterministic_classification_race_on_real_postgres():
    """WI-3-correction §10 — 8 genuinely concurrent callers to
    `classify_evidence_deterministically` (WI-2's own service function,
    directly — not the full orchestrator) for the SAME evidence item
    and the SAME single matching ACTIVE rule. Require exactly 1
    classification row, exactly 1 distinct classification_id returned,
    exactly 1 EVIDENCE_CLASSIFIED audit event, exactly one caller
    CLASSIFIED, every other caller EXISTING, no raw DB error."""
    rule_repo = _default_rule_repository()
    rule = rule_repo.create_rule(
        sender_scope_type="EXACT_SENDER_DOMAIN", sender_scope_value=f"wi3-corr-race-{identity.generate_id()}.example",
        subject_predicate_type="EXACT", subject_predicate_value="Concurrent Statement",
        document_type="SUPPLIER_INVOICE", source="OPERATOR",
    )
    source = PostgresSourceRepository().register_source(source_type="MAILBOX_TEST", provider="test", status="ACTIVE")
    now = datetime.now(timezone.utc)
    evidence = _default_evidence_repository().register_evidence(
        entity_id=None, evidence_type="EMAIL", source_id=source.source_id, observed_at=now, received_at=now,
        content_hash={"algorithm": "SHA-256", "value": hashlib.sha256(identity.generate_id().encode()).hexdigest()},
        mime_type="message/rfc822", size_bytes=100,
        metadata={"sender_address": f"billing@{rule.sender_scope_value}", "subject": "Concurrent Statement"},
    )
    evidence_id = evidence.evidence_id

    n_workers = 8
    barrier = threading.Barrier(n_workers)
    results: list[dict] = [{} for _ in range(n_workers)]

    def _worker(index: int) -> None:
        evidence_repo = _default_evidence_repository()
        rule_repo_local = _default_rule_repository()
        classification_repo = _default_classification_repository()
        audit_repo = _default_audit_repository()
        barrier.wait()
        start = time.monotonic()
        outcome = classify_evidence_deterministically(
            evidence_id=evidence_id, evidence_repository=evidence_repo, rule_repository=rule_repo_local,
            classification_repository=classification_repo, audit_repository=audit_repo,
            actor_type="SYSTEM", actor_id=f"wi3-corr-det-race-{index}",
        )
        results[index] = {
            "outcome": outcome.outcome, "classification_id": outcome.classification.classification_id,
            "start": start, "end": time.monotonic(),
        }

    threads = [threading.Thread(target=_worker, args=(i,)) for i in range(n_workers)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=30)

    assert all(r for r in results), f"one or more worker threads did not complete: {results}"

    classification_ids = {r["classification_id"] for r in results}
    assert len(classification_ids) == 1, f"expected all callers to agree on ONE classification_id: {results}"
    winning_classification_id = next(iter(classification_ids))

    classified = [r for r in results if r["outcome"] == DET_OUTCOME_CLASSIFIED]
    existing = [r for r in results if r["outcome"] == DET_OUTCOME_EXISTING]
    assert len(classified) == 1, f"expected exactly ONE CLASSIFIED outcome, got {len(classified)}: {results}"
    assert len(existing) == n_workers - 1, f"expected every other caller to report EXISTING: {results}"

    windows = [(r["start"], r["end"]) for r in results]
    overlap_found = any(
        a_start < b_end and b_start < a_end
        for i, (a_start, a_end) in enumerate(windows)
        for j, (b_start, b_end) in enumerate(windows)
        if i < j
    )
    assert overlap_found, f"no genuine wall-clock overlap detected between worker call windows: {windows}"

    with session_scope(get_engine()) as session:
        count = session.query(EvidenceClassificationRow).filter_by(evidence_id=evidence_id).count()
        assert count == 1

    audit_events = _default_audit_repository().list_by_subject("EvidenceClassification", winning_classification_id)
    created_events = [e for e in audit_events if e.event_type == "EVIDENCE_CLASSIFIED"]
    assert len(created_events) == 1, f"expected exactly ONE EVIDENCE_CLASSIFIED audit event, got {len(created_events)}"


def test_concurrent_orchestrated_persist_reusing_succeeded_invocation_converges_to_one_classification():
    """WI-3-correction §11/§12 — proves the PUBLIC persistent
    orchestration service itself (`classify_evidence(persist=True)`),
    not only repository internals. Starting from a reusable,
    already-SUCCEEDED `DOCUMENT_TYPE_PROPOSAL` v2 invocation and zero
    classification rows, N genuinely concurrent
    `classify_evidence(persist=True)` calls (each with its OWN
    repository/litellm-client instances) must converge on exactly ONE
    classification row, ONE classification audit, and ZERO real AI
    provider calls during the race (the invocation is reused via §25's
    idempotency path by every caller — `run_background_task` is never
    reached)."""
    source = PostgresSourceRepository().register_source(source_type="MAILBOX_TEST", provider="test", status="ACTIVE")
    now = datetime.now(timezone.utc)
    import email.message

    msg = email.message.EmailMessage()
    msg["From"] = "billing@wi3-corr-ai-race.example"
    msg["Subject"] = "Concurrent AI Persist Race"
    msg.set_content("Please pay $250 for services rendered.")
    content = bytes(msg)
    content_hash = {"algorithm": "SHA-256", "value": hashlib.sha256(content).hexdigest()}
    object_store_instance = InMemoryObjectStore()
    storage_reference = object_store_instance.put(identity.generate_id(), content_hash, content)
    evidence = _default_evidence_repository().register_evidence(
        entity_id=None, evidence_type="EMAIL", source_id=source.source_id, observed_at=now, received_at=now,
        content_hash=content_hash, mime_type="message/rfc822", size_bytes=len(content),
        storage_reference=storage_reference,
        metadata={"sender_address": "billing@wi3-corr-ai-race.example", "subject": "Concurrent AI Persist Race"},
    )
    evidence_id = evidence.evidence_id

    # Establish the ONE already-SUCCEEDED invocation every racing caller
    # will independently resolve/reuse via the exact same fingerprint —
    # a genuinely new call would go through `run_background_task`
    # (which each thread's own FakeLiteLLMClient below would record).
    setup_litellm_client = FakeLiteLLMClient()
    setup_litellm_client.queue_success(
        capability_alias="bagman-core",
        content=json.dumps({"proposed_type": "SUPPLIER_INVOICE", "confidence": 0.88, "signals": [], "warnings": []}),
    )
    setup_audit_repository = _default_audit_repository()
    setup_result = classify_evidence(
        evidence_id=evidence_id, persist=False,
        evidence_repository=_default_evidence_repository(), rule_repository=_default_rule_repository(),
        classification_repository=_default_classification_repository(),
        ai_invocation_repository=_default_ai_invocation_repository(),
        litellm_client=setup_litellm_client, object_store=object_store_instance,
        audit_repository=setup_audit_repository, record_audit_event=setup_audit_repository.record_audit_event,
        actor_type="SYSTEM", actor_id="wi3-corr-ai-race-setup",
    )
    assert setup_result.ai_invocation_id is not None
    assert len(setup_litellm_client.calls) == 1

    n_workers = 8
    barrier = threading.Barrier(n_workers)
    results: list[dict] = [{} for _ in range(n_workers)]
    litellm_clients = [FakeLiteLLMClient() for _ in range(n_workers)]

    def _worker(index: int) -> None:
        classification_repo = _default_classification_repository()
        audit_repo = _default_audit_repository()
        barrier.wait()
        start = time.monotonic()
        result = classify_evidence(
            evidence_id=evidence_id, persist=True,
            evidence_repository=_default_evidence_repository(), rule_repository=_default_rule_repository(),
            classification_repository=classification_repo,
            ai_invocation_repository=_default_ai_invocation_repository(),
            litellm_client=litellm_clients[index], object_store=object_store_instance,
            audit_repository=audit_repo, record_audit_event=audit_repo.record_audit_event,
            actor_type="SYSTEM", actor_id=f"wi3-corr-ai-race-{index}",
        )
        results[index] = {
            "outcome": result.outcome,
            "classification_id": result.classification.classification_id if result.classification else None,
            "was_created": result.was_created, "start": start, "end": time.monotonic(),
        }

    threads = [threading.Thread(target=_worker, args=(i,)) for i in range(n_workers)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=30)

    assert all(r for r in results), f"one or more worker threads did not complete: {results}"

    # Zero real AI provider calls during the race — every caller reused
    # the already-SUCCEEDED invocation from setup.
    total_race_calls = sum(len(c.calls) for c in litellm_clients)
    assert total_race_calls == 0, f"expected ZERO AI provider calls during the race, got {total_race_calls}"

    classification_ids = {r["classification_id"] for r in results}
    assert len(classification_ids) == 1, f"expected all callers to agree on ONE classification_id: {results}"
    winning_classification_id = next(iter(classification_ids))

    created_true = [r for r in results if r["was_created"] is True]
    created_false = [r for r in results if r["was_created"] is False]
    assert len(created_true) == 1, f"expected exactly ONE was_created=True, got {len(created_true)}: {results}"
    assert len(created_false) == n_workers - 1, f"expected every other caller was_created=False: {results}"

    windows = [(r["start"], r["end"]) for r in results]
    overlap_found = any(
        a_start < b_end and b_start < a_end
        for i, (a_start, a_end) in enumerate(windows)
        for j, (b_start, b_end) in enumerate(windows)
        if i < j
    )
    assert overlap_found, f"no genuine wall-clock overlap detected between worker call windows: {windows}"

    with session_scope(get_engine()) as session:
        count = session.query(EvidenceClassificationRow).filter_by(evidence_id=evidence_id).count()
        assert count == 1

    audit_events = _default_audit_repository().list_by_subject("EvidenceClassification", winning_classification_id)
    created_events = [e for e in audit_events if e.event_type == "EVIDENCE_CLASSIFIED"]
    assert len(created_events) == 1, f"expected exactly ONE EVIDENCE_CLASSIFIED audit event, got {len(created_events)}"
