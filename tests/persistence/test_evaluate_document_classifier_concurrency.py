"""CD-6 Slice 5 WI-6 §33-35 — real-threaded concurrency/load proofs for
`scripts/evaluate_document_classifier.py` against a REAL, disposable
PostgreSQL container (the same session-scoped `postgres_container`/
`fresh_engine` fixtures every other file in this directory shares).

Mirrors `tests/persistence/test_document_type_proposal_v2_postgres.py`'s
own genuine-threaded-race harness EXACTLY (per-thread, per-worker
independent repository instances built off the default, process-wide
engine — never a single shared `fresh_engine` across threads — real
`threading.Barrier`s, wall-clock-overlap proof). `FakeLiteLLMClient`
only (PID §61) — no real/live model call anywhere in this file.
"""
from __future__ import annotations

import email.message
import hashlib
import json
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone

from ai.providers.litellm.fake import FakeLiteLLMClient
from core import identity
from persistence.objects.memory_store import InMemoryObjectStore
from persistence.postgres.ai_invocation_repository import PostgresAIInvocationRepository
from persistence.postgres.audit_repository import PostgresAuditRepository
from persistence.postgres.evidence_classification_repository import PostgresEvidenceClassificationRepository
from persistence.postgres.evidence_classification_rule_repository import PostgresEvidenceClassificationRuleRepository
from persistence.postgres.evidence_repository import PostgresEvidenceRepository
from persistence.postgres.external_reference_repository import PostgresExternalReferenceRepository
from persistence.postgres.needs_you_repository import PostgresNeedsYouRepository
from persistence.postgres.session import get_engine
from persistence.postgres.source_repository import PostgresSourceRepository

import scripts.evaluate_document_classifier as evl

ACTOR_ID = "wi6-evaluator-postgres-concurrency"


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


def _default_needs_you_repository() -> PostgresNeedsYouRepository:
    return PostgresNeedsYouRepository(get_engine())


def _register_email_evidence(object_store, *, tag: str) -> str:
    sender = f"other-{tag}@wi6-eval-race.example"
    subject = f"unmatched subject {tag}"
    msg = email.message.EmailMessage()
    msg["From"] = sender
    msg["Subject"] = subject
    msg.set_content(f"body {tag}")
    content = bytes(msg)
    content_hash = {"algorithm": "SHA-256", "value": hashlib.sha256(content).hexdigest()}
    storage_reference = object_store.put(identity.generate_id(), content_hash, content)
    source = PostgresSourceRepository().register_source(source_type="MAILBOX_TEST", provider="test", status="ACTIVE")
    now = datetime.now(timezone.utc)
    evidence = _default_evidence_repository().register_evidence(
        entity_id=None, evidence_type="EMAIL", source_id=source.source_id, observed_at=now, received_at=now,
        content_hash=content_hash, mime_type="message/rfc822", size_bytes=len(content),
        storage_reference=storage_reference, metadata={"sender_address": sender, "subject": subject},
    )
    return evidence.evidence_id


def _eval_deps(*, object_store, litellm_client, classification_repo=None, audit_repo=None, needs_you_repo=None):
    classification_repo = classification_repo or _default_classification_repository()
    audit_repo = audit_repo or _default_audit_repository()
    needs_you_repo = needs_you_repo or _default_needs_you_repository()
    return dict(
        evidence_repository=_default_evidence_repository(), rule_repository=_default_rule_repository(),
        classification_repository=classification_repo, ai_invocation_repository=_default_ai_invocation_repository(),
        litellm_client=litellm_client, object_store=object_store, audit_repository=audit_repo,
        record_audit_event=audit_repo.record_audit_event, needs_you_repository=needs_you_repo,
        actor_type="SYSTEM", actor_id=ACTOR_ID, correlation_id=None,
    )


# ---------------------------------------------------------------------
# WI-6 §35(c) — same-evidence/same-fingerprint concurrency: N
# genuinely simultaneous shadow evaluations of the IDENTICAL evidence
# converge on exactly ONE governed AIInvocation, never a duplicate
# provider call silently accepted.
# ---------------------------------------------------------------------


def test_same_fingerprint_concurrent_shadow_evaluations_converge_on_one_ai_invocation():
    object_store = InMemoryObjectStore()
    evidence_id = _register_email_evidence(object_store, tag="race")

    n_workers = 8
    barrier = threading.Barrier(n_workers)
    results: list[dict] = [{} for _ in range(n_workers)]
    litellm_clients = [FakeLiteLLMClient() for _ in range(n_workers)]
    for client in litellm_clients:
        client.queue_success(
            capability_alias="bagman-core",
            content=json.dumps({"proposed_type": "RECEIPT", "confidence": 0.7, "signals": [], "warnings": []}),
        )

    item = {"evidence_id": evidence_id, "expected_document_type": "RECEIPT", "label_family": "race"}

    def _worker(index: int) -> None:
        deps = _eval_deps(object_store=object_store, litellm_client=litellm_clients[index])
        barrier.wait()
        start = time.monotonic()
        result = evl.evaluate_item(item, **deps)
        results[index] = {
            "outcome": result.outcome, "ai_invocation_id": result.ai_invocation_id,
            "new_invocation": result.new_invocation, "start": start, "end": time.monotonic(),
        }

    threads = [threading.Thread(target=_worker, args=(i,)) for i in range(n_workers)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=30)

    assert all(r for r in results), f"one or more worker threads did not complete (a crash?): {results}"

    # Never a duplicate provider call: at most one real `.complete()`
    # call total, across every worker's own independent client.
    total_provider_calls = sum(len(c.calls) for c in litellm_clients)
    assert total_provider_calls <= 1, f"expected at most ONE real provider call across the race, got {total_provider_calls}"

    new_invocation_winners = [r for r in results if r["new_invocation"] is True]
    assert len(new_invocation_winners) == 1, f"expected exactly ONE genuinely new AIInvocation winner: {results}"

    # Every other worker either reused the winner's invocation
    # (`new_invocation=False`) or safely reported AI_IN_PROGRESS
    # (evaluate_item's own ActiveInvocationConflictError handling) —
    # never a crash, never a second provider call.
    for r in results:
        if r is new_invocation_winners[0]:
            continue
        assert r["new_invocation"] in (False, None), f"unexpected result shape for a non-winner: {r}"

    windows = [(r["start"], r["end"]) for r in results]
    overlap_found = any(
        a_start < b_end and b_start < a_end
        for i, (a_start, a_end) in enumerate(windows)
        for j, (b_start, b_end) in enumerate(windows)
        if i < j
    )
    assert overlap_found, f"no genuine wall-clock overlap detected between worker call windows: {windows}"

    # Exactly one real AIInvocation row exists for this subject.
    invocations = _default_ai_invocation_repository().list_invocations(
        task_id="DOCUMENT_TYPE_PROPOSAL", task_version=2, primary_input_reference=evidence_id,
    )
    assert len(invocations) == 1, f"expected exactly ONE AIInvocation row for this subject, got {len(invocations)}"
    # Zero orphan RUNNING rows: the one real invocation reached a
    # terminal status.
    assert invocations[0].status == "SUCCEEDED"

    # Zero canonical mutation of any kind (shadow evaluation doctrine).
    current = _default_classification_repository().get_current_classification(evidence_id, "DOCUMENT_TYPE")
    assert current is None


# ---------------------------------------------------------------------
# WI-6 §35(b) — bounded concurrency=2 background load: distinct items,
# zero duplicate-fingerprint races, zero orphan RUNNING invocations.
# ---------------------------------------------------------------------


def test_bounded_concurrency_two_load_produces_zero_orphan_running_invocations():
    object_store = InMemoryObjectStore()
    n_items = 6
    evidence_ids = [_register_email_evidence(object_store, tag=f"load-{i}") for i in range(n_items)]

    def _run_one(evidence_id: str):
        litellm_client = FakeLiteLLMClient()
        litellm_client.queue_success(
            capability_alias="bagman-core",
            content=json.dumps({"proposed_type": "RECEIPT", "confidence": 0.6, "signals": [], "warnings": []}),
        )
        deps = _eval_deps(object_store=object_store, litellm_client=litellm_client)
        item = {"evidence_id": evidence_id, "expected_document_type": "RECEIPT", "label_family": "load"}
        return evl.evaluate_item(item, **deps)

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(_run_one, evidence_ids))

    assert len(results) == n_items
    for result in results:
        assert result.outcome in (
            "AI_PROPOSAL_REVIEW_REQUIRED", "AI_PROPOSAL_UNCLASSIFIABLE", "AI_IN_PROGRESS",
        )

    ai_repo = _default_ai_invocation_repository()
    for evidence_id in evidence_ids:
        invocations = ai_repo.list_invocations(
            task_id="DOCUMENT_TYPE_PROPOSAL", task_version=2, primary_input_reference=evidence_id,
        )
        # Each distinct item gets at most one invocation (no
        # duplicate-fingerprint race across the bounded pool), and
        # whichever exists must have reached a terminal status — zero
        # orphan RUNNING rows left behind by this bounded load.
        assert len(invocations) <= 1, f"expected at most one AIInvocation per evidence_id, got {len(invocations)} for {evidence_id}"
        for invocation in invocations:
            assert invocation.status != "RUNNING", f"orphan RUNNING invocation left behind for {evidence_id}"
