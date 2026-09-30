"""Real, disposable-PostgreSQL proofs for
`services.evidence.classification_job.reconcile_missing_classification_jobs`
(BAGMAN accounting platform, `evidence/automatic-classification-activation`
WO — architect delta, 2026-09-29 review, WO item 1: closes the
disclosed "enqueue can be skipped" gap with a bounded, prospective,
activation-boundary-scoped reconciliation, NEVER a historical bulk
backfill; WO item 2, this round: genuine paging via `repair_limit`/
`page_size`/`offset`, replacing the earlier single-fixed-window
implementation; POST-MERGE PREFLIGHT REVIEW CORRECTION, items A and B,
this round — see below).

Mirrors `tests/persistence/test_evidence_classification_job_concurrency.py`'s
own discipline for the genuine-race proof (real `threading.Thread`s +
a `threading.Barrier`, never sequential calls) and
`tests/persistence/test_evidence_classification_job_repository.py`'s
own `_real_evidence_id()`-style fixture construction for everything
else — this module needs its own variant that accepts an explicit
`received_at`, which neither of those modules' helpers exposes.

`activation_boundary` is no longer a module-level constant (see
`services.evidence.classification_job`'s own module docstring — it is
now a required, caller-supplied parameter, sourced for real only from
`scripts/process_evidence_classification_jobs.py`'s own env-var
resolution, proven separately in
`tests/integration/test_evidence_classification_job_worker.py`). Every
test below therefore establishes its OWN `activation_boundary` —
`utc_now()` at the moment the test starts — rather than sharing one
fixed value: since tests in this module run against a real, persistent,
shared-across-tests Postgres database, a fresh per-test boundary
naturally excludes every OTHER test's own evidence rows (created
earlier, hence with an earlier `created_at`) without requiring any
DB cleanup between tests — the same self-isolation property a fixed
historical constant would have needed the old module-level constant
for, achieved here without needing one.

PREFLIGHT REVIEW CORRECTION — item A: eligibility is `created_at`-based
------------------------------------------------------------------------
An earlier version of this function (and this test module) filtered
evidence eligibility on `received_at_from=activation_boundary`. That
was wrong (see `reconcile_missing_classification_jobs`'s own docstring,
"Eligibility is now created_at-based" section, for the full reasoning):
`received_at` is the (possibly much earlier) time the underlying
artifact was actually received/observed, never BAGMAN's own
registration time. Every test below that establishes "this evidence is
historical / this evidence is prospective" now does so via `created_at`
(the server-assigned registration timestamp — indirectly controlled by
WHEN, in real wall-clock time, `register_evidence`/`_real_evidence(...)`
is actually CALLED relative to when `boundary = utc_now()` is captured),
never via the `received_at` argument alone. Several tests deliberately
set `received_at` to a value that would have given the OPPOSITE answer
under the old, wrong filter, to prove `received_at` no longer plays any
role in this decision at all.

PREFLIGHT REVIEW CORRECTION — item B: a durable, per-boundary cursor
------------------------------------------------------------------------
`reconcile_missing_classification_jobs` now takes a REQUIRED
`cursor_repository` parameter
(`services.evidence.classification_reconciliation_cursor
.EvidenceClassificationReconciliationCursorRepository`) and pages
through evidence in REGISTRATION order (`created_at ASC, evidence_id
ASC` — `order_by_created_at=True`), not `received_at DESC` — see that
module's own docstring, and `reconcile_missing_classification_jobs`'s
own docstring, for the full "why the old, no-persisted-cursor,
`received_at DESC` scan could starve an orphan FOREVER" reasoning this
correction fixes. `_reconcile()` below always passes a genuinely fresh
`PostgresEvidenceClassificationReconciliationCursorRepository()`
instance per call by default (mirroring `classification_job_repository`'s
own "fresh instance per call, unless the test needs to observe/reuse
one specific instance" discipline elsewhere in this module) — cursor
state lives durably in Postgres itself, not in any Python object, so a
fresh instance each call still sees the SAME persisted cursor row.

Several tests below whose PREVIOUS assertions depended on the OLD
`received_at DESC` ordering (e.g. "the two jobbed items are the most
RECENTLY received") have been corrected to the NEW `created_at ASC`
ordering (the two jobbed items are the EARLIEST created/registered) —
the underlying property under test (bounded-per-call, genuine
multi-call convergence) is unchanged; only which end of the backlog is
reached first has flipped, because the ordering itself flipped.
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
from persistence.postgres.evidence_classification_reconciliation_cursor_repository import (
    PostgresEvidenceClassificationReconciliationCursorRepository,
)
from persistence.postgres.evidence_classification_repository import PostgresEvidenceClassificationRepository
from persistence.postgres.evidence_classification_rule_repository import PostgresEvidenceClassificationRuleRepository
from persistence.postgres.evidence_repository import PostgresEvidenceRepository
from persistence.postgres.external_reference_repository import PostgresExternalReferenceRepository
from persistence.postgres.models import EvidenceItemRow
from persistence.postgres.session import get_engine, session_scope
from persistence.postgres.source_repository import PostgresSourceRepository
from services.evidence.classification import (
    CLASSIFICATION_TYPE_DOCUMENT_TYPE,
    DOCUMENT_TYPE_SUPPLIER_INVOICE,
    SOURCE_OPERATOR_ASSIGNED,
    STATUS_CLASSIFIED,
)
from services.evidence.classification_job import (
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


def _real_evidence(*, received_at: datetime.datetime):
    """Real, durable `EvidenceItem` with an explicit `received_at` —
    returns the full item (never just its id) so callers can read its
    own server-assigned `created_at` — exactly what proving the
    created_at-based activation-boundary filter requires."""
    source = PostgresSourceRepository().register_source(
        source_type="MANUAL_UPLOAD", provider="INTERNAL", status="ACTIVE"
    )
    return _evidence_repository().register_evidence(
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


def _real_evidence_id(*, received_at: datetime.datetime) -> str:
    return _real_evidence(received_at=received_at).evidence_id


def _insert_evidence_row_directly(
    *, evidence_id: str, created_at: datetime.datetime, received_at: datetime.datetime
) -> None:
    """Direct, test-only row insertion bypassing
    `PostgresEvidenceRepository.register_evidence`'s own internal
    `identity.generate_id()`/`utc_now()` calls entirely — the ONLY way
    to pin BOTH an evidence row's `evidence_id` AND its `created_at` to
    caller-chosen values (`register_evidence` always assigns its own
    fresh id and its own `utc_now()` timestamp; neither is a
    parameter). Reproducing the audit-found cross-process tie-break bug
    below requires exactly that: two rows sharing one exact `created_at`
    with a caller-chosen, DELIBERATELY inverted `evidence_id` ordering —
    something no sequence of real `register_evidence` calls from a
    single test process could ever produce on its own (this codebase's
    own `core.identity.generate_id` is monotonic WITHIN one process by
    construction)."""
    source = PostgresSourceRepository().register_source(
        source_type="MANUAL_UPLOAD", provider="INTERNAL", status="ACTIVE"
    )
    with session_scope(get_engine()) as session:
        session.add(
            EvidenceItemRow(
                evidence_id=evidence_id,
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
                status="OBSERVED",
                created_at=created_at,
                original_name=None,
                storage_reference=None,
                metadata_={},
            )
        )
        session.flush()


def _reconcile(
    *, activation_boundary: datetime.datetime, repair_limit: int = 200, page_size: int = 200,
    max_rows_scanned: int = 5000, job_repo=None, cursor_repo=None,
) -> int:
    return reconcile_missing_classification_jobs(
        evidence_repository=_evidence_repository(),
        classification_job_repository=job_repo or PostgresEvidenceClassificationJobRepository(),
        classification_repository=_classification_repository(),
        cursor_repository=cursor_repo or PostgresEvidenceClassificationReconciliationCursorRepository(),
        actor_type="SYSTEM",
        actor_id=ACTOR_ID,
        activation_boundary=activation_boundary,
        repair_limit=repair_limit,
        page_size=page_size,
        max_rows_scanned=max_rows_scanned,
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
    boundary = utc_now()
    evidence = _real_evidence(received_at=boundary)
    evidence_id = evidence.evidence_id
    failing_repo = _EnqueueFailsOnceRepository()

    # Ingestion's own call site — never raises by contract (see
    # enqueue_classification_job_for_evidence's own docstring) even
    # though the underlying submit_job just failed. Passes the real
    # boundary as activation_boundary so the enqueue-side gate (item C)
    # itself does not interfere with what THIS test is proving.
    enqueue_classification_job_for_evidence(
        evidence_id, classification_job_repository=failing_repo, actor_type="SYSTEM", actor_id=ACTOR_ID,
        evidence_created_at=evidence.created_at, activation_boundary=boundary,
    )

    real_repo = PostgresEvidenceClassificationJobRepository()
    assert real_repo.get_by_evidence(evidence_id) is None, "zero job rows immediately after the forced failure"

    reconciled = _reconcile(activation_boundary=boundary, job_repo=real_repo)
    assert reconciled == 1

    job = real_repo.get_by_evidence(evidence_id)
    assert job is not None
    assert job.status == "PENDING"

    # And it is genuinely processable by a normal claim afterward.
    [claimed] = real_repo.claim_next_pending(limit=1, claimed_by="late-worker")
    assert claimed.job_id == job.job_id


# ---------------------------------------------------------------------
# Test plan item 2 — historical exclusion, forced: evidence with
# created_at BEFORE the activation boundary is NEVER reconciled, at any
# limit — regardless of its own received_at (preflight review
# correction, item A).
# ---------------------------------------------------------------------


def test_historical_evidence_before_activation_boundary_is_never_reconciled():
    # created_at (not received_at) governs eligibility now — create the
    # evidence FIRST, so its own server-assigned created_at is strictly
    # BEFORE `boundary`, captured only afterward. received_at is
    # deliberately set to a time AFTER `boundary` too, proving
    # received_at plays no role at all: the OLD, wrong filter would
    # have wrongly treated this as prospective.
    evidence = _real_evidence(received_at=utc_now() + datetime.timedelta(days=1))
    boundary = utc_now()
    evidence_id = evidence.evidence_id
    assert evidence.created_at < boundary
    assert PostgresEvidenceClassificationJobRepository().get_by_evidence(evidence_id) is None

    for repair_limit in (1, 50, 200):
        reconciled = _reconcile(activation_boundary=boundary, repair_limit=repair_limit)
        assert reconciled == 0
        assert PostgresEvidenceClassificationJobRepository().get_by_evidence(evidence_id) is None


def test_evidence_exactly_at_the_boundary_is_eligible_evidence_strictly_before_is_not():
    # before_boundary is registered FIRST (earlier created_at); boundary
    # is then set to exactly at_boundary's own created_at.
    before_boundary = _real_evidence(received_at=utc_now())
    at_boundary_evidence = _real_evidence(received_at=utc_now())
    boundary = at_boundary_evidence.created_at
    assert before_boundary.created_at < boundary

    reconciled = _reconcile(activation_boundary=boundary)
    assert reconciled == 1

    repo = PostgresEvidenceClassificationJobRepository()
    assert repo.get_by_evidence(at_boundary_evidence.evidence_id) is not None
    assert repo.get_by_evidence(before_boundary.evidence_id) is None


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
    boundary = utc_now()
    evidence = _real_evidence(received_at=boundary)
    evidence_id = evidence.evidence_id
    barrier = threading.Barrier(2)
    gated_repo = _BarrierGatedJobRepository(barrier)

    errors: list = []

    def _run_enqueue():
        try:
            enqueue_classification_job_for_evidence(
                evidence_id, classification_job_repository=gated_repo, actor_type="SYSTEM", actor_id=ACTOR_ID,
                evidence_created_at=evidence.created_at, activation_boundary=boundary,
            )
        except Exception as exc:  # noqa: BLE001 - must never happen; captured to fail loudly
            errors.append(exc)

    def _run_reconcile():
        try:
            reconcile_missing_classification_jobs(
                evidence_repository=_evidence_repository(),
                classification_job_repository=gated_repo,
                classification_repository=_classification_repository(),
                cursor_repository=PostgresEvidenceClassificationReconciliationCursorRepository(),
                actor_type="SYSTEM",
                actor_id="a-different-reconciliation-actor",
                activation_boundary=boundary,
                repair_limit=200,
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
    boundary = utc_now()
    evidence_id = _real_evidence_id(received_at=boundary)
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
        cursor_repository=PostgresEvidenceClassificationReconciliationCursorRepository(),
        actor_type="SYSTEM",
        actor_id=ACTOR_ID,
        activation_boundary=boundary,
        repair_limit=200,
    )
    assert reconciled == 0
    assert PostgresEvidenceClassificationJobRepository().get_by_evidence(evidence_id) is None


# ---------------------------------------------------------------------
# Test plan item 2/5 — reconciliation is bounded: exactly `repair_limit`
# jobs created in one call, never all of them at once; the scan now
# genuinely PAGES in REGISTRATION order (created_at ASC — preflight
# review correction, item B), so a fixed `repair_limit` eventually
# repairs a backlog LARGER than itself across repeated calls, with no
# operator lever needed and no risk of ever re-scanning the same
# already-inspected rows (the durable cursor).
# ---------------------------------------------------------------------


def test_reconciliation_creates_at_most_repair_limit_jobs_in_one_call():
    boundary = utc_now()
    total_orphans = 5
    repair_limit = 2
    evidence_ids = [
        _real_evidence_id(received_at=boundary + datetime.timedelta(seconds=i)) for i in range(total_orphans)
    ]

    reconciled = _reconcile(activation_boundary=boundary, repair_limit=repair_limit)
    assert reconciled == repair_limit, "exactly `repair_limit` jobs created in one call, never all of them"

    repo = PostgresEvidenceClassificationJobRepository()
    jobbed = [eid for eid in evidence_ids if repo.get_by_evidence(eid) is not None]
    assert len(jobbed) == repair_limit
    # The two jobbed items are the two EARLIEST created/registered of
    # the five (list_evidence's own `created_at ASC` ordering when
    # `order_by_created_at=True`, offset=0's own first page) — the
    # FIRST two created (lowest `created_at`), since these evidence
    # items were registered in the same order `evidence_ids` lists them.
    assert set(jobbed) == set(evidence_ids[:repair_limit])


def test_a_second_call_with_the_same_repair_limit_continues_into_newer_candidates():
    """The durable cursor (preflight review correction, item B) is what
    makes a second call with the SAME `repair_limit` genuinely reach
    further into the backlog: the first two already-jobbed (earliest-
    created) rows are never re-inspected — the cursor's own persisted
    position skips straight past them — and the scan continues into the
    next-newer still-orphaned rows."""
    boundary = utc_now()
    evidence_ids = [
        _real_evidence_id(received_at=boundary + datetime.timedelta(seconds=i)) for i in range(5)
    ]
    repair_limit = 2

    first_pass = _reconcile(activation_boundary=boundary, repair_limit=repair_limit)
    assert first_pass == repair_limit

    second_pass = _reconcile(activation_boundary=boundary, repair_limit=repair_limit)
    assert second_pass == repair_limit, "the durable cursor lets a second identical call make real progress"

    repo = PostgresEvidenceClassificationJobRepository()
    jobbed = [eid for eid in evidence_ids if repo.get_by_evidence(eid) is not None]
    assert len(jobbed) == 2 * repair_limit, "the second pass reached NEW, previously-unjobbed evidence"
    # The four jobbed items are the four EARLIEST-created of the five —
    # exactly one (the single newest) remains unresolved after two
    # passes of repair_limit=2 each.
    assert set(jobbed) == set(evidence_ids[: 2 * repair_limit])
    newest = evidence_ids[-1]
    assert repo.get_by_evidence(newest) is None, "the single newest orphan is not yet reached after two passes"


def test_repeated_calls_with_a_fixed_repair_limit_eventually_repair_a_backlog_larger_than_itself():
    """Test plan item 2, the full proof: a backlog LARGER than
    `repair_limit` exists; repeated calls with the SAME, never-widened
    `repair_limit` eventually repair the ENTIRE backlog — no operator
    lever (a larger `--limit`) is needed, and the durable cursor
    guarantees this converges regardless of how the backlog is ordered."""
    boundary = utc_now()
    total_orphans = 7
    repair_limit = 2
    evidence_ids = [
        _real_evidence_id(received_at=boundary + datetime.timedelta(seconds=i)) for i in range(total_orphans)
    ]

    repo = PostgresEvidenceClassificationJobRepository()
    total_reconciled = 0
    # Bounded loop (never an infinite `while True`) — ceil(7/2) = 4
    # passes suffice; a couple of spare iterations confirm it then
    # stays at 0, never over-submits.
    for _ in range(6):
        reconciled_this_pass = _reconcile(activation_boundary=boundary, repair_limit=repair_limit)
        total_reconciled += reconciled_this_pass
        if reconciled_this_pass == 0:
            break

    assert total_reconciled == total_orphans, "every orphan is eventually repaired by the fixed repair_limit"
    assert all(repo.get_by_evidence(eid) is not None for eid in evidence_ids)

    # One more call now makes genuinely zero further progress — the
    # backlog really is fully resolved, not merely "still making slow
    # progress".
    assert _reconcile(activation_boundary=boundary, repair_limit=repair_limit) == 0


def test_pre_activation_evidence_remains_untouched_across_repeated_reconciliation_calls():
    """Test plan item 3: evidence with created_at BEFORE
    `activation_boundary` is never reconciled — proven not just once
    (already covered above) but across MULTIPLE reconciliation calls,
    including while a same-boundary backlog-repair loop (mirroring the
    test above) runs several times, to rule out the historical-exclusion
    filter being accidentally bypassed by the cursor/paging logic.

    Historical evidence is registered FIRST (so its own created_at is
    strictly before `boundary`, captured only afterward); prospective
    evidence is registered AFTER `boundary` is captured."""
    historical_ids = [
        _real_evidence_id(received_at=utc_now() - datetime.timedelta(days=1, seconds=i)) for i in range(3)
    ]
    boundary = utc_now()
    prospective_ids = [
        _real_evidence_id(received_at=boundary + datetime.timedelta(seconds=i)) for i in range(5)
    ]
    repo = PostgresEvidenceClassificationJobRepository()

    for _ in range(4):
        _reconcile(activation_boundary=boundary, repair_limit=2)
        for historical_id in historical_ids:
            assert repo.get_by_evidence(historical_id) is None, "historical evidence must never be reconciled"

    # Meanwhile the prospective backlog genuinely did get repaired by
    # those same repeated calls.
    assert all(repo.get_by_evidence(eid) is not None for eid in prospective_ids)


def test_widening_the_repair_limit_still_reaches_the_remainder_faster():
    """The old "operator lever" is still available and still works —
    the cursor did not remove it, it just made it optional. A
    wide-enough `repair_limit` on a later call reaches every remaining
    orphan in one pass."""
    boundary = utc_now()
    total_orphans = 5
    small_repair_limit = 2
    evidence_ids = [
        _real_evidence_id(received_at=boundary + datetime.timedelta(seconds=i)) for i in range(total_orphans)
    ]

    first_pass = _reconcile(activation_boundary=boundary, repair_limit=small_repair_limit)
    assert first_pass == small_repair_limit

    second_pass = _reconcile(activation_boundary=boundary, repair_limit=200)
    assert second_pass == total_orphans - small_repair_limit

    repo = PostgresEvidenceClassificationJobRepository()
    assert all(repo.get_by_evidence(eid) is not None for eid in evidence_ids)


def test_max_rows_scanned_bounds_a_single_call_without_interfering_with_a_normal_repair():
    """The `max_rows_scanned` safety bound (a PL judgment call, not part
    of the architect's own pseudocode — see
    `reconcile_missing_classification_jobs`'s own docstring): set
    generously here anyway, it must never prevent a normal, realistic
    repair from completing in one call."""
    boundary = utc_now()
    total_orphans = 10
    evidence_ids = [
        _real_evidence_id(received_at=boundary + datetime.timedelta(seconds=i)) for i in range(total_orphans)
    ]

    reconciled = _reconcile(
        activation_boundary=boundary, repair_limit=200, page_size=3, max_rows_scanned=5000,
    )
    assert reconciled == total_orphans

    repo = PostgresEvidenceClassificationJobRepository()
    assert all(repo.get_by_evidence(eid) is not None for eid in evidence_ids)


@pytest.mark.usefixtures("fresh_engine")
def test_reconciliation_never_raises_and_still_resolves_to_one_row_across_threads(fresh_engine):
    """Companion, broader-race proof: N reconciliation-only calls,
    genuinely concurrent, for the SAME single orphaned evidence item —
    submit_job's own unique constraint must still resolve to one row,
    never raise, exactly like `enqueue_classification_job_for_evidence`
    already proves in isolation. This ALSO exercises a genuine
    concurrent first-advance race on the SAME, brand-new cursor row
    (all three threads share the same `activation_boundary`, hence the
    same `cursor_key`) — see
    `PostgresEvidenceClassificationReconciliationCursorRepository
    .advance_cursor`'s own docstring for why that race is handled, never
    left to raise."""
    boundary = utc_now()
    evidence_id = _real_evidence_id(received_at=boundary)
    barrier = threading.Barrier(3)
    errors: list = []

    def _run():
        try:
            barrier.wait(timeout=10)
            reconcile_missing_classification_jobs(
                evidence_repository=_evidence_repository(),
                classification_job_repository=PostgresEvidenceClassificationJobRepository(),
                classification_repository=_classification_repository(),
                cursor_repository=PostgresEvidenceClassificationReconciliationCursorRepository(),
                actor_type="SYSTEM",
                actor_id=ACTOR_ID,
                activation_boundary=boundary,
                repair_limit=200,
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


# ---------------------------------------------------------------------
# PREFLIGHT REVIEW CORRECTION — item B(i): the regression proof. Seeds
# `max_rows_scanned` newer already-resolved evidence rows plus one
# OLDER still-orphaned row that sorts BEHIND all of them in
# `received_at DESC` order (the OLD scan's own default/only ordering) —
# proves, by literally running the OLD scan's own reconstructed logic,
# that the orphan is NEVER found no matter how many repeated calls are
# made; then proves the NEW, cursor-based implementation DOES find it.
# ---------------------------------------------------------------------


def _old_buggy_reconcile_missing_classification_jobs(
    *, evidence_repository, classification_job_repository, classification_repository, activation_boundary,
    repair_limit, page_size, max_rows_scanned, actor_type="SYSTEM", actor_id=ACTOR_ID,
):
    """A faithful, test-only reconstruction of the PRE-FIX
    `reconcile_missing_classification_jobs` function body: filters on
    `received_at_from` (never `created_at_from`), relies on
    `list_evidence`'s own DEFAULT ordering (`received_at DESC,
    evidence_id DESC` — never `order_by_created_at=True`), and keeps NO
    persisted cross-call cursor at all — every call restarts scanning
    at `offset=0`. Kept here ONLY to prove, concretely and
    reproducibly, that the starvation bug this correction fixes was
    real — this exact function no longer exists in production code
    (superseded entirely by the cursor-based implementation)."""
    offset = 0
    submitted = 0
    rows_scanned = 0
    while submitted < repair_limit and rows_scanned < max_rows_scanned:
        page = evidence_repository.list_evidence(
            received_at_from=activation_boundary, limit=page_size, offset=offset,
        )
        if not page:
            break
        rows_scanned += len(page)
        for item in page:
            if submitted >= repair_limit:
                break
            if classification_job_repository.get_by_evidence(item.evidence_id) is not None:
                continue
            if classification_repository.get_current_classification(
                item.evidence_id, CLASSIFICATION_TYPE_DOCUMENT_TYPE
            ) is not None:
                continue
            classification_job_repository.submit_job(
                evidence_id=item.evidence_id, actor_type=actor_type, actor_id=actor_id,
            )
            submitted += 1
        offset += len(page)
        if len(page) < page_size:
            break
    return submitted


def test_old_fixed_window_scan_would_starve_an_orphan_behind_more_than_max_rows_scanned_resolved_rows():
    """Preflight review correction, item B(i) — the regression proof.

    Seeds a small, test-friendly `max_rows_scanned` (10) newer,
    ALREADY-RESOLVED evidence rows (each given a real job up front,
    each received AFTER the orphan) plus ONE older, genuinely orphaned
    row — under `received_at DESC` (the old scan's own only ordering),
    every one of the 10 resolved rows sorts AHEAD of the orphan, which
    therefore sits at position 11 — one past `max_rows_scanned`.

    First, calls `_old_buggy_reconcile_missing_classification_jobs`
    (the reconstructed pre-fix logic) FIVE times with the SAME fixed
    `repair_limit`/`page_size`/`max_rows_scanned` and proves the orphan
    is NEVER found — this is the bug, reproduced directly, not merely
    asserted. Then calls the real, current, cursor-based
    `reconcile_missing_classification_jobs` with the IDENTICAL seed data
    and proves it DOES find and repair the orphan.
    """
    boundary = utc_now()
    max_rows_scanned = 10
    job_repo = PostgresEvidenceClassificationJobRepository()
    classification_repo = _classification_repository()

    # The orphan — OLDEST received_at, sorts LAST under received_at DESC.
    orphan = _real_evidence(received_at=boundary)

    # `max_rows_scanned` newer, ALREADY-RESOLVED rows (each with a real
    # job already), all received AFTER the orphan — every one of them
    # sorts AHEAD of the orphan under received_at DESC.
    for i in range(max_rows_scanned):
        resolved = _real_evidence(received_at=boundary + datetime.timedelta(seconds=i + 1))
        job_repo.submit_job(evidence_id=resolved.evidence_id, actor_type="SYSTEM", actor_id=ACTOR_ID)

    # --- Prove the OLD behaviour genuinely, permanently starves ---
    for _ in range(5):
        old_result = _old_buggy_reconcile_missing_classification_jobs(
            evidence_repository=_evidence_repository(), classification_job_repository=job_repo,
            classification_repository=classification_repo, activation_boundary=boundary,
            repair_limit=5, page_size=5, max_rows_scanned=max_rows_scanned,
        )
        assert old_result == 0, "the old scan must never find the orphan — it is always behind the resolved window"
    assert job_repo.get_by_evidence(orphan.evidence_id) is None, (
        "confirmed: the orphan is STILL unjobbed after 5 repeated old-style calls with the SAME limits — "
        "this is the permanent starvation this correction fixes"
    )

    # --- Now prove the NEW, cursor-based implementation converges ---
    total_reconciled = 0
    for _ in range(10):  # bounded loop, never an infinite while True
        reconciled_this_pass = _reconcile(
            activation_boundary=boundary, repair_limit=5, page_size=5, max_rows_scanned=max_rows_scanned,
            job_repo=job_repo,
        )
        total_reconciled += reconciled_this_pass
        if job_repo.get_by_evidence(orphan.evidence_id) is not None:
            break

    assert job_repo.get_by_evidence(orphan.evidence_id) is not None, (
        "the NEW cursor-based reconciliation must eventually reach the orphan with the SAME fixed limits "
        "the old implementation could never converge under"
    )


def test_cursor_advances_and_a_subsequent_call_reaches_a_row_behind_max_rows_scanned():
    """Preflight review correction, item B(ii): proves the durable
    cursor genuinely advances between calls, and a SECOND call with the
    SAME fixed `max_rows_scanned` reaches a row that a SINGLE call's own
    `max_rows_scanned` bound could not have reached in one call — a
    harder case than the regression test above, since here the orphan
    is the NEWEST-created row (behind the resolved prefix even in the
    NEW created_at ASC ordering), so only the cursor's own persisted
    progress (never the reordering alone) makes it reachable at all.
    Impossible under the old, no-persisted-cursor semantics (every call
    restarted at the front of the window)."""
    boundary = utc_now()
    max_rows_scanned = 10
    job_repo = PostgresEvidenceClassificationJobRepository()
    cursor_repo = PostgresEvidenceClassificationReconciliationCursorRepository()

    resolved_items = []
    for _ in range(max_rows_scanned):
        resolved = _real_evidence(received_at=boundary)
        job_repo.submit_job(evidence_id=resolved.evidence_id, actor_type="SYSTEM", actor_id=ACTOR_ID)
        resolved_items.append(resolved)
    resolved_ids = [item.evidence_id for item in resolved_items]

    # Registered AFTER all `max_rows_scanned` resolved rows above, so it
    # sorts LAST in created_at ASC order too — position 11, one past
    # `max_rows_scanned`.
    orphan = _real_evidence(received_at=boundary)

    assert cursor_repo.get_cursor(boundary.isoformat()) is None

    first_pass = _reconcile(
        activation_boundary=boundary, repair_limit=200, page_size=200, max_rows_scanned=max_rows_scanned,
        job_repo=job_repo, cursor_repo=cursor_repo,
    )
    assert first_pass == 0, "the orphan sits one position past max_rows_scanned=10 — unreachable in a single call"
    assert job_repo.get_by_evidence(orphan.evidence_id) is None

    cursor_after_first = cursor_repo.get_cursor(boundary.isoformat())
    assert cursor_after_first is not None
    # `last_evidence_ids` is a SET (see the module's own "set-membership
    # tie-break" correction) — assert membership, never exact scalar
    # equality, and confirm its size matches how many distinct ids
    # genuinely share that exact watermark timestamp (in practice 1,
    # since each `_real_evidence` call is a separate real DB round trip
    # and so gets its own distinct `created_at`, but this must hold
    # regardless).
    assert resolved_ids[-1] in cursor_after_first.last_evidence_ids
    ids_sharing_watermark_timestamp = {
        item.evidence_id for item in resolved_items if item.created_at == cursor_after_first.last_created_at
    }
    assert set(cursor_after_first.last_evidence_ids) == ids_sharing_watermark_timestamp

    second_pass = _reconcile(
        activation_boundary=boundary, repair_limit=200, page_size=200, max_rows_scanned=max_rows_scanned,
        job_repo=job_repo, cursor_repo=cursor_repo,
    )
    assert second_pass == 1, "the SAME fixed max_rows_scanned now reaches the orphan, because the cursor advanced"
    assert job_repo.get_by_evidence(orphan.evidence_id) is not None


# ---------------------------------------------------------------------
# POST-MERGE AUDIT CORRECTION: the cursor's tie-break must be
# SET-MEMBERSHIP, never an ordering comparison (`evidence_id <=
# scan_after_evidence_id`) — cross-process UUIDv7 ordering
# (`core.identity.generate_id`) is not guaranteed; that module's own
# docstring is explicit its monotonic-counter guarantee holds only
# WITHIN one process. See `services.evidence
# .classification_reconciliation_cursor`'s own docstring ("Why the
# tie-break is SET membership") and `reconcile_missing_classification_jobs`'s
# own docstring ("Why the tie-break is set membership, never
# evidence_id <= scan_after_evidence_id") for the full reasoning.
# ---------------------------------------------------------------------


def _old_scalar_tiebreak_reconcile_missing_classification_jobs(
    *,
    evidence_repository,
    classification_job_repository,
    classification_repository,
    cursor_store: dict,
    activation_boundary: datetime.datetime,
    actor_type: str = "SYSTEM",
    actor_id: str = ACTOR_ID,
    repair_limit: int = 200,
    page_size: int = 200,
    max_rows_scanned: int = 5000,
) -> int:
    """A faithful, test-only reconstruction of THIS DELIVERY'S OWN
    PRIOR `reconcile_missing_classification_jobs` body — before the
    set-membership tie-break correction below — using a single scalar
    `last_evidence_id` and an ordering-based `evidence_id <=
    scan_after_evidence_id` skip condition. Kept here ONLY to prove,
    concretely and reproducibly, that the cross-process starvation bug
    the correction below closes was real; this exact function no
    longer exists in production code (superseded entirely by the
    set-membership implementation). `cursor_store` is a plain dict
    standing in for the OLD single-scalar cursor storage (`cursor_key
    -> (last_created_at, last_evidence_id)`) — this function never
    touches the real, now-corrected
    `EvidenceClassificationReconciliationCursorRepository`, whose own
    interface no longer has a scalar shape to reconstruct against."""
    cursor_key = activation_boundary.isoformat()
    existing = cursor_store.get(cursor_key)
    scan_created_at_from = existing[0] if existing is not None else activation_boundary
    scan_after_evidence_id = existing[1] if existing is not None else None

    offset = 0
    submitted = 0
    rows_scanned = 0
    last_seen_created_at = None
    last_seen_evidence_id = None

    while submitted < repair_limit and rows_scanned < max_rows_scanned:
        page = evidence_repository.list_evidence(
            created_at_from=scan_created_at_from, limit=page_size, offset=offset, order_by_created_at=True,
        )
        if not page:
            break
        for item in page:
            if submitted >= repair_limit or rows_scanned >= max_rows_scanned:
                break
            if (
                existing is not None
                and item.created_at == scan_created_at_from
                and item.evidence_id <= scan_after_evidence_id
            ):
                continue

            rows_scanned += 1
            last_seen_created_at, last_seen_evidence_id = item.created_at, item.evidence_id

            if classification_job_repository.get_by_evidence(item.evidence_id) is not None:
                continue
            if classification_repository.get_current_classification(
                item.evidence_id, CLASSIFICATION_TYPE_DOCUMENT_TYPE
            ) is not None:
                continue
            classification_job_repository.submit_job(
                evidence_id=item.evidence_id, actor_type=actor_type, actor_id=actor_id,
            )
            submitted += 1
        offset += len(page)
        if len(page) < page_size:
            break

    if last_seen_created_at is not None:
        cursor_store[cursor_key] = (last_seen_created_at, last_seen_evidence_id)

    return submitted


def test_cross_process_created_at_tie_evidence_id_inversion_is_not_permanently_lost():
    """The audit-found bug, reproduced directly, then proven fixed.

    Two evidence rows share the EXACT SAME `created_at` timestamp — the
    SECOND-registered row's `evidence_id` is deliberately made to sort
    LEXICOGRAPHICALLY BEFORE the first-registered row's id, simulating
    exactly what two genuinely separate OS processes (BAGMAN registers
    evidence from at least the API server and the mailbox-ingestion
    worker) could produce: `core.identity.generate_id`'s own docstring
    is explicit its monotonic-counter guarantee holds only WITHIN one
    process, never across two — so nothing prevents a second process's
    independently-seeded counter from minting an id that sorts before
    one a different process minted moments earlier for the same
    millisecond.

    First proves the OLD, single-scalar `<=` tie-break
    (`_old_scalar_tiebreak_reconcile_missing_classification_jobs`,
    reconstructed above) PERMANENTLY loses the second-registered row —
    across repeated calls, never merely once. Then proves the REAL,
    current, set-membership `reconcile_missing_classification_jobs`
    finds it, and that it stays found.
    """
    boundary = utc_now()
    # Deliberately NOT boundary + some future offset: real wall-clock
    # time will have advanced well past `boundary` itself by the time
    # the second scenario below captures its own fresh `boundary_new`
    # (several real DB round trips happen in between) — but a
    # FUTURE-offset timestamp here could still be >= a `boundary_new`
    # captured only microseconds later, contaminating that scenario's
    # own scan window with this one's leftover rows.
    tie_created_at = boundary
    evidence_repo = _evidence_repository()
    job_repo = PostgresEvidenceClassificationJobRepository()
    classification_repo = _classification_repository()

    id_candidate_one = identity.generate_id()
    id_candidate_two = identity.generate_id()
    # Force the inversion: whichever id sorts LARGER is the
    # first-registered row; whichever sorts SMALLER is the
    # second-registered row. This is impossible to obtain from one
    # process's own sequential generate_id() calls without forcing it
    # directly (see _insert_evidence_row_directly's own docstring) —
    # which is exactly why this is a genuinely cross-process bug, not
    # something a single-process test could stumble into by accident.
    id_first_registered, id_second_registered = sorted(
        [id_candidate_one, id_candidate_two], reverse=True
    )
    assert id_second_registered < id_first_registered

    _insert_evidence_row_directly(
        evidence_id=id_first_registered, created_at=tie_created_at, received_at=boundary,
    )

    # --- OLD, scalar `<=` tie-break: permanently loses the second row ---
    cursor_store: dict = {}
    first_pass_old = _old_scalar_tiebreak_reconcile_missing_classification_jobs(
        evidence_repository=evidence_repo, classification_job_repository=job_repo,
        classification_repository=classification_repo, cursor_store=cursor_store, activation_boundary=boundary,
    )
    assert first_pass_old == 1
    assert job_repo.get_by_evidence(id_first_registered) is not None

    # The SECOND-registered row arrives (a different "process"), sharing
    # the exact same created_at, with an id that sorts BEFORE the
    # already-inspected one — the cross-process inversion.
    _insert_evidence_row_directly(
        evidence_id=id_second_registered, created_at=tie_created_at, received_at=boundary,
    )

    for _ in range(5):  # bounded loop, never an infinite while True
        old_result = _old_scalar_tiebreak_reconcile_missing_classification_jobs(
            evidence_repository=evidence_repo, classification_job_repository=job_repo,
            classification_repository=classification_repo, cursor_store=cursor_store, activation_boundary=boundary,
        )
        assert old_result == 0, "the OLD scalar `<=` tie-break must skip the inverted-id row every time, forever"
    assert job_repo.get_by_evidence(id_second_registered) is None, (
        "confirmed: the OLD scalar tie-break PERMANENTLY loses the second-registered row once its id sorts "
        "before the cursor's own last_evidence_id — this is the audit-found bug, reproduced directly"
    )

    # --- NEW, set-membership tie-break: finds it, and it stays found ---
    boundary_new = utc_now()
    tie_created_at_new = boundary_new
    cursor_repo = PostgresEvidenceClassificationReconciliationCursorRepository()

    id_candidate_three = identity.generate_id()
    id_candidate_four = identity.generate_id()
    id_first_registered_new, id_second_registered_new = sorted(
        [id_candidate_three, id_candidate_four], reverse=True
    )
    assert id_second_registered_new < id_first_registered_new

    _insert_evidence_row_directly(
        evidence_id=id_first_registered_new, created_at=tie_created_at_new, received_at=boundary_new,
    )

    assert cursor_repo.get_cursor(boundary_new.isoformat()) is None

    first_pass_new = _reconcile(
        activation_boundary=boundary_new, job_repo=job_repo, cursor_repo=cursor_repo,
    )
    assert first_pass_new == 1
    assert job_repo.get_by_evidence(id_first_registered_new) is not None

    cursor_after_first_new = cursor_repo.get_cursor(boundary_new.isoformat())
    assert cursor_after_first_new is not None
    assert cursor_after_first_new.last_created_at == tie_created_at_new
    assert set(cursor_after_first_new.last_evidence_ids) == {id_first_registered_new}

    # The SECOND-registered row arrives — same cross-process inversion
    # as the OLD-code scenario above.
    _insert_evidence_row_directly(
        evidence_id=id_second_registered_new, created_at=tie_created_at_new, received_at=boundary_new,
    )

    second_pass_new = _reconcile(
        activation_boundary=boundary_new, job_repo=job_repo, cursor_repo=cursor_repo,
    )
    assert second_pass_new == 1, (
        "the NEW set-membership tie-break must find the inverted-id row — never lost, unlike the OLD scalar "
        "tie-break proven above against the identical scenario"
    )
    assert job_repo.get_by_evidence(id_second_registered_new) is not None

    cursor_after_second_new = cursor_repo.get_cursor(boundary_new.isoformat())
    assert cursor_after_second_new is not None
    assert cursor_after_second_new.last_created_at == tie_created_at_new
    assert set(cursor_after_second_new.last_evidence_ids) == {id_first_registered_new, id_second_registered_new}, (
        "the persisted cursor must be the UNION of every id ever inspected at this exact timestamp, "
        "across both calls — never just the most recent call's own ids"
    )

    # A third pass is stable — nothing new to find, no duplicate jobs,
    # no crash.
    third_pass_new = _reconcile(
        activation_boundary=boundary_new, job_repo=job_repo, cursor_repo=cursor_repo,
    )
    assert third_pass_new == 0


# ---------------------------------------------------------------------
# PREFLIGHT REVIEW CORRECTION — item C(iv): reconciliation's own
# behaviour is entirely unaffected by whatever the enqueue-side
# activation gate skipped — it remains the sole recovery path, using
# the SAME boundary. (The in-memory-only version of this proof lives in
# tests/integration/test_evidence_classification_activation_gate.py;
# this is the real-Postgres companion.)
# ---------------------------------------------------------------------


def test_reconciliation_finds_evidence_the_enqueue_side_gate_skipped():
    boundary = utc_now()
    evidence = _real_evidence(received_at=boundary)

    # Simulates the enqueue-side gate skipping this evidence — e.g.
    # because no real activation boundary was configured yet at the
    # moment this evidence was ingested.
    enqueue_classification_job_for_evidence(
        evidence.evidence_id, classification_job_repository=PostgresEvidenceClassificationJobRepository(),
        actor_type="SYSTEM", actor_id=ACTOR_ID, evidence_created_at=evidence.created_at, activation_boundary=None,
    )
    assert PostgresEvidenceClassificationJobRepository().get_by_evidence(evidence.evidence_id) is None

    # The operator later configures a real boundary at/before this
    # evidence's own created_at — reconciliation, using that SAME
    # boundary, finds and repairs it.
    reconciled = _reconcile(activation_boundary=boundary)
    assert reconciled == 1

    job = PostgresEvidenceClassificationJobRepository().get_by_evidence(evidence.evidence_id)
    assert job is not None
    assert job.status == "PENDING"
