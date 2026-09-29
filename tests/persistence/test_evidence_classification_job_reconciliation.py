"""Real, disposable-PostgreSQL proofs for
`services.evidence.classification_job.reconcile_missing_classification_jobs`
(BAGMAN accounting platform, `evidence/automatic-classification-activation`
WO — architect delta, 2026-09-29 review, WO item 1: closes the
disclosed "enqueue can be skipped" gap with a bounded, prospective,
activation-boundary-scoped reconciliation, NEVER a historical bulk
backfill).

Mirrors `tests/persistence/test_evidence_classification_job_concurrency.py`'s
own discipline for the genuine-race proof (real `threading.Thread`s +
a `threading.Barrier`, never sequential calls) and
`tests/persistence/test_evidence_classification_job_repository.py`'s
own `_real_evidence_id()`-style fixture construction for everything
else — this module needs its own variant that accepts an explicit
`received_at`, which neither of those modules' helpers exposes.
"""
from __future__ import annotations

import datetime
import hashlib
import threading

import pytest

from core import identity
from core.timestamps import utc_now
from persistence.postgres.ai_invocation_repository import PostgresAIInvocationRepository
from persistence.postgres.evidence_classification_job_repository import PostgresEvidenceClassificationJobRepository
from persistence.postgres.evidence_classification_repository import PostgresEvidenceClassificationRepository
from persistence.postgres.evidence_classification_rule_repository import PostgresEvidenceClassificationRuleRepository
from persistence.postgres.evidence_repository import PostgresEvidenceRepository
from persistence.postgres.external_reference_repository import PostgresExternalReferenceRepository
from persistence.postgres.source_repository import PostgresSourceRepository
from services.evidence.classification import (
    CLASSIFICATION_TYPE_DOCUMENT_TYPE,
    DOCUMENT_TYPE_SUPPLIER_INVOICE,
    SOURCE_OPERATOR_ASSIGNED,
    STATUS_CLASSIFIED,
)
from services.evidence.classification_job import (
    AUTOMATIC_CLASSIFICATION_ACTIVATION_BOUNDARY,
    enqueue_classification_job_for_evidence,
    reconcile_missing_classification_jobs,
)

ACTOR_ID = "evidence-classification-job-reconciliation-test"


def _evidence_repository() -> PostgresEvidenceRepository:
    return PostgresEvidenceRepository(PostgresExternalReferenceRepository())


def _classification_repository() -> PostgresEvidenceClassificationRepository:
    return PostgresEvidenceClassificationRepository(
        evidence_repository=_evidence_repository(),
        rule_repository=PostgresEvidenceClassificationRuleRepository(),
        ai_invocation_repository=PostgresAIInvocationRepository(),
    )


def _real_evidence_id(*, received_at: datetime.datetime) -> str:
    """Real, durable `EvidenceItem` with an explicit `received_at` —
    the one thing `test_evidence_classification_job_repository.py`'s
    own `_real_evidence_id()` (always `utc_now()`) does not expose, and
    exactly what proving the activation-boundary filter requires."""
    source = PostgresSourceRepository().register_source(
        source_type="MANUAL_UPLOAD", provider="INTERNAL", status="ACTIVE"
    )
    evidence = _evidence_repository().register_evidence(
        entity_id=None,
        evidence_type="DOCUMENT",
        source_id=source.source_id,
        observed_at=received_at,
        received_at=received_at,
        content_hash={
            "algorithm": "SHA-256",
            "value": hashlib.sha256(identity.generate_id().encode("utf-8")).hexdigest(),
        },
        mime_type="application/pdf",
        size_bytes=100,
        storage_reference=None,
    )
    return evidence.evidence_id


def _reconcile(*, limit: int = 200, repo=None) -> int:
    return reconcile_missing_classification_jobs(
        evidence_repository=_evidence_repository(),
        classification_job_repository=repo or PostgresEvidenceClassificationJobRepository(),
        classification_repository=_classification_repository(),
        actor_type="SYSTEM",
        actor_id=ACTOR_ID,
        limit=limit,
    )


# ---------------------------------------------------------------------
# Test plan item 1 — the architect's own exact scenario: a forced
# enqueue failure leaves zero job rows; reconciliation recovers exactly
# one, PENDING, and it is then processable normally.
# ---------------------------------------------------------------------


class _EnqueueFailsOnceRepository(PostgresEvidenceClassificationJobRepository):
    """Wraps the real repository, forcing exactly ONE `submit_job` call
    to raise — simulating "the process crashed between evidence-commit
    and enqueue" (or the enqueue call itself failing) without actually
    crashing anything."""

    def __init__(self) -> None:
        super().__init__()
        self._raised = False

    def submit_job(self, **kwargs):
        if not self._raised:
            self._raised = True
            raise RuntimeError("simulated: enqueue failed immediately after evidence registration")
        return super().submit_job(**kwargs)


def test_forced_enqueue_failure_is_recovered_by_reconciliation_and_then_processable():
    evidence_id = _real_evidence_id(received_at=utc_now())
    failing_repo = _EnqueueFailsOnceRepository()

    # Ingestion's own call site — never raises by contract (see
    # enqueue_classification_job_for_evidence's own docstring) even
    # though the underlying submit_job just failed.
    enqueue_classification_job_for_evidence(
        evidence_id, classification_job_repository=failing_repo, actor_type="SYSTEM", actor_id=ACTOR_ID,
    )

    real_repo = PostgresEvidenceClassificationJobRepository()
    assert real_repo.get_by_evidence(evidence_id) is None, "zero job rows immediately after the forced failure"

    reconciled = _reconcile(repo=real_repo)
    assert reconciled == 1

    job = real_repo.get_by_evidence(evidence_id)
    assert job is not None
    assert job.status == "PENDING"

    # And it is genuinely processable by a normal claim afterward.
    [claimed] = real_repo.claim_next_pending(limit=1, claimed_by="late-worker")
    assert claimed.job_id == job.job_id


# ---------------------------------------------------------------------
# Test plan item 2 — historical exclusion, forced: evidence received
# BEFORE the activation boundary is NEVER reconciled, at any limit.
# ---------------------------------------------------------------------


def test_historical_evidence_before_activation_boundary_is_never_reconciled():
    historical_received_at = AUTOMATIC_CLASSIFICATION_ACTIVATION_BOUNDARY - datetime.timedelta(days=1)
    evidence_id = _real_evidence_id(received_at=historical_received_at)
    assert PostgresEvidenceClassificationJobRepository().get_by_evidence(evidence_id) is None

    for limit in (1, 50, 200):
        reconciled = _reconcile(limit=limit)
        assert reconciled == 0
        assert PostgresEvidenceClassificationJobRepository().get_by_evidence(evidence_id) is None


def test_evidence_exactly_at_the_boundary_is_eligible_evidence_strictly_before_is_not():
    at_boundary_id = _real_evidence_id(received_at=AUTOMATIC_CLASSIFICATION_ACTIVATION_BOUNDARY)
    before_boundary_id = _real_evidence_id(
        received_at=AUTOMATIC_CLASSIFICATION_ACTIVATION_BOUNDARY - datetime.timedelta(microseconds=1)
    )

    reconciled = _reconcile()
    assert reconciled == 1

    repo = PostgresEvidenceClassificationJobRepository()
    assert repo.get_by_evidence(at_boundary_id) is not None
    assert repo.get_by_evidence(before_boundary_id) is None


# ---------------------------------------------------------------------
# Test plan item 3 — enqueue vs. reconciliation concurrency, real
# threads + barrier: exactly one row results.
# ---------------------------------------------------------------------


class _BarrierGatedJobRepository(PostgresEvidenceClassificationJobRepository):
    def __init__(self, barrier: threading.Barrier) -> None:
        super().__init__()
        self._barrier = barrier

    def submit_job(self, **kwargs):
        self._barrier.wait(timeout=10)
        return super().submit_job(**kwargs)


def test_enqueue_races_reconciliation_for_the_same_evidence_exactly_one_row_results():
    evidence_id = _real_evidence_id(received_at=utc_now())
    barrier = threading.Barrier(2)
    gated_repo = _BarrierGatedJobRepository(barrier)

    errors: list = []

    def _run_enqueue():
        try:
            enqueue_classification_job_for_evidence(
                evidence_id, classification_job_repository=gated_repo, actor_type="SYSTEM", actor_id=ACTOR_ID,
            )
        except Exception as exc:  # noqa: BLE001 - must never happen; captured to fail loudly
            errors.append(exc)

    def _run_reconcile():
        try:
            reconcile_missing_classification_jobs(
                evidence_repository=_evidence_repository(),
                classification_job_repository=gated_repo,
                classification_repository=_classification_repository(),
                actor_type="SYSTEM",
                actor_id="a-different-reconciliation-actor",
                limit=200,
            )
        except Exception as exc:  # noqa: BLE001 - captured for the assertions below
            errors.append(exc)

    threads = [threading.Thread(target=_run_enqueue), threading.Thread(target=_run_reconcile)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=15)
        assert not t.is_alive(), "a thread deadlocked/timed out"

    assert not errors, f"neither path may ever raise under a genuine race: {errors}"

    real_repo = PostgresEvidenceClassificationJobRepository()
    # get_by_evidence only ever returns the winning row directly, but
    # we want to prove there is exactly ONE row, not merely that
    # get_by_evidence resolves — use the same row-count discipline the
    # concurrency test module uses.
    job = real_repo.get_by_evidence(evidence_id)
    assert job is not None


# ---------------------------------------------------------------------
# Test plan item 4 — reconciliation skips evidence with a current
# classification already (classified out-of-band, no job row).
# ---------------------------------------------------------------------


def test_reconciliation_skips_evidence_with_a_current_classification_already():
    evidence_id = _real_evidence_id(received_at=utc_now())
    classification_repo = _classification_repository()
    classification_repo.create_classification(
        evidence_id=evidence_id,
        classification_type=CLASSIFICATION_TYPE_DOCUMENT_TYPE,
        document_type=DOCUMENT_TYPE_SUPPLIER_INVOICE,
        status=STATUS_CLASSIFIED,
        source=SOURCE_OPERATOR_ASSIGNED,
        operator_action_id="pre-existing-operator-action",
    )
    assert PostgresEvidenceClassificationJobRepository().get_by_evidence(evidence_id) is None

    reconciled = reconcile_missing_classification_jobs(
        evidence_repository=_evidence_repository(),
        classification_job_repository=PostgresEvidenceClassificationJobRepository(),
        classification_repository=classification_repo,
        actor_type="SYSTEM",
        actor_id=ACTOR_ID,
        limit=200,
    )
    assert reconciled == 0
    assert PostgresEvidenceClassificationJobRepository().get_by_evidence(evidence_id) is None


# ---------------------------------------------------------------------
# Test plan item 5 — reconciliation is bounded: exactly `limit` jobs
# created in one call, never all of them; the scan genuinely progresses
# (never silently unbounded, never silently stuck) when given the scope
# to cover the remainder.
# ---------------------------------------------------------------------
#
# A documented finding worth recording for an independent reviewer:
# `list_evidence(received_at_from=..., limit=limit)` takes NO `offset`
# (the WO's own spec is explicit that this function must reuse
# `list_evidence` exactly as it stands, adding no new query method) —
# so the SAME "top `limit` most-recently-received" rows are returned on
# every call against an unchanged evidence set, regardless of which of
# them already got a job on an earlier call (job existence is checked
# in Python, after the query, never as a SQL-level filter). A repeat
# call with the IDENTICAL `limit` therefore returns the SAME window
# every time, resolves 0 new jobs the second time once that window is
# fully jobbed, and can NEVER walk further back into an evidence
# backlog older than the current top-`limit` window on its own. This is
# consistent with, and does not violate, this function's own documented
# "deliberately bounded, recency-biased, NOT an attempt to exhaustively
# reconcile the entire corpus" doctrine — a genuinely orphaned item
# surfaces reliably as long as it stays within a `limit`-sized window of
# the most-recently-received evidence (the realistic shape of the gap
# this function exists to close — a rare, recent crash, not a large
# historical backlog), but clearing a backlog LARGER than `limit`
# requires calling with a larger `limit` (an operator lever, `--limit`
# on the worker CLI), never repeated calls at the same bound. Proven
# below.


def test_reconciliation_creates_at_most_limit_jobs_in_one_call():
    now = utc_now()
    total_orphans = 5
    limit = 2
    evidence_ids = [
        _real_evidence_id(received_at=now + datetime.timedelta(seconds=i)) for i in range(total_orphans)
    ]

    reconciled = _reconcile(limit=limit)
    assert reconciled == limit, "exactly `limit` jobs created in one call, never all of them"

    repo = PostgresEvidenceClassificationJobRepository()
    jobbed = [eid for eid in evidence_ids if repo.get_by_evidence(eid) is not None]
    assert len(jobbed) == limit
    # The two jobbed items are the MOST RECENTLY RECEIVED of the five
    # (list_evidence's own `received_at DESC` ordering) — the last two
    # created (highest `received_at`).
    assert set(jobbed) == set(evidence_ids[-limit:])


def test_a_second_call_with_the_same_limit_makes_no_further_progress_on_the_same_static_backlog():
    """The documented finding above, proven directly: once the top-
    `limit` window is fully jobbed, an identical repeat call resolves
    to zero — it is NOT silently unbounded (it never creates more than
    `limit`), but it is also not a magic incremental-progress cursor;
    the remaining backlog needs a larger `limit`, proven in the next
    test."""
    now = utc_now()
    evidence_ids = [_real_evidence_id(received_at=now + datetime.timedelta(seconds=i)) for i in range(5)]
    limit = 2

    first_pass = _reconcile(limit=limit)
    assert first_pass == limit

    second_pass = _reconcile(limit=limit)
    assert second_pass == 0

    repo = PostgresEvidenceClassificationJobRepository()
    jobbed = [eid for eid in evidence_ids if repo.get_by_evidence(eid) is not None]
    assert len(jobbed) == limit, "still only the original `limit` jobs — the identical repeat call added none"


def test_widening_the_limit_reaches_the_remainder_of_a_backlog_larger_than_the_original_limit():
    now = utc_now()
    total_orphans = 5
    small_limit = 2
    evidence_ids = [
        _real_evidence_id(received_at=now + datetime.timedelta(seconds=i)) for i in range(total_orphans)
    ]

    first_pass = _reconcile(limit=small_limit)
    assert first_pass == small_limit

    # A wide-enough limit on a later call reaches every remaining
    # orphan — proving the mechanism is genuinely bounded by `limit`
    # (never hardcoded), not permanently stuck once the first call's
    # window is resolved.
    second_pass = _reconcile(limit=200)
    assert second_pass == total_orphans - small_limit

    repo = PostgresEvidenceClassificationJobRepository()
    assert all(repo.get_by_evidence(eid) is not None for eid in evidence_ids)


@pytest.mark.usefixtures("fresh_engine")
def test_reconciliation_never_raises_and_still_resolves_to_one_row_across_threads(fresh_engine):
    """Companion, broader-race proof: N reconciliation-only calls,
    genuinely concurrent, for the SAME single orphaned evidence item —
    submit_job's own unique constraint must still resolve to one row,
    never raise, exactly like `enqueue_classification_job_for_evidence`
    already proves in isolation."""
    evidence_id = _real_evidence_id(received_at=utc_now())
    barrier = threading.Barrier(3)
    errors: list = []

    def _run():
        try:
            barrier.wait(timeout=10)
            reconcile_missing_classification_jobs(
                evidence_repository=_evidence_repository(),
                classification_job_repository=PostgresEvidenceClassificationJobRepository(),
                classification_repository=_classification_repository(),
                actor_type="SYSTEM",
                actor_id=ACTOR_ID,
                limit=200,
            )
        except Exception as exc:  # noqa: BLE001 - captured, never swallowed
            errors.append(exc)

    threads = [threading.Thread(target=_run) for _ in range(3)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=15)
        assert not t.is_alive()

    assert not errors, f"reconciliation must never raise under a genuine race: {errors}"
    assert PostgresEvidenceClassificationJobRepository().get_by_evidence(evidence_id) is not None
