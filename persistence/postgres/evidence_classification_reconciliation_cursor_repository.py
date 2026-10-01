"""`PostgresEvidenceClassificationReconciliationCursorRepository` —
durable implementation of `services.evidence
.classification_reconciliation_cursor
.EvidenceClassificationReconciliationCursorRepository` (BAGMAN
accounting platform, `evidence/automatic-classification-activation`
WO — post-merge preflight review correction, item B; MERGE-ON-WRITE
correction, `evidence/classification-activation-preflight` WO this
round — see below).

`advance_cursor`'s upsert mirrors
`persistence.postgres.mailbox_microsoft_repository
.PostgresMailboxFolderCursorRepository.get_or_bootstrap`/
`advance_cursor`'s own established idiom for the common case: a
`SELECT ... FOR UPDATE` lookup by primary key, then either an in-place
column update (row already exists) or a fresh `INSERT` (first call for
this `cursor_key`) — a real transactional SELECT-then-INSERT-or-UPDATE,
not a bare application-level pre-check alone.

Unlike that precedent, a genuine concurrent first-advance race for the
SAME, brand-new `cursor_key` is NOT vanishingly unlikely here:
`reconcile_missing_classification_jobs` is explicitly proven safe to
call from genuinely concurrent callers for the same `activation_boundary`
(`tests/persistence/test_evidence_classification_job_reconciliation.py
::test_reconciliation_never_raises_and_still_resolves_to_one_row_across_threads`),
and every one of those concurrent calls shares the exact same,
possibly-brand-new `cursor_key = activation_boundary.isoformat()`. So
this repository adds one thing `PostgresMailboxFolderCursorRepository`
does not need: if the INSERT branch above loses a genuine race (another
caller's INSERT committed first, surfacing here as a primary-key
`IntegrityError`), this is caught and retried as an UPDATE against the
now-existing row — mirroring
`PostgresEvidenceClassificationJobRepository.submit_job`'s own
"`IntegrityError` -> re-fetch and merge with the winner, never raise"
discipline exactly.

CORRECTED this round: that retry-as-UPDATE (and the primary, non-racing
UPDATE path above it) is NO LONGER a blind "apply this caller's own
values" overwrite — see `services.evidence
.classification_reconciliation_cursor`'s own "Why advance_cursor merges
instead of overwrites" docstring section, and
:func:`services.evidence.classification_reconciliation_cursor
._resolve_cursor_advance` (the one, shared, pure merge decision this
repository and `InMemoryEvidenceClassificationReconciliationCursorRepository`
both call, from inside their own lock, so the two implementations can
never silently drift apart on what counts as forward progress). A
caller's proposed `(last_created_at, last_evidence_ids)` is no longer
assumed to be "an equally valid cursor position after this call" purely
because it arrived — it is compared, inside the SAME `FOR UPDATE` lock,
against whatever is CURRENTLY persisted, and a proposal that does not
represent genuine forward progress from the current persisted state
(equal -> union the id sets; strictly less -> no-op, never regress) is
handled accordingly rather than blindly applied.

CORRECTED AGAIN this round: the merge decision above is no longer a
plain `last_created_at` comparison — it is now lexicographic on `(lap,
last_created_at)`, `lap` compared first, per `_resolve_cursor_advance`'s
own docstring — see `services.evidence.classification_reconciliation_cursor`'s
own module docstring, "lap" section, for why (closes a genuine,
independently-confirmed permanent-stall bug in the sweep cursor's own
lap-restart mechanism). `row.lap` is updated alongside `row.last_created_at`/
`row.last_evidence_ids` in every branch below that actually writes.

CORRECTED YET AGAIN this round (evidence/classification-activation-
preflight WO — final lap-boundary-liveness correction): `row.target`
is now updated alongside the fields above in every branch that
actually writes, per `_resolve_cursor_advance`'s own "target" rule —
see `services.evidence.classification_reconciliation_cursor`'s own
module docstring, "target" section, for the full reasoning (a lap's
own upper bound is frozen at the moment the lap starts, never
recomputed from a freshly-read forward frontier on every continuing
call).

`last_evidence_ids` (post-merge audit correction — see
`services.evidence.classification_reconciliation_cursor`'s own
docstring, "Why the tie-break is SET membership" section) is stored as
a plain JSON array of strings, mirroring this codebase's own existing
"a plain list of short strings is JSONB" convention (e.g.
`persistence.postgres.evidence_classification_models
::EvidenceClassificationRow.reason_codes`,
`persistence.postgres.xero_account_suggestion_models
::XeroAccountSuggestionRow.signals`) — never a scalar column, and
never re-ordered/deduplicated here (the caller, `services.evidence
.classification_job.reconcile_missing_classification_jobs`, already
hands this repository the exact, final set to persist).
"""
from __future__ import annotations

from datetime import datetime
from typing import Collection, Optional

from sqlalchemy.engine import Engine
from sqlalchemy.exc import IntegrityError, SQLAlchemyError

from core.errors import PersistenceError
from core.timestamps import utc_now
from persistence.postgres.evidence_classification_job_models import (
    EvidenceClassificationReconciliationCursorRow,
)
from persistence.postgres.session import get_engine, session_scope
from services.evidence.classification_reconciliation_cursor import (
    EvidenceClassificationReconciliationCursor,
    EvidenceClassificationReconciliationCursorRepository,
    _resolve_cursor_advance,
)


def _row_to_cursor(row: EvidenceClassificationReconciliationCursorRow) -> EvidenceClassificationReconciliationCursor:
    return EvidenceClassificationReconciliationCursor(
        cursor_key=row.cursor_key,
        lap=row.lap,
        last_created_at=row.last_created_at,
        last_evidence_ids=tuple(sorted(row.last_evidence_ids)),
        target=row.target,
        created_at=row.created_at,
        updated_at=row.updated_at,
    )


class PostgresEvidenceClassificationReconciliationCursorRepository(
    EvidenceClassificationReconciliationCursorRepository
):
    """PostgreSQL-backed `EvidenceClassificationReconciliationCursorRepository`.
    Stateless: every method reads/writes the database directly via a
    fresh `Session` — mirrors every other Postgres-backed repository in
    this codebase's own construction/statelessness discipline."""

    def __init__(self, engine: Optional[Engine] = None) -> None:
        self._engine = engine or get_engine()

    def get_cursor(self, cursor_key: str) -> Optional[EvidenceClassificationReconciliationCursor]:
        try:
            with session_scope(self._engine) as session:
                row = session.get(EvidenceClassificationReconciliationCursorRow, cursor_key)
                return _row_to_cursor(row) if row is not None else None
        except SQLAlchemyError as exc:
            raise PersistenceError(
                f"could not read EvidenceClassificationReconciliationCursor: {exc}"
            ) from exc

    def advance_cursor(
        self,
        cursor_key: str,
        *,
        lap: int,
        last_created_at: datetime,
        last_evidence_ids: Collection[str],
        target: datetime,
    ) -> EvidenceClassificationReconciliationCursor:
        try:
            with session_scope(self._engine) as session:
                row = session.get(
                    EvidenceClassificationReconciliationCursorRow, cursor_key, with_for_update=True
                )
                existing = _row_to_cursor(row) if row is not None else None
                # Merge-on-write, decided INSIDE this SELECT ... FOR
                # UPDATE lock — see services.evidence
                # .classification_reconciliation_cursor's own "Why
                # advance_cursor merges instead of overwrites", "lap",
                # AND "target" docstring sections, and
                # _resolve_cursor_advance's own docstring.
                merged_lap, merged_created_at, merged_ids, merged_target, changed = _resolve_cursor_advance(
                    existing=existing,
                    proposed_lap=lap,
                    proposed_last_created_at=last_created_at,
                    proposed_last_evidence_ids=last_evidence_ids,
                    proposed_target=target,
                )

                if row is not None:
                    if not changed:
                        # A slower/stale-started caller's proposal never
                        # regresses a more-advanced persisted cursor —
                        # return it completely unchanged, not even
                        # updated_at bumped.
                        return existing  # type: ignore[return-value]
                    row.lap = merged_lap
                    row.last_created_at = merged_created_at
                    row.last_evidence_ids = list(merged_ids)
                    row.target = merged_target
                    row.updated_at = utc_now()
                    session.flush()
                    return _row_to_cursor(row)

                # No existing row yet: merged_* == the caller's own
                # proposal verbatim (see _resolve_cursor_advance's own
                # existing=None branch) — nothing to merge against.
                now = utc_now()
                new_row = EvidenceClassificationReconciliationCursorRow(
                    cursor_key=cursor_key,
                    lap=merged_lap,
                    last_created_at=merged_created_at,
                    last_evidence_ids=list(merged_ids),
                    target=merged_target,
                    created_at=now,
                    updated_at=now,
                )
                session.add(new_row)
                session.flush()
                return _row_to_cursor(new_row)
        except IntegrityError as exc:
            # A genuine concurrent first-advance race for the SAME,
            # brand-new cursor_key — see module docstring. Never raise
            # for this: retry as a merge-on-write UPDATE against the
            # now-existing winning row (identical merge discipline as
            # the primary path above — never a blind overwrite here
            # either).
            try:
                with session_scope(self._engine) as session:
                    row = session.get(
                        EvidenceClassificationReconciliationCursorRow, cursor_key, with_for_update=True
                    )
                    if row is None:
                        raise PersistenceError(
                            "EvidenceClassificationReconciliationCursor unique-constraint conflict for "
                            f"cursor_key {cursor_key!r} but no existing row could be re-read"
                        ) from exc
                    existing = _row_to_cursor(row)
                    merged_lap, merged_created_at, merged_ids, merged_target, changed = _resolve_cursor_advance(
                        existing=existing,
                        proposed_lap=lap,
                        proposed_last_created_at=last_created_at,
                        proposed_last_evidence_ids=last_evidence_ids,
                        proposed_target=target,
                    )
                    if not changed:
                        return existing
                    row.lap = merged_lap
                    row.last_created_at = merged_created_at
                    row.last_evidence_ids = list(merged_ids)
                    row.target = merged_target
                    row.updated_at = utc_now()
                    session.flush()
                    return _row_to_cursor(row)
            except SQLAlchemyError as retry_exc:
                raise PersistenceError(
                    f"could not advance EvidenceClassificationReconciliationCursor after a concurrent "
                    f"first-advance race: {retry_exc}"
                ) from retry_exc
        except SQLAlchemyError as exc:
            raise PersistenceError(
                f"could not advance EvidenceClassificationReconciliationCursor: {exc}"
            ) from exc
