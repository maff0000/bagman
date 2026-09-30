"""`PostgresEvidenceClassificationReconciliationCursorRepository` —
durable implementation of `services.evidence
.classification_reconciliation_cursor
.EvidenceClassificationReconciliationCursorRepository` (BAGMAN
accounting platform, `evidence/automatic-classification-activation`
WO — post-merge preflight review correction, item B).

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
`IntegrityError`), this is caught and retried as a plain UPDATE against
the now-existing row — mirroring
`PostgresEvidenceClassificationJobRepository.submit_job`'s own
"`IntegrityError` -> re-fetch and merge with the winner, never raise"
discipline exactly. `advance_cursor` has no independent identity
semantics to preserve on such a race (unlike `submit_job`, which must
return the WINNING row unchanged) — any of the racing callers' own
`(last_created_at, last_evidence_ids)` values is an equally valid
"cursor position after this call", so the retry simply applies this
caller's own values as an update.

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
)


def _row_to_cursor(row: EvidenceClassificationReconciliationCursorRow) -> EvidenceClassificationReconciliationCursor:
    return EvidenceClassificationReconciliationCursor(
        cursor_key=row.cursor_key,
        last_created_at=row.last_created_at,
        last_evidence_ids=tuple(sorted(row.last_evidence_ids)),
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
        self, cursor_key: str, *, last_created_at: datetime, last_evidence_ids: Collection[str]
    ) -> EvidenceClassificationReconciliationCursor:
        evidence_ids_list = sorted(last_evidence_ids)
        try:
            with session_scope(self._engine) as session:
                row = session.get(
                    EvidenceClassificationReconciliationCursorRow, cursor_key, with_for_update=True
                )
                if row is not None:
                    row.last_created_at = last_created_at
                    row.last_evidence_ids = evidence_ids_list
                    row.updated_at = utc_now()
                    session.flush()
                    return _row_to_cursor(row)

                now = utc_now()
                new_row = EvidenceClassificationReconciliationCursorRow(
                    cursor_key=cursor_key,
                    last_created_at=last_created_at,
                    last_evidence_ids=evidence_ids_list,
                    created_at=now,
                    updated_at=now,
                )
                session.add(new_row)
                session.flush()
                return _row_to_cursor(new_row)
        except IntegrityError as exc:
            # A genuine concurrent first-advance race for the SAME,
            # brand-new cursor_key — see module docstring. Never raise
            # for this: retry as a plain UPDATE against the now-existing
            # winning row.
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
                    row.last_created_at = last_created_at
                    row.last_evidence_ids = evidence_ids_list
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
