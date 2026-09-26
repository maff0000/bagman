"""PostgreSQL-backed implementations of the four CD-6 Slice 4 Microsoft-
adapter repository abstractions: `MailboxSweepRunRepository`,
`MailboxFolderCursorRepository`, `MailboxSweepLock`, and
`MailboxOAuthStateRepository` (the mailbox-scoped one). Mirrors
`persistence/postgres/mailbox_repository.py`/`xero_repository.py`'s own
"fresh Session per call, with_for_update row-locking, translate
constraint violations, never let a raw SQLAlchemy exception escape"
discipline throughout.
"""
from __future__ import annotations

from datetime import datetime, timedelta
from typing import Mapping, Optional, Sequence

from sqlalchemy.engine import Engine
from sqlalchemy.exc import IntegrityError, SQLAlchemyError

from core.contract_validation import validate_against_contract
from core.errors import InvalidStateTransitionError, NotFoundError, OAuthStateError, PersistenceError, ValidationError
from core.timestamps import utc_now
from persistence.postgres.mailbox_microsoft_models import (
    MailboxFolderCursorRow,
    MailboxMicrosoftOAuthStateRow,
    MailboxSweepLockRow,
    MailboxSweepRunRow,
)
from persistence.postgres.session import get_engine, session_scope
from services.mailbox.cursor import MailboxFolderCursor, MailboxFolderCursorRepository
from services.mailbox.lock import DEFAULT_LEASE_DURATION_SECONDS, MailboxSweepLock, MailboxSweepLockError
from services.mailbox.microsoft.oauth_state import MailboxOAuthState, MailboxOAuthStateRepository
from services.mailbox.sweep_run import MailboxSweepRun, MailboxSweepRunRepository, transition as sweep_run_transition

_SWEEP_RUN_SCHEMA = "mailbox/bagman.mailbox_sweep_run.v1.schema.json"


# ---------------------------------------------------------------------
# MailboxSweepRun
# ---------------------------------------------------------------------


def _sweep_run_row_to_domain(row: MailboxSweepRunRow) -> MailboxSweepRun:
    return MailboxSweepRun(
        sweep_run_id=row.sweep_run_id,
        mailbox_id=row.mailbox_id,
        trigger=row.trigger,
        status=row.status,
        started_at=row.started_at,
        completed_at=row.completed_at,
        folders_attempted=tuple(row.folders_attempted or []),
        messages_seen=row.messages_seen,
        messages_new=row.messages_new,
        evidence_created=row.evidence_created,
        duplicates=row.duplicates,
        quarantined=row.quarantined,
        failures=row.failures,
        unique_sender_domains=row.unique_sender_domains,
        allowed_domain_messages=row.allowed_domain_messages,
        ignored_domain_messages=row.ignored_domain_messages,
        unknown_domain_messages=row.unknown_domain_messages,
        likely_financial_candidates=row.likely_financial_candidates,
        messages_with_attachments=row.messages_with_attachments,
        graph_throttle_retries=row.graph_throttle_retries,
        error_code=row.error_code,
        error_detail=row.error_detail,
    )


class PostgresMailboxSweepRunRepository(MailboxSweepRunRepository):
    def __init__(self, engine: Optional[Engine] = None) -> None:
        self._engine = engine or get_engine()

    def create_run(self, *, mailbox_id: str, trigger: str) -> MailboxSweepRun:
        from core import identity

        try:
            candidate = MailboxSweepRun(
                sweep_run_id=identity.generate_id(), mailbox_id=mailbox_id, trigger=trigger,
                status="RUNNING", started_at=utc_now(),
            )
            validate_against_contract(candidate.to_dict(), _SWEEP_RUN_SCHEMA)
        except ValidationError:
            raise
        except Exception as exc:  # noqa: BLE001
            raise ValidationError(f"could not create MailboxSweepRun: {exc}") from exc

        row = MailboxSweepRunRow(
            sweep_run_id=candidate.sweep_run_id, mailbox_id=candidate.mailbox_id, trigger=candidate.trigger,
            status=candidate.status, started_at=candidate.started_at, folders_attempted=[],
        )
        try:
            with session_scope(self._engine) as session:
                session.add(row)
        except SQLAlchemyError as exc:
            raise PersistenceError(f"could not create MailboxSweepRun: {exc}") from exc
        return candidate

    def complete_run(
        self,
        sweep_run_id: str,
        *,
        new_status: str,
        folders_attempted: Sequence[Mapping[str, str]],
        messages_seen: int,
        messages_new: int,
        evidence_created: int,
        duplicates: int,
        quarantined: int,
        failures: int,
        unique_sender_domains: int = 0,
        allowed_domain_messages: int = 0,
        ignored_domain_messages: int = 0,
        unknown_domain_messages: int = 0,
        likely_financial_candidates: int = 0,
        messages_with_attachments: int = 0,
        graph_throttle_retries: int = 0,
        error_code: Optional[str] = None,
        error_detail: Optional[str] = None,
    ) -> MailboxSweepRun:
        try:
            with session_scope(self._engine) as session:
                row = session.get(MailboxSweepRunRow, sweep_run_id, with_for_update=True)
                if row is None:
                    raise NotFoundError(f"no MailboxSweepRun with sweep_run_id '{sweep_run_id}'")
                current = _sweep_run_row_to_domain(row)
                updated = sweep_run_transition(
                    current, new_status, folders_attempted=tuple(folders_attempted),
                    messages_seen=messages_seen, messages_new=messages_new, evidence_created=evidence_created,
                    duplicates=duplicates, quarantined=quarantined, failures=failures,
                    unique_sender_domains=unique_sender_domains, allowed_domain_messages=allowed_domain_messages,
                    ignored_domain_messages=ignored_domain_messages, unknown_domain_messages=unknown_domain_messages,
                    likely_financial_candidates=likely_financial_candidates,
                    messages_with_attachments=messages_with_attachments,
                    graph_throttle_retries=graph_throttle_retries,
                    error_code=error_code, error_detail=error_detail,
                )
                row.status = updated.status
                row.completed_at = updated.completed_at
                row.folders_attempted = list(updated.folders_attempted)
                row.messages_seen = updated.messages_seen
                row.messages_new = updated.messages_new
                row.evidence_created = updated.evidence_created
                row.duplicates = updated.duplicates
                row.quarantined = updated.quarantined
                row.failures = updated.failures
                row.unique_sender_domains = updated.unique_sender_domains
                row.allowed_domain_messages = updated.allowed_domain_messages
                row.ignored_domain_messages = updated.ignored_domain_messages
                row.unknown_domain_messages = updated.unknown_domain_messages
                row.likely_financial_candidates = updated.likely_financial_candidates
                row.messages_with_attachments = updated.messages_with_attachments
                row.graph_throttle_retries = updated.graph_throttle_retries
                row.error_code = updated.error_code
                row.error_detail = updated.error_detail
        except (NotFoundError, InvalidStateTransitionError, ValidationError):
            raise
        except SQLAlchemyError as exc:
            raise PersistenceError(f"could not complete MailboxSweepRun: {exc}") from exc
        return updated

    def get_run(self, sweep_run_id: str) -> MailboxSweepRun:
        try:
            with session_scope(self._engine) as session:
                row = session.get(MailboxSweepRunRow, sweep_run_id)
                if row is None:
                    raise NotFoundError(f"no MailboxSweepRun with sweep_run_id '{sweep_run_id}'")
                return _sweep_run_row_to_domain(row)
        except NotFoundError:
            raise
        except SQLAlchemyError as exc:
            raise PersistenceError(f"could not read MailboxSweepRun: {exc}") from exc

    def list_runs(self, *, mailbox_id: str, limit: Optional[int] = None) -> list[MailboxSweepRun]:
        try:
            with session_scope(self._engine) as session:
                query = (
                    session.query(MailboxSweepRunRow)
                    .filter_by(mailbox_id=mailbox_id)
                    .order_by(MailboxSweepRunRow.started_at.desc())
                )
                if limit is not None:
                    query = query.limit(limit)
                return [_sweep_run_row_to_domain(r) for r in query.all()]
        except SQLAlchemyError as exc:
            raise PersistenceError(f"could not list MailboxSweepRun rows: {exc}") from exc


# ---------------------------------------------------------------------
# MailboxFolderCursor
# ---------------------------------------------------------------------


class PostgresMailboxFolderCursorRepository(MailboxFolderCursorRepository):
    def __init__(self, engine: Optional[Engine] = None) -> None:
        self._engine = engine or get_engine()

    def get_or_bootstrap(
        self, *, mailbox_id: str, provider_kind: str, folder: str, bootstrap_timestamp: datetime
    ) -> MailboxFolderCursor:
        try:
            with session_scope(self._engine) as session:
                row = session.get(
                    MailboxFolderCursorRow, (mailbox_id, provider_kind, folder), with_for_update=True
                )
                if row is not None:
                    return MailboxFolderCursor(
                        mailbox_id=row.mailbox_id, provider_kind=row.provider_kind, folder=row.folder,
                        delta_link=row.delta_link, bootstrap_timestamp=row.bootstrap_timestamp,
                        created_at=row.created_at, updated_at=row.updated_at,
                    )
                now = utc_now()
                new_row = MailboxFolderCursorRow(
                    mailbox_id=mailbox_id, provider_kind=provider_kind, folder=folder, delta_link=None,
                    bootstrap_timestamp=bootstrap_timestamp, created_at=now, updated_at=now,
                )
                session.add(new_row)
                session.flush()
                return MailboxFolderCursor(
                    mailbox_id=mailbox_id, provider_kind=provider_kind, folder=folder, delta_link=None,
                    bootstrap_timestamp=bootstrap_timestamp, created_at=now, updated_at=now,
                )
        except SQLAlchemyError as exc:
            raise PersistenceError(f"could not resolve MailboxFolderCursor: {exc}") from exc

    def advance_cursor(
        self, *, mailbox_id: str, provider_kind: str, folder: str, delta_link: str
    ) -> MailboxFolderCursor:
        try:
            with session_scope(self._engine) as session:
                row = session.get(
                    MailboxFolderCursorRow, (mailbox_id, provider_kind, folder), with_for_update=True
                )
                if row is None:
                    raise NotFoundError(
                        f"no MailboxFolderCursor for mailbox_id={mailbox_id!r} folder={folder!r} — "
                        "get_or_bootstrap must be called before advance_cursor"
                    )
                row.delta_link = delta_link
                row.updated_at = utc_now()
                return MailboxFolderCursor(
                    mailbox_id=row.mailbox_id, provider_kind=row.provider_kind, folder=row.folder,
                    delta_link=row.delta_link, bootstrap_timestamp=row.bootstrap_timestamp,
                    created_at=row.created_at, updated_at=row.updated_at,
                )
        except NotFoundError:
            raise
        except SQLAlchemyError as exc:
            raise PersistenceError(f"could not advance MailboxFolderCursor: {exc}") from exc


# ---------------------------------------------------------------------
# MailboxSweepLock
# ---------------------------------------------------------------------


class PostgresMailboxSweepLock(MailboxSweepLock):
    """See `services.mailbox.lock`'s own module docstring for why this
    is a dedicated lease table rather than a `with_for_update()` row
    lock on the mailbox's own row."""

    def __init__(self, engine: Optional[Engine] = None) -> None:
        self._engine = engine or get_engine()

    def try_acquire(self, mailbox_id: str, *, lease_seconds: float = DEFAULT_LEASE_DURATION_SECONDS) -> str:
        import uuid as _uuid

        token = _uuid.uuid4().hex
        now = utc_now()
        expires_at = now + timedelta(seconds=lease_seconds)
        try:
            with session_scope(self._engine) as session:
                row = session.get(MailboxSweepLockRow, mailbox_id, with_for_update=True)
                if row is not None:
                    if row.expires_at > now:
                        raise MailboxSweepLockError(
                            f"mailbox '{mailbox_id}' is already being swept (lease held until "
                            f"{row.expires_at.isoformat()})"
                        )
                    # Expired — safely reclaim (see module docstring).
                    row.owner_token = token
                    row.acquired_at = now
                    row.expires_at = expires_at
                    return token
                session.add(
                    MailboxSweepLockRow(mailbox_id=mailbox_id, owner_token=token, acquired_at=now, expires_at=expires_at)
                )
                return token
        except MailboxSweepLockError:
            raise
        except IntegrityError:
            # A genuine race: another process inserted between our read
            # and write — treated identically to "already held".
            raise MailboxSweepLockError(f"mailbox '{mailbox_id}' is already being swept")
        except SQLAlchemyError as exc:
            raise PersistenceError(f"could not acquire MailboxSweepLock: {exc}") from exc

    def release(self, mailbox_id: str, owner_token: str) -> None:
        try:
            with session_scope(self._engine) as session:
                row = session.get(MailboxSweepLockRow, mailbox_id, with_for_update=True)
                if row is not None and row.owner_token == owner_token:
                    session.delete(row)
        except SQLAlchemyError:
            # Release must never itself become a new failure mode on
            # the way out of an already-completed sweep — see
            # `MailboxSweepLock.release`'s own abstract docstring.
            pass


# ---------------------------------------------------------------------
# MailboxOAuthState (mailbox-scoped, Microsoft)
# ---------------------------------------------------------------------


class PostgresMailboxMicrosoftOAuthStateRepository(MailboxOAuthStateRepository):
    def __init__(self, engine: Optional[Engine] = None) -> None:
        self._engine = engine or get_engine()

    def create_state(self, *, mailbox_id: str) -> MailboxOAuthState:
        from services.mailbox.microsoft.oauth_state import STATE_TTL_SECONDS, generate_state_value

        now = utc_now()
        candidate = MailboxOAuthState(
            state=generate_state_value(), mailbox_id=mailbox_id, created_at=now,
            expires_at=now + timedelta(seconds=STATE_TTL_SECONDS), consumed_at=None,
        )
        try:
            with session_scope(self._engine) as session:
                session.add(
                    MailboxMicrosoftOAuthStateRow(
                        state=candidate.state, mailbox_id=candidate.mailbox_id, created_at=candidate.created_at,
                        expires_at=candidate.expires_at, consumed_at=None,
                    )
                )
        except SQLAlchemyError as exc:
            raise PersistenceError(f"could not create MailboxOAuthState: {exc}") from exc
        return candidate

    def get_state(self, state: str) -> Optional[MailboxOAuthState]:
        try:
            with session_scope(self._engine) as session:
                row = session.get(MailboxMicrosoftOAuthStateRow, state)
                if row is None:
                    return None
                return MailboxOAuthState(
                    state=row.state, mailbox_id=row.mailbox_id, created_at=row.created_at,
                    expires_at=row.expires_at, consumed_at=row.consumed_at,
                )
        except SQLAlchemyError as exc:
            raise PersistenceError(f"could not read MailboxOAuthState: {exc}") from exc

    def mark_consumed(self, state: str, *, now: Optional[datetime] = None) -> MailboxOAuthState:
        resolved_now = now if now is not None else utc_now()
        try:
            with session_scope(self._engine) as session:
                row = session.get(MailboxMicrosoftOAuthStateRow, state, with_for_update=True)
                if row is None:
                    raise NotFoundError(f"no MailboxOAuthState with state '{state}'")
                if row.consumed_at is not None:
                    raise OAuthStateError(
                        f"Microsoft OAuth state value was already consumed at {row.consumed_at.isoformat()} — "
                        "refusing a replayed callback"
                    )
                if resolved_now > row.expires_at:
                    raise OAuthStateError(
                        f"Microsoft OAuth state value expired at {row.expires_at.isoformat()} "
                        f"(now {resolved_now.isoformat()}) — refusing a stale callback"
                    )
                row.consumed_at = resolved_now
                return MailboxOAuthState(
                    state=row.state, mailbox_id=row.mailbox_id, created_at=row.created_at,
                    expires_at=row.expires_at, consumed_at=row.consumed_at,
                )
        except (NotFoundError, OAuthStateError):
            raise
        except SQLAlchemyError as exc:
            raise PersistenceError(f"could not consume MailboxOAuthState: {exc}") from exc
