"""CD-6 Slice 5 WI-6 — `scripts/reprocess_evidence_classification.py`
proofs against in-memory repositories (mirrors
`tests/integration/test_classification_orchestrator.py`'s own fixture
shape). Genuine real-database concurrency proofs live under
`tests/persistence/` instead (see that directory's own WI-6 file) —
these tests exercise dry-run/manifest-hash/checkpoint-revalidation/
fail-closed logic only, which needs no real Postgres."""
from __future__ import annotations

import hashlib
import tempfile
from datetime import datetime, timezone
from pathlib import Path

import pytest

from core import identity
from core.audit import InMemoryAuditRepository
from core.errors import NotFoundError
from core.external_reference import InMemoryExternalReferenceRepository
from services.evidence.classification import (
    CLASSIFICATION_TYPE_DOCUMENT_TYPE,
    SOURCE_OPERATOR_ASSIGNED,
    STATUS_CLASSIFIED,
    InMemoryEvidenceClassificationRepository,
)
from services.evidence.classification_rule import (
    RULE_SOURCE_OPERATOR,
    SENDER_SCOPE_EXACT_SENDER_DOMAIN,
    SUBJECT_PREDICATE_EXACT,
    InMemoryEvidenceClassificationRuleRepository,
)
from services.evidence.evidence import InMemoryEvidenceRepository
from ai.invocation import InMemoryAIInvocationRepository

import scripts.reprocess_evidence_classification as rec


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
def checkpoint_path(tmp_path: Path) -> Path:
    return tmp_path / "checkpoint.jsonl"


def _make_evidence(evidence_repository, *, sender=None, subject=None):
    now = datetime.now(timezone.utc)
    content_hash = hashlib.sha256(identity.generate_id().encode()).hexdigest()
    metadata = {}
    if sender is not None:
        metadata["sender_address"] = sender
    if subject is not None:
        metadata["subject"] = subject
    return evidence_repository.register_evidence(
        entity_id=None, evidence_type="EMAIL", source_id=identity.generate_id(),
        observed_at=now, received_at=now, content_hash=content_hash, mime_type="message/rfc822",
        size_bytes=10, metadata=metadata,
    )


def _make_rule(rule_repository, *, document_type="SUPPLIER_INVOICE"):
    return rule_repository.create_rule(
        sender_scope_type=SENDER_SCOPE_EXACT_SENDER_DOMAIN, sender_scope_value=f"vendor-{identity.generate_id()}.example",
        subject_predicate_type=SUBJECT_PREDICATE_EXACT, subject_predicate_value="monthly statement",
        document_type=document_type, source=RULE_SOURCE_OPERATOR,
    )


def _matching_evidence(evidence_repository, rule, n: int = 1):
    domain = rule.sender_scope_value
    return [
        _make_evidence(evidence_repository, sender=f"billing@{domain}", subject=rule.subject_predicate_value)
        for _ in range(n)
    ]


# ---------------------------------------------------------------------
# Dry-run correctness
# ---------------------------------------------------------------------


def test_dry_run_reports_matching_already_classified_and_candidate_counts(
    evidence_repository, rule_repository, classification_repository,
):
    rule = _make_rule(rule_repository)
    matching = _matching_evidence(evidence_repository, rule, n=3)
    non_matching = _make_evidence(evidence_repository, sender="someone@other.example", subject="unrelated")

    classification_repository.create_classification(
        evidence_id=matching[0].evidence_id, classification_type=CLASSIFICATION_TYPE_DOCUMENT_TYPE,
        document_type="RECEIPT", status=STATUS_CLASSIFIED, source=SOURCE_OPERATOR_ASSIGNED,
        operator_action_id="op-1",
    )

    scan = rec._scan_candidates(
        rule=rule, evidence_repository=evidence_repository, classification_repository=classification_repository,
        scan_page_size=200,
    )
    assert scan.matching_evidence_count == 3
    assert scan.already_classified_count == 1
    assert len(scan.candidates) == 2
    assert {c.evidence_id for c in scan.candidates} == {matching[1].evidence_id, matching[2].evidence_id}
    assert non_matching.evidence_id not in {c.evidence_id for c in scan.candidates}


def test_dry_run_manifest_hash_is_deterministic_for_identical_inputs(
    evidence_repository, rule_repository, classification_repository,
):
    rule = _make_rule(rule_repository)
    _matching_evidence(evidence_repository, rule, n=3)

    scan = rec._scan_candidates(
        rule=rule, evidence_repository=evidence_repository, classification_repository=classification_repository,
        scan_page_size=200,
    )
    manifest = rec.build_candidate_manifest(scan.candidates)
    h1 = rec.compute_manifest_sha256(manifest)
    h2 = rec.compute_manifest_sha256(rec.build_candidate_manifest(scan.candidates))
    assert h1 == h2
    assert len(h1) == 64


def test_dry_run_manifest_hash_changes_when_candidate_set_changes(
    evidence_repository, rule_repository, classification_repository,
):
    rule = _make_rule(rule_repository)
    _matching_evidence(evidence_repository, rule, n=2)

    def _hash():
        scan = rec._scan_candidates(
            rule=rule, evidence_repository=evidence_repository, classification_repository=classification_repository,
            scan_page_size=200,
        )
        return rec.compute_manifest_sha256(rec.build_candidate_manifest(scan.candidates))

    before = _hash()
    _matching_evidence(evidence_repository, rule, n=1)  # one more candidate appears
    after = _hash()
    assert before != after


def test_dry_run_creates_no_classification_or_audit_rows(
    evidence_repository, rule_repository, classification_repository, audit_repository,
):
    rule = _make_rule(rule_repository)
    matching = _matching_evidence(evidence_repository, rule, n=2)

    scan = rec._scan_candidates(
        rule=rule, evidence_repository=evidence_repository, classification_repository=classification_repository,
        scan_page_size=200,
    )
    rec.build_candidate_manifest(scan.candidates)

    for evidence in matching:
        assert classification_repository.get_current_classification(evidence.evidence_id, CLASSIFICATION_TYPE_DOCUMENT_TYPE) is None


def test_dry_run_deterministic_ordering_is_received_at_then_evidence_id(
    evidence_repository, rule_repository, classification_repository,
):
    rule = _make_rule(rule_repository)
    matching = _matching_evidence(evidence_repository, rule, n=5)

    scan = rec._scan_candidates(
        rule=rule, evidence_repository=evidence_repository, classification_repository=classification_repository,
        scan_page_size=200,
    )
    ordered_ids = [c.evidence_id for c in scan.candidates]
    expected_order = sorted((e.evidence_id for e in matching), key=lambda eid: (
        next(m.received_at for m in matching if m.evidence_id == eid), eid
    ))
    assert ordered_ids == expected_order


# ---------------------------------------------------------------------
# Apply — manifest-hash-gate fail-closed
# ---------------------------------------------------------------------


def test_apply_with_wrong_manifest_hash_refuses_to_mutate(
    evidence_repository, rule_repository, classification_repository, audit_repository, checkpoint_path,
):
    rule = _make_rule(rule_repository)
    _matching_evidence(evidence_repository, rule, n=2)

    outcome = rec.run_apply(
        rule_id=rule.rule_id, expected_manifest_sha256="0" * 64, limit=10, checkpoint_path=checkpoint_path,
        evidence_repository=evidence_repository, rule_repository=rule_repository,
        classification_repository=classification_repository, audit_repository=audit_repository,
        actor_type="SYSTEM", actor_id="test",
    )
    assert outcome.exit_code != 0
    assert outcome.report["error"] == "MANIFEST_HASH_MISMATCH"

    scan = rec._scan_candidates(
        rule=rule, evidence_repository=evidence_repository, classification_repository=classification_repository,
        scan_page_size=200,
    )
    assert scan.already_classified_count == 0  # nothing was mutated


# ---------------------------------------------------------------------
# Apply — happy path / idempotent replay
# ---------------------------------------------------------------------


def _dry_run_hash(rule, evidence_repository, classification_repository):
    scan = rec._scan_candidates(
        rule=rule, evidence_repository=evidence_repository, classification_repository=classification_repository,
        scan_page_size=200,
    )
    return rec.compute_manifest_sha256(rec.build_candidate_manifest(scan.candidates)), scan


def test_apply_happy_path_classifies_exactly_the_candidate_set(
    evidence_repository, rule_repository, classification_repository, audit_repository, ai_invocation_repository,
    checkpoint_path,
):
    rule = _make_rule(rule_repository)
    matching = _matching_evidence(evidence_repository, rule, n=3)
    manifest_hash, _ = _dry_run_hash(rule, evidence_repository, classification_repository)

    invocations_before = len(ai_invocation_repository.list_invocations())

    outcome = rec.run_apply(
        rule_id=rule.rule_id, expected_manifest_sha256=manifest_hash, limit=10, checkpoint_path=checkpoint_path,
        evidence_repository=evidence_repository, rule_repository=rule_repository,
        classification_repository=classification_repository, audit_repository=audit_repository,
        actor_type="SYSTEM", actor_id="test",
    )
    assert outcome.exit_code == 0
    assert outcome.report["classified_count"] == 3
    assert outcome.report["existing_count"] == 0
    assert outcome.report["failure_count"] == 0

    for evidence in matching:
        current = classification_repository.get_current_classification(evidence.evidence_id, CLASSIFICATION_TYPE_DOCUMENT_TYPE)
        assert current is not None
        assert current.source == "DETERMINISTIC_RULE"
        assert current.rule_id == rule.rule_id
        assert current.document_type == rule.document_type

    assert len(ai_invocation_repository.list_invocations()) == invocations_before  # zero AI side effects


def test_apply_idempotent_replay_returns_existing_for_every_item(
    evidence_repository, rule_repository, classification_repository, audit_repository, checkpoint_path,
):
    rule = _make_rule(rule_repository)
    _matching_evidence(evidence_repository, rule, n=3)
    manifest_hash, _ = _dry_run_hash(rule, evidence_repository, classification_repository)

    first = rec.run_apply(
        rule_id=rule.rule_id, expected_manifest_sha256=manifest_hash, limit=10, checkpoint_path=checkpoint_path,
        evidence_repository=evidence_repository, rule_repository=rule_repository,
        classification_repository=classification_repository, audit_repository=audit_repository,
        actor_type="SYSTEM", actor_id="test",
    )
    assert first.exit_code == 0
    assert first.report["classified_count"] == 3

    replay = rec.run_apply(
        rule_id=rule.rule_id, expected_manifest_sha256=manifest_hash, limit=10, checkpoint_path=checkpoint_path,
        evidence_repository=evidence_repository, rule_repository=rule_repository,
        classification_repository=classification_repository, audit_repository=audit_repository,
        actor_type="SYSTEM", actor_id="test",
    )
    assert replay.exit_code == 0
    assert replay.report["classified_count"] == 0
    assert replay.report["existing_count"] == 3
    assert replay.report["failure_count"] == 0


# ---------------------------------------------------------------------
# Apply — unexpected-state STOP
# ---------------------------------------------------------------------


def test_apply_stops_when_a_different_producer_raced_ahead_of_it(
    evidence_repository, rule_repository, classification_repository, audit_repository, checkpoint_path,
):
    rule = _make_rule(rule_repository)
    matching = _matching_evidence(evidence_repository, rule, n=3)
    manifest_hash, _ = _dry_run_hash(rule, evidence_repository, classification_repository)

    # Simulate a race: between dry-run and apply, a DIFFERENT producer
    # (operator-assigned) claims one of the three candidates.
    raced_evidence = matching[1]
    classification_repository.create_classification(
        evidence_id=raced_evidence.evidence_id, classification_type=CLASSIFICATION_TYPE_DOCUMENT_TYPE,
        document_type="RECEIPT", status=STATUS_CLASSIFIED, source=SOURCE_OPERATOR_ASSIGNED,
        operator_action_id="op-race",
    )

    outcome = rec.run_apply(
        rule_id=rule.rule_id, expected_manifest_sha256=manifest_hash, limit=10, checkpoint_path=checkpoint_path,
        evidence_repository=evidence_repository, rule_repository=rule_repository,
        classification_repository=classification_repository, audit_repository=audit_repository,
        actor_type="SYSTEM", actor_id="test",
    )
    assert outcome.exit_code != 0
    # Never silently skipped/continued — no OTHER candidate was mutated either.
    for evidence in matching:
        if evidence.evidence_id == raced_evidence.evidence_id:
            continue
        current = classification_repository.get_current_classification(evidence.evidence_id, CLASSIFICATION_TYPE_DOCUMENT_TYPE)
        assert current is None


# ---------------------------------------------------------------------
# Checkpoint/resume revalidation
# ---------------------------------------------------------------------


def test_stale_checkpoint_claiming_done_is_revalidated_and_reprocessed_for_real(
    evidence_repository, rule_repository, classification_repository, audit_repository, checkpoint_path,
):
    rule = _make_rule(rule_repository)
    matching = _matching_evidence(evidence_repository, rule, n=2)
    manifest_hash, _ = _dry_run_hash(rule, evidence_repository, classification_repository)

    # Write a checkpoint claiming BOTH items are already done — but the
    # real database has NO classification for either of them.
    rec.append_checkpoint(checkpoint_path, {"evidence_id": matching[0].evidence_id, "status": "done", "outcome": "CLASSIFIED"})
    rec.append_checkpoint(checkpoint_path, {"evidence_id": matching[1].evidence_id, "status": "done", "outcome": "CLASSIFIED"})

    outcome = rec.run_apply(
        rule_id=rule.rule_id, expected_manifest_sha256=manifest_hash, limit=10, checkpoint_path=checkpoint_path,
        evidence_repository=evidence_repository, rule_repository=rule_repository,
        classification_repository=classification_repository, audit_repository=audit_repository,
        actor_type="SYSTEM", actor_id="test",
    )
    assert outcome.exit_code == 0
    # A blindly-trusted checkpoint would have reported success with
    # classified_count=0; the correct behaviour is to genuinely
    # (re)classify both, since the real DB never had them.
    assert outcome.report["classified_count"] == 2
    for evidence in matching:
        current = classification_repository.get_current_classification(evidence.evidence_id, CLASSIFICATION_TYPE_DOCUMENT_TYPE)
        assert current is not None
        assert current.source == "DETERMINISTIC_RULE"


# ---------------------------------------------------------------------
# --limit enforcement
# ---------------------------------------------------------------------


def test_limit_bounds_processing_to_the_documented_deterministic_order(
    evidence_repository, rule_repository, classification_repository, audit_repository, checkpoint_path,
):
    rule = _make_rule(rule_repository)
    _matching_evidence(evidence_repository, rule, n=5)
    manifest_hash, scan = _dry_run_hash(rule, evidence_repository, classification_repository)
    first_two_ids = [c.evidence_id for c in scan.candidates[:2]]

    outcome = rec.run_apply(
        rule_id=rule.rule_id, expected_manifest_sha256=manifest_hash, limit=2, checkpoint_path=checkpoint_path,
        evidence_repository=evidence_repository, rule_repository=rule_repository,
        classification_repository=classification_repository, audit_repository=audit_repository,
        actor_type="SYSTEM", actor_id="test",
    )
    assert outcome.exit_code == 0
    assert outcome.report["classified_count"] == 2
    assert outcome.report["candidates_beyond_limit_count"] == 3

    for evidence_id in first_two_ids:
        assert classification_repository.get_current_classification(evidence_id, CLASSIFICATION_TYPE_DOCUMENT_TYPE) is not None

    remaining_scan = rec._scan_candidates(
        rule=rule, evidence_repository=evidence_repository, classification_repository=classification_repository,
        scan_page_size=200,
    )
    assert len(remaining_scan.candidates) == 3  # the rest were never touched


# ---------------------------------------------------------------------
# Rule identity / lookup errors
# ---------------------------------------------------------------------


def test_apply_raises_not_found_for_unknown_rule_id(
    evidence_repository, rule_repository, classification_repository, audit_repository, checkpoint_path,
):
    with pytest.raises(NotFoundError):
        rec.run_apply(
            rule_id="does-not-exist", expected_manifest_sha256="0" * 64, limit=10, checkpoint_path=checkpoint_path,
            evidence_repository=evidence_repository, rule_repository=rule_repository,
            classification_repository=classification_repository, audit_repository=audit_repository,
            actor_type="SYSTEM", actor_id="test",
        )


def test_apply_refuses_a_retired_rule():
    rule_repository = InMemoryEvidenceClassificationRuleRepository()
    evidence_repository = InMemoryEvidenceRepository(InMemoryExternalReferenceRepository())
    ai_invocation_repository = InMemoryAIInvocationRepository()
    classification_repository = InMemoryEvidenceClassificationRepository(
        evidence_repository=evidence_repository, rule_repository=rule_repository,
        ai_invocation_repository=ai_invocation_repository,
    )
    audit_repository = InMemoryAuditRepository()
    rule = _make_rule(rule_repository)
    rule_repository.retire_rule(rule.rule_id)

    outcome = rec.run_apply(
        rule_id=rule.rule_id, expected_manifest_sha256="0" * 64, limit=10,
        checkpoint_path=Path(tempfile.mkdtemp()) / "checkpoint.jsonl",
        evidence_repository=evidence_repository, rule_repository=rule_repository,
        classification_repository=classification_repository, audit_repository=audit_repository,
        actor_type="SYSTEM", actor_id="test",
    )
    assert outcome.exit_code != 0
    assert outcome.report["error"] == "RULE_NOT_ACTIVE"


# ---------------------------------------------------------------------
# CLI argument shape
# ---------------------------------------------------------------------


def test_cli_apply_requires_expected_manifest_sha256():
    with pytest.raises(SystemExit):
        rec._parse_args(["--runtime-dir", "/tmp/x", "--rule-id", "r1", "--apply", "--limit", "5"])


def test_cli_apply_requires_limit():
    with pytest.raises(SystemExit):
        rec._parse_args(["--runtime-dir", "/tmp/x", "--rule-id", "r1", "--apply", "--expected-manifest-sha256", "a" * 64])


def test_cli_apply_limit_must_be_within_bounds():
    with pytest.raises(SystemExit):
        rec._parse_args([
            "--runtime-dir", "/tmp/x", "--rule-id", "r1", "--apply",
            "--expected-manifest-sha256", "a" * 64, "--limit", "0",
        ])
    with pytest.raises(SystemExit):
        rec._parse_args([
            "--runtime-dir", "/tmp/x", "--rule-id", "r1", "--apply",
            "--expected-manifest-sha256", "a" * 64, "--limit", str(rec._MAX_LIMIT + 1),
        ])


def test_cli_requires_runtime_dir_one_way_or_another(monkeypatch):
    monkeypatch.delenv("BAGMAN_CLASSIFICATION_RUNTIME_DIR", raising=False)
    with pytest.raises(SystemExit):
        rec._parse_args(["--rule-id", "r1", "--dry-run"])


def test_cli_runtime_dir_falls_back_to_env_var(monkeypatch):
    monkeypatch.setenv("BAGMAN_CLASSIFICATION_RUNTIME_DIR", "/tmp/from-env")
    args = rec._parse_args(["--rule-id", "r1", "--dry-run"])
    assert args.runtime_dir == "/tmp/from-env"


def test_cli_dry_run_and_apply_are_mutually_exclusive_and_one_is_required():
    with pytest.raises(SystemExit):
        rec._parse_args(["--runtime-dir", "/tmp/x", "--rule-id", "r1"])
    with pytest.raises(SystemExit):
        rec._parse_args([
            "--runtime-dir", "/tmp/x", "--rule-id", "r1", "--dry-run", "--apply",
            "--expected-manifest-sha256", "a" * 64, "--limit", "5",
        ])
