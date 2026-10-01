"""Real, disposable-PostgreSQL proofs for THIS ROUND's "safety sweep"
backstop cursor and `advance_cursor`'s merge-on-write correction (BAGMAN
accounting platform, `evidence/classification-activation-preflight`
WO — architect-confirmed defect: `PostgresEvidenceRepository
.register_evidence` assigns `EvidenceItem.created_at` in Python, in the
application, BEFORE its own transaction commits; under Postgres's
default READ COMMITTED isolation, two concurrent registrations can
COMMIT in an order DIFFERENT from their `created_at` VALUE order — a
transaction that started first (an earlier `created_at`) can commit
LATER than one that started after it (a later `created_at`). The
forward reconciliation cursor
(`tests/persistence/test_evidence_classification_job_reconciliation.py`)
advances its watermark from whatever is VISIBLE (committed) at scan
time and then treats that watermark as a hard, permanent exclusion
filter — so a row that commits AFTER a scan has already advanced past
its own `created_at` is excluded from every future forward scan,
forever, even once it becomes visible.

Kept in its own file (rather than folded into
`test_evidence_classification_job_reconciliation.py`, already large)
per this round's own PID: "keep it discoverable and consistently
named" — this file's own name makes its scope (the sweep + the
concurrency-safe cursor write it depends on) obvious at a glance.

Reuses this codebase's own established test conventions throughout:
`tests/persistence/conftest.py`'s `postgres_container`/`fresh_engine`
fixtures; `test_evidence_classification_job_reconciliation.py`'s own
`_real_evidence`/`_insert_evidence_row_directly`/`_old_*_reconstruction`
patterns (duplicated here in this module's own self-contained variant,
exactly as that file's own docstring already establishes as the norm:
"this module needs its own variant... which neither of those modules'
helpers exposes"); and
`test_reconciliation_never_raises_and_still_resolves_to_one_row_across_threads`'s
own real-`threading.Thread`+`threading.Barrier` discipline for every
genuine-concurrency proof below — never sequential calls dressed up as
a race.
"""
from __future__ import annotations

import datetime
import hashlib
import threading
import time

import pytest
import sqlalchemy as sa
from sqlalchemy.exc import IntegrityError

from core import identity
from core.timestamps import utc_now
from persistence.postgres.ai_invocation_repository import PostgresAIInvocationRepository
from persistence.postgres.evidence_classification_job_models import (
    EvidenceClassificationJobRow,
    EvidenceClassificationReconciliationCursorRow,
)
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
from services.evidence.classification import CLASSIFICATION_TYPE_DOCUMENT_TYPE
from services.evidence.classification_job import (
    enqueue_classification_job_for_evidence,
    reconcile_missing_classification_jobs,
)

ACTOR_ID = "evidence-classification-job-reconciliation-sweep-test"


def _evidence_repository() -> PostgresEvidenceRepository:
    return PostgresEvidenceRepository(PostgresExternalReferenceRepository())


def _classification_repository() -> PostgresEvidenceClassificationRepository:
    return PostgresEvidenceClassificationRepository(
        evidence_repository=_evidence_repository(),
        rule_repository=PostgresEvidenceClassificationRuleRepository(),
        ai_invocation_repository=PostgresAIInvocationRepository(),
    )


def _real_evidence(*, received_at: datetime.datetime):
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
    """Direct, committed, test-only row insertion pinning a caller-chosen
    `created_at` — identical purpose to the same-named helper in
    `test_evidence_classification_job_reconciliation.py` (duplicated
    here per this file's own self-contained-helpers convention)."""
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


def _begin_uncommitted_evidence_insert(*, created_at: datetime.datetime, received_at: datetime.datetime):
    """Starts registering an `EvidenceItem` on a MANUALLY managed
    connection/transaction — NOT `session_scope`'s own auto-commit-
    and-close context manager — held OPEN (uncommitted) until the
    caller explicitly commits it. This is the only way to genuinely
    reproduce "a transaction that started registering evidence but has
    not committed yet, and is therefore invisible to any OTHER
    connection's READ COMMITTED snapshot" — the exact precondition the
    architect's own reproduction scenario requires.

    Returns `(evidence_id, connection, transaction)`; the caller must
    eventually call `transaction.commit()` (or `.rollback()`) and then
    `connection.close()` — never left open past the end of a test.
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


def _reconcile(*, cursor_repo, job_repo=None, activation_boundary: datetime.datetime, **overrides) -> int:
    kwargs = dict(
        evidence_repository=_evidence_repository(),
        classification_job_repository=job_repo or PostgresEvidenceClassificationJobRepository(),
        classification_repository=_classification_repository(),
        cursor_repository=cursor_repo,
        actor_type="SYSTEM",
        actor_id=ACTOR_ID,
        activation_boundary=activation_boundary,
    )
    kwargs.update(overrides)
    return reconcile_missing_classification_jobs(**kwargs)


def _sweep_cursor_key(activation_boundary: datetime.datetime) -> str:
    return f"{activation_boundary.isoformat()}::sweep"


# ---------------------------------------------------------------------
# Old (pre-this-round), forward-cursor-ONLY reconstruction — no sweep —
# used ONLY to prove the commit-order-inversion gap was real under the
# sweep-less implementation. Mirrors this delivery's own established
# `_old_buggy_...`/`_old_scalar_tiebreak_...` reconstruction pattern
# (`test_evidence_classification_job_reconciliation.py`). This exact
# function no longer exists in production code.
# ---------------------------------------------------------------------


def _old_forward_only_reconcile_missing_classification_jobs(
    *,
    evidence_repository,
    classification_job_repository,
    classification_repository,
    cursor_repository,
    activation_boundary: datetime.datetime,
    actor_type: str = "SYSTEM",
    actor_id: str = ACTOR_ID,
    repair_limit: int = 200,
    page_size: int = 200,
    max_rows_scanned: int = 5000,
) -> int:
    cursor_key = activation_boundary.isoformat()
    existing_cursor = cursor_repository.get_cursor(cursor_key)
    scan_created_at_from = existing_cursor.last_created_at if existing_cursor is not None else activation_boundary
    already_inspected_ids_at_boundary: set = (
        set(existing_cursor.last_evidence_ids) if existing_cursor is not None else set()
    )

    offset = 0
    submitted = 0
    rows_scanned = 0
    running_created_at = scan_created_at_from if existing_cursor is not None else None
    running_ids: set = set(already_inspected_ids_at_boundary)
    any_inspected = False

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
                existing_cursor is not None
                and item.created_at == scan_created_at_from
                and item.evidence_id in already_inspected_ids_at_boundary
            ):
                continue
            rows_scanned += 1
            any_inspected = True
            if running_created_at is None or item.created_at > running_created_at:
                running_created_at = item.created_at
                running_ids = {item.evidence_id}
            else:
                running_ids.add(item.evidence_id)
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

    if any_inspected:
        # lap=0 always: this reconstruction predates the `lap` counter
        # entirely (see module docstring's own "Old (single-dimension,
        # no-lap)" section below) — a constant lap of 0 on every call
        # means `_resolve_cursor_advance` always takes its
        # "proposed.lap == existing.lap" branch, collapsing to exactly
        # the single-dimension `created_at` comparison this reconstructs.
        # `target` is a required field on the REAL, current
        # `advance_cursor` (this reconstruction calls the real,
        # production repository method — only the SCAN logic above is
        # reconstructed, not the cursor-write plumbing) — this
        # reconstruction predates `target` entirely too, so it proposes
        # `running_created_at` itself, a value never actually consulted:
        # `lap` is always 0 here, so the real `advance_cursor` always
        # takes its "proposed.lap == existing.lap" branch, which NEVER
        # changes an already-persisted target regardless of what is
        # proposed for it.
        cursor_repository.advance_cursor(
            cursor_key,
            lap=0,
            last_created_at=running_created_at,
            last_evidence_ids=running_ids,
            target=running_created_at,
        )
    return submitted


# ---------------------------------------------------------------------
# Old (pre-this-round) `advance_cursor` reconstruction — blind
# overwrite, no merge — used ONLY to prove the concurrent-writer
# regression/loss this round's merge-on-write fix prevents. This exact
# behaviour no longer exists in production code.
# ---------------------------------------------------------------------


def _old_overwriting_advance_cursor(
    engine, cursor_key: str, *, last_created_at: datetime.datetime, last_evidence_ids
) -> None:
    evidence_ids_list = sorted(last_evidence_ids)
    try:
        with session_scope(engine) as session:
            row = session.get(EvidenceClassificationReconciliationCursorRow, cursor_key, with_for_update=True)
            if row is not None:
                row.last_created_at = last_created_at
                row.last_evidence_ids = evidence_ids_list
                row.updated_at = utc_now()
                session.flush()
                return
            now = utc_now()
            session.add(
                EvidenceClassificationReconciliationCursorRow(
                    cursor_key=cursor_key,
                    lap=0,
                    last_created_at=last_created_at,
                    last_evidence_ids=evidence_ids_list,
                    # This reconstruction predates `target` entirely —
                    # a placeholder value is all a brand-new row needs
                    # (never meaningfully exercised by this
                    # reconstruction's own "blind overwrite" behaviour,
                    # which this function exists to reproduce on the
                    # `last_created_at`/`last_evidence_ids` dimensions
                    # only).
                    target=last_created_at,
                    created_at=now,
                    updated_at=now,
                )
            )
            session.flush()
    except IntegrityError:
        with session_scope(engine) as session:
            row = session.get(EvidenceClassificationReconciliationCursorRow, cursor_key, with_for_update=True)
            row.last_created_at = last_created_at
            row.last_evidence_ids = evidence_ids_list
            row.updated_at = utc_now()
            session.flush()


# ---------------------------------------------------------------------
# Old (single-dimension, no-`lap`) SWEEP reconstruction — this is the
# CURRENT, pre-this-round sweep mechanism, reconstructed exactly as it
# exists (existed) in this delivery's own previously-reviewed,
# uncommitted state, used ONLY to directly reproduce the permanent-
# stall bug an external audit found AND the architect independently
# reproduced: once one full sweep lap has completed and the backlog
# window exceeds `sweep_max_rows_scanned`, a newly-started lap's own
# first, budget-bounded write is rejected as "behind" the old lap's
# much-further-along persisted watermark — forever, because the
# rejection does not depend on anything that later changes (see
# `services.evidence.classification_job.reconcile_missing_classification_jobs`'s
# own corrected docstring, "The lap generation counter" section, for
# the full, now-honest account). This exact code path no longer exists
# in production: the real, fixed version threads a real `lap` value
# through `advance_cursor`; this reconstruction always proposes `lap=0`,
# which collapses `_resolve_cursor_advance` onto exactly the single,
# pre-fix `created_at`-only comparison — reproducing the bug precisely,
# via the SAME real, durable Postgres cursor table/repository (proving
# this is a genuine persistence-level defect, not a test artefact).
# ---------------------------------------------------------------------


def _old_no_lap_scan_and_repair_evidence_missing_classification_jobs(
    *,
    evidence_repository,
    classification_job_repository,
    classification_repository,
    cursor_repository,
    actor_type: str,
    actor_id: str,
    cursor_key: str,
    scan_created_at_from: datetime.datetime,
    created_at_to,
    already_inspected_ids_at_boundary: set,
    has_existing_cursor: bool,
    repair_limit: int,
    page_size: int,
    max_rows_scanned: int,
) -> int:
    offset = 0
    submitted = 0
    rows_scanned = 0
    running_created_at = scan_created_at_from if has_existing_cursor else None
    running_ids: set = set(already_inspected_ids_at_boundary)
    any_inspected = False

    while submitted < repair_limit and rows_scanned < max_rows_scanned:
        page = evidence_repository.list_evidence(
            created_at_from=scan_created_at_from,
            created_at_to=created_at_to,
            limit=page_size,
            offset=offset,
            order_by_created_at=True,
        )
        if not page:
            break
        for item in page:
            if submitted >= repair_limit or rows_scanned >= max_rows_scanned:
                break
            if (
                has_existing_cursor
                and item.created_at == scan_created_at_from
                and item.evidence_id in already_inspected_ids_at_boundary
            ):
                continue
            rows_scanned += 1
            any_inspected = True
            if running_created_at is None or item.created_at > running_created_at:
                running_created_at = item.created_at
                running_ids = {item.evidence_id}
            else:
                running_ids.add(item.evidence_id)
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

    if any_inspected:
        # THE BUG: lap=0 always — no second ordering dimension exists
        # in this reconstruction, so a newly-started lap's own partial
        # position is compared on created_at ALONE against whatever the
        # old, completed lap left persisted, and loses forever once
        # that old position is further ahead. `target` is likewise
        # proposed as a placeholder (`running_created_at`) — this
        # reconstruction predates `target` entirely, and since `lap` is
        # always 0 here, the real `advance_cursor`'s "proposed.lap ==
        # existing.lap" branch never lets a proposed target change
        # whatever is already persisted anyway.
        cursor_repository.advance_cursor(
            cursor_key,
            lap=0,
            last_created_at=running_created_at,
            last_evidence_ids=running_ids,
            target=running_created_at,
        )

    return submitted


def _old_no_lap_reconcile_missing_classification_jobs(
    *,
    evidence_repository,
    classification_job_repository,
    classification_repository,
    cursor_repository,
    actor_type: str,
    actor_id: str,
    activation_boundary: datetime.datetime,
    repair_limit: int = 200,
    page_size: int = 200,
    max_rows_scanned: int = 5000,
    sweep_repair_limit: int = 50,
    sweep_page_size: int = 200,
    sweep_max_rows_scanned: int = 1000,
) -> int:
    forward_cursor_key = activation_boundary.isoformat()
    existing_forward_cursor = cursor_repository.get_cursor(forward_cursor_key)
    forward_scan_from = (
        existing_forward_cursor.last_created_at if existing_forward_cursor is not None else activation_boundary
    )
    forward_already_inspected: set = (
        set(existing_forward_cursor.last_evidence_ids) if existing_forward_cursor is not None else set()
    )

    forward_submitted = _old_no_lap_scan_and_repair_evidence_missing_classification_jobs(
        evidence_repository=evidence_repository,
        classification_job_repository=classification_job_repository,
        classification_repository=classification_repository,
        cursor_repository=cursor_repository,
        actor_type=actor_type,
        actor_id=actor_id,
        cursor_key=forward_cursor_key,
        scan_created_at_from=forward_scan_from,
        created_at_to=None,
        already_inspected_ids_at_boundary=forward_already_inspected,
        has_existing_cursor=existing_forward_cursor is not None,
        repair_limit=repair_limit,
        page_size=page_size,
        max_rows_scanned=max_rows_scanned,
    )

    sweep_submitted = 0
    forward_frontier_cursor = cursor_repository.get_cursor(forward_cursor_key)
    forward_frontier = forward_frontier_cursor.last_created_at if forward_frontier_cursor is not None else None

    if forward_frontier is not None:
        sweep_cursor_key = f"{forward_cursor_key}::sweep"
        existing_sweep_cursor = cursor_repository.get_cursor(sweep_cursor_key)
        if existing_sweep_cursor is None or existing_sweep_cursor.last_created_at >= forward_frontier:
            sweep_scan_from = activation_boundary
            sweep_already_inspected: set = set()
            sweep_has_existing_cursor = False
        else:
            sweep_scan_from = existing_sweep_cursor.last_created_at
            sweep_already_inspected = set(existing_sweep_cursor.last_evidence_ids)
            sweep_has_existing_cursor = True

        sweep_submitted = _old_no_lap_scan_and_repair_evidence_missing_classification_jobs(
            evidence_repository=evidence_repository,
            classification_job_repository=classification_job_repository,
            classification_repository=classification_repository,
            cursor_repository=cursor_repository,
            actor_type=actor_type,
            actor_id=actor_id,
            cursor_key=sweep_cursor_key,
            scan_created_at_from=sweep_scan_from,
            created_at_to=forward_frontier,
            already_inspected_ids_at_boundary=sweep_already_inspected,
            has_existing_cursor=sweep_has_existing_cursor,
            repair_limit=sweep_repair_limit,
            page_size=sweep_page_size,
            max_rows_scanned=sweep_max_rows_scanned,
        )

    return forward_submitted + sweep_submitted


# ---------------------------------------------------------------------
# Old (pre-this-round), LAP-AWARE but UNFROZEN-target sweep
# reconstruction — this is the CURRENT, pre-this-round sweep mechanism
# EXACTLY as it existed (exists) in this delivery's own previously-
# reviewed, already-committed state (it DOES thread a real `lap` value
# through `advance_cursor`, closing the permanent-STALL bug the
# `test_old_no_lap_...` reconstruction above reproduces) — but still
# decides "has this lap finished" by comparing against a FRESHLY
# RE-READ `forward_frontier` on every call, and uses that same
# freshly-re-read value directly as the scan's own upper bound
# (`created_at_to`) for whichever lap is in progress, NEVER a frozen,
# persisted `target`. Used ONLY to directly reproduce the SECOND,
# independent liveness gap this round's own `target` correction closes:
# if the forward frontier keeps growing faster than the sweep's own
# bounded per-call budget, this lap's own completion condition
# (`last_created_at >= forward_frontier`) can never be satisfied — the
# lap never completes, a new lap never starts, and a late-committing
# row sorting behind the sweep's current in-lap position is never
# found. See `services.evidence.classification_reconciliation_cursor`'s
# own docstring, "target" section, and
# `reconcile_missing_classification_jobs`'s own corrected docstring,
# "Freezing each lap's own upper bound at target" section, for the full,
# now-honest account. This exact code path no longer exists in
# production: the real, fixed version threads a real, FROZEN `target`
# value through `advance_cursor`, reusing it unchanged across every
# continuing call within a lap; this reconstruction always proposes
# whatever `forward_frontier` happened to be at write time as a
# stand-in `target` value (needed only to satisfy the real, now-
# required `advance_cursor` parameter — never consulted by this
# reconstruction's own scan-bound/lap-completion decisions, which use
# `forward_frontier` directly instead, exactly reproducing the bug).
# ---------------------------------------------------------------------


def _old_lap_aware_no_target_scan_and_repair_evidence_missing_classification_jobs(
    *,
    evidence_repository,
    classification_job_repository,
    classification_repository,
    cursor_repository,
    actor_type: str,
    actor_id: str,
    cursor_key: str,
    scan_created_at_from: datetime.datetime,
    created_at_to,
    already_inspected_ids_at_boundary: set,
    has_existing_cursor: bool,
    repair_limit: int,
    page_size: int,
    max_rows_scanned: int,
    lap: int,
    stand_in_target: datetime.datetime,
) -> int:
    offset = 0
    submitted = 0
    rows_scanned = 0
    running_created_at = scan_created_at_from if has_existing_cursor else None
    running_ids: set = set(already_inspected_ids_at_boundary)
    any_inspected = False

    while submitted < repair_limit and rows_scanned < max_rows_scanned:
        page = evidence_repository.list_evidence(
            created_at_from=scan_created_at_from,
            created_at_to=created_at_to,
            limit=page_size,
            offset=offset,
            order_by_created_at=True,
        )
        if not page:
            break
        for item in page:
            if submitted >= repair_limit or rows_scanned >= max_rows_scanned:
                break
            if (
                has_existing_cursor
                and item.created_at == scan_created_at_from
                and item.evidence_id in already_inspected_ids_at_boundary
            ):
                continue
            rows_scanned += 1
            any_inspected = True
            if running_created_at is None or item.created_at > running_created_at:
                running_created_at = item.created_at
                running_ids = {item.evidence_id}
            else:
                running_ids.add(item.evidence_id)
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

    if any_inspected:
        # THE BUG (second, independent liveness gap): `target` is
        # proposed as a mere stand-in (`stand_in_target`, whatever
        # `forward_frontier` happened to be at write time) — this
        # reconstruction's own lap-completion/scan-bound decisions
        # (below, in the caller) never read a persisted target back at
        # all, they always re-read `forward_frontier` fresh instead.
        cursor_repository.advance_cursor(
            cursor_key, lap=lap, last_created_at=running_created_at, last_evidence_ids=running_ids,
            target=stand_in_target,
        )

    return submitted


def _old_lap_aware_no_target_reconcile_missing_classification_jobs(
    *,
    evidence_repository,
    classification_job_repository,
    classification_repository,
    cursor_repository,
    activation_boundary: datetime.datetime,
    actor_type: str = "SYSTEM",
    actor_id: str = ACTOR_ID,
    repair_limit: int = 200,
    page_size: int = 200,
    max_rows_scanned: int = 5000,
    sweep_repair_limit: int = 50,
    sweep_page_size: int = 200,
    sweep_max_rows_scanned: int = 1000,
) -> int:
    forward_cursor_key = activation_boundary.isoformat()
    existing_forward_cursor = cursor_repository.get_cursor(forward_cursor_key)
    forward_scan_from = (
        existing_forward_cursor.last_created_at if existing_forward_cursor is not None else activation_boundary
    )
    forward_already_inspected: set = (
        set(existing_forward_cursor.last_evidence_ids) if existing_forward_cursor is not None else set()
    )
    forward_lap = existing_forward_cursor.lap if existing_forward_cursor is not None else 0

    forward_submitted = _old_lap_aware_no_target_scan_and_repair_evidence_missing_classification_jobs(
        evidence_repository=evidence_repository,
        classification_job_repository=classification_job_repository,
        classification_repository=classification_repository,
        cursor_repository=cursor_repository,
        actor_type=actor_type,
        actor_id=actor_id,
        cursor_key=forward_cursor_key,
        scan_created_at_from=forward_scan_from,
        created_at_to=None,
        already_inspected_ids_at_boundary=forward_already_inspected,
        has_existing_cursor=existing_forward_cursor is not None,
        repair_limit=repair_limit,
        page_size=page_size,
        max_rows_scanned=max_rows_scanned,
        lap=forward_lap,
        stand_in_target=activation_boundary,
    )

    sweep_submitted = 0
    # THE BUG: re-read fresh every call and used DIRECTLY as both the
    # lap-completion comparison AND the scan's own upper bound — see
    # this helper's own module-level docstring above.
    forward_frontier_cursor = cursor_repository.get_cursor(forward_cursor_key)
    forward_frontier = forward_frontier_cursor.last_created_at if forward_frontier_cursor is not None else None

    if forward_frontier is not None:
        sweep_cursor_key = f"{forward_cursor_key}::sweep"
        existing_sweep_cursor = cursor_repository.get_cursor(sweep_cursor_key)
        if existing_sweep_cursor is None or existing_sweep_cursor.last_created_at >= forward_frontier:
            sweep_scan_from = activation_boundary
            sweep_already_inspected: set = set()
            sweep_has_existing_cursor = False
            sweep_lap = (existing_sweep_cursor.lap + 1) if existing_sweep_cursor is not None else 0
        else:
            sweep_scan_from = existing_sweep_cursor.last_created_at
            sweep_already_inspected = set(existing_sweep_cursor.last_evidence_ids)
            sweep_has_existing_cursor = True
            sweep_lap = existing_sweep_cursor.lap

        sweep_submitted = _old_lap_aware_no_target_scan_and_repair_evidence_missing_classification_jobs(
            evidence_repository=evidence_repository,
            classification_job_repository=classification_job_repository,
            classification_repository=classification_repository,
            cursor_repository=cursor_repository,
            actor_type=actor_type,
            actor_id=actor_id,
            cursor_key=sweep_cursor_key,
            scan_created_at_from=sweep_scan_from,
            created_at_to=forward_frontier,
            already_inspected_ids_at_boundary=sweep_already_inspected,
            has_existing_cursor=sweep_has_existing_cursor,
            repair_limit=sweep_repair_limit,
            page_size=sweep_page_size,
            max_rows_scanned=sweep_max_rows_scanned,
            lap=sweep_lap,
            stand_in_target=forward_frontier,
        )

    return forward_submitted + sweep_submitted


# =======================================================================
# Test plan item 1 — the architect's own exact 6-step reproduction, then
# the fix (the safety sweep) proven to close it.
# =======================================================================


def test_commit_order_inversion_reproduction_and_sweep_fix():
    boundary = utc_now()
    job_repo = PostgresEvidenceClassificationJobRepository()
    classification_repo = _classification_repository()
    cursor_repo = PostgresEvidenceClassificationReconciliationCursorRepository()
    evidence_repo = _evidence_repository()

    # Step 1: begin registering evidence A (earlier created_at == the
    # activation boundary itself), held open (not committed) on a
    # manually-managed connection/transaction.
    a_id, a_conn, a_txn = _begin_uncommitted_evidence_insert(created_at=boundary, received_at=boundary)

    try:
        # Step 2: register and commit evidence B (later created_at)
        # normally — a real, separate, immediately-committed call.
        b = _real_evidence(received_at=boundary)
        assert b.created_at > boundary, "B must genuinely register (and commit) after A's created_at value"

        # Step 3: run reconciliation while A's transaction is STILL
        # open/uncommitted — A must be entirely invisible to this scan.
        reconciled_while_a_uncommitted = reconcile_missing_classification_jobs(
            evidence_repository=evidence_repo,
            classification_job_repository=job_repo,
            classification_repository=classification_repo,
            cursor_repository=cursor_repo,
            actor_type="SYSTEM",
            actor_id=ACTOR_ID,
            activation_boundary=boundary,
        )
        assert reconciled_while_a_uncommitted == 1, "only B was visible to this scan"
        assert job_repo.get_by_evidence(b.evidence_id) is not None, "B got a job"
        assert job_repo.get_by_evidence(a_id) is None, "A is invisible — it cannot have a job yet"

        forward_cursor = cursor_repo.get_cursor(boundary.isoformat())
        assert forward_cursor is not None
        assert forward_cursor.last_created_at >= b.created_at, "the forward cursor advanced to (at least) B's position"

        # Step 4: commit A's transaction.
        a_txn.commit()
        a_conn.close()

        # Step 5: confirm A still has no job — simulating "the
        # immediate enqueue was never even attempted for A" (the
        # simplest, architect-specified way to reach this state: never
        # call enqueue_classification_job_for_evidence for A at all).
        assert job_repo.get_by_evidence(a_id) is None

        # --- First, prove the bug is real: the OLD, sweep-less
        # reconstruction, reusing the SAME already-advanced forward
        # cursor, NEVER finds A across repeated calls. ---
        for _ in range(5):  # bounded loop, never an infinite while True
            old_result = _old_forward_only_reconcile_missing_classification_jobs(
                evidence_repository=evidence_repo,
                classification_job_repository=job_repo,
                classification_repository=classification_repo,
                cursor_repository=cursor_repo,
                activation_boundary=boundary,
            )
            assert old_result == 0, "A's created_at is strictly before the forward cursor's watermark — excluded forever"
        assert job_repo.get_by_evidence(a_id) is None, (
            "confirmed: the OLD, sweep-less reconciliation permanently starves A — this is the "
            "architect-confirmed commit-order-inversion gap, reproduced directly"
        )

        # --- Step 6: now prove the REAL, current (sweep-enabled)
        # reconciliation eventually recovers A. ---
        found = False
        for _ in range(5):  # bounded loop, never an infinite while True
            reconcile_missing_classification_jobs(
                evidence_repository=evidence_repo,
                classification_job_repository=job_repo,
                classification_repository=classification_repo,
                cursor_repository=cursor_repo,
                actor_type="SYSTEM",
                actor_id=ACTOR_ID,
                activation_boundary=boundary,
            )
            if job_repo.get_by_evidence(a_id) is not None:
                found = True
                break
        assert found, "the safety sweep must eventually recover A, closing the commit-order-inversion gap"
        job = job_repo.get_by_evidence(a_id)
        assert job.status == "PENDING"
    finally:
        if not a_conn.closed:
            a_txn.rollback()
            a_conn.close()


# =======================================================================
# Test plan item 2 — repeated bounded reconciliation: the sweep makes
# real, monotonic lap progress across repeated calls, and correctly
# WRAPS to activation_boundary once it catches up to the forward
# frontier, without ever violating the activation boundary.
# =======================================================================


def test_sweep_makes_monotonic_lap_progress_and_wraps_correctly_once_caught_up():
    boundary = utc_now()
    total = 6
    evidence_items = [_real_evidence(received_at=boundary + datetime.timedelta(seconds=i)) for i in range(total)]
    evidence_ids = [item.evidence_id for item in evidence_items]
    job_repo = PostgresEvidenceClassificationJobRepository()
    cursor_repo = PostgresEvidenceClassificationReconciliationCursorRepository()
    sweep_key = _sweep_cursor_key(boundary)

    def _call(**overrides):
        defaults = dict(
            repair_limit=200, page_size=200, max_rows_scanned=5000,
            sweep_repair_limit=1, sweep_page_size=2, sweep_max_rows_scanned=2,
        )
        defaults.update(overrides)
        return _reconcile(cursor_repo=cursor_repo, job_repo=job_repo, activation_boundary=boundary, **defaults)

    # Call 1: forward pass resolves everything in one go (generous
    # forward budget); the sweep starts its very first lap with a tiny
    # budget (2 rows/call).
    _call()
    assert all(job_repo.get_by_evidence(eid) is not None for eid in evidence_ids)

    forward_frontier = cursor_repo.get_cursor(boundary.isoformat()).last_created_at
    first_sweep_cursor = cursor_repo.get_cursor(sweep_key)
    assert first_sweep_cursor is not None
    assert first_sweep_cursor.last_created_at >= boundary, "the sweep never starts before the activation boundary"

    # Repeated, bounded calls: the sweep cursor makes real, monotonic
    # progress (never backward) — mirrors the forward cursor's own
    # test_cursor_advances_and_a_subsequent_call_reaches_a_row_behind_max_rows_scanned
    # style proof.
    previous = first_sweep_cursor.last_created_at
    reached_frontier = False
    for _ in range(8):  # bounded loop, never an infinite while True
        _call()
        current = cursor_repo.get_cursor(sweep_key).last_created_at
        assert current >= previous, "the sweep cursor must never move backward within one lap"
        previous = current
        if current >= forward_frontier:
            reached_frontier = True
            break
    assert reached_frontier, "the sweep must complete its first lap within a bounded number of calls"
    assert cursor_repo.get_cursor(sweep_key).last_created_at >= boundary

    # --- Now prove it correctly WRAPS once caught up: a "late"
    # evidence row is injected with created_at strictly BETWEEN two
    # already-covered rows (inside the range the forward cursor has
    # already passed, and the sweep has already fully lapped) —
    # exactly what the sweep exists to catch. ---
    orphan_created_at = evidence_items[2].created_at + datetime.timedelta(microseconds=1)
    assert evidence_items[2].created_at < orphan_created_at < evidence_items[3].created_at
    orphan_id = identity.generate_id()
    _insert_evidence_row_directly(evidence_id=orphan_id, created_at=orphan_created_at, received_at=boundary)
    assert job_repo.get_by_evidence(orphan_id) is None

    # A generously-budgeted call lets the wrapped lap reach it in one
    # shot (proving discovery, not merely the read-derivation logic).
    _call(sweep_repair_limit=200, sweep_page_size=200, sweep_max_rows_scanned=1000)
    assert job_repo.get_by_evidence(orphan_id) is not None, "the wrapped lap must find the orphan"

    sweep_cursor_after_wrap = cursor_repo.get_cursor(sweep_key)
    assert sweep_cursor_after_wrap.last_created_at >= boundary, "the sweep never violates the activation boundary"


# =======================================================================
# Test plan item 3 — two concurrent cursor writers, real threads, real
# Postgres.
# =======================================================================


@pytest.mark.usefixtures("fresh_engine")
def test_advance_cursor_never_regresses_a_more_advanced_persisted_cursor_real_threads(fresh_engine):
    """(a) `advance_cursor` never regresses a more-advanced persisted
    cursor when a slower/stale-started caller's proposed watermark is
    behind it — real threads, real Postgres.

    First proves the OLD, unconditional-overwrite reconstruction
    genuinely regresses (whichever caller's write lands LAST wins,
    even if its own proposal represents LESS progress) — then proves
    the NEW, merge-protected repository never does, under a
    real-thread race with the same two proposals."""
    engine = fresh_engine
    t_low = datetime.datetime.now(datetime.timezone.utc)
    t_high = t_low + datetime.timedelta(seconds=5)

    # --- First, the OLD behaviour: whichever write lands last wins,
    # even if it represents LESS progress than what was already there. ---
    old_key = f"old-overwrite-{identity.generate_id()}"
    _old_overwriting_advance_cursor(engine, old_key, last_created_at=t_high, last_evidence_ids=["fast-writer"])
    _old_overwriting_advance_cursor(engine, old_key, last_created_at=t_low, last_evidence_ids=["slow-writer"])
    with session_scope(engine) as session:
        stale_row = session.get(EvidenceClassificationReconciliationCursorRow, old_key)
    assert stale_row.last_created_at == t_low, (
        "confirmed: the OLD, unconditional-overwrite advance_cursor genuinely regresses the cursor "
        "backward when a slower/stale write lands after a faster one"
    )

    # --- Now, the NEW, merge-protected repository, under a genuine,
    # real-thread race with the SAME two proposals. ---
    repo = PostgresEvidenceClassificationReconciliationCursorRepository(engine)
    new_key = f"new-merge-{identity.generate_id()}"
    barrier = threading.Barrier(2)
    errors: list = []

    def _fast_writer():
        try:
            barrier.wait(timeout=10)
            repo.advance_cursor(new_key, lap=0, last_created_at=t_high, last_evidence_ids=["fast-writer"], target=t_high)
        except Exception as exc:  # noqa: BLE001 - captured, never swallowed
            errors.append(exc)

    def _slow_writer():
        try:
            barrier.wait(timeout=10)
            # A small, deliberate delay so the "slow/stale-started"
            # caller's own write attempt reliably lands AFTER the fast
            # writer's — without this, the race is genuine but the
            # assertion below would be flaky (either order is a valid
            # race outcome; we want to specifically exercise "the
            # slower call's own proposal arrives after a faster one
            # already committed a higher watermark").
            time.sleep(0.2)
            repo.advance_cursor(new_key, lap=0, last_created_at=t_low, last_evidence_ids=["slow-writer"], target=t_low)
        except Exception as exc:  # noqa: BLE001 - captured, never swallowed
            errors.append(exc)

    threads = [threading.Thread(target=_fast_writer), threading.Thread(target=_slow_writer)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=15)
        assert not t.is_alive(), "a thread deadlocked/timed out"
    assert not errors, f"advance_cursor must never raise under a genuine race: {errors}"

    final = repo.get_cursor(new_key)
    assert final is not None
    assert final.last_created_at == t_high, (
        "the slower/stale-started caller's lower proposal must NEVER regress the cursor — unlike the OLD "
        "behaviour proven above against the identical pair of proposals"
    )
    assert "fast-writer" in final.last_evidence_ids
    assert final.target == t_high, (
        "whichever writer's proposal actually established the row first fixes target permanently — the "
        "slower writer's own, different proposed target is discarded"
    )


@pytest.mark.usefixtures("fresh_engine")
def test_advance_cursor_unions_evidence_ids_when_concurrent_proposals_share_the_same_watermark(fresh_engine):
    """(b) the persisted `last_evidence_ids` set, after both concurrent
    calls complete, reflects the correct UNION (nothing lost) when both
    callers' proposed watermarks land on the exact same
    `last_created_at` — real threads, real Postgres."""
    engine = fresh_engine
    repo = PostgresEvidenceClassificationReconciliationCursorRepository(engine)
    key = f"union-{identity.generate_id()}"
    t_shared = datetime.datetime.now(datetime.timezone.utc)
    barrier = threading.Barrier(2)
    errors: list = []

    def _writer(evidence_id: str):
        try:
            barrier.wait(timeout=10)
            repo.advance_cursor(key, lap=0, last_created_at=t_shared, last_evidence_ids=[evidence_id], target=t_shared)
        except Exception as exc:  # noqa: BLE001 - captured, never swallowed
            errors.append(exc)

    threads = [
        threading.Thread(target=_writer, args=("evidence-one",)),
        threading.Thread(target=_writer, args=("evidence-two",)),
    ]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=15)
        assert not t.is_alive()
    assert not errors, f"advance_cursor must never raise under a genuine race: {errors}"

    final = repo.get_cursor(key)
    assert final is not None
    assert final.last_created_at == t_shared
    assert set(final.last_evidence_ids) == {"evidence-one", "evidence-two"}, (
        "both concurrent callers' ids sharing the exact same watermark must be UNIONED — never replaced, "
        "never dropped"
    )


def test_cursor_regression_would_have_permanently_starved_new_evidence_under_old_advance_cursor():
    """(c) no evidence becomes permanently unreachable as a result of a
    concurrent cursor-write race (a harmless repeat scan is fine;
    permanent omission is not) — constructs a scenario where a
    regressed cursor, combined with a bounded per-call budget, would
    have caused a genuinely NEW, later-registered orphan to be starved
    under the OLD, unconditional-overwrite `advance_cursor` (every
    subsequent bounded call wastes its entire budget re-verifying the
    redundant, already-resolved region the regression reopened, instead
    of reaching the genuinely new evidence past it) — and proves the
    real, merge-protected repository never lets this happen, because a
    stale/slower write can never regress the cursor in the first place
    (see the real-thread proof above)."""
    boundary = utc_now()
    job_repo = PostgresEvidenceClassificationJobRepository()
    classification_repo = _classification_repository()
    evidence_repo = _evidence_repository()

    # A backlog of 6 already-resolved evidence rows, forward-scanned and
    # fully resolved in one generous-budget call.
    backlog = [_real_evidence(received_at=boundary + datetime.timedelta(seconds=i)) for i in range(6)]
    real_cursor_repo = PostgresEvidenceClassificationReconciliationCursorRepository()
    _reconcile(cursor_repo=real_cursor_repo, job_repo=job_repo, activation_boundary=boundary)
    assert all(job_repo.get_by_evidence(item.evidence_id) is not None for item in backlog)
    advanced_frontier = real_cursor_repo.get_cursor(boundary.isoformat()).last_created_at
    assert advanced_frontier == backlog[-1].created_at

    # --- Simulate the OLD code's own regression: a stale caller (whose
    # own scan started from an EARLIER position, e.g. backlog[2], and
    # only got as far as backlog[3] before writing) races in AFTER the
    # real, fully-advanced call above and — under the OLD,
    # unconditional-overwrite semantics — clobbers the cursor backward. ---
    old_style_cursor_repo = PostgresEvidenceClassificationReconciliationCursorRepository()
    # Reproduce the exact persisted state the real call above left
    # behind, then simulate the stale overwrite directly against the
    # underlying table (this is what a real race would have produced
    # under the OLD code — see the real-thread proof above for the
    # genuine-race version of this exact mechanism).
    _old_overwriting_advance_cursor(
        get_engine(), boundary.isoformat(),
        last_created_at=backlog[3].created_at, last_evidence_ids=[backlog[3].evidence_id],
    )

    # A brand-new, genuinely NEW evidence item registers AFTER all of
    # this — the real-world case this whole mechanism protects.
    new_orphan = _real_evidence(received_at=boundary + datetime.timedelta(seconds=100))

    # Under the regressed cursor, a bounded subsequent call wastes its
    # ENTIRE budget re-verifying the now-redundant [backlog[3],
    # advanced_frontier] region (already-resolved rows — cheap skip
    # checks, but each one still consumes rows_scanned) and NEVER
    # reaches new_orphan, which sits past the TRUE frontier.
    tiny_budget_result = reconcile_missing_classification_jobs(
        evidence_repository=evidence_repo,
        classification_job_repository=job_repo,
        classification_repository=classification_repo,
        cursor_repository=old_style_cursor_repo,
        actor_type="SYSTEM",
        actor_id=ACTOR_ID,
        activation_boundary=boundary,
        repair_limit=1, page_size=1, max_rows_scanned=2,
        sweep_repair_limit=0, sweep_page_size=1, sweep_max_rows_scanned=0,
    )
    assert tiny_budget_result == 0
    assert job_repo.get_by_evidence(new_orphan.evidence_id) is None, (
        "confirmed: under the regressed cursor state the OLD code could produce, a small, realistic "
        "per-call budget is entirely consumed re-verifying already-resolved evidence, permanently "
        "postponing genuinely new evidence for as many calls as the redundant region takes to re-cross"
    )

    # --- Now the real, merge-protected repository: an equivalent
    # "stale caller" write attempt (same proposal) NEVER regresses the
    # real cursor in the first place (proven directly by the real-
    # thread tests above) — so the SAME tiny per-call budget reaches
    # new_orphan without first needing to re-cross any redundant
    # ground. ---
    real_cursor_repo.advance_cursor(
        boundary.isoformat(),
        lap=0,
        last_created_at=backlog[3].created_at,
        last_evidence_ids=[backlog[3].evidence_id],
        target=backlog[3].created_at,
    )
    assert real_cursor_repo.get_cursor(boundary.isoformat()).last_created_at == advanced_frontier, (
        "the merge-protected repository must reject the stale proposal outright — the cursor never moved"
    )

    tiny_budget_result_fixed = reconcile_missing_classification_jobs(
        evidence_repository=evidence_repo,
        classification_job_repository=job_repo,
        classification_repository=classification_repo,
        cursor_repository=real_cursor_repo,
        actor_type="SYSTEM",
        actor_id=ACTOR_ID,
        activation_boundary=boundary,
        repair_limit=1, page_size=1, max_rows_scanned=2,
        sweep_repair_limit=0, sweep_page_size=1, sweep_max_rows_scanned=0,
    )
    assert tiny_budget_result_fixed == 1
    assert job_repo.get_by_evidence(new_orphan.evidence_id) is not None, (
        "with the cursor never regressed, the identical tiny per-call budget reaches the genuinely new "
        "evidence immediately — no starvation"
    )


# =======================================================================
# Test plan item 4 — the sweep never scans below activation_boundary,
# across many laps.
# =======================================================================


def test_sweep_never_scans_below_activation_boundary_across_many_laps():
    historical_id = _real_evidence_id(received_at=utc_now() - datetime.timedelta(days=1))
    boundary = utc_now()
    prospective_ids = [
        _real_evidence_id(received_at=boundary + datetime.timedelta(seconds=i)) for i in range(4)
    ]
    job_repo = PostgresEvidenceClassificationJobRepository()
    cursor_repo = PostgresEvidenceClassificationReconciliationCursorRepository()

    for _ in range(10):  # bounded loop; a tiny sweep budget forces many laps/wraps
        _reconcile(
            cursor_repo=cursor_repo, job_repo=job_repo, activation_boundary=boundary,
            repair_limit=200, page_size=200, max_rows_scanned=5000,
            sweep_repair_limit=1, sweep_page_size=1, sweep_max_rows_scanned=1,
        )
        assert job_repo.get_by_evidence(historical_id) is None, "the sweep must never reach pre-boundary evidence"
        sweep_cursor = cursor_repo.get_cursor(_sweep_cursor_key(boundary))
        if sweep_cursor is not None:
            assert sweep_cursor.last_created_at >= boundary, "the sweep cursor must never sit before the boundary"

    assert all(job_repo.get_by_evidence(eid) is not None for eid in prospective_ids)
    assert job_repo.get_by_evidence(historical_id) is None


# =======================================================================
# Test plan item 5 — unchanged ordinary idempotency: normal enqueue +
# forward reconciliation + sweep reconciliation, all running together,
# still produce exactly one job per evidence item.
# =======================================================================


def test_normal_enqueue_plus_forward_plus_sweep_together_produce_exactly_one_job_per_evidence():
    boundary = utc_now()
    job_repo = PostgresEvidenceClassificationJobRepository()
    cursor_repo = PostgresEvidenceClassificationReconciliationCursorRepository()
    evidence_items = [_real_evidence(received_at=boundary + datetime.timedelta(seconds=i)) for i in range(5)]

    # The normal, immediate-enqueue path — exactly as real ingestion
    # call sites do.
    for item in evidence_items:
        enqueue_classification_job_for_evidence(
            item.evidence_id, classification_job_repository=job_repo, actor_type="SYSTEM", actor_id=ACTOR_ID,
            evidence_created_at=item.created_at, activation_boundary=boundary,
        )

    # Forward + sweep, run together, several times — the common,
    # happy-path case now running with the sweep active on every call.
    for _ in range(3):  # bounded loop
        _reconcile(cursor_repo=cursor_repo, job_repo=job_repo, activation_boundary=boundary)

    with get_engine().connect() as conn:
        count = conn.execute(
            sa.select(sa.func.count())
            .select_from(EvidenceClassificationJobRow)
            .where(EvidenceClassificationJobRow.evidence_id.in_([item.evidence_id for item in evidence_items]))
        ).scalar_one()
    assert count == len(evidence_items), "exactly one job row per evidence item — no duplicates from the new sweep pass"

    for item in evidence_items:
        job = job_repo.get_by_evidence(item.evidence_id)
        assert job is not None
        assert job.status == "PENDING"


# =======================================================================
# THIS ROUND's own correction: the `lap` generation counter, closing a
# genuine, independently-confirmed (external audit + direct
# reproduction) permanent-stall defect in the sweep cursor mechanism
# above. See `services.evidence.classification_reconciliation_cursor`'s
# own docstring, "lap" section, and `reconcile_missing_classification_jobs`'s
# own corrected docstring, "The lap generation counter" section, for the
# full mechanism.
# =======================================================================


def _bulk_insert_resolved_evidence_and_jobs(
    *, boundary: datetime.datetime, count: int, start_index: int = 0
) -> list[tuple[str, datetime.datetime]]:
    """Bulk-inserts `count` EvidenceItem rows (`created_at = boundary +
    (start_index + i)` milliseconds, `i in range(count)`), each PAIRED
    with an already-`SUCCEEDED` EvidenceClassificationJob row, via two
    single-round-trip Core `INSERT`s — the per-row
    `_real_evidence`/`submit_job` service-call helpers this module's
    other tests use are far too slow at the thousands-of-rows scale a
    genuine `sweep_max_rows_scanned`-exceeding reproduction requires.
    Returns `[(evidence_id, created_at), ...]` in creation order. Every
    row is already "resolved" (has a job) so a scan pass over it does
    zero real classification work per row — a cheap `get_by_evidence`
    lookup only — exactly the "backlog of already-resolved evidence"
    the architect's own reproduction describes.
    """
    source = PostgresSourceRepository().register_source(
        source_type="MANUAL_UPLOAD", provider="INTERNAL", status="ACTIVE"
    )
    now = utc_now()
    evidence_rows = []
    job_rows = []
    result: list[tuple[str, datetime.datetime]] = []
    for i in range(count):
        evidence_id = identity.generate_id()
        created_at = boundary + datetime.timedelta(milliseconds=start_index + i)
        result.append((evidence_id, created_at))
        evidence_rows.append(
            dict(
                evidence_id=evidence_id,
                entity_id=None,
                evidence_type="DOCUMENT",
                source_id=source.source_id,
                observed_at=created_at,
                received_at=created_at,
                content_hash={
                    "algorithm": "SHA-256",
                    "value": hashlib.sha256(evidence_id.encode("utf-8")).hexdigest(),
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
        job_rows.append(
            dict(
                job_id=identity.generate_id(),
                evidence_id=evidence_id,
                status="SUCCEEDED",
                actor_type="SYSTEM",
                actor_id=ACTOR_ID,
                correlation_id=identity.generate_id(),
                created_at=now,
                updated_at=now,
                attempt_count=1,
                max_attempts=3,
                claimed_by=None,
                claimed_at=None,
                last_error=None,
                classification_outcome="DETERMINISTIC_CLASSIFIED",
            )
        )
    with session_scope(get_engine()) as session:
        session.execute(sa.insert(EvidenceItemRow), evidence_rows)
        session.execute(sa.insert(EvidenceClassificationJobRow), job_rows)
    return result


def test_old_no_lap_sweep_permanently_stalls_after_a_completed_lap_and_the_real_lap_aware_fix_recovers():
    """Direct reproduction, at REALISTIC scale, of the architect-
    confirmed permanent-stall defect in the CURRENT (pre-this-round),
    single-dimension sweep cursor mechanism reconstructed above —
    mirrors the architect's own independent reproduction exactly: seed
    a backlog whose total window genuinely exceeds
    `sweep_max_rows_scanned` (1000, production default), let the sweep
    complete ONE FULL LAP over it (establishing a completed-lap
    watermark), insert a genuine orphan (no job at all) beyond position
    `sweep_max_rows_scanned` from `activation_boundary`, and prove:

    1. The OLD, single-dimension reconstruction NEVER finds the orphan
       across several repeated calls, and its sweep cursor's own
       watermark never moves from the completed-lap value — the
       permanent stall, reproduced directly.
    2. The REAL, lap-aware `reconcile_missing_classification_jobs`,
       resuming from the EXACT SAME persisted (stuck) cursor row this
       reconstruction left behind, DOES find the orphan within a small,
       bounded number of calls — a genuine persistence-level fix, not
       merely a fresh-state proof.
    """
    boundary = utc_now()
    job_repo = PostgresEvidenceClassificationJobRepository()
    classification_repo = _classification_repository()
    cursor_repo = PostgresEvidenceClassificationReconciliationCursorRepository()
    evidence_repo = _evidence_repository()
    sweep_key = _sweep_cursor_key(boundary)

    def _old_call(**overrides):
        defaults = dict(
            evidence_repository=evidence_repo,
            classification_job_repository=job_repo,
            classification_repository=classification_repo,
            cursor_repository=cursor_repo,
            actor_type="SYSTEM",
            actor_id=ACTOR_ID,
            activation_boundary=boundary,
        )
        defaults.update(overrides)
        return _old_no_lap_reconcile_missing_classification_jobs(**defaults)

    # --- Seed 1500 already-resolved rows (backlog > sweep_max_rows_scanned),
    # then resolve/advance the FORWARD cursor over the whole thing in one
    # generously-budgeted call. ---
    backlog = _bulk_insert_resolved_evidence_and_jobs(boundary=boundary, count=1500)
    _old_call(repair_limit=2000, page_size=500, max_rows_scanned=5000)
    forward_frontier = cursor_repo.get_cursor(boundary.isoformat()).last_created_at
    assert forward_frontier == backlog[-1][1]

    # --- Let the sweep complete ONE FULL LAP over the backlog, using
    # PRODUCTION-DEFAULT sweep budgets throughout (no override) — more
    # than one call is required since 1500 > sweep_max_rows_scanned
    # (1000). ---
    lap_completed = False
    for _ in range(6):  # bounded loop
        _old_call()
        sweep_cursor = cursor_repo.get_cursor(sweep_key)
        if sweep_cursor is not None and sweep_cursor.last_created_at >= forward_frontier:
            lap_completed = True
            break
    assert lap_completed, "the sweep must complete its first lap over the backlog within a bounded number of calls"
    completed_lap_watermark = cursor_repo.get_cursor(sweep_key).last_created_at
    assert completed_lap_watermark == forward_frontier

    # --- Insert a genuine orphan — no job, no classification — INSIDE
    # the already-swept window, beyond position sweep_max_rows_scanned
    # (1000) from activation_boundary (the architect's own "position
    # ~1200" reproduction). No new evidence arrives after this — the
    # forward frontier stays exactly where it is. ---
    orphan_created_at = boundary + datetime.timedelta(milliseconds=1200, microseconds=500)
    orphan_id = identity.generate_id()
    _insert_evidence_row_directly(evidence_id=orphan_id, created_at=orphan_created_at, received_at=boundary)
    assert job_repo.get_by_evidence(orphan_id) is None

    # --- Prove the bug is real: calling the OLD, single-dimension
    # reconstruction again triggers a NEW lap (the sweep already caught
    # up to forward_frontier), but that new lap's own first,
    # budget-bounded call (1000 rows, starting back at
    # activation_boundary) can only reach position ~999ms — strictly
    # LESS than the completed lap's persisted ~1499ms watermark — and is
    # therefore REJECTED as a regression. This repeats, identically,
    # forever: the orphan (beyond position 1000) is never even reached
    # by the scan, and the sweep cursor's own watermark never moves. ---
    stale_watermark_before = cursor_repo.get_cursor(sweep_key).last_created_at
    for _ in range(7):  # bounded loop, mirrors the architect's own 7-call reproduction
        _old_call()
        assert job_repo.get_by_evidence(orphan_id) is None, (
            "confirmed: the OLD, single-dimension sweep never even reaches the orphan — every restart "
            "attempt re-scans the exact same first-1000-rows window and is rejected"
        )
        assert cursor_repo.get_cursor(sweep_key).last_created_at == stale_watermark_before, (
            "confirmed: the OLD, single-dimension sweep cursor's own watermark never moves once a new "
            "lap's first, budget-bounded write is rejected as 'behind' the old lap's completed position "
            "— this is the permanent-stall bug, reproduced directly"
        )

    # --- Now prove the REAL, lap-aware fix recovers it — resuming from
    # the EXACT SAME persisted (stuck) cursor row left behind above,
    # using PRODUCTION-DEFAULT budgets (no override), within a small,
    # bounded number of calls. ---
    found = False
    for _ in range(5):  # bounded loop
        reconcile_missing_classification_jobs(
            evidence_repository=evidence_repo,
            classification_job_repository=job_repo,
            classification_repository=classification_repo,
            cursor_repository=cursor_repo,
            actor_type="SYSTEM",
            actor_id=ACTOR_ID,
            activation_boundary=boundary,
        )
        if job_repo.get_by_evidence(orphan_id) is not None:
            found = True
            break
    assert found, (
        "the REAL, lap-aware reconcile_missing_classification_jobs must recover the orphan the OLD "
        "mechanism permanently starved — the actual fix, proven against the exact stuck persisted state"
    )
    recovered_job = job_repo.get_by_evidence(orphan_id)
    assert recovered_job.status == "PENDING"

    final_sweep_cursor = cursor_repo.get_cursor(sweep_key)
    assert final_sweep_cursor.lap >= 1, "the real fix must have started (and progressed into) a new lap generation"


def test_sweep_requires_and_completes_within_multiple_calls_at_realistic_scale_with_production_defaults():
    """Realistic-scale counterpart to
    `test_sweep_makes_monotonic_lap_progress_and_wraps_correctly_once_caught_up`,
    whose own 6-row corpus (even with an artificially-widened budget)
    was specifically called out by the audit as unable to ever exercise
    a backlog that genuinely EXCEEDS `sweep_max_rows_scanned` in a
    single call. Uses PRODUCTION-DEFAULT sweep budgets throughout
    (`sweep_repair_limit=50`, `sweep_page_size=200`,
    `sweep_max_rows_scanned=1000` — never overridden) over a 1100-row
    backlog, and proves multiple bounded calls are both NEEDED (a
    single call cannot complete the lap) and SUFFICIENT (a further
    small, bounded number of calls completes it and finds a late
    orphan within it)."""
    boundary = utc_now()
    job_repo = PostgresEvidenceClassificationJobRepository()
    cursor_repo = PostgresEvidenceClassificationReconciliationCursorRepository()
    evidence_repo = _evidence_repository()
    classification_repo = _classification_repository()
    sweep_key = _sweep_cursor_key(boundary)

    total = 1100
    rows = _bulk_insert_resolved_evidence_and_jobs(boundary=boundary, count=total)

    # Forward pass: a generous override so the (already-resolved)
    # backlog is fully traversed and the forward cursor reaches the
    # true frontier in one call. Sweep budgets are left at PRODUCTION
    # DEFAULTS — no sweep_* override anywhere in this test.
    reconcile_missing_classification_jobs(
        evidence_repository=evidence_repo,
        classification_job_repository=job_repo,
        classification_repository=classification_repo,
        cursor_repository=cursor_repo,
        actor_type="SYSTEM",
        actor_id=ACTOR_ID,
        activation_boundary=boundary,
        repair_limit=total + 10,
        page_size=500,
        max_rows_scanned=total + 100,
    )
    forward_frontier = cursor_repo.get_cursor(boundary.isoformat()).last_created_at
    assert forward_frontier == rows[-1][1]

    sweep_after_call_1 = cursor_repo.get_cursor(sweep_key)
    assert sweep_after_call_1 is not None
    assert sweep_after_call_1.last_created_at < forward_frontier, (
        "a SINGLE call, at PRODUCTION-DEFAULT sweep budgets, must NOT be able to complete a lap over a "
        "backlog genuinely larger than sweep_max_rows_scanned=1000 — proves multiple calls are NEEDED"
    )

    # A genuine orphan beyond the first call's own reach (position
    # ~1050, past sweep_max_rows_scanned=1000 from activation_boundary).
    orphan_created_at = boundary + datetime.timedelta(milliseconds=1050, microseconds=500)
    orphan_id = identity.generate_id()
    _insert_evidence_row_directly(evidence_id=orphan_id, created_at=orphan_created_at, received_at=boundary)

    lap_completed = False
    for _ in range(5):  # bounded loop — proves multiple calls are SUFFICIENT
        reconcile_missing_classification_jobs(
            evidence_repository=evidence_repo,
            classification_job_repository=job_repo,
            classification_repository=classification_repo,
            cursor_repository=cursor_repo,
            actor_type="SYSTEM",
            actor_id=ACTOR_ID,
            activation_boundary=boundary,
        )
        current = cursor_repo.get_cursor(sweep_key).last_created_at
        if current >= forward_frontier:
            lap_completed = True
            break
    assert lap_completed, "the sweep must complete its lap within a small, bounded number of further calls"
    assert job_repo.get_by_evidence(orphan_id) is not None, "the orphan beyond the first call's own reach must be found"


@pytest.mark.usefixtures("fresh_engine")
def test_lap_transition_concurrent_writers_union_evidence_ids_at_the_same_new_lap_watermark(fresh_engine):
    """Two real threads/real Postgres callers BOTH independently decide
    'time to start lap N+1' for the SAME sweep `cursor_key` at the same
    time (both having read the same old `lap=N` persisted cursor) and
    both happen to reach the exact SAME new watermark — proves neither
    caller's legitimate progress is lost: whichever write lands first
    sets `lap=N+1` at that position (the 'proposed.lap > existing.lap'
    branch, unconditional acceptance); the SECOND writer's own proposal
    (also `lap=N+1`, from its own independent scan) then finds
    `proposed.lap == existing.lap` and merges via the ORDINARY,
    already-safe `created_at`/`last_evidence_ids` UNION — no
    special-casing needed, exactly as
    `services.evidence.classification_reconciliation_cursor`'s own
    docstring, 'lap' section, documents, and exactly as
    `test_advance_cursor_unions_evidence_ids_when_concurrent_proposals_share_the_same_watermark`
    already proves for the no-lap-transition case."""
    engine = fresh_engine
    repo = PostgresEvidenceClassificationReconciliationCursorRepository(engine)
    key = f"lap-transition-union-{identity.generate_id()}"

    t_old_lap = datetime.datetime.now(datetime.timezone.utc)
    repo.advance_cursor(key, lap=0, last_created_at=t_old_lap, last_evidence_ids=["old-lap-final"], target=t_old_lap)

    # Both callers' own independent new-lap scans land on the exact SAME
    # new watermark — far EARLIER than the old lap's final position
    # (exactly the "a new lap's first call cannot yet reach the old
    # lap's position" shape the whole fix exists for). Both ALSO
    # snapshot the exact SAME new target (a realistic case: both read
    # the same forward frontier at nearly the same moment) — the
    # dedicated "different target" variant lives in
    # `test_lap_transition_concurrent_writers_propose_different_targets_the_first_to_land_wins`
    # below.
    t_new_lap_shared = t_old_lap - datetime.timedelta(hours=1)
    t_new_lap_target = t_old_lap + datetime.timedelta(hours=1)
    barrier = threading.Barrier(2)
    errors: list = []

    def _writer(evidence_id: str):
        try:
            barrier.wait(timeout=10)
            repo.advance_cursor(
                key, lap=1, last_created_at=t_new_lap_shared, last_evidence_ids=[evidence_id], target=t_new_lap_target
            )
        except Exception as exc:  # noqa: BLE001 - captured, never swallowed
            errors.append(exc)

    threads = [
        threading.Thread(target=_writer, args=("writer-one",)),
        threading.Thread(target=_writer, args=("writer-two",)),
    ]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=15)
        assert not t.is_alive(), "a thread deadlocked/timed out"
    assert not errors, f"advance_cursor must never raise under a genuine lap-transition race: {errors}"

    final = repo.get_cursor(key)
    assert final is not None
    assert final.lap == 1, "the new lap must be accepted even though BOTH writers proposed the SAME new generation"
    assert final.last_created_at == t_new_lap_shared
    assert set(final.last_evidence_ids) == {"writer-one", "writer-two"}, (
        "both concurrent lap-transition callers' ids sharing the exact same new-lap watermark must be "
        "UNIONED — never replaced, never dropped"
    )
    assert final.target == t_new_lap_target


@pytest.mark.usefixtures("fresh_engine")
def test_lap_transition_concurrent_writers_at_different_positions_the_later_one_advances_within_the_new_lap(
    fresh_engine,
):
    """A variant where the two concurrent 'start lap N+1' callers' own
    independent scans reach genuinely DIFFERENT positions — proves the
    SECOND writer's own `lap=N+1` proposal, landing after the first,
    correctly ADVANCES past the first writer's partial position (an
    ordinary 'equal lap, greater created_at' merge — replaces, exactly
    as the single-lap case already does), rather than being wrongly
    rejected as though it were racing against the OLD lap."""
    engine = fresh_engine
    repo = PostgresEvidenceClassificationReconciliationCursorRepository(engine)
    key = f"lap-transition-diff-{identity.generate_id()}"

    t_old_lap = datetime.datetime.now(datetime.timezone.utc)
    repo.advance_cursor(key, lap=0, last_created_at=t_old_lap, last_evidence_ids=["old-lap-final"], target=t_old_lap)

    t_first = t_old_lap - datetime.timedelta(hours=2)
    t_second = t_old_lap - datetime.timedelta(hours=1)  # further along than t_first, still far behind t_old_lap
    # Both writers propose the SAME target here too (this test's own
    # focus is the created_at/last_evidence_ids dimension, already
    # covered) — the DIFFERENT-target race is its own dedicated test,
    # directly below.
    t_shared_target = t_old_lap + datetime.timedelta(hours=1)
    barrier = threading.Barrier(2)
    errors: list = []

    def _first_writer():
        try:
            barrier.wait(timeout=10)
            repo.advance_cursor(
                key, lap=1, last_created_at=t_first, last_evidence_ids=["first-writer"], target=t_shared_target
            )
        except Exception as exc:  # noqa: BLE001 - captured, never swallowed
            errors.append(exc)

    def _second_writer():
        try:
            barrier.wait(timeout=10)
            # A small, deliberate delay so this write reliably lands
            # AFTER the first writer's — mirrors the existing
            # fast/slow-writer real-thread proof's own discipline.
            time.sleep(0.2)
            repo.advance_cursor(
                key, lap=1, last_created_at=t_second, last_evidence_ids=["second-writer"], target=t_shared_target
            )
        except Exception as exc:  # noqa: BLE001 - captured, never swallowed
            errors.append(exc)

    threads = [threading.Thread(target=_first_writer), threading.Thread(target=_second_writer)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=15)
        assert not t.is_alive(), "a thread deadlocked/timed out"
    assert not errors, f"advance_cursor must never raise under a genuine lap-transition race: {errors}"

    final = repo.get_cursor(key)
    assert final is not None
    assert final.lap == 1
    assert final.last_created_at == t_second, (
        "the second writer's own lap=1 proposal, landing after the first, must ADVANCE past the first "
        "writer's partial position — an ordinary same-lap merge, never rejected as though racing the OLD lap"
    )
    assert final.last_evidence_ids == ("second-writer",), (
        "a strictly greater created_at within the SAME lap replaces last_evidence_ids wholesale — mirrors "
        "the ordinary same-lap 'greater' semantics exactly, unchanged"
    )
    assert final.target == t_shared_target, "target is unaffected by the created_at/ids dimension racing above"


# =======================================================================
# THIS ROUND's own final correction: `target` — freezing each lap's own
# upper bound at the moment it starts, closing the SECOND, independent
# liveness gap `lap` alone did not close (a continuing lap's own scan
# bound used to be re-read fresh from the forward cursor on every call,
# so a continuously-advancing frontier could keep the lap from EVER
# completing). See `services.evidence.classification_reconciliation_cursor`'s
# own docstring, "target" section, and
# `reconcile_missing_classification_jobs`'s own corrected docstring,
# "Freezing each lap's own upper bound at target" section, for the full
# mechanism.
# =======================================================================


@pytest.mark.usefixtures("fresh_engine")
def test_lap_transition_concurrent_writers_propose_different_targets_the_first_to_land_wins(fresh_engine):
    """The architect's own exact "concurrent-writer semantics"
    requirement for `target`: two real threads/real Postgres callers
    BOTH independently decide 'time to start lap N+1' for the SAME
    sweep `cursor_key` (both having read the same old `lap=N` persisted
    cursor) and each snapshots a DIFFERENT forward-frontier value at a
    slightly different moment, proposing DIFFERENT `target`s alongside
    the identical `lap=N+1`. Proves: whichever write lands FIRST, inside
    the row lock, establishes `target` for that lap generation
    PERMANENTLY; the second writer's own, different proposed `target`
    is silently discarded, never applied — mirrors
    `test_advance_cursor_never_regresses_a_more_advanced_persisted_cursor_real_threads`'s
    own fast/slow-writer discipline exactly, now proving the dedicated
    `target` invariant rather than `last_created_at`."""
    engine = fresh_engine
    repo = PostgresEvidenceClassificationReconciliationCursorRepository(engine)
    key = f"lap-transition-diff-target-{identity.generate_id()}"

    t_old_lap = datetime.datetime.now(datetime.timezone.utc)
    repo.advance_cursor(key, lap=0, last_created_at=t_old_lap, last_evidence_ids=["old-lap-final"], target=t_old_lap)

    # Both writers' own independent new-lap scans land on the exact SAME
    # new watermark/ids (irrelevant to this test's own focus) — the
    # thing that differs is EACH ONE'S OWN SNAPSHOTTED target, exactly
    # the architect's own "two callers independently decided 'start lap
    # N+1' and each snapshotted a DIFFERENT forward-frontier value"
    # scenario.
    t_new_lap_position = t_old_lap - datetime.timedelta(hours=1)
    t_fast_target = t_old_lap + datetime.timedelta(hours=1)
    t_slow_target = t_old_lap + datetime.timedelta(hours=2)  # a DIFFERENT snapshot than the fast writer's
    barrier = threading.Barrier(2)
    errors: list = []

    def _fast_writer():
        try:
            barrier.wait(timeout=10)
            repo.advance_cursor(
                key, lap=1, last_created_at=t_new_lap_position, last_evidence_ids=["fast-writer"],
                target=t_fast_target,
            )
        except Exception as exc:  # noqa: BLE001 - captured, never swallowed
            errors.append(exc)

    def _slow_writer():
        try:
            barrier.wait(timeout=10)
            # A small, deliberate delay so this write reliably lands
            # AFTER the fast writer's — mirrors this file's own
            # established fast/slow-writer discipline.
            time.sleep(0.2)
            repo.advance_cursor(
                key, lap=1, last_created_at=t_new_lap_position, last_evidence_ids=["slow-writer"],
                target=t_slow_target,
            )
        except Exception as exc:  # noqa: BLE001 - captured, never swallowed
            errors.append(exc)

    threads = [threading.Thread(target=_fast_writer), threading.Thread(target=_slow_writer)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=15)
        assert not t.is_alive(), "a thread deadlocked/timed out"
    assert not errors, f"advance_cursor must never raise under a genuine lap-transition target race: {errors}"

    final = repo.get_cursor(key)
    assert final is not None
    assert final.lap == 1, "the new lap must be accepted even though both writers proposed DIFFERENT targets"
    assert final.target == t_fast_target, (
        "whichever writer's lap=1 proposal actually lands FIRST (inside the row lock) establishes target "
        "for that lap generation PERMANENTLY — the fast writer's write is the one that creates the row "
        "(existing=None at that moment), so its OWN proposed target is what gets persisted"
    )
    assert final.target != t_slow_target, (
        "the slow writer's own, DIFFERENT proposed target must be silently discarded, never applied — it "
        "finds proposed.lap == existing.lap once it takes the lock (the fast writer already bumped lap to "
        "1) and falls into the ordinary same-lap merge path, which never changes an already-persisted target"
    )
    # Both writers' identical created_at/ids proposal still unions
    # correctly — the target race above does not disturb that dimension.
    assert final.last_created_at == t_new_lap_position
    assert set(final.last_evidence_ids) == {"fast-writer", "slow-writer"}


# =======================================================================
# Test plan item: the architect's own exact 7-step growing-frontier
# liveness proof — NOT a static corpus (the earlier "wraps correctly"
# test above, `test_sweep_makes_monotonic_lap_progress_and_wraps_correctly_once_caught_up`,
# was specifically called out as unable to exercise this: its own
# forward frontier never moves once established). Here the forward
# frontier genuinely keeps growing WHILE the sweep is working, across
# every subsequent call, faster than the sweep's own tiny per-call
# budget could ever catch up to it if `target` were NOT frozen.
# =======================================================================


def _setup_growing_frontier_scenario(*, reconcile_fn, boundary: datetime.datetime, job_repo, cursor_repo, sweep_budget: int):
    """Shared setup for the two growing-frontier tests below (steps 1-2
    of the architect's own 7-step plan): seeds a 200-row already-
    resolved backlog, resolves the forward cursor over it in one
    generous call, advances the sweep cursor PARTWAY through its first
    lap (3 bounded calls at `sweep_budget` rows/call — genuinely
    partway: neither at the very start nor finished), then BEGINS (does
    not yet commit — the caller must) a genuine commit-order-inversion
    orphan, using the exact SAME `_begin_uncommitted_evidence_insert`
    technique `test_commit_order_inversion_reproduction_and_sweep_fix`
    already establishes, positioned BEHIND wherever the sweep has
    already scanned to within this in-progress lap.

    `reconcile_fn` is whichever reconciliation callable the caller
    drives this scenario with — either the OLD, lap-aware-but-
    unfrozen-target reconstruction above, or the REAL, fixed
    `reconcile_missing_classification_jobs` — so the two tests below
    run a STRUCTURALLY IDENTICAL scenario, differing only in which
    implementation drives it.
    """
    evidence_repo = _evidence_repository()
    classification_repo = _classification_repository()
    forward_key = boundary.isoformat()
    sweep_key = _sweep_cursor_key(boundary)

    backlog = _bulk_insert_resolved_evidence_and_jobs(boundary=boundary, count=200)

    def _call(**overrides):
        defaults = dict(
            evidence_repository=evidence_repo,
            classification_job_repository=job_repo,
            classification_repository=classification_repo,
            cursor_repository=cursor_repo,
            actor_type="SYSTEM",
            actor_id=ACTOR_ID,
            activation_boundary=boundary,
            repair_limit=100_000, page_size=1000, max_rows_scanned=100_000,
            sweep_repair_limit=sweep_budget, sweep_page_size=sweep_budget, sweep_max_rows_scanned=sweep_budget,
        )
        defaults.update(overrides)
        return reconcile_fn(**defaults)

    # Forward pass resolves the whole 200-row backlog in one
    # generously-budgeted call (the forward budget above is large
    # enough on every call this scenario ever makes — the forward pass
    # is never this test's own bottleneck, only the sweep's tiny budget
    # is).
    _call()
    forward_frontier_before_growth = cursor_repo.get_cursor(forward_key).last_created_at
    assert forward_frontier_before_growth == backlog[-1][1]

    for _ in range(3):  # bounded — advances the sweep PARTWAY through lap 0, never completing it here
        _call()
    partway_cursor = cursor_repo.get_cursor(sweep_key)
    assert partway_cursor is not None
    assert partway_cursor.lap == 0
    partway_position = partway_cursor.last_created_at
    assert boundary < partway_position < forward_frontier_before_growth, (
        "the sweep must be genuinely PARTWAY through its first lap — neither at its very start nor finished "
        "(step 1 of the architect's own 7-step plan)"
    )

    orphan_created_at = boundary + datetime.timedelta(milliseconds=45, microseconds=500)
    assert orphan_created_at < partway_position, (
        "the orphan must sit BEHIND wherever the sweep has already scanned to within this in-progress lap "
        "(step 2 of the architect's own 7-step plan)"
    )
    orphan_id, orphan_conn, orphan_txn = _begin_uncommitted_evidence_insert(
        created_at=orphan_created_at, received_at=boundary
    )

    return {
        "call": _call,
        "forward_key": forward_key,
        "sweep_key": sweep_key,
        "forward_frontier_before_growth": forward_frontier_before_growth,
        "partway_position": partway_position,
        "orphan_id": orphan_id,
        "orphan_conn": orphan_conn,
        "orphan_txn": orphan_txn,
        "next_start_index": 200,
    }


_GROWING_FRONTIER_SWEEP_BUDGET = 30
_GROWING_FRONTIER_GROWTH_PER_CALL = 150


def test_old_lap_aware_unfrozen_target_sweep_never_completes_its_lap_against_a_continuously_growing_frontier():
    """Steps 1-4 of the architect's own 7-step growing-frontier
    liveness plan, against the OLD, lap-aware-but-unfrozen-target
    reconstruction: proves the SECOND, independent liveness gap is
    real — once the forward frontier keeps growing, on EVERY
    subsequent call, faster than the sweep's own tiny per-call budget
    (30 rows) could ever catch up to it, the in-progress lap's own
    completion condition (`last_created_at >= a FRESHLY RE-READ
    forward_frontier`) can never be satisfied: the lap never completes,
    a new lap is therefore never started, and the orphan — sitting
    BEHIND the sweep's own stuck position — is never found, across a
    bounded (never infinite) number of repeated calls."""
    boundary = utc_now()
    job_repo = PostgresEvidenceClassificationJobRepository()
    cursor_repo = PostgresEvidenceClassificationReconciliationCursorRepository()

    state = _setup_growing_frontier_scenario(
        reconcile_fn=_old_lap_aware_no_target_reconcile_missing_classification_jobs,
        boundary=boundary, job_repo=job_repo, cursor_repo=cursor_repo, sweep_budget=_GROWING_FRONTIER_SWEEP_BUDGET,
    )
    call = state["call"]
    orphan_id = state["orphan_id"]
    sweep_key = state["sweep_key"]
    forward_key = state["forward_key"]
    next_start_index = state["next_start_index"]

    try:
        # --- Step 3 (first iteration): a growth batch arrives, and this
        # call runs while the orphan is STILL uncommitted/invisible. ---
        _bulk_insert_resolved_evidence_and_jobs(
            boundary=boundary, count=_GROWING_FRONTIER_GROWTH_PER_CALL, start_index=next_start_index
        )
        next_start_index += _GROWING_FRONTIER_GROWTH_PER_CALL
        call()
        assert job_repo.get_by_evidence(orphan_id) is None, "the orphan is still genuinely uncommitted — invisible"

        # Now commit it — a real, late commit, already sorting BEHIND
        # the sweep's own current (stuck) position.
        state["orphan_txn"].commit()
        state["orphan_conn"].close()

        # --- Step 3 (continued) + step 4: every subsequent call ALSO
        # inserts a fresh growth batch, advancing the forward frontier
        # far beyond what the sweep's own 30-row budget could ever
        # cross in one call — bounded, never an infinite loop. ---
        previous_sweep_position = cursor_repo.get_cursor(sweep_key).last_created_at
        for _ in range(8):  # bounded
            _bulk_insert_resolved_evidence_and_jobs(
                boundary=boundary, count=_GROWING_FRONTIER_GROWTH_PER_CALL, start_index=next_start_index
            )
            next_start_index += _GROWING_FRONTIER_GROWTH_PER_CALL
            call()
            assert job_repo.get_by_evidence(orphan_id) is None, (
                "confirmed: the OLD, lap-aware-but-unfrozen-target sweep NEVER finds the orphan while the "
                "forward frontier keeps growing faster than its own bounded per-call budget — the "
                "architect-confirmed SECOND, independent liveness gap, reproduced directly"
            )
            current_sweep_cursor = cursor_repo.get_cursor(sweep_key)
            current_forward_frontier = cursor_repo.get_cursor(forward_key).last_created_at
            assert current_sweep_cursor.lap == 0, (
                "confirmed: this lap's own completion condition (last_created_at >= a FRESHLY RE-READ "
                "forward_frontier) can never be satisfied while the frontier keeps growing faster than the "
                "sweep's own budget — the lap never completes, so a new lap is never started either"
            )
            assert current_sweep_cursor.last_created_at >= previous_sweep_position, (
                "the sweep still makes SOME bounded forward progress each call — it is not frozen in place, "
                "it simply never catches up to the ever-receding finish line"
            )
            assert current_sweep_cursor.last_created_at < current_forward_frontier, (
                "the sweep's own position must remain STRICTLY BEHIND the ever-growing forward frontier — "
                "this is precisely why its completion condition is never satisfied"
            )
            previous_sweep_position = current_sweep_cursor.last_created_at

        assert job_repo.get_by_evidence(orphan_id) is None
        assert cursor_repo.get_cursor(sweep_key).lap == 0
    finally:
        if not state["orphan_conn"].closed:
            state["orphan_txn"].rollback()
            state["orphan_conn"].close()


def test_sweep_target_freeze_proves_liveness_against_a_continuously_growing_frontier_and_recovers_the_orphan():
    """Steps 1-7 of the architect's own 7-step growing-frontier liveness
    plan, against the REAL, fixed implementation: the IDENTICAL
    scenario (structurally) the OLD reconstruction above fails —
    proves the real fix (a) never changes an in-progress lap's own
    FROZEN `target` no matter how much the forward frontier grows in
    the meantime, (b) still completes that lap within a small, bounded
    number of further calls despite the continued growth, (c) starts a
    new lap whose own freshly-snapshotted `target` is strictly LARGER
    than the old lap's frozen one (since evidence kept arriving), and
    (d) that new lap — restarting from `activation_boundary` — recovers
    the orphan within a further small, bounded number of calls."""
    boundary = utc_now()
    job_repo = PostgresEvidenceClassificationJobRepository()
    cursor_repo = PostgresEvidenceClassificationReconciliationCursorRepository()

    state = _setup_growing_frontier_scenario(
        reconcile_fn=reconcile_missing_classification_jobs,
        boundary=boundary, job_repo=job_repo, cursor_repo=cursor_repo, sweep_budget=_GROWING_FRONTIER_SWEEP_BUDGET,
    )
    call = state["call"]
    orphan_id = state["orphan_id"]
    sweep_key = state["sweep_key"]
    forward_key = state["forward_key"]
    next_start_index = state["next_start_index"]

    try:
        frozen_target_before = cursor_repo.get_cursor(sweep_key).target
        assert frozen_target_before == state["forward_frontier_before_growth"], (
            "lap 0's own target was snapshotted at the moment lap 0 started — BEFORE any growth — and must "
            "equal the forward frontier as of that exact moment"
        )

        # --- Step 3 (first iteration): a growth batch arrives, and this
        # call runs while the orphan is STILL uncommitted/invisible. ---
        _bulk_insert_resolved_evidence_and_jobs(
            boundary=boundary, count=_GROWING_FRONTIER_GROWTH_PER_CALL, start_index=next_start_index
        )
        next_start_index += _GROWING_FRONTIER_GROWTH_PER_CALL
        call()
        assert job_repo.get_by_evidence(orphan_id) is None, "the orphan is still genuinely uncommitted — invisible"
        assert cursor_repo.get_cursor(sweep_key).target == frozen_target_before, (
            "still CONTINUING lap 0 — the frozen target must not have moved despite this call's own growth"
        )

        # Now commit it — a real, late commit, already sorting BEHIND
        # the sweep's own current (stuck) position.
        state["orphan_txn"].commit()
        state["orphan_conn"].close()

        # --- Step 3 (continued) + step 5: every subsequent call ALSO
        # inserts a fresh growth batch — yet lap 0 still completes
        # within a small, bounded number of further calls, BECAUSE its
        # own target is frozen and therefore immune to that growth. ---
        lap_completed = False
        new_target = None
        for _ in range(8):  # bounded
            _bulk_insert_resolved_evidence_and_jobs(
                boundary=boundary, count=_GROWING_FRONTIER_GROWTH_PER_CALL, start_index=next_start_index
            )
            next_start_index += _GROWING_FRONTIER_GROWTH_PER_CALL
            call()
            current = cursor_repo.get_cursor(sweep_key)
            if current.lap == 0:
                assert current.target == frozen_target_before, (
                    "the frozen target must NEVER change while CONTINUING lap 0, no matter how much the "
                    "forward frontier has grown in the meantime — this is the actual fix (step 5)"
                )
            else:
                lap_completed = True
                new_target = current.target
                break
        assert lap_completed, (
            "lap 0 must complete within a small, bounded number of further calls despite continued forward "
            "growth — step 5 of the architect's own 7-step plan"
        )
        assert current.lap == 1, "step 6: lap must have incremented to a new generation"
        assert new_target is not None and new_target > frozen_target_before, (
            "step 6: the NEW lap's own target must be strictly LARGER than lap 0's own frozen target, since "
            "evidence kept arriving while lap 0 was in progress"
        )
        assert new_target == cursor_repo.get_cursor(forward_key).last_created_at, (
            "the new lap's own target is exactly the forward frontier AS OF the moment it was snapshotted"
        )

        # --- Step 7: lap 1 (restarting scanning from activation_boundary)
        # must eventually discover the orphan, within a further bounded
        # number of calls — no further growth needed here; this phase
        # isolates pure discovery. ---
        found = False
        for _ in range(5):  # bounded
            call()
            if job_repo.get_by_evidence(orphan_id) is not None:
                found = True
                break
        assert found, (
            "step 7: lap 1 (restarting from activation_boundary) must discover the orphan within a bounded "
            "number of further calls"
        )
        recovered_job = job_repo.get_by_evidence(orphan_id)
        assert recovered_job.status == "PENDING"
    finally:
        if not state["orphan_conn"].closed:
            state["orphan_txn"].rollback()
            state["orphan_conn"].close()


# =======================================================================
# Re-proving what this round's `target` correction must NOT regress:
# pre-activation exclusion, bounded per-call repair, and job/evidence_id
# uniqueness are all already covered by this file's own existing tests
# above (which were updated to also thread/assert `target`, never
# removed or weakened) — see, respectively,
# `test_sweep_never_scans_below_activation_boundary_across_many_laps`,
# every bounded-budget assertion throughout this file (e.g.
# `test_sweep_requires_and_completes_within_multiple_calls_at_realistic_scale_with_production_defaults`),
# and `test_normal_enqueue_plus_forward_plus_sweep_together_produce_exactly_one_job_per_evidence`
# (the real `evidence_id` unique constraint, unaffected by this round's
# correction).
# =======================================================================
