"""``PostgresAuditRepository`` — durable implementation of
``core.audit.AuditRepository`` (CD-3 WI-1, PID §13-15/§21).

Append-only BY DESIGN: this class defines only ``record_audit_event()``
plus read/query methods (``get_audit_event``, ``list_by_correlation``,
``list_by_subject``) — there is no update or delete method anywhere on
this class, matching PID §21's "repository design should make audit
mutation difficult by default". No database-level ``REVOKE``/role
management is introduced here — out of scope for this work item.
"""
from __future__ import annotations

from typing import Any, Mapping, Optional

from datetime import datetime

from sqlalchemy.engine import Engine
from sqlalchemy.exc import DataError, SQLAlchemyError

from core import actor, identity
from core.audit import AuditEvent, AuditRepository
from core.contract_validation import validate_against_contract
from core.errors import NotFoundError, PersistenceError, ValidationError
from core.timestamps import utc_now
from persistence.postgres.db_errors import is_invalid_uuid_format
from persistence.postgres.models import AuditEventRow
from persistence.postgres.session import get_engine, session_scope

_SCHEMA = "audit/bagman.audit_event.v1.schema.json"

#: Matches core.audit.SCHEMA_VERSION — CD-2's only supported contract
#: version for AuditEvent.
SCHEMA_VERSION = "bagman.audit_event.v1"


def _row_to_audit_event(row: AuditEventRow) -> AuditEvent:
    return AuditEvent(
        audit_event_id=row.audit_event_id,
        event_type=row.event_type,
        occurred_at=row.occurred_at,
        actor_type=row.actor_type,
        actor_id=row.actor_id,
        subject_type=row.subject_type,
        subject_id=row.subject_id,
        correlation_id=row.correlation_id,
        causation_id=row.causation_id,
        payload=dict(row.payload),
        schema_version=row.schema_version,
    )


class PostgresAuditRepository(AuditRepository):
    """PostgreSQL-backed ``AuditRepository``. Stateless: every method
    reads/writes the database directly via a fresh ``Session``."""

    def __init__(self, engine: Optional[Engine] = None) -> None:
        self._engine = engine or get_engine()

    def record_audit_event(
        self,
        *,
        event_type: str,
        actor_type: str,
        actor_id: str,
        subject_type: str,
        subject_id: str,
        correlation_id: str,
        causation_id: Optional[str],
        occurred_at: Optional[datetime] = None,
        payload: Optional[Mapping[str, Any]] = None,
    ) -> AuditEvent:
        if not actor.is_valid(actor_type):
            raise ValidationError(
                f"actor_type '{actor_type}' is not one of the closed set "
                f"{sorted(actor.ALL)} (PID §14)"
            )

        try:
            candidate = AuditEvent(
                audit_event_id=identity.generate_id(),
                event_type=event_type,
                occurred_at=occurred_at if occurred_at is not None else utc_now(),
                actor_type=actor_type,
                actor_id=actor_id,
                subject_type=subject_type,
                subject_id=subject_id,
                correlation_id=correlation_id,
                causation_id=causation_id,
                payload=dict(payload) if payload is not None else {},
                schema_version=SCHEMA_VERSION,
            )
            validate_against_contract(candidate.to_dict(), _SCHEMA)
        except ValidationError:
            raise
        except Exception as exc:  # noqa: BLE001 - never leak a raw exception
            raise ValidationError(f"could not record AuditEvent: {exc}") from exc

        row = AuditEventRow(
            audit_event_id=candidate.audit_event_id,
            event_type=candidate.event_type,
            occurred_at=candidate.occurred_at,
            actor_type=candidate.actor_type,
            actor_id=candidate.actor_id,
            subject_type=candidate.subject_type,
            subject_id=candidate.subject_id,
            correlation_id=candidate.correlation_id,
            causation_id=candidate.causation_id,
            payload=dict(candidate.payload),
            schema_version=candidate.schema_version,
        )

        try:
            with session_scope(self._engine) as session:
                session.add(row)
        except SQLAlchemyError as exc:
            raise PersistenceError(f"could not record AuditEvent: {exc}") from exc

        return candidate

    def get_audit_event(self, audit_event_id: str) -> AuditEvent:
        try:
            with session_scope(self._engine) as session:
                row = session.get(AuditEventRow, audit_event_id)
                if row is None:
                    raise NotFoundError(f"no AuditEvent with audit_event_id '{audit_event_id}'")
                return _row_to_audit_event(row)
        except NotFoundError:
            raise
        except DataError as exc:
            if is_invalid_uuid_format(exc):
                raise NotFoundError(
                    f"no AuditEvent with audit_event_id '{audit_event_id}' "
                    "(malformed identifier can never exist)"
                ) from exc
            raise PersistenceError(f"could not read AuditEvent: {exc}") from exc
        except SQLAlchemyError as exc:
            raise PersistenceError(f"could not read AuditEvent: {exc}") from exc

    def list_by_correlation(self, correlation_id: str) -> list[AuditEvent]:
        try:
            with session_scope(self._engine) as session:
                rows = (
                    session.query(AuditEventRow)
                    .filter_by(correlation_id=correlation_id)
                    .order_by(AuditEventRow.audit_event_id)
                    .all()
                )
                return [_row_to_audit_event(row) for row in rows]
        except SQLAlchemyError as exc:
            raise PersistenceError(f"could not list AuditEvent rows by correlation: {exc}") from exc

    def list_by_subject(self, subject_type: str, subject_id: str) -> list[AuditEvent]:
        try:
            with session_scope(self._engine) as session:
                rows = (
                    session.query(AuditEventRow)
                    .filter_by(subject_type=subject_type, subject_id=subject_id)
                    .order_by(AuditEventRow.audit_event_id)
                    .all()
                )
                return [_row_to_audit_event(row) for row in rows]
        except SQLAlchemyError as exc:
            raise PersistenceError(f"could not list AuditEvent rows by subject: {exc}") from exc

    def list_recent(
        self,
        *,
        limit: int = 50,
        before: Optional[datetime] = None,
        event_type_prefix: Optional[str] = None,
    ) -> list[AuditEvent]:
        try:
            with session_scope(self._engine) as session:
                query = session.query(AuditEventRow)
                if before is not None:
                    query = query.filter(AuditEventRow.occurred_at < before)
                if event_type_prefix is not None:
                    # Escaped LIKE prefix match — event_type_prefix is
                    # ALWAYS a caller-supplied literal (e.g.
                    # "NEEDS_YOU_" from app/api/routers/needs_you.py),
                    # never end-user input reaching this far unescaped,
                    # but escaping the two LIKE wildcard characters
                    # costs nothing and keeps this correct even if a
                    # future caller's prefix happens to contain one.
                    escaped = event_type_prefix.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
                    query = query.filter(AuditEventRow.event_type.like(f"{escaped}%", escape="\\"))
                query = query.order_by(
                    AuditEventRow.occurred_at.desc(), AuditEventRow.audit_event_id.desc()
                ).limit(limit)
                rows = query.all()
                return [_row_to_audit_event(row) for row in rows]
        except SQLAlchemyError as exc:
            raise PersistenceError(f"could not list recent AuditEvent rows: {exc}") from exc
