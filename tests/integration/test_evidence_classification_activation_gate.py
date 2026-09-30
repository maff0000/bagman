"""Preflight review correction, item C — "one coherent activation
contract": `enqueue_classification_job_for_evidence`'s own new
activation gate (`evidence_created_at`/`activation_boundary`), and
`reconcile_missing_classification_jobs`'s own unaffected behaviour as
the sole recovery path for whatever that gate skips (BAGMAN accounting
platform, `evidence/automatic-classification-activation` WO).

Entirely in-memory — no real Postgres needed: `reconcile_missing_classification_jobs`
is fully duck-typed across every repository parameter it takes, exactly
like every other real caller in this codebase already relies on (see
`tests/integration/test_evidence_classification_job_worker.py`'s own
identical in-memory discipline). The real-Postgres companion proof for
item C(iv) lives in
`tests/persistence/test_evidence_classification_job_reconciliation.py
::test_reconciliation_finds_evidence_the_enqueue_side_gate_skipped`.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

from ai.invocation import InMemoryAIInvocationRepository
from core import actor, identity
from core.audit import InMemoryAuditRepository
from core.external_reference import InMemoryExternalReferenceRepository
from services.evidence.classification import InMemoryEvidenceClassificationRepository
from services.evidence.classification_job import (
    InMemoryEvidenceClassificationJobRepository,
    enqueue_classification_job_for_evidence,
    reconcile_missing_classification_jobs,
)
from services.evidence.classification_reconciliation_cursor import (
    InMemoryEvidenceClassificationReconciliationCursorRepository,
)
from services.evidence.classification_rule import InMemoryEvidenceClassificationRuleRepository
from services.evidence.evidence import InMemoryEvidenceRepository

ACTOR_ID = "evidence-classification-activation-gate-test"


def _evidence_repository() -> InMemoryEvidenceRepository:
    return InMemoryEvidenceRepository(InMemoryExternalReferenceRepository())


def _register_evidence(evidence_repository, *, received_at=None):
    now = datetime.now(timezone.utc)
    received_at = received_at if received_at is not None else now
    return evidence_repository.register_evidence(
        entity_id=None, evidence_type="DOCUMENT", source_id=identity.generate_id(),
        observed_at=received_at, received_at=received_at,
        content_hash={"algorithm": "SHA-256", "value": "0" * 64},
        mime_type="application/pdf", size_bytes=10, storage_reference=None,
    )


# ---------------------------------------------------------------------
# Item C(i) — activation boundary unset -> never creates a job,
# regardless of the evidence's own created_at.
# ---------------------------------------------------------------------


def test_activation_boundary_unset_never_creates_a_job_regardless_of_created_at():
    evidence_repository = _evidence_repository()
    job_repository = InMemoryEvidenceClassificationJobRepository()
    evidence = _register_evidence(evidence_repository)

    enqueue_classification_job_for_evidence(
        evidence.evidence_id, classification_job_repository=job_repository, actor_type=actor.SYSTEM,
        actor_id=ACTOR_ID, evidence_created_at=evidence.created_at, activation_boundary=None,
    )

    assert job_repository.get_by_evidence(evidence.evidence_id) is None


# ---------------------------------------------------------------------
# Item C(ii) — activation boundary set, evidence created_at on/after it
# -> a job IS created.
# ---------------------------------------------------------------------


def test_activation_boundary_set_evidence_created_at_on_or_after_creates_a_job():
    evidence_repository = _evidence_repository()
    job_repository = InMemoryEvidenceClassificationJobRepository()
    evidence = _register_evidence(evidence_repository)
    boundary = evidence.created_at  # exactly on the boundary — inclusive

    enqueue_classification_job_for_evidence(
        evidence.evidence_id, classification_job_repository=job_repository, actor_type=actor.SYSTEM,
        actor_id=ACTOR_ID, evidence_created_at=evidence.created_at, activation_boundary=boundary,
    )

    job = job_repository.get_by_evidence(evidence.evidence_id)
    assert job is not None
    assert job.status == "PENDING"


def test_activation_boundary_set_evidence_created_at_after_boundary_creates_a_job():
    evidence_repository = _evidence_repository()
    job_repository = InMemoryEvidenceClassificationJobRepository()
    evidence = _register_evidence(evidence_repository)
    boundary = evidence.created_at - timedelta(microseconds=1)  # strictly BEFORE evidence.created_at

    enqueue_classification_job_for_evidence(
        evidence.evidence_id, classification_job_repository=job_repository, actor_type=actor.SYSTEM,
        actor_id=ACTOR_ID, evidence_created_at=evidence.created_at, activation_boundary=boundary,
    )

    job = job_repository.get_by_evidence(evidence.evidence_id)
    assert job is not None
    assert job.status == "PENDING"


# ---------------------------------------------------------------------
# Item C(iii) — activation boundary set, evidence created_at strictly
# before it -> NO job is created (defensive edge case).
# ---------------------------------------------------------------------


def test_activation_boundary_set_evidence_created_at_strictly_before_never_creates_a_job():
    evidence_repository = _evidence_repository()
    job_repository = InMemoryEvidenceClassificationJobRepository()
    evidence = _register_evidence(evidence_repository)
    boundary = evidence.created_at + timedelta(microseconds=1)  # strictly AFTER evidence.created_at

    enqueue_classification_job_for_evidence(
        evidence.evidence_id, classification_job_repository=job_repository, actor_type=actor.SYSTEM,
        actor_id=ACTOR_ID, evidence_created_at=evidence.created_at, activation_boundary=boundary,
    )

    assert job_repository.get_by_evidence(evidence.evidence_id) is None


# ---------------------------------------------------------------------
# Item C(iv) — reconciliation's own behaviour is entirely unaffected by
# whatever the enqueue-side gate skipped: an evidence item the gate
# skipped is still found and given a job by a subsequent reconciliation
# call, using the SAME boundary ("a missed enqueue after activation
# remains recoverable" — by design, not an accident).
# ---------------------------------------------------------------------


def test_reconciliation_finds_evidence_the_enqueue_side_gate_skipped_using_the_same_boundary():
    evidence_repository = _evidence_repository()
    job_repository = InMemoryEvidenceClassificationJobRepository()
    rule_repository = InMemoryEvidenceClassificationRuleRepository()
    audit_repository = InMemoryAuditRepository()
    ai_invocation_repository = InMemoryAIInvocationRepository(audit_repository=audit_repository)
    classification_repository = InMemoryEvidenceClassificationRepository(
        evidence_repository=evidence_repository, rule_repository=rule_repository,
        ai_invocation_repository=ai_invocation_repository,
    )
    cursor_repository = InMemoryEvidenceClassificationReconciliationCursorRepository()

    evidence = _register_evidence(evidence_repository)

    # Simulates "the enqueue-side gate skipped this evidence" — e.g.
    # because no real activation boundary was configured yet at the
    # moment this evidence was ingested.
    enqueue_classification_job_for_evidence(
        evidence.evidence_id, classification_job_repository=job_repository, actor_type=actor.SYSTEM,
        actor_id=ACTOR_ID, evidence_created_at=evidence.created_at, activation_boundary=None,
    )
    assert job_repository.get_by_evidence(evidence.evidence_id) is None

    # The operator later configures a real boundary at/before this
    # evidence's own created_at — reconciliation, using that SAME
    # boundary, finds and repairs it.
    boundary = evidence.created_at

    reconciled = reconcile_missing_classification_jobs(
        evidence_repository=evidence_repository, classification_job_repository=job_repository,
        classification_repository=classification_repository, cursor_repository=cursor_repository,
        actor_type=actor.SYSTEM, actor_id="a-reconciliation-actor", activation_boundary=boundary,
    )

    assert reconciled == 1
    job = job_repository.get_by_evidence(evidence.evidence_id)
    assert job is not None
    assert job.status == "PENDING"
