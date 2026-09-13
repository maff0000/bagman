"""``PostgresIntakeRepository`` — durable implementation of
``services.evidence.intake.intake.IntakeRepository`` (CD-4 WI-1, PID
§8/§25/§52/§53).

Preserves, durably, the exact idempotency-conflict semantics
``services.evidence.intake.intake.InMemoryIntakeRepository`` documents
(see that module's docstring for the full "same identifying request"
rationale): an ``idempotency_key`` replay with an identical identifying
tuple (``source_id``, ``entity_hint``, ``original_filename``,
``reported_mime_type``) returns the SAME ``IntakeRecord``; a replay
with a different tuple raises ``core.errors.IdempotencyConflictError``.

Unlike the in-memory version, this implementation never decides the
"seen key" outcome from a separate application-level check-then-insert
alone (which could race under concurrent callers): it always attempts
the insert; the database's own partial unique index
(``uq_intake_records_idempotency_key`` — see
``persistence/postgres/intake_models.py``) is the real source of truth
for the composite-uniqueness decision. An initial ``find_by_idempotency_key``
pre-check is still performed first (exactly like
``PostgresEvidenceRepository.register_evidence`` does for its external-
reference hint) purely to avoid an unnecessary failed insert attempt in
the common, non-racing case — but the constraint violation path below
is what actually proves correctness under a genuine race, by re-reading
whichever row actually won and deciding replay-vs-conflict from THAT
row, never from the stale pre-check.
"""
from __future__ import annotations

from typing import Any, Mapping, Optional

from sqlalchemy.engine import Engine
from sqlalchemy.exc import DataError, IntegrityError, SQLAlchemyError

from core import identity
from core.contract_validation import validate_against_contract
from core.errors import (
    IdempotencyConflictError,
    InvalidStateTransitionError,
    NotFoundError,
    PersistenceError,
    ValidationError,
)
from core.timestamps import utc_now
from persistence.postgres.db_errors import (
    is_foreign_key_violation,
    is_invalid_uuid_format,
    unique_violation_constraint,
)
from persistence.postgres.intake_models import IntakeRecordRow
from persistence.postgres.session import get_engine, session_scope
from services.evidence.intake.intake import (
    IntakeRecord,
    IntakeRepository,
    _identifying_fields,
    transition,
)

_SCHEMA = "intake/bagman.intake_record.v1.schema.json"

_IDEMPOTENCY_CONSTRAINT = "uq_intake_records_idempotency_key"


def _row_to_intake(row: IntakeRecordRow) -> IntakeRecord:
    return IntakeRecord(
        intake_id=row.intake_id,
        source_id=row.source_id,
        entity_hint=row.entity_hint,
        status=row.status,
        received_at=row.received_at,
        completed_at=row.completed_at,
        original_filename=row.original_filename,
        reported_mime_type=row.reported_mime_type,
        detected_mime_type=row.detected_mime_type,
        size_bytes=row.size_bytes,
        content_hash=dict(row.content_hash) if row.content_hash is not None else None,
        evidence_id=row.evidence_id,
        failure_code=row.failure_code,
        quarantine_reason=row.quarantine_reason,
        correlation_id=row.correlation_id,
        idempotency_key=row.idempotency_key,
        metadata=dict(row.metadata_),
    )


def _replay_or_conflict(
    winner: Optional[IntakeRecord],
    *,
    idempotency_key: str,
    source_id: str,
    entity_hint: Optional[str],
    original_filename: Optional[str],
    reported_mime_type: Optional[str],
) -> IntakeRecord:
    """Given the row that actually holds `idempotency_key` (from a
    pre-check or from re-reading after a constraint violation), decide
    idempotent-replay vs. genuine conflict — never from a stale read."""
    requested = _identifying_fields(
        source_id=source_id,
        entity_hint=entity_hint,
        original_filename=original_filename,
        reported_mime_type=reported_mime_type,
    )
    existing_tuple = _identifying_fields(
        source_id=winner.source_id,
        entity_hint=winner.entity_hint,
        original_filename=winner.original_filename,
        reported_mime_type=winner.reported_mime_type,
    )
    if requested == existing_tuple:
        return winner
    raise IdempotencyConflictError(
        f"idempotency_key '{idempotency_key}' already maps to IntakeRecord "
        f"'{winner.intake_id}' with different identifying request content "
        "(source_id/entity_hint/original_filename/reported_mime_type); refusing "
        "to silently reuse the key for different content (PID §25/§53)"
    )


class PostgresIntakeRepository(IntakeRepository):
    """PostgreSQL-backed ``IntakeRepository``. Stateless: every method
    reads/writes the database directly via a fresh ``Session``."""

    def __init__(self, engine: Optional[Engine] = None) -> None:
        self._engine = engine or get_engine()

    def create_intake_record(
        self,
        *,
        source_id: str,
        entity_hint: Optional[str] = None,
        original_filename: Optional[str] = None,
        reported_mime_type: Optional[str] = None,
        correlation_id: Optional[str] = None,
        idempotency_key: Optional[str] = None,
        metadata: Optional[Mapping[str, Any]] = None,
    ) -> IntakeRecord:
        if idempotency_key is not None:
            existing = self.find_by_idempotency_key(idempotency_key)
            if existing is not None:
                return _replay_or_conflict(
                    existing,
                    idempotency_key=idempotency_key,
                    source_id=source_id,
                    entity_hint=entity_hint,
                    original_filename=original_filename,
                    reported_mime_type=reported_mime_type,
                )

        try:
            candidate = IntakeRecord(
                intake_id=identity.generate_id(),
                source_id=source_id,
                status="RECEIVED",
                received_at=utc_now(),
                correlation_id=correlation_id if correlation_id is not None else identity.generate_id(),
                entity_hint=entity_hint,
                original_filename=original_filename,
                reported_mime_type=reported_mime_type,
                idempotency_key=idempotency_key,
                metadata=dict(metadata) if metadata is not None else {},
            )
            validate_against_contract(candidate.to_dict(), _SCHEMA)
        except ValidationError:
            raise
        except Exception as exc:  # noqa: BLE001 - never leak a raw exception
            raise ValidationError(f"could not create IntakeRecord: {exc}") from exc

        row = IntakeRecordRow(
            intake_id=candidate.intake_id,
            source_id=candidate.source_id,
            entity_hint=candidate.entity_hint,
            status=candidate.status,
            received_at=candidate.received_at,
            completed_at=candidate.completed_at,
            original_filename=candidate.original_filename,
            reported_mime_type=candidate.reported_mime_type,
            detected_mime_type=candidate.detected_mime_type,
            size_bytes=candidate.size_bytes,
            content_hash=dict(candidate.content_hash) if candidate.content_hash is not None else None,
            evidence_id=candidate.evidence_id,
            failure_code=candidate.failure_code,
            quarantine_reason=candidate.quarantine_reason,
            correlation_id=candidate.correlation_id,
            idempotency_key=candidate.idempotency_key,
            metadata_=dict(candidate.metadata),
        )

        try:
            with session_scope(self._engine) as session:
                session.add(row)
        except IntegrityError as exc:
            if idempotency_key is not None and unique_violation_constraint(exc) == _IDEMPOTENCY_CONSTRAINT:
                winner = self.find_by_idempotency_key(idempotency_key)
                if winner is not None:
                    return _replay_or_conflict(
                        winner,
                        idempotency_key=idempotency_key,
                        source_id=source_id,
                        entity_hint=entity_hint,
                        original_filename=original_filename,
                        reported_mime_type=reported_mime_type,
                    )
                raise PersistenceError(
                    f"could not create IntakeRecord: unique-constraint conflict on "
                    f"idempotency_key '{idempotency_key}' but no winning row could be re-read: {exc}"
                ) from exc
            if is_foreign_key_violation(exc):
                raise NotFoundError(f"no Source with source_id '{source_id}'") from exc
            raise PersistenceError(f"could not create IntakeRecord: {exc}") from exc
        except SQLAlchemyError as exc:
            # Note: unlike `get_intake_record`/`transition_status` below,
            # there is deliberately no separate `except DataError` /
            # `is_invalid_uuid_format` branch here — a malformed
            # `source_id` never reaches the database at all, because
            # `validate_against_contract` above already rejects it
            # against `bagman.identifier.v1`'s UUID pattern as a
            # `ValidationError` first (exactly like every other
            # `register_*` call in this codebase, e.g.
            # `PostgresSourceRepository.register_source`). `DataError`
            # is still a `SQLAlchemyError` subclass, so a genuinely
            # unanticipated DB-level shape error still lands here as
            # `PersistenceError`, same as any other unexpected failure.
            raise PersistenceError(f"could not create IntakeRecord: {exc}") from exc

        return candidate

    def get_intake_record(self, intake_id: str) -> IntakeRecord:
        try:
            with session_scope(self._engine) as session:
                row = session.get(IntakeRecordRow, intake_id)
                if row is None:
                    raise NotFoundError(f"no IntakeRecord with intake_id '{intake_id}'")
                return _row_to_intake(row)
        except NotFoundError:
            raise
        except DataError as exc:
            if is_invalid_uuid_format(exc):
                raise NotFoundError(
                    f"no IntakeRecord with intake_id '{intake_id}' "
                    "(malformed identifier can never exist)"
                ) from exc
            raise PersistenceError(f"could not read IntakeRecord: {exc}") from exc
        except SQLAlchemyError as exc:
            raise PersistenceError(f"could not read IntakeRecord: {exc}") from exc

    def transition_status(self, intake_id: str, new_status: str, **field_updates: Any) -> IntakeRecord:
        try:
            with session_scope(self._engine) as session:
                # SELECT ... FOR UPDATE: locks the row for the rest of
                # this transaction so a concurrent second
                # transition_status call cannot race past the
                # allowed-transition check (mirrors
                # PostgresEvidenceRepository.assign_entity).
                try:
                    row = session.get(IntakeRecordRow, intake_id, with_for_update=True)
                except DataError as exc:
                    if is_invalid_uuid_format(exc):
                        raise NotFoundError(
                            f"no IntakeRecord with intake_id '{intake_id}' "
                            "(malformed identifier can never exist)"
                        ) from exc
                    raise
                if row is None:
                    raise NotFoundError(f"no IntakeRecord with intake_id '{intake_id}'")

                current = _row_to_intake(row)
                updated = transition(current, new_status, **field_updates)

                row.status = updated.status
                row.completed_at = updated.completed_at
                row.detected_mime_type = updated.detected_mime_type
                row.size_bytes = updated.size_bytes
                row.content_hash = dict(updated.content_hash) if updated.content_hash is not None else None
                row.evidence_id = updated.evidence_id
                row.failure_code = updated.failure_code
                row.quarantine_reason = updated.quarantine_reason
                # PL reconciliation fix (CD-4 WI-2): `transition()` can
                # legitimately update `metadata` via `field_updates` (e.g.
                # the CD-4 WI-2 validation pipeline stamps
                # `storage_reference` into it on ACCEPTED) — this was
                # previously never copied back to the row, so a durable
                # PostgreSQL-backed transition silently dropped any
                # metadata change while the in-memory repository (which
                # stores the whole dataclass) masked the gap. Without
                # this, WI-3 would have no durable way to find the
                # staged bytes an ACCEPTED IntakeRecord points to.
                row.metadata_ = dict(updated.metadata)
                session.flush()
        except (NotFoundError, InvalidStateTransitionError, ValidationError):
            raise
        except IntegrityError as exc:
            if is_foreign_key_violation(exc):
                evidence_id = field_updates.get("evidence_id")
                raise NotFoundError(f"no EvidenceItem with evidence_id '{evidence_id}'") from exc
            raise PersistenceError(f"could not transition IntakeRecord: {exc}") from exc
        except SQLAlchemyError as exc:
            raise PersistenceError(f"could not transition IntakeRecord: {exc}") from exc

        return updated

    def find_by_idempotency_key(self, idempotency_key: str) -> Optional[IntakeRecord]:
        try:
            with session_scope(self._engine) as session:
                row = (
                    session.query(IntakeRecordRow)
                    .filter_by(idempotency_key=idempotency_key)
                    .one_or_none()
                )
                return _row_to_intake(row) if row is not None else None
        except SQLAlchemyError as exc:
            raise PersistenceError(f"could not look up IntakeRecord by idempotency_key: {exc}") from exc

    def list_intake_records(
        self,
        *,
        entity_hint: Optional[str] = None,
        status: Optional[str] = None,
        received_at_from=None,
        received_at_to=None,
        limit: Optional[int] = None,
        offset: int = 0,
    ) -> list[IntakeRecord]:
        try:
            with session_scope(self._engine) as session:
                query = session.query(IntakeRecordRow)
                if entity_hint is not None:
                    query = query.filter(IntakeRecordRow.entity_hint == entity_hint)
                if status is not None:
                    query = query.filter(IntakeRecordRow.status == status)
                if received_at_from is not None:
                    query = query.filter(IntakeRecordRow.received_at >= received_at_from)
                if received_at_to is not None:
                    query = query.filter(IntakeRecordRow.received_at <= received_at_to)
                query = query.order_by(
                    IntakeRecordRow.received_at.desc(), IntakeRecordRow.intake_id.desc()
                )
                if offset:
                    query = query.offset(offset)
                if limit is not None:
                    query = query.limit(limit)
                rows = query.all()
                return [_row_to_intake(row) for row in rows]
        except SQLAlchemyError as exc:
            raise PersistenceError(f"could not list IntakeRecord rows: {exc}") from exc
