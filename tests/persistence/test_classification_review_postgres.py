"""CD-6 Slice 5 WI-4 §59 — genuine real-Postgres concurrency proof for
``services.evidence.classification_review.resolve_classification_review``:
N truly concurrent operator-resolution attempts against the SAME
``CLASSIFICATION_REVIEW`` ``NeedsYouItem`` (identical resolution,
including an identical ``teach_rule`` request) must converge to exactly
ONE ``OPERATOR_ASSIGNED`` classification row, ONE confirmation/
correction audit event, and ONE active
``EvidenceClassificationRule`` — never a branch, a duplicate, or a raw
database error escaping to any caller.

Mirrors ``tests/persistence/test_evidence_classification_repository.py``'s
own
``test_genuinely_concurrent_same_producer_identity_resolves_to_exactly_one_created_row``
style exactly: real ``threading.Thread``s, each with its OWN repository
instances against the shared default engine, released simultaneously
via a ``threading.Barrier``, with a genuine wall-clock-overlap
assertion (never a single-worker simulated race).
"""
from __future__ import annotations

import hashlib
import threading
import time

from core import identity
from core.errors import ConflictError
from core.timestamps import utc_now
from persistence.postgres.ai_invocation_repository import PostgresAIInvocationRepository
from persistence.postgres.audit_repository import PostgresAuditRepository
from persistence.postgres.evidence_classification_repository import PostgresEvidenceClassificationRepository
from persistence.postgres.evidence_classification_rule_repository import PostgresEvidenceClassificationRuleRepository
from persistence.postgres.evidence_repository import PostgresEvidenceRepository
from persistence.postgres.external_reference_repository import PostgresExternalReferenceRepository
from persistence.postgres.needs_you_repository import PostgresNeedsYouRepository
from persistence.postgres.session import get_engine
from persistence.postgres.source_repository import PostgresSourceRepository
from services.evidence.classification import (
    CLASSIFICATION_TYPE_DOCUMENT_TYPE,
    SOURCE_AI_PROPOSAL,
    SOURCE_OPERATOR_ASSIGNED,
    STATUS_REVIEW_REQUIRED,
)
from services.evidence.classification_review import (
    EVIDENCE_CLASSIFICATION_CONFIRMED,
    EVIDENCE_CLASSIFICATION_CORRECTED,
    ensure_classification_review_item,
    resolve_classification_review,
)
from services.evidence.classification_rule import RULE_STATUS_ACTIVE


def _fabricated_content_hash(tag: str) -> str:
    return hashlib.sha256(tag.encode("utf-8")).hexdigest()


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


def _make_ai_review_item():
    """Sets up: a real EvidenceItem (sender/subject shaped so a later
    ``teach_rule`` genuinely matches it), a real SUCCEEDED
    ``AIInvocation``, a real ``AI_PROPOSAL`` classification, and a real
    OPEN ``CLASSIFICATION_REVIEW`` ``NeedsYouItem`` anchored to it —
    everything the racing workers below will contend to resolve."""
    source = PostgresSourceRepository().register_source(
        source_type="MAILBOX_TEST", provider="wi4-concurrency-test", status="ACTIVE",
    )
    sender_domain = f"wi4-concurrency-{identity.generate_id()}.example"
    sender_address = f"orders@{sender_domain}"
    subject = "Order confirmed: concurrency test"
    now = utc_now()
    evidence = _default_evidence_repository().register_evidence(
        entity_id=None, evidence_type="EMAIL", source_id=source.source_id, observed_at=now, received_at=now,
        content_hash=_fabricated_content_hash(identity.generate_id()), mime_type="message/rfc822", size_bytes=100,
        metadata={"sender_address": sender_address, "subject": subject},
    )
    ai_invocation_repo = _default_ai_invocation_repository()
    invocation = ai_invocation_repo.create_invocation(
        task_id="DOCUMENT_TYPE_PROPOSAL", task_version=2, role="BACKGROUND", provider="LITELLM",
        capability_alias="bagman-core", input_references={"evidence_id": evidence.evidence_id},
        actor_type="SYSTEM", actor_id="wi4-concurrency-test",
    )
    ai_invocation_repo.transition_status(invocation.ai_invocation_id, "RUNNING")
    ai_invocation_repo.transition_status(invocation.ai_invocation_id, "SUCCEEDED")

    classification_repo = _default_classification_repository()
    ai_classification = classification_repo.create_classification(
        evidence_id=evidence.evidence_id, classification_type=CLASSIFICATION_TYPE_DOCUMENT_TYPE,
        document_type="ORDER_CONFIRMATION", status=STATUS_REVIEW_REQUIRED, source=SOURCE_AI_PROPOSAL,
        confidence=0.85, ai_invocation_id=invocation.ai_invocation_id, expected_current_classification_id=None,
    )
    needs_you_item = ensure_classification_review_item(
        classification=ai_classification, evidence=evidence, ai_invocation_id=invocation.ai_invocation_id,
        needs_you_repository=_default_needs_you_repository(),
    )
    return evidence, ai_classification, needs_you_item, sender_domain


def test_genuinely_concurrent_resolution_of_same_review_item_converges_to_one_operator_row_one_rule():
    evidence, _ai_classification, needs_you_item, sender_domain = _make_ai_review_item()

    n_workers = 8
    barrier = threading.Barrier(n_workers)
    results: list[dict] = [{} for _ in range(n_workers)]
    resolution = {
        "document_type": "ORDER_CONFIRMATION",
        "teach_rule": {
            "sender_scope_type": "EXACT_SENDER_DOMAIN", "sender_scope_value": sender_domain,
            "subject_predicate_type": "STARTS_WITH", "subject_predicate_value": "Order confirmed:",
        },
    }

    def _worker(index: int) -> None:
        evidence_repo = _default_evidence_repository()
        classification_repo = _default_classification_repository()
        rule_repo = _default_rule_repository()
        audit_repo = _default_audit_repository()
        barrier.wait()
        start = time.monotonic()
        result = resolve_classification_review(
            needs_you_item=needs_you_item, resolution=resolution, actor_type="USER", actor_id=f"matt-{index}",
            evidence_repository=evidence_repo, classification_repository=classification_repo,
            rule_repository=rule_repo, audit_repository=audit_repo,
            record_audit_event=audit_repo.record_audit_event,
        )
        results[index] = {
            "classification_id": result.classification.classification_id,
            "classification_was_created": result.classification_was_created,
            "rule_id": result.rule.rule_id if result.rule else None,
            "rule_was_created": result.rule_was_created,
            "start": start, "end": time.monotonic(),
        }

    threads = [threading.Thread(target=_worker, args=(i,)) for i in range(n_workers)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=30)

    assert all(r for r in results), f"one or more worker threads did not complete: {results}"

    classification_ids = {r["classification_id"] for r in results}
    assert len(classification_ids) == 1, f"expected all workers to agree on ONE operator classification: {results}"
    created_true = [r for r in results if r["classification_was_created"] is True]
    assert len(created_true) == 1, f"expected exactly ONE classification_was_created=True: {results}"

    rule_ids = {r["rule_id"] for r in results}
    assert len(rule_ids) == 1 and None not in rule_ids, f"expected all workers to agree on ONE rule: {results}"
    rule_created_true = [r for r in results if r["rule_was_created"] is True]
    assert len(rule_created_true) == 1, f"expected exactly ONE rule_was_created=True: {results}"

    windows = [(r["start"], r["end"]) for r in results]
    overlap_found = any(
        a_start < b_end and b_start < a_end
        for i, (a_start, a_end) in enumerate(windows)
        for j, (b_start, b_end) in enumerate(windows)
        if i < j
    )
    assert overlap_found, f"no genuine wall-clock overlap detected between worker call windows: {windows}"

    # Exactly one OPERATOR_ASSIGNED row durably persisted.
    history = _default_classification_repository().list_classification_history(
        evidence.evidence_id, CLASSIFICATION_TYPE_DOCUMENT_TYPE
    )
    operator_rows = [c for c in history if c.source == SOURCE_OPERATOR_ASSIGNED]
    assert len(operator_rows) == 1

    # Exactly one confirmation/correction audit event.
    audit_events = _default_audit_repository().list_by_subject(
        "EvidenceClassification", operator_rows[0].classification_id
    )
    decision_events = [
        e for e in audit_events
        if e.event_type in (EVIDENCE_CLASSIFICATION_CONFIRMED, EVIDENCE_CLASSIFICATION_CORRECTED)
    ]
    assert len(decision_events) == 1

    # Exactly one ACTIVE rule at this identity.
    active_rules = _default_rule_repository().list_rules(status=RULE_STATUS_ACTIVE, document_type="ORDER_CONFIRMATION")
    matching = [r for r in active_rules if r.sender_scope_value == sender_domain]
    assert len(matching) == 1


# ---------------------------------------------------------------------
# WI-4-correction §15/§16 — genuinely concurrent, DIFFERENT resolutions
# against the SAME review item. Uses the actual review-resolution
# service path plus, for whichever caller's service call genuinely
# succeeds, the same final `resolve_needs_you_item` step the real HTTP
# router performs — mirroring the router's own division of labour
# (`app/api/routers/needs_you.py`) without needing a Postgres-backed
# TestClient fixture.
# ---------------------------------------------------------------------


def test_concurrent_different_resolutions_converge_to_one_semantic_decision():
    """WI-4-correction §15 — two synchronized callers request DIFFERENT
    final document_types for the SAME OPEN CLASSIFICATION_REVIEW item.
    Require: exactly one OPERATOR_ASSIGNED row; exactly one semantic
    decision survives; the loser receives ConflictError; and — the
    invariant that matters most — the terminal NeedsYouItem's own
    stored resolution.document_type always equals the CURRENT
    OPERATOR_ASSIGNED row's document_type, for every possible
    interleaving."""
    evidence, _ai_classification, needs_you_item, _sender_domain = _make_ai_review_item()

    n_workers = 2
    barrier = threading.Barrier(n_workers)
    results: list[dict] = [{} for _ in range(n_workers)]
    requested_types = ["BROKER_STATEMENT", "RECEIPT"]

    def _worker(index: int) -> None:
        evidence_repo = _default_evidence_repository()
        classification_repo = _default_classification_repository()
        rule_repo = _default_rule_repository()
        audit_repo = _default_audit_repository()
        needs_you_repo = _default_needs_you_repository()
        resolution = {"document_type": requested_types[index], "teach_rule": None}
        barrier.wait()
        start = time.monotonic()
        try:
            result = resolve_classification_review(
                needs_you_item=needs_you_item, resolution=resolution, actor_type="USER", actor_id=f"matt-{index}",
                evidence_repository=evidence_repo, classification_repository=classification_repo,
                rule_repository=rule_repo, audit_repository=audit_repo,
                record_audit_event=audit_repo.record_audit_event,
            )
        except ConflictError:
            results[index] = {"outcome": "conflict", "start": start, "end": time.monotonic()}
            return
        # Mirrors the router's OWN remaining step, exactly as
        # `app/api/routers/needs_you.py` performs it after
        # `resolve_classification_review` succeeds.
        updated_item = needs_you_repo.resolve_needs_you_item(
            needs_you_item.item_id, new_status="RESOLVED", resolution=resolution,
            actor_type="USER", actor_id=f"matt-{index}",
        )
        results[index] = {
            "outcome": "resolved", "classification_id": result.classification.classification_id,
            "document_type": requested_types[index], "resolved_resolution": updated_item.resolution,
            "start": start, "end": time.monotonic(),
        }

    threads = [threading.Thread(target=_worker, args=(i,)) for i in range(n_workers)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=30)

    assert all(r for r in results), f"one or more worker threads did not complete: {results}"

    resolved = [r for r in results if r["outcome"] == "resolved"]
    conflicted = [r for r in results if r["outcome"] == "conflict"]
    assert len(resolved) == 1, f"expected exactly ONE semantic decision to survive: {results}"
    assert len(conflicted) == 1, f"expected exactly ONE caller to receive ConflictError: {results}"

    windows = [(r["start"], r["end"]) for r in results]
    overlap_found = any(
        a_start < b_end and b_start < a_end
        for i, (a_start, a_end) in enumerate(windows)
        for j, (b_start, b_end) in enumerate(windows)
        if i < j
    )
    assert overlap_found, f"no genuine wall-clock overlap detected between worker call windows: {windows}"

    # Exactly one OPERATOR_ASSIGNED row.
    history = _default_classification_repository().list_classification_history(
        evidence.evidence_id, CLASSIFICATION_TYPE_DOCUMENT_TYPE
    )
    operator_rows = [c for c in history if c.source == SOURCE_OPERATOR_ASSIGNED]
    assert len(operator_rows) == 1

    # The invariant that matters most (WI-4-correction §15): the
    # terminal NeedsYouItem's own stored resolution.document_type must
    # equal the CURRENT OPERATOR_ASSIGNED row's document_type — no
    # possible interleaving may leave those values different.
    final_item = _default_needs_you_repository().get_needs_you_item(needs_you_item.item_id)
    assert final_item.status == "RESOLVED"
    assert final_item.resolution["document_type"] == operator_rows[0].document_type
    assert final_item.resolution["document_type"] == resolved[0]["document_type"]


def test_concurrent_different_resolution_with_teaching_never_creates_a_rule_for_the_losing_type():
    """WI-4-correction §16 — the loser's request ALSO carries a
    teach_rule that would otherwise validly match the reviewed
    evidence. No deterministic rule may ever be created for a document
    type that differs from the winning operator classification — the
    semantic-compatibility ConflictError must fire before rule teaching
    is even attempted."""
    evidence, _ai_classification, needs_you_item, sender_domain = _make_ai_review_item()

    n_workers = 2
    barrier = threading.Barrier(n_workers)
    results: list[dict] = [{} for _ in range(n_workers)]
    # Both callers request DIFFERENT document_types; ONLY the "losing"
    # shape (index 1, RECEIPT) supplies a teach_rule — one that
    # genuinely matches the reviewed evidence, so the ONLY thing that
    # can stop it is the semantic-compatibility guard itself.
    resolutions = [
        {"document_type": "ORDER_CONFIRMATION", "teach_rule": None},
        {
            "document_type": "RECEIPT",
            "teach_rule": {
                "sender_scope_type": "EXACT_SENDER_DOMAIN", "sender_scope_value": sender_domain,
                "subject_predicate_type": "STARTS_WITH", "subject_predicate_value": "Order confirmed:",
            },
        },
    ]

    def _worker(index: int) -> None:
        evidence_repo = _default_evidence_repository()
        classification_repo = _default_classification_repository()
        rule_repo = _default_rule_repository()
        audit_repo = _default_audit_repository()
        barrier.wait()
        try:
            result = resolve_classification_review(
                needs_you_item=needs_you_item, resolution=resolutions[index], actor_type="USER",
                actor_id=f"matt-{index}",
                evidence_repository=evidence_repo, classification_repository=classification_repo,
                rule_repository=rule_repo, audit_repository=audit_repo,
                record_audit_event=audit_repo.record_audit_event,
            )
            results[index] = {
                "outcome": "resolved", "document_type": resolutions[index]["document_type"],
                "rule_created": result.rule_was_created,
            }
        except ConflictError:
            results[index] = {"outcome": "conflict", "document_type": resolutions[index]["document_type"]}

    threads = [threading.Thread(target=_worker, args=(i,)) for i in range(n_workers)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=30)

    assert all(r for r in results), f"one or more worker threads did not complete: {results}"
    resolved = [r for r in results if r["outcome"] == "resolved"]
    assert len(resolved) == 1, f"expected exactly ONE semantic decision to survive: {results}"

    # WI-4-correction §16's real invariant: a rule may exist ONLY for
    # the WINNING document_type — never for whichever type did NOT
    # become the canonical operator classification. Which caller
    # actually wins the genuine classification-creation race is not
    # deterministic (either thread's INSERT may physically win), so
    # this assertion must be symmetric, not hardcoded to one caller
    # always losing.
    active_rules = _default_rule_repository().list_rules(status=RULE_STATUS_ACTIVE)
    matching_scoped_rules = [r for r in active_rules if r.sender_scope_value == sender_domain]
    if resolved[0]["document_type"] == "ORDER_CONFIRMATION":
        # ORDER_CONFIRMATION won — that caller requested no teaching,
        # and RECEIPT's own teach_rule must never have been reached
        # (blocked by the semantic-compatibility guard before rule
        # creation) — zero rules at this identity.
        assert matching_scoped_rules == [], (
            f"RECEIPT lost the classification race — no rule may exist for it: {matching_scoped_rules}"
        )
    else:
        # RECEIPT won — it IS now the canonical operator classification,
        # so its own teach_rule (which genuinely matches the reviewed
        # evidence) is legitimate: exactly one RECEIPT-typed rule.
        assert len(matching_scoped_rules) == 1
        assert matching_scoped_rules[0].document_type == "RECEIPT"
        assert resolved[0]["rule_created"] is True
