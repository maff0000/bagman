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
import threading
import time
from datetime import datetime, timedelta, timezone

import pytest

from core import actor, identity
from core.audit import AuditRepository
from core.errors import ConflictError
from persistence.postgres.audit_repository import PostgresAuditRepository
from persistence.postgres.evidence_classification_repository import PostgresEvidenceClassificationRepository
from persistence.postgres.evidence_classification_rule_repository import PostgresEvidenceClassificationRuleRepository
from persistence.postgres.evidence_repository import PostgresEvidenceRepository
from persistence.postgres.external_reference_repository import PostgresExternalReferenceRepository
from persistence.postgres.source_repository import PostgresSourceRepository
from services.evidence.classification_observation import observed_evidence_guard, preview_classification_rule
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


# ---------------------------------------------------------------------
# CD-6 correctness delta (2026-09-25), §8 — real Postgres proof that
# observation truth is exhaustive, never bounded by the underlying
# page size. Mirrors the production interactivebrokers.com shape (226
# total domain-matching EvidenceItems).
# ---------------------------------------------------------------------


def test_guard_preview_and_creation_find_match_outside_first_page_on_postgres(
    evidence_repository, rule_repository, classification_repository, audit_repository
):
    base = datetime.now(timezone.utc)
    source = _make_source()
    total = 226
    oldest_evidence_id = None
    for i in range(total):
        evidence = evidence_repository.register_evidence(
            entity_id=None, evidence_type="EMAIL", source_id=source.source_id,
            observed_at=base - timedelta(days=total - i), received_at=base - timedelta(days=total - i),
            content_hash=_fabricated_content_hash(f"pg-page-{i}"), mime_type="message/rfc822", size_bytes=100,
            metadata={
                "sender_address": "donotreply@interactivebrokers.com",
                # Only the OLDEST record (i == 0) genuinely matches; the
                # newest 225 (well over one 200-row page) are decoys with
                # a different subject.
                "subject": "FYI: Changes in Analyst Ratings" if i == 0 else "Some other newsletter",
            },
        )
        if i == 0:
            oldest_evidence_id = evidence.evidence_id
    assert oldest_evidence_id is not None

    match_count = observed_evidence_guard(
        sender_scope_type="EXACT_SENDER_DOMAIN", sender_scope_value="interactivebrokers.com",
        subject_predicate_type="EXACT", subject_predicate_value="FYI: Changes in Analyst Ratings",
        evidence_repository=evidence_repository,
    )
    assert match_count == 1

    preview = preview_classification_rule(
        sender_scope_type="EXACT_SENDER_DOMAIN", sender_scope_value="interactivebrokers.com",
        subject_predicate_type="EXACT", subject_predicate_value="FYI: Changes in Analyst Ratings",
        document_type="NON_ACCOUNTING_DOCUMENT",
        evidence_repository=evidence_repository, classification_repository=classification_repository,
    )
    assert preview.match_count == 1
    assert preview.representative_evidence_ids == (oldest_evidence_id,)

    created = create_classification_rule(
        sender_scope_type="EXACT_SENDER_DOMAIN", sender_scope_value="interactivebrokers.com",
        subject_predicate_type="EXACT", subject_predicate_value="FYI: Changes in Analyst Ratings",
        document_type="NON_ACCOUNTING_DOCUMENT", source="OPERATOR", actor_type=actor.SYSTEM, actor_id="pg-test-page",
        rule_repository=rule_repository, evidence_repository=evidence_repository, audit_repository=audit_repository,
    )
    assert created.was_created is True
    assert created.match_count == 1


# ---------------------------------------------------------------------
# CD-6 correctness delta (2026-09-25), §16/§17 — genuine real-threaded
# concurrency proofs for `create_classification_rule`'s
# repository-authoritative `was_created` decision. Mirrors
# `tests/persistence/test_evidence_classification_repository.py`'s own
# genuine-threaded-race pattern (real `threading.Thread`s, each with
# their OWN repository instances, released simultaneously via a
# `threading.Barrier` — never a single-worker simulated race).
# ---------------------------------------------------------------------


def _default_evidence_repository() -> PostgresEvidenceRepository:
    return PostgresEvidenceRepository(PostgresExternalReferenceRepository())


def _default_rule_repository() -> PostgresEvidenceClassificationRuleRepository:
    return PostgresEvidenceClassificationRuleRepository()


def _default_audit_repository() -> PostgresAuditRepository:
    return PostgresAuditRepository()


def test_concurrent_identical_rule_creation_resolves_to_exactly_one_created_and_one_audit_event(
    evidence_repository, audit_repository,
):
    """§16 — n synchronized callers, all requesting the EXACT SAME
    sender identity + predicate identity + document_type. Require
    exactly 1 ACTIVE rule row, exactly 1 CREATED audit event, all
    successful callers return the same rule_id, and exactly one result
    reports was_created=True (all others False)."""
    _make_email_evidence(evidence_repository, sender_address="billing@race-vendor.example", subject="Race Statement")

    n_workers = 8
    barrier = threading.Barrier(n_workers)
    results: list[dict] = [{} for _ in range(n_workers)]

    create_kwargs = dict(
        sender_scope_type="EXACT_SENDER_DOMAIN", sender_scope_value="race-vendor.example",
        subject_predicate_type="EXACT", subject_predicate_value="Race Statement",
        document_type="SUPPLIER_INVOICE", source="OPERATOR",
    )

    def _worker(index: int) -> None:
        rule_repo = _default_rule_repository()
        evidence_repo = _default_evidence_repository()
        audit_repo = _default_audit_repository()
        barrier.wait()
        start = time.monotonic()
        result = create_classification_rule(
            actor_type=actor.SYSTEM, actor_id=f"pg-race-{index}",
            rule_repository=rule_repo, evidence_repository=evidence_repo, audit_repository=audit_repo,
            **create_kwargs,
        )
        results[index] = {
            "rule_id": result.rule.rule_id, "was_created": result.was_created,
            "start": start, "end": time.monotonic(),
        }

    threads = [threading.Thread(target=_worker, args=(i,)) for i in range(n_workers)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=30)

    assert all(r for r in results), f"one or more worker threads did not complete: {results}"

    rule_ids = {r["rule_id"] for r in results}
    assert len(rule_ids) == 1, f"expected all callers to agree on ONE rule_id, got: {results}"
    rule_id = next(iter(rule_ids))

    created_true = [r for r in results if r["was_created"] is True]
    created_false = [r for r in results if r["was_created"] is False]
    assert len(created_true) == 1, f"expected exactly ONE was_created=True, got {len(created_true)}: {results}"
    assert len(created_false) == n_workers - 1, f"expected every other caller to report was_created=False: {results}"

    windows = [(r["start"], r["end"]) for r in results]
    overlap_found = any(
        a_start < b_end and b_start < a_end
        for i, (a_start, a_end) in enumerate(windows)
        for j, (b_start, b_end) in enumerate(windows)
        if i < j
    )
    assert overlap_found, f"no genuine wall-clock overlap detected between worker call windows: {windows}"

    active_rules = [
        r for r in _default_rule_repository().list_rules(status="ACTIVE") if r.sender_scope_value == "race-vendor.example"
    ]
    assert len(active_rules) == 1, f"expected exactly ONE ACTIVE rule row for this identity, got {len(active_rules)}"
    assert active_rules[0].rule_id == rule_id

    created_events = [
        e for e in audit_repository.list_by_subject("EvidenceClassificationRule", rule_id)
        if e.event_type == "EVIDENCE_CLASSIFICATION_RULE_CREATED"
    ]
    assert len(created_events) == 1, (
        f"expected exactly ONE CREATED audit event under a genuine concurrent race, got {len(created_events)}"
    )


def test_concurrent_conflicting_document_type_rule_creation_resolves_to_one_winner_and_conflicts(
    evidence_repository, audit_repository,
):
    """§17 — two synchronized callers at the SAME rule identity but a
    DIFFERENT document_type. Require exactly one ACTIVE row, the winner
    succeeds, the loser receives ConflictError, exactly one CREATED
    audit event, no ambiguous winner state."""
    _make_email_evidence(
        evidence_repository, sender_address="billing@conflict-vendor.example", subject="Conflict Statement"
    )

    n_workers = 2
    barrier = threading.Barrier(n_workers)
    results: list[dict] = [{} for _ in range(n_workers)]
    document_types = ["SUPPLIER_INVOICE", "RECEIPT"]

    def _worker(index: int) -> None:
        rule_repo = _default_rule_repository()
        evidence_repo = _default_evidence_repository()
        audit_repo = _default_audit_repository()
        barrier.wait()
        try:
            result = create_classification_rule(
                sender_scope_type="EXACT_SENDER_DOMAIN", sender_scope_value="conflict-vendor.example",
                subject_predicate_type="EXACT", subject_predicate_value="Conflict Statement",
                document_type=document_types[index], source="OPERATOR",
                actor_type=actor.SYSTEM, actor_id=f"pg-conflict-{index}",
                rule_repository=rule_repo, evidence_repository=evidence_repo, audit_repository=audit_repo,
            )
            results[index] = {"outcome": "created", "rule_id": result.rule.rule_id, "was_created": result.was_created}
        except ConflictError:
            results[index] = {"outcome": "conflict"}

    threads = [threading.Thread(target=_worker, args=(i,)) for i in range(n_workers)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=30)

    assert all(r for r in results), f"one or more worker threads did not complete: {results}"

    created = [r for r in results if r["outcome"] == "created"]
    conflicted = [r for r in results if r["outcome"] == "conflict"]
    assert len(created) == 1, f"expected exactly ONE winner, got {len(created)}: {results}"
    assert len(conflicted) == n_workers - 1, f"expected every other caller to receive a real ConflictError: {results}"
    assert created[0]["was_created"] is True

    winner_rule_id = created[0]["rule_id"]
    active_rules = [
        r for r in _default_rule_repository().list_rules(status="ACTIVE")
        if r.sender_scope_value == "conflict-vendor.example"
    ]
    assert len(active_rules) == 1, f"expected exactly ONE ACTIVE row despite the document_type conflict: {active_rules}"
    assert active_rules[0].rule_id == winner_rule_id

    created_events = [
        e for e in audit_repository.list_by_subject("EvidenceClassificationRule", winner_rule_id)
        if e.event_type == "EVIDENCE_CLASSIFICATION_RULE_CREATED"
    ]
    assert len(created_events) == 1, f"expected exactly ONE CREATED audit event, got {len(created_events)}"
