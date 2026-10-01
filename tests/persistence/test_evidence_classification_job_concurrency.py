"""Real, disposable-PostgreSQL concurrency proof for
`EvidenceClassificationJobRepository.submit_job`'s own evidence_id-
keyed idempotency doctrine (BAGMAN accounting platform,
`evidence/automatic-classification-activation` WO).

Mirrors `tests/persistence/test_xero_account_suggestion_concurrency.py`'s
exact discipline (genuine OS threads + a `threading.Barrier`, never
sequential calls — Python's GIL and this codebase's own
fresh-`Session`-per-call discipline make sequential calls misleadingly
"safe" even when the underlying table has no real constraint) — the
suspected race here is structurally identical to the one that WO's own
post-merge follow-up found and fixed for `xero_account_suggestions`:
two concurrent callers could both pass an application-level
`get_by_evidence(evidence_id) is None` pre-check before either had
committed, producing two rows for the same `evidence_id`. This test
proves `uq_evidence_classification_jobs_evidence_id` (migration
`a7f34c9e2d18`) actually prevents that under genuine concurrency, both
via `EvidenceClassificationJobRepository.submit_job` directly and via
the real `enqueue_classification_job_for_evidence` integration point
both real evidence-creation call sites use.
"""
from __future__ import annotations

import hashlib
import threading
from datetime import datetime, timezone

import pytest

from core import identity
from core.timestamps import utc_now
from persistence.postgres.evidence_classification_job_models import EvidenceClassificationJobRow
from persistence.postgres.evidence_classification_job_repository import PostgresEvidenceClassificationJobRepository
from persistence.postgres.evidence_repository import PostgresEvidenceRepository
from persistence.postgres.external_reference_repository import PostgresExternalReferenceRepository
from persistence.postgres.session import get_engine, session_scope
from persistence.postgres.source_repository import PostgresSourceRepository
from services.evidence.classification_job import enqueue_classification_job_for_evidence

ACTOR_ID = "evidence-classification-job-concurrency-test"

#: This module's own tests exercise SUBMIT/ENQUEUE concurrency — never
#: the preflight-review enqueue-side activation gate (item C) itself.
#: A boundary far enough in the past that every evidence item this
#: module creates always satisfies that gate.
_ALWAYS_ACTIVE_BOUNDARY = datetime(2000, 1, 1, tzinfo=timezone.utc)


def _real_evidence_id() -> str:
    return _real_evidence().evidence_id


def _real_evidence():
    source = PostgresSourceRepository().register_source(
        source_type="MANUAL_UPLOAD", provider="INTERNAL", status="ACTIVE"
    )
    now = utc_now()
    return PostgresEvidenceRepository(PostgresExternalReferenceRepository()).register_evidence(
        entity_id=None,
        evidence_type="DOCUMENT",
        source_id=source.source_id,
        observed_at=now,
        received_at=now,
        content_hash={
            "algorithm": "SHA-256",
            "value": hashlib.sha256(identity.generate_id().encode("utf-8")).hexdigest(),
        },
        mime_type="application/pdf",
        size_bytes=100,
        storage_reference=None,
    )


class _BarrierGatedJobRepository(PostgresEvidenceClassificationJobRepository):
    """Wraps the real Postgres repository, forcing every caller to
    reach `submit_job`'s own INSERT at (as close to) the exact same
    instant — the real TOCTOU window under test."""

    def __init__(self, barrier: threading.Barrier) -> None:
        super().__init__()
        self._barrier = barrier

    def submit_job(self, **kwargs):
        self._barrier.wait(timeout=10)
        return super().submit_job(**kwargs)


def _row_count(evidence_id: str) -> int:
    with session_scope(get_engine()) as session:
        return (
            session.query(EvidenceClassificationJobRow)
            .filter_by(evidence_id=evidence_id)
            .count()
        )


def _run_submit(*, evidence_id: str, repo, results: list, errors: list) -> None:
    try:
        job = repo.submit_job(evidence_id=evidence_id, actor_type="SYSTEM", actor_id=ACTOR_ID)
        results.append(job)
    except Exception as exc:  # noqa: BLE001 - captured for the assertions below, not swallowed
        errors.append(exc)


def _run_enqueue(*, evidence_id: str, evidence_created_at, repo, barrier: threading.Barrier, errors: list) -> None:
    # enqueue_classification_job_for_evidence never raises by contract
    # — any exception observed here would itself be a defect.
    try:
        barrier.wait(timeout=10)
        enqueue_classification_job_for_evidence(
            evidence_id, classification_job_repository=repo, actor_type="SYSTEM", actor_id=ACTOR_ID,
            evidence_created_at=evidence_created_at, activation_boundary=_ALWAYS_ACTIVE_BOUNDARY,
        )
    except Exception as exc:  # noqa: BLE001 - must never happen; captured to fail loudly if it does
        errors.append(exc)


@pytest.mark.usefixtures("fresh_engine")
def test_two_genuinely_concurrent_submit_job_calls_for_the_same_evidence_id(fresh_engine):
    evidence_id = _real_evidence_id()
    barrier = threading.Barrier(2)
    shared_gated_repo = _BarrierGatedJobRepository(barrier)

    results: list = []
    errors: list = []
    threads = [
        threading.Thread(
            target=_run_submit, kwargs=dict(evidence_id=evidence_id, repo=shared_gated_repo, results=results, errors=errors)
        )
        for _ in range(2)
    ]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=15)
        assert not t.is_alive(), "a submit_job thread deadlocked/timed out"

    assert not errors, f"submit_job must never raise under a genuine race: {errors}"
    row_count = _row_count(evidence_id)
    print(f"\n[CONCURRENCY REPRODUCTION] job rows for evidence_id={evidence_id}: {row_count}")
    print(f"[CONCURRENCY REPRODUCTION] thread job_ids: {[r.job_id for r in results]}")

    assert row_count == 1, (
        f"DUPLICATE JOB ROWS: {row_count} EvidenceClassificationJob rows exist for one evidence_id "
        "after two genuinely concurrent submit_job calls."
    )
    # And both callers must agree on the SAME winning row.
    assert len({r.job_id for r in results}) == 1


@pytest.mark.usefixtures("fresh_engine")
def test_two_genuinely_concurrent_enqueue_calls_for_the_same_evidence_id(fresh_engine):
    """Same proof, but through the REAL integration-point function both
    evidence-creation call sites invoke
    (`enqueue_classification_job_for_evidence`), not the repository
    method directly — proves the end-to-end path is race-safe, not
    merely the repository in isolation."""
    evidence = _real_evidence()
    evidence_id = evidence.evidence_id
    barrier = threading.Barrier(2)
    shared_gated_repo = _BarrierGatedJobRepository(barrier)

    errors: list = []
    threads = [
        threading.Thread(
            target=_run_enqueue,
            kwargs=dict(
                evidence_id=evidence_id, evidence_created_at=evidence.created_at, repo=shared_gated_repo,
                barrier=barrier, errors=errors,
            ),
        )
        for _ in range(2)
    ]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=15)
        assert not t.is_alive(), "an enqueue thread deadlocked/timed out"

    assert not errors, f"enqueue_classification_job_for_evidence must never raise: {errors}"
    row_count = _row_count(evidence_id)
    assert row_count == 1, (
        f"DUPLICATE JOB ROWS: {row_count} EvidenceClassificationJob rows exist for one evidence_id "
        "after two genuinely concurrent enqueue_classification_job_for_evidence calls."
    )


@pytest.mark.usefixtures("fresh_engine")
def test_genuinely_concurrent_claim_next_pending_calls_never_double_claim(fresh_engine):
    """Companion proof to `test_evidence_classification_job_repository.py`'s
    own SKIP LOCKED test, using a larger worker/job ratio and the
    `fresh_engine`-scoped repository construction discipline this
    module's other tests use."""
    n_jobs = 8
    n_workers = 3
    evidence_ids = [_real_evidence_id() for _ in range(n_jobs)]
    submit_repo = PostgresEvidenceClassificationJobRepository()
    job_ids = [
        submit_repo.submit_job(evidence_id=eid, actor_type="SYSTEM", actor_id=ACTOR_ID).job_id
        for eid in evidence_ids
    ]

    barrier = threading.Barrier(n_workers)
    results: list[list] = [[] for _ in range(n_workers)]

    def _claim(index: int) -> None:
        repo = PostgresEvidenceClassificationJobRepository()
        barrier.wait(timeout=10)
        claimed = repo.claim_next_pending(limit=n_jobs, claimed_by=f"worker-{index}")
        results[index] = [j.job_id for j in claimed]

    threads = [threading.Thread(target=_claim, args=(i,)) for i in range(n_workers)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=20)

    all_claimed = [jid for worker_result in results for jid in worker_result]
    assert len(all_claimed) == n_jobs
    assert len(set(all_claimed)) == n_jobs, f"a job was claimed by more than one worker: {results}"
    assert set(all_claimed) == set(job_ids)
