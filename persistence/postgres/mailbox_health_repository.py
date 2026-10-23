"""``PostgresMailboxHealthIncidentRepository``/
``PostgresMailboxHealthNotificationRepository`` — durable
implementations of
``services.mailbox.health.incident.MailboxHealthIncidentRepository``/
``services.mailbox.health.notification.MailboxHealthNotificationRepository``
(PID §118, WO-BAGMAN-118-MAILBOX-HEALTH-ALERTING §6.A.2).

Raw SQL, not the ORM-insert-and-catch-IntegrityError idiom this
codebase's OTHER idempotent-create repositories use (PID §118.F's
reason this must be different)
------------------------------------------------------------------------
``persistence.postgres.needs_you_repository``/
``persistence.postgres.evidence_classification_job_repository`` both
implement "idempotent create" by attempting a plain ORM insert and
catching the resulting ``IntegrityError`` on a genuine race. That idiom
is correct for THOSE repositories because every one of their own create
calls is always its own standalone, single-statement transaction — an
aborted statement aborts nothing else.

This delivery's own governing PID (§118.F) requires incident creation,
its ``NeedsYouItem``, and its ``INITIAL`` notification intent to
compose into ONE shared PostgreSQL transaction (the evaluator's own
``with session_scope(engine) as session: ...`` block) — and a
PostgreSQL transaction that hits ANY exception (including a caught-and-
handled ``IntegrityError``) is left in an aborted state for every
further statement until it is rolled back. Catching an ``IntegrityError``
from ``create_incident``/``create_notification`` would therefore poison
the REST of that same shared transaction. The real, non-exception-
raising primitive PID §118.C/§6.A.1.A both specify —
``INSERT ... ON CONFLICT ... DO NOTHING RETURNING`` — never raises on a
conflict at all, so it composes safely inside a caller-supplied,
multi-statement transaction. This is why this module uses raw SQL via
``sqlalchemy.text()`` for every create/claim/completion write, while
plain reads still go through the ORM exactly like every sibling
repository.

The fencing protocol (PID §118.E.1, WO §6.A.1.B/C) — reproduced exactly
as specified
------------------------------------------------------------------------
See ``claim_due_or_expired``/``complete_claim``/``cancel_claimed``'s own
docstrings. The SQL below is deliberately as close to PID §118.E.1's own
literal, audited pseudocode as plain SQLAlchemy ``text()`` allows.
"""
from __future__ import annotations

import json
from datetime import datetime
from typing import Any, Mapping, Optional

from sqlalchemy import text
from sqlalchemy.engine import Engine
from sqlalchemy.exc import DataError, SQLAlchemyError
from sqlalchemy.orm import Session

from core import identity
from core.errors import InvalidStateTransitionError, NotFoundError, PersistenceError, ValidationError
from core.timestamps import utc_now
from persistence.postgres.db_errors import is_invalid_uuid_format
from persistence.postgres.mailbox_health_models import MailboxHealthIncidentRow, MailboxHealthNotificationRow
from persistence.postgres.session import get_engine, session_scope
from services.mailbox.health.incident import (
    FAILURE_CLASSES,
    STATUS_OPEN,
    IncidentCreationResult,
    MailboxHealthIncident,
    MailboxHealthIncidentRepository,
    resolve,
)
from services.mailbox.health.notification import (
    PURPOSES,
    STATE_ACCEPTED,
    STATE_RETRYABLE_FAILURE,
    STATE_TERMINAL_FAILURE,
    MailboxHealthNotification,
    MailboxHealthNotificationRepository,
    NotificationCreationResult,
)


def _parse_jsonb(value: Any) -> Optional[dict]:
    """Raw ``text()`` query results are not guaranteed to come back
    already-decoded the same way the ORM's typed ``JSONB`` column does —
    accept either a plain ``dict`` (already decoded) or a JSON string
    (decode it here) so this module never depends on a specific driver
    behaviour for this detail."""
    if value is None:
        return None
    if isinstance(value, (dict, list)):
        return value
    return json.loads(value)


def _run_in_session(engine: Engine, session: Optional[Session], fn):
    """Run ``fn(session)``. If the caller supplied its own ``session``
    (the PID §118.F shared-transaction case), use it directly — commit/
    rollback remain entirely the CALLER's own ``session_scope`` context
    manager's responsibility, never this function's. Otherwise open and
    own a fresh, single-statement transaction."""
    if session is not None:
        return fn(session)
    with session_scope(engine) as owned_session:
        return fn(owned_session)


# ---------------------------------------------------------------------
# Incidents
# ---------------------------------------------------------------------

_INCIDENT_COLUMNS = (
    "incident_id, mailbox_id, failure_class, status, first_observed_at, last_observed_at, "
    "resolved_at, recovery_evidence, metadata"
)


def _row_to_incident(row: MailboxHealthIncidentRow) -> MailboxHealthIncident:
    return MailboxHealthIncident(
        incident_id=row.incident_id,
        mailbox_id=row.mailbox_id,
        failure_class=row.failure_class,
        status=row.status,
        first_observed_at=row.first_observed_at,
        last_observed_at=row.last_observed_at,
        resolved_at=row.resolved_at,
        recovery_evidence=dict(row.recovery_evidence) if row.recovery_evidence is not None else None,
        metadata=dict(row.metadata_),
    )


def _mapping_to_incident(mapping) -> MailboxHealthIncident:
    return MailboxHealthIncident(
        incident_id=str(mapping["incident_id"]),
        mailbox_id=str(mapping["mailbox_id"]),
        failure_class=mapping["failure_class"],
        status=mapping["status"],
        first_observed_at=mapping["first_observed_at"],
        last_observed_at=mapping["last_observed_at"],
        resolved_at=mapping["resolved_at"],
        recovery_evidence=_parse_jsonb(mapping["recovery_evidence"]),
        metadata=_parse_jsonb(mapping["metadata"]) or {},
    )


class PostgresMailboxHealthIncidentRepository(MailboxHealthIncidentRepository):
    """PostgreSQL-backed `MailboxHealthIncidentRepository`."""

    def __init__(self, engine: Optional[Engine] = None) -> None:
        self._engine = engine or get_engine()

    def _select_open(self, session: Session, *, mailbox_id: str, failure_class: str) -> Optional[MailboxHealthIncident]:
        result = session.execute(
            text(
                f"SELECT {_INCIDENT_COLUMNS} FROM mailbox_health_incidents "
                "WHERE mailbox_id = :mailbox_id AND failure_class = :failure_class AND status = 'OPEN'"
            ),
            {"mailbox_id": mailbox_id, "failure_class": failure_class},
        )
        mapping = result.mappings().first()
        return _mapping_to_incident(mapping) if mapping is not None else None

    def create_incident(
        self,
        *,
        mailbox_id: str,
        failure_class: str,
        metadata: Optional[Mapping[str, Any]] = None,
        now: Optional[datetime] = None,
        session: Optional[Session] = None,
    ) -> IncidentCreationResult:
        if failure_class not in FAILURE_CLASSES:
            raise ValidationError(f"'{failure_class}' is not a governed failure_class — must be one of {sorted(FAILURE_CLASSES)}")

        resolved_now = now if now is not None else utc_now()
        candidate_id = identity.generate_id()
        meta = dict(metadata) if metadata is not None else {}

        def _do(s: Session) -> IncidentCreationResult:
            result = s.execute(
                text(
                    """
                    INSERT INTO mailbox_health_incidents
                        (incident_id, mailbox_id, failure_class, status, first_observed_at, last_observed_at,
                         resolved_at, recovery_evidence, metadata)
                    VALUES
                        (:incident_id, :mailbox_id, :failure_class, 'OPEN', :now, :now, NULL, NULL,
                         CAST(:metadata AS JSONB))
                    ON CONFLICT (mailbox_id, failure_class) WHERE status = 'OPEN' DO NOTHING
                    RETURNING incident_id
                    """
                ),
                {
                    "incident_id": candidate_id,
                    "mailbox_id": mailbox_id,
                    "failure_class": failure_class,
                    "now": resolved_now,
                    "metadata": json.dumps(meta),
                },
            )
            if result.first() is not None:
                return IncidentCreationResult(
                    incident=MailboxHealthIncident(
                        incident_id=candidate_id,
                        mailbox_id=mailbox_id,
                        failure_class=failure_class,
                        status=STATUS_OPEN,
                        first_observed_at=resolved_now,
                        last_observed_at=resolved_now,
                        metadata=meta,
                    ),
                    created=True,
                )
            existing = self._select_open(s, mailbox_id=mailbox_id, failure_class=failure_class)
            if existing is None:
                raise PersistenceError(
                    f"ON CONFLICT on mailbox_health_incidents(open) for mailbox_id={mailbox_id!r}, "
                    f"failure_class={failure_class!r} but no winning row could be re-read"
                )
            return IncidentCreationResult(incident=existing, created=False)

        try:
            return _run_in_session(self._engine, session, _do)
        except SQLAlchemyError as exc:
            raise PersistenceError(f"could not create MailboxHealthIncident: {exc}") from exc

    def touch_observed(
        self, incident_id: str, *, observed_at: Optional[datetime] = None, session: Optional[Session] = None
    ) -> MailboxHealthIncident:
        resolved_observed_at = observed_at if observed_at is not None else utc_now()

        def _do(s: Session) -> MailboxHealthIncident:
            row = self._get_row_for_update(s, incident_id)
            row.last_observed_at = resolved_observed_at
            s.flush()
            return _row_to_incident(row)

        try:
            return _run_in_session(self._engine, session, _do)
        except NotFoundError:
            raise
        except SQLAlchemyError as exc:
            raise PersistenceError(f"could not update MailboxHealthIncident last_observed_at: {exc}") from exc

    def resolve_incident(
        self,
        incident_id: str,
        *,
        recovery_evidence: Mapping[str, Any],
        now: Optional[datetime] = None,
        session: Optional[Session] = None,
    ) -> MailboxHealthIncident:
        def _do(s: Session) -> MailboxHealthIncident:
            row = self._get_row_for_update(s, incident_id)
            current = _row_to_incident(row)
            updated = resolve(current, recovery_evidence=recovery_evidence, now=now)
            row.status = updated.status
            row.resolved_at = updated.resolved_at
            row.recovery_evidence = dict(updated.recovery_evidence) if updated.recovery_evidence is not None else None
            s.flush()
            return updated

        try:
            return _run_in_session(self._engine, session, _do)
        except (NotFoundError, InvalidStateTransitionError, ValidationError):
            raise
        except SQLAlchemyError as exc:
            raise PersistenceError(f"could not resolve MailboxHealthIncident: {exc}") from exc

    def _get_row_for_update(self, session: Session, incident_id: str) -> MailboxHealthIncidentRow:
        try:
            row = session.get(MailboxHealthIncidentRow, incident_id, with_for_update=True)
        except DataError as exc:
            if is_invalid_uuid_format(exc):
                raise NotFoundError(f"no MailboxHealthIncident with incident_id '{incident_id}' (malformed identifier)") from exc
            raise
        if row is None:
            raise NotFoundError(f"no MailboxHealthIncident with incident_id '{incident_id}'")
        return row

    def get_incident(self, incident_id: str) -> MailboxHealthIncident:
        try:
            with session_scope(self._engine) as session:
                row = session.get(MailboxHealthIncidentRow, incident_id)
                if row is None:
                    raise NotFoundError(f"no MailboxHealthIncident with incident_id '{incident_id}'")
                return _row_to_incident(row)
        except NotFoundError:
            raise
        except DataError as exc:
            if is_invalid_uuid_format(exc):
                raise NotFoundError(f"no MailboxHealthIncident with incident_id '{incident_id}' (malformed identifier)") from exc
            raise PersistenceError(f"could not read MailboxHealthIncident: {exc}") from exc
        except SQLAlchemyError as exc:
            raise PersistenceError(f"could not read MailboxHealthIncident: {exc}") from exc

    def get_open_incident(self, *, mailbox_id: str, failure_class: str) -> Optional[MailboxHealthIncident]:
        try:
            with session_scope(self._engine) as session:
                return self._select_open(session, mailbox_id=mailbox_id, failure_class=failure_class)
        except DataError as exc:
            if is_invalid_uuid_format(exc):
                return None
            raise PersistenceError(f"could not look up MailboxHealthIncident: {exc}") from exc
        except SQLAlchemyError as exc:
            raise PersistenceError(f"could not look up MailboxHealthIncident: {exc}") from exc

    def list_open_incidents(self) -> list[MailboxHealthIncident]:
        try:
            with session_scope(self._engine) as session:
                rows = session.query(MailboxHealthIncidentRow).filter(MailboxHealthIncidentRow.status == STATUS_OPEN).all()
                return [_row_to_incident(row) for row in rows]
        except SQLAlchemyError as exc:
            raise PersistenceError(f"could not list open MailboxHealthIncident rows: {exc}") from exc


# ---------------------------------------------------------------------
# Notifications
# ---------------------------------------------------------------------

_NOTIFICATION_COLUMNS = (
    "notification_id, incident_id, purpose, state, attempt_count, max_attempts, next_attempt_at, "
    "claimed_by, claimed_at, claim_generation, created_at, completed_at, provider_response_metadata, "
    "cancellation_reason"
)


def _qualified_notification_columns(alias: str) -> str:
    """``_NOTIFICATION_COLUMNS``, each column prefixed with ``alias.`` —
    used only by the claim UPDATE's own ``RETURNING`` clause, which
    updates through a table alias (``t``)."""
    return ", ".join(f"{alias}.{column.strip()}" for column in _NOTIFICATION_COLUMNS.split(","))


def _row_to_notification(row: MailboxHealthNotificationRow) -> MailboxHealthNotification:
    return MailboxHealthNotification(
        notification_id=row.notification_id,
        incident_id=row.incident_id,
        purpose=row.purpose,
        state=row.state,
        attempt_count=row.attempt_count,
        max_attempts=row.max_attempts,
        next_attempt_at=row.next_attempt_at,
        created_at=row.created_at,
        claimed_by=row.claimed_by,
        claimed_at=row.claimed_at,
        claim_generation=row.claim_generation,
        completed_at=row.completed_at,
        provider_response_metadata=dict(row.provider_response_metadata) if row.provider_response_metadata is not None else None,
        cancellation_reason=dict(row.cancellation_reason) if row.cancellation_reason is not None else None,
    )


def _mapping_to_notification(mapping) -> MailboxHealthNotification:
    return MailboxHealthNotification(
        notification_id=str(mapping["notification_id"]),
        incident_id=str(mapping["incident_id"]),
        purpose=mapping["purpose"],
        state=mapping["state"],
        attempt_count=mapping["attempt_count"],
        max_attempts=mapping["max_attempts"],
        next_attempt_at=mapping["next_attempt_at"],
        created_at=mapping["created_at"],
        claimed_by=mapping["claimed_by"],
        claimed_at=mapping["claimed_at"],
        claim_generation=mapping["claim_generation"],
        completed_at=mapping["completed_at"],
        provider_response_metadata=_parse_jsonb(mapping["provider_response_metadata"]),
        cancellation_reason=_parse_jsonb(mapping["cancellation_reason"]),
    )


class PostgresMailboxHealthNotificationRepository(MailboxHealthNotificationRepository):
    """PostgreSQL-backed `MailboxHealthNotificationRepository` — the
    fencing protocol's real, concurrency-safe implementation (see module
    docstring)."""

    def __init__(self, engine: Optional[Engine] = None) -> None:
        self._engine = engine or get_engine()

    def _select_by_incident_purpose(
        self, session: Session, *, incident_id: str, purpose: str
    ) -> Optional[MailboxHealthNotification]:
        result = session.execute(
            text(
                f"SELECT {_NOTIFICATION_COLUMNS} FROM mailbox_health_notifications "
                "WHERE incident_id = :incident_id AND purpose = :purpose"
            ),
            {"incident_id": incident_id, "purpose": purpose},
        )
        mapping = result.mappings().first()
        return _mapping_to_notification(mapping) if mapping is not None else None

    def create_notification(
        self,
        *,
        incident_id: str,
        purpose: str,
        max_attempts: int = 3,
        now: Optional[datetime] = None,
        session: Optional[Session] = None,
    ) -> NotificationCreationResult:
        if purpose not in PURPOSES:
            raise ValidationError(f"'{purpose}' is not a governed purpose — must be one of {sorted(PURPOSES)}")

        resolved_now = now if now is not None else utc_now()
        candidate_id = identity.generate_id()

        def _do(s: Session) -> NotificationCreationResult:
            result = s.execute(
                text(
                    """
                    INSERT INTO mailbox_health_notifications
                        (notification_id, incident_id, purpose, state, attempt_count, max_attempts,
                         next_attempt_at, claimed_by, claimed_at, claim_generation, created_at,
                         completed_at, provider_response_metadata, cancellation_reason)
                    VALUES
                        (:notification_id, :incident_id, :purpose, 'PENDING', 0, :max_attempts, :now,
                         NULL, NULL, 0, :now, NULL, NULL, NULL)
                    ON CONFLICT (incident_id, purpose) DO NOTHING
                    RETURNING notification_id
                    """
                ),
                {
                    "notification_id": candidate_id,
                    "incident_id": incident_id,
                    "purpose": purpose,
                    "max_attempts": max_attempts,
                    "now": resolved_now,
                },
            )
            if result.first() is not None:
                return NotificationCreationResult(
                    notification=MailboxHealthNotification(
                        notification_id=candidate_id,
                        incident_id=incident_id,
                        purpose=purpose,
                        state="PENDING",
                        attempt_count=0,
                        max_attempts=max_attempts,
                        next_attempt_at=resolved_now,
                        created_at=resolved_now,
                    ),
                    created=True,
                )
            existing = self._select_by_incident_purpose(s, incident_id=incident_id, purpose=purpose)
            if existing is None:
                raise PersistenceError(
                    f"ON CONFLICT on mailbox_health_notifications(incident_id, purpose) for "
                    f"incident_id={incident_id!r}, purpose={purpose!r} but no winning row could be re-read"
                )
            return NotificationCreationResult(notification=existing, created=False)

        try:
            return _run_in_session(self._engine, session, _do)
        except SQLAlchemyError as exc:
            raise PersistenceError(f"could not create MailboxHealthNotification: {exc}") from exc

    def claim_due_or_expired(
        self,
        *,
        limit: int,
        claimed_by: str,
        claim_timeout_seconds: float,
        now: Optional[datetime] = None,
    ) -> list[MailboxHealthNotification]:
        if limit <= 0:
            return []
        resolved_now = now if now is not None else utc_now()

        try:
            with session_scope(self._engine) as session:
                # Exhaustion pass FIRST, unconditional (WO §6.A.1.B's
                # corrected boundary: attempt_count >= max_attempts means
                # every permitted claim has already occurred) — a
                # due/expired row with no budget left is terminated
                # directly, never claimed again.
                session.execute(
                    text(
                        """
                        UPDATE mailbox_health_notifications
                        SET state = 'TERMINAL_FAILURE', completed_at = :now
                        WHERE (
                            (state IN ('PENDING', 'RETRYABLE_FAILURE') AND next_attempt_at <= :now)
                            OR (state = 'IN_PROGRESS' AND claimed_at < :now - (interval '1 second' * :claim_timeout_seconds))
                          )
                          AND attempt_count >= max_attempts
                        """
                    ),
                    {"now": resolved_now, "claim_timeout_seconds": claim_timeout_seconds},
                )

                # Claim pass: SKIP LOCKED lets concurrent workers each
                # claim a disjoint subset; the UPDATE...FROM...RETURNING
                # shape is PID §118.E.1's own claim predicate, with the
                # fencing claim_generation bump and the attempt_count
                # increment applied atomically in the SAME statement
                # (WO §6.A.1.B) — never a separate read-then-write.
                result = session.execute(
                    text(
                        f"""
                        UPDATE mailbox_health_notifications AS t
                        SET state = 'IN_PROGRESS',
                            claimed_by = :claimed_by,
                            claimed_at = :now,
                            claim_generation = t.claim_generation + 1,
                            attempt_count = t.attempt_count + 1
                        FROM (
                            SELECT notification_id
                            FROM mailbox_health_notifications
                            WHERE (
                                (state IN ('PENDING', 'RETRYABLE_FAILURE') AND next_attempt_at <= :now)
                                OR (state = 'IN_PROGRESS' AND claimed_at < :now - (interval '1 second' * :claim_timeout_seconds))
                              )
                              AND attempt_count < max_attempts
                            ORDER BY next_attempt_at ASC, notification_id ASC
                            FOR UPDATE SKIP LOCKED
                            LIMIT :limit
                        ) AS sub
                        WHERE t.notification_id = sub.notification_id
                        RETURNING {_qualified_notification_columns("t")}
                        """
                    ),
                    {
                        "claimed_by": claimed_by,
                        "now": resolved_now,
                        "claim_timeout_seconds": claim_timeout_seconds,
                        "limit": limit,
                    },
                )
                return [_mapping_to_notification(mapping) for mapping in result.mappings().all()]
        except SQLAlchemyError as exc:
            raise PersistenceError(f"could not claim MailboxHealthNotification rows: {exc}") from exc

    def complete_claim(
        self,
        notification_id: str,
        *,
        claim_generation: int,
        outcome: str,
        next_attempt_at: Optional[datetime] = None,
        provider_response_metadata: Optional[Mapping[str, Any]] = None,
        now: Optional[datetime] = None,
    ) -> Optional[MailboxHealthNotification]:
        if outcome not in (STATE_ACCEPTED, STATE_RETRYABLE_FAILURE, STATE_TERMINAL_FAILURE):
            raise ValidationError(f"'{outcome}' is not a valid completion outcome")
        if outcome == STATE_RETRYABLE_FAILURE and next_attempt_at is None:
            raise ValidationError("next_attempt_at is required when outcome is RETRYABLE_FAILURE")
        resolved_now = now if now is not None else utc_now()

        try:
            with session_scope(self._engine) as session:
                result = session.execute(
                    text(
                        f"""
                        UPDATE mailbox_health_notifications
                        SET state = CAST(:outcome AS VARCHAR),
                            completed_at = CASE WHEN CAST(:outcome AS VARCHAR) = 'RETRYABLE_FAILURE' THEN completed_at ELSE :now END,
                            next_attempt_at = CASE WHEN CAST(:outcome AS VARCHAR) = 'RETRYABLE_FAILURE' THEN :next_attempt_at ELSE next_attempt_at END,
                            provider_response_metadata = CAST(:provider_response_metadata AS JSONB)
                        WHERE notification_id = :notification_id
                          AND claim_generation = :claim_generation
                          AND state = 'IN_PROGRESS'
                        RETURNING {_NOTIFICATION_COLUMNS}
                        """
                    ),
                    {
                        "outcome": outcome,
                        "now": resolved_now,
                        "next_attempt_at": next_attempt_at,
                        "provider_response_metadata": (
                            json.dumps(dict(provider_response_metadata)) if provider_response_metadata is not None else None
                        ),
                        "notification_id": notification_id,
                        "claim_generation": claim_generation,
                    },
                )
                mapping = result.mappings().first()
                # A stale/superseded worker's write affects zero rows —
                # a correct, SILENT no-op (WO §6.A.1.B), never an error.
                return _mapping_to_notification(mapping) if mapping is not None else None
        except DataError as exc:
            if is_invalid_uuid_format(exc):
                return None
            raise PersistenceError(f"could not complete MailboxHealthNotification claim: {exc}") from exc
        except SQLAlchemyError as exc:
            raise PersistenceError(f"could not complete MailboxHealthNotification claim: {exc}") from exc

    def cancel_claimed(
        self,
        notification_id: str,
        *,
        claim_generation: int,
        cancellation_reason: Mapping[str, Any],
        now: Optional[datetime] = None,
    ) -> Optional[MailboxHealthNotification]:
        resolved_now = now if now is not None else utc_now()
        try:
            with session_scope(self._engine) as session:
                result = session.execute(
                    text(
                        f"""
                        UPDATE mailbox_health_notifications
                        SET state = 'CANCELLED', completed_at = :now, cancellation_reason = CAST(:reason AS JSONB)
                        WHERE notification_id = :notification_id
                          AND claim_generation = :claim_generation
                          AND state = 'IN_PROGRESS'
                        RETURNING {_NOTIFICATION_COLUMNS}
                        """
                    ),
                    {
                        "now": resolved_now,
                        "reason": json.dumps(dict(cancellation_reason)),
                        "notification_id": notification_id,
                        "claim_generation": claim_generation,
                    },
                )
                mapping = result.mappings().first()
                return _mapping_to_notification(mapping) if mapping is not None else None
        except DataError as exc:
            if is_invalid_uuid_format(exc):
                return None
            raise PersistenceError(f"could not cancel MailboxHealthNotification claim: {exc}") from exc
        except SQLAlchemyError as exc:
            raise PersistenceError(f"could not cancel MailboxHealthNotification claim: {exc}") from exc

    def get_notification(self, notification_id: str) -> MailboxHealthNotification:
        try:
            with session_scope(self._engine) as session:
                row = session.get(MailboxHealthNotificationRow, notification_id)
                if row is None:
                    raise NotFoundError(f"no MailboxHealthNotification with notification_id '{notification_id}'")
                return _row_to_notification(row)
        except NotFoundError:
            raise
        except DataError as exc:
            if is_invalid_uuid_format(exc):
                raise NotFoundError(
                    f"no MailboxHealthNotification with notification_id '{notification_id}' (malformed identifier)"
                ) from exc
            raise PersistenceError(f"could not read MailboxHealthNotification: {exc}") from exc
        except SQLAlchemyError as exc:
            raise PersistenceError(f"could not read MailboxHealthNotification: {exc}") from exc

    def list_by_incident(self, incident_id: str) -> list[MailboxHealthNotification]:
        try:
            with session_scope(self._engine) as session:
                rows = (
                    session.query(MailboxHealthNotificationRow)
                    .filter(MailboxHealthNotificationRow.incident_id == incident_id)
                    .all()
                )
                return [_row_to_notification(row) for row in rows]
        except SQLAlchemyError as exc:
            raise PersistenceError(f"could not list MailboxHealthNotification rows by incident: {exc}") from exc

    def get_latest_completed(self, incident_id: str) -> Optional[MailboxHealthNotification]:
        try:
            with session_scope(self._engine) as session:
                row = (
                    session.query(MailboxHealthNotificationRow)
                    .filter(
                        MailboxHealthNotificationRow.incident_id == incident_id,
                        MailboxHealthNotificationRow.completed_at.isnot(None),
                    )
                    .order_by(MailboxHealthNotificationRow.completed_at.desc(), MailboxHealthNotificationRow.notification_id.desc())
                    .first()
                )
                return _row_to_notification(row) if row is not None else None
        except SQLAlchemyError as exc:
            raise PersistenceError(f"could not look up latest completed MailboxHealthNotification: {exc}") from exc
