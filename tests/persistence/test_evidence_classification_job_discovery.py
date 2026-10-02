"""Real, disposable-PostgreSQL proofs for
`EvidenceClassificationJobRepository.list_missing_classification_candidates`
and `services.evidence.classification_job.create_missing_classification_jobs`
(BAGMAN accounting platform, `evidence/classification-simplification`
WO).

This is the entire discovery mechanism that replaces the earlier
enqueue-at-ingestion + durable reconciliation-cursor design (three
prior delivery rounds: `evidence/automatic-classification-activation`,
`evidence/classification-activation-preflight`, and this module's own
second architect-review delta) — see
`services.evidence.classification_job`'s own module docstring,
"Simplified design" section, for the full reasoning. The whole point of
this simplification is that there is NO cursor, NO scan state, and NO
sweep/lap/target machinery left anywhere: every call queries
PostgreSQL's own current state fresh, via a single real
`NOT EXISTS`/anti-join SQL query
(`PostgresEvidenceClassificationJobRepository
.list_missing_classification_candidates`). The tests below are
organised to mirror exactly the required-proof checklist this WO's own
PID enumerates: eligibility (1-5), then reliability (6-10) — the
reliability section in particular replaces every one of the deleted
`test_evidence_classification_job_reconciliation(_sweep).py` tests
(cursor starvation, commit-order inversion, lap-counter stall,
lap-boundary liveness) with dramatically simpler proofs, because the
whole class of "cursor got stuck/inverted/starved" bug this delivery's
prior rounds fought with simply cannot occur under this design — there
is no cursor to get stuck.

Mirrors `tests/persistence/test_evidence_classification_job_repository.py`'s
own `_real_evidence_id()`-style fixture construction, and
`tests/persistence/test_evidence_classification_job_concurrency.py`'s
own real `threading.Thread`+`threading.Barrier` discipline for every
genuine-concurrency proof below — never sequential calls dressed up as
a race. The `_begin_uncommitted_evidence_insert`/
`_insert_evidence_row_directly` helpers below are adapted from the
now-deleted `test_evidence_classification_job_reconciliation_sweep.py`'s
own identical techniques (recreated here, self-contained, per this
codebase's own established "each module owns its own helper variant"
convention).
"""
from __future__ import annotations

import hashlib
import threading
from datetime import datetime, timedelta

import sqlalchemy as sa
from sqlalchemy import create_engine

from core import identity
from core.timestamps import utc_now
from persistence.postgres.ai_invocation_repository import PostgresAIInvocationRepository
from persistence.postgres.evidence_classification_job_models import EvidenceClassificationJobRow
from persistence.postgres.evidence_classification_job_repository import PostgresEvidenceClassificationJobRepository
from persistence.postgres.evidence_classification_repository import PostgresEvidenceClassificationRepository
from persistence.postgres.evidence_classification_rule_repository import PostgresEvidenceClassificationRuleRepository
from persistence.postgres.evidence_repository import PostgresEvidenceRepository
from persistence.postgres.external_reference_repository import PostgresExternalReferenceRepository
from persistence.postgres.models import EvidenceItemRow
from persistence.postgres.session import get_database_url, get_engine, session_scope
from persistence.postgres.source_repository import PostgresSourceRepository
from services.evidence.classification import (
    CLASSIFICATION_TYPE_DOCUMENT_TYPE,
    DOCUMENT_TYPE_SUPPLIER_INVOICE,
    SOURCE_OPERATOR_ASSIGNED,
    STATUS_CLASSIFIED,
)
from services.evidence.classification_job import create_missing_classification_jobs

ACTOR_ID = "evidence-classification-job-discovery-test"


def _evidence_repository() -> PostgresEvidenceRepository:
    return PostgresEvidenceRepository(PostgresExternalReferenceRepository())


def _classification_repository() -> PostgresEvidenceClassificationRepository:
    return PostgresEvidenceClassificationRepository(
        evidence_repository=_evidence_repository(),
        rule_repository=PostgresEvidenceClassificationRuleRepository(),
        ai_invocation_repository=PostgresAIInvocationRepository(),
    )


def _job_repository() -> PostgresEvidenceClassificationJobRepository:
    return PostgresEvidenceClassificationJobRepository()


def _register_evidence(*, received_at: datetime):
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


def _insert_evidence_row_directly(*, evidence_id: str, created_at: datetime, received_at: datetime) -> None:
    """Direct, committed, test-only row insertion pinning a caller-chosen
    `created_at` independently of `received_at` — the only way to prove
    eligibility is governed solely by `created_at` (test 3 below)."""
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


def _begin_uncommitted_evidence_insert(*, created_at: datetime, received_at: datetime):
    """Starts registering an `EvidenceItem` on a MANUALLY managed
    connection/transaction — NOT `session_scope`'s own auto-commit-
    and-close context manager — held OPEN (uncommitted) until the
    caller explicitly commits it. This is the only way to genuinely
    reproduce "a transaction that started registering evidence but has
    not committed yet, and is therefore invisible to any OTHER
    connection's READ COMMITTED snapshot".

    Returns `(evidence_id, connection, transaction)`; the caller must
    eventually call `transaction.commit()` and `connection.close()` —
    never left open past the end of a test.
    """
    source = PostgresSourceRepository().register_source(
        source_type="MANUAL_UPLOAD", provider="INTERNAL", status="ACTIVE"
    )
    evidence_id = identity.generate_id()
    connection = get_engine().connect()
    transaction = connection.begin()
    connection.execute(
        sa.insert(EvidenceItemRow).values(
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
    return evidence_id, connection, transaction


def _discover(*, activation_boundary: datetime, job_repository=None, **overrides) -> int:
    kwargs = dict(
        classification_job_repository=job_repository or _job_repository(),
        actor_type="SYSTEM",
        actor_id=ACTOR_ID,
        activation_boundary=activation_boundary,
    )
    kwargs.update(overrides)
    return create_missing_classification_jobs(**kwargs)


# =======================================================================
# Eligibility (1-5)
# =======================================================================


def test_post_activation_evidence_with_no_job_or_classification_is_discovered():
    boundary = utc_now()
    evidence = _register_evidence(received_at=boundary + timedelta(hours=1))

    candidates = _job_repository().list_missing_classification_candidates(activation_boundary=boundary, limit=10)
    assert evidence.evidence_id in candidates

    created = _discover(activation_boundary=boundary)
    assert created == 1
    job = _job_repository().get_by_evidence(evidence.evidence_id)
    assert job is not None
    assert job.status == "PENDING"
    assert job.actor_id == ACTOR_ID


def test_pre_activation_evidence_is_never_discovered_regardless_of_call_count():
    evidence = _register_evidence(received_at=utc_now())
    # Strictly AFTER this evidence's own created_at — the boundary is
    # "activated" only for evidence registered from this point forward.
    boundary = utc_now() + timedelta(days=1)

    for call_number in range(1, 6):
        created = _discover(activation_boundary=boundary)
        assert created == 0, f"call {call_number}: pre-activation evidence must never be discovered"

    assert _job_repository().get_by_evidence(evidence.evidence_id) is None


def test_old_received_at_but_qualifying_created_at_is_discovered():
    """received_at must have ZERO bearing on eligibility — only
    created_at (server-assigned registration time) governs it."""
    boundary = utc_now()
    evidence_id = identity.generate_id()
    _insert_evidence_row_directly(
        evidence_id=evidence_id,
        created_at=boundary + timedelta(seconds=1),  # qualifies: >= boundary
        received_at=boundary - timedelta(days=365),  # a whole year "old" by received_at
    )

    created = _discover(activation_boundary=boundary)
    assert created == 1
    assert _job_repository().get_by_evidence(evidence_id) is not None


def test_evidence_with_an_existing_current_classification_is_excluded():
    boundary = utc_now()
    evidence = _register_evidence(received_at=boundary + timedelta(hours=1))
    _classification_repository().create_classification(
        evidence_id=evidence.evidence_id,
        classification_type=CLASSIFICATION_TYPE_DOCUMENT_TYPE,
        document_type=DOCUMENT_TYPE_SUPPLIER_INVOICE,
        status=STATUS_CLASSIFIED,
        source=SOURCE_OPERATOR_ASSIGNED,
        operator_action_id="pre-existing-operator-action",
    )

    candidates = _job_repository().list_missing_classification_candidates(activation_boundary=boundary, limit=10)
    assert evidence.evidence_id not in candidates

    created = _discover(activation_boundary=boundary)
    assert created == 0
    assert _job_repository().get_by_evidence(evidence.evidence_id) is None


def test_evidence_with_an_existing_job_is_excluded_even_without_a_classification():
    boundary = utc_now()
    evidence = _register_evidence(received_at=boundary + timedelta(hours=1))
    pre_existing = _job_repository().submit_job(
        evidence_id=evidence.evidence_id, actor_type="SYSTEM", actor_id="pre-existing-caller"
    )

    candidates = _job_repository().list_missing_classification_candidates(activation_boundary=boundary, limit=10)
    assert evidence.evidence_id not in candidates

    created = _discover(activation_boundary=boundary)
    assert created == 0
    job = _job_repository().get_by_evidence(evidence.evidence_id)
    assert job.job_id == pre_existing.job_id  # unchanged, never duplicated


# =======================================================================
# Reliability (6-10)
# =======================================================================


def test_evidence_committed_after_one_call_is_found_by_the_very_next_call():
    """Direct proof that the whole commit-order-inversion class of bug
    the prior cursor-based design fought with cannot occur here: there
    is no watermark to advance past an invisible row, so the very next
    call — with zero cursor/state needed in between — finds it the
    moment it becomes visible."""
    boundary = utc_now()
    uncommitted_id, connection, transaction = _begin_uncommitted_evidence_insert(
        created_at=boundary + timedelta(seconds=1), received_at=boundary
    )
    try:
        first = _discover(activation_boundary=boundary)
        assert first == 0, "the uncommitted row must be entirely invisible to this call"
        assert _job_repository().get_by_evidence(uncommitted_id) is None
    finally:
        transaction.commit()
        connection.close()

    second = _discover(activation_boundary=boundary)
    assert second == 1, "no cursor/state of any kind should be needed to find it now that it has committed"
    assert _job_repository().get_by_evidence(uncommitted_id) is not None


def test_backlog_larger_than_one_calls_limit_drains_fully_across_repeated_calls():
    boundary = utc_now()
    evidence_ids = [
        _register_evidence(received_at=boundary + timedelta(seconds=i)).evidence_id for i in range(25)
    ]
    limit = 10

    total_created = 0
    calls = 0
    while True:
        created = _discover(activation_boundary=boundary, limit=limit)
        calls += 1
        total_created += created
        if created == 0:
            break
        assert calls <= 10, "safety bound against a genuinely infinite loop defect"

    assert total_created == len(evidence_ids)
    assert calls >= 3, f"a backlog of 25 with limit=10 must genuinely page (got {calls} call(s))"
    for evidence_id in evidence_ids:
        assert _job_repository().get_by_evidence(evidence_id) is not None


def test_continuously_arriving_newer_evidence_does_not_starve_an_older_orphan():
    """Directly replaces every one of the deleted sweep/lap/target
    tests with a dramatically simpler proof: seed an old orphan, then
    interleave several rounds of "insert newer evidence + create a job
    for it immediately (never via discovery) + re-check discovery",
    and prove the old orphan is still found every single round —
    because there is no cursor position for the newer arrivals to push
    it behind."""
    boundary = utc_now()
    orphan = _register_evidence(received_at=boundary + timedelta(seconds=1))

    for round_number in range(5):
        newer = _register_evidence(received_at=boundary + timedelta(seconds=10 + round_number))
        # The newer evidence gets its job through the ordinary,
        # immediate path — never through discovery — so it is never
        # itself a discovery candidate.
        _job_repository().submit_job(evidence_id=newer.evidence_id, actor_type="SYSTEM", actor_id="immediate-caller")

        candidates = _job_repository().list_missing_classification_candidates(activation_boundary=boundary, limit=200)
        assert orphan.evidence_id in candidates, (
            f"round {round_number}: the orphan must still be a live candidate no matter how much newer "
            "evidence has since arrived"
        )

    created = _discover(activation_boundary=boundary)
    assert created == 1
    assert _job_repository().get_by_evidence(orphan.evidence_id) is not None


def test_two_real_concurrent_discovery_calls_produce_exactly_one_job_per_evidence_id():
    """Genuine `threading.Thread`s + a `threading.Barrier`, never
    sequential calls — complements
    `tests/persistence/test_evidence_classification_job_concurrency.py
    ::test_two_genuinely_concurrent_discover_and_create_calls_for_the_same_evidence_id`'s
    own single-evidence-id proof with a multi-evidence-id discovery
    pass: both threads discover the SAME deterministically-ordered set
    of candidates and race on `submit_job` for each in turn."""
    boundary = utc_now()
    evidence_ids = [
        _register_evidence(received_at=boundary + timedelta(seconds=i)).evidence_id for i in range(10)
    ]

    barrier = threading.Barrier(2)

    class _BarrierGatedJobRepository(PostgresEvidenceClassificationJobRepository):
        def submit_job(self, **kwargs):
            barrier.wait(timeout=10)
            return super().submit_job(**kwargs)

    errors: list = []

    def _run() -> None:
        try:
            create_missing_classification_jobs(
                classification_job_repository=_BarrierGatedJobRepository(),
                actor_type="SYSTEM", actor_id=ACTOR_ID, activation_boundary=boundary, limit=200,
            )
        except Exception as exc:  # noqa: BLE001 - captured for the assertions below, not swallowed
            errors.append(exc)

    threads = [threading.Thread(target=_run) for _ in range(2)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=30)
        assert not t.is_alive(), "a discovery thread deadlocked/timed out"

    assert not errors, f"create_missing_classification_jobs must never raise under a genuine race: {errors}"

    with session_scope(get_engine()) as session:
        for evidence_id in evidence_ids:
            count = (
                session.query(EvidenceClassificationJobRow).filter_by(evidence_id=evidence_id).count()
            )
            assert count == 1, f"evidence_id={evidence_id} has {count} job rows after a genuine concurrent race"


def test_worker_restart_from_a_fresh_repository_instance_behaves_identically():
    """Structural proof: there is no repository/table left anywhere a
    restarted worker could even query for "where did I leave off" — a
    "crash and restart" is simulated here by calling again from a
    BRAND NEW repository instance, built from a BRAND NEW `Engine`
    (never the process-wide cached one, never any Python object shared
    with the first call), and the system behaves identically to an
    uninterrupted run: each call simply finds whatever is left,
    oldest-first, exactly as it would have if the process had never
    stopped."""
    boundary = utc_now()
    first_evidence = _register_evidence(received_at=boundary + timedelta(seconds=1))
    second_evidence = _register_evidence(received_at=boundary + timedelta(seconds=2))

    engine_a = create_engine(get_database_url(), pool_pre_ping=True, future=True)
    try:
        repo_a = PostgresEvidenceClassificationJobRepository(engine_a)
        created_first_run = _discover(activation_boundary=boundary, job_repository=repo_a, limit=1)
        assert created_first_run == 1
    finally:
        engine_a.dispose()

    # "Crash": engine_a is fully disposed above, repo_a goes out of
    # scope — nothing from the first call is reused below.
    engine_b = create_engine(get_database_url(), pool_pre_ping=True, future=True)
    try:
        repo_b = PostgresEvidenceClassificationJobRepository(engine_b)
        created_second_run = _discover(activation_boundary=boundary, job_repository=repo_b, limit=1)
        assert created_second_run == 1, (
            "a 'restarted worker' with zero shared Python state must still find exactly the remaining "
            "candidate, identically to an uninterrupted run"
        )
    finally:
        engine_b.dispose()

    assert _job_repository().get_by_evidence(first_evidence.evidence_id) is not None
    assert _job_repository().get_by_evidence(second_evidence.evidence_id) is not None

    # No cursor table of any kind exists for this to have relied on.
    inspector = sa.inspect(get_engine())
    table_names = set(inspector.get_table_names())
    assert "evidence_classification_reconciliation_cursors" not in table_names
    assert not any("cursor" in name and "classification" in name for name in table_names)
