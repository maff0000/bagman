"""PostgreSQL-backed implementation of
``services.mailbox.mailbox.MailboxSourceRepository`` (CD-6 Slice 3:
Mailbox Management).

Follows the exact same "stateless, fresh `Session` per call, translate
constraint violations to the matching canonical error, never let a raw
SQLAlchemy/psycopg exception escape" discipline every other
``persistence/postgres/*_repository.py`` module in this codebase
already establishes — see ``persistence/postgres/xero_repository.py``
for the closest structural precedent (same "app-level check + DB
constraint is the real proof under a race" pattern, applied here to
``email_address``, and the same ``with_for_update()`` row-locking
discipline for every lifecycle transition).
"""
from __future__ import annotations

import dataclasses
from typing import Mapping, Optional, Any

from sqlalchemy.engine import Engine
from sqlalchemy.exc import DataError, IntegrityError, SQLAlchemyError

from core import identity
from core.contract_validation import validate_against_contract
from core.errors import ConflictError, InvalidStateTransitionError, NotFoundError, PersistenceError, ValidationError
from core.timestamps import utc_now
from persistence.postgres.db_errors import is_invalid_uuid_format, unique_violation_constraint
from persistence.postgres.mailbox_models import MailboxSourceRow
from persistence.postgres.session import get_engine, session_scope
from services.mailbox.mailbox import (
    CONNECTION_STATE_NOT_CONFIGURED,
    MailboxSource,
    MailboxSourceRepository,
    normalize_email,
    transition as mailbox_transition,
    validate_email_or_raise,
    validate_provider_kind_or_raise,
)

_SCHEMA = "mailbox/bagman.mailbox_source.v1.schema.json"

_DEDUPE_EMAIL_CONSTRAINT = "uq_mailbox_sources_email_address"


def _row_to_domain(row: MailboxSourceRow) -> MailboxSource:
    return MailboxSource(
        mailbox_id=row.mailbox_id,
        display_name=row.display_name,
        email_address=row.email_address,
        provider_kind=row.provider_kind,
        default_entity_id=row.default_entity_id,
        enabled=row.enabled,
        status=row.status,
        connection_state=row.connection_state,
        last_connection_check_at=row.last_connection_check_at,
        last_successful_sweep_at=row.last_successful_sweep_at,
        last_error_code=row.last_error_code,
        last_error_detail=row.last_error_detail,
        created_at=row.created_at,
        updated_at=row.updated_at,
        metadata=dict(row.metadata_),
    )


def _apply_to_row(row: MailboxSourceRow, mailbox: MailboxSource) -> None:
    row.display_name = mailbox.display_name
    row.email_address = mailbox.email_address
    row.provider_kind = mailbox.provider_kind
    row.default_entity_id = mailbox.default_entity_id
    row.enabled = mailbox.enabled
    row.status = mailbox.status
    row.connection_state = mailbox.connection_state
    row.last_connection_check_at = mailbox.last_connection_check_at
    row.last_successful_sweep_at = mailbox.last_successful_sweep_at
    row.last_error_code = mailbox.last_error_code
    row.last_error_detail = mailbox.last_error_detail
    row.updated_at = mailbox.updated_at
    row.metadata_ = dict(mailbox.metadata)


class PostgresMailboxSourceRepository(MailboxSourceRepository):
    """PostgreSQL-backed ``MailboxSourceRepository``."""

    def __init__(self, engine: Optional[Engine] = None) -> None:
        self._engine = engine or get_engine()

    def create_mailbox(
        self,
        *,
        display_name: str,
        email_address: str,
        provider_kind: str,
        default_entity_id: Optional[str] = None,
        metadata: Optional[Mapping[str, Any]] = None,
    ) -> MailboxSource:
        validate_email_or_raise(email_address)
        validate_provider_kind_or_raise(provider_kind)
        now = utc_now()
        try:
            candidate = MailboxSource(
                mailbox_id=identity.generate_id(),
                display_name=display_name,
                email_address=normalize_email(email_address),
                provider_kind=provider_kind,
                default_entity_id=default_entity_id,
                enabled=True,
                status="ACTIVE",
                connection_state=CONNECTION_STATE_NOT_CONFIGURED,
                last_connection_check_at=None,
                last_successful_sweep_at=None,
                last_error_code=None,
                last_error_detail=None,
                created_at=now,
                updated_at=now,
                metadata=dict(metadata) if metadata is not None else {},
            )
            validate_against_contract(candidate.to_dict(), _SCHEMA)
        except ValidationError:
            raise
        except Exception as exc:  # noqa: BLE001 - never leak a raw exception
            raise ValidationError(f"could not create MailboxSource: {exc}") from exc

        row = MailboxSourceRow(
            mailbox_id=candidate.mailbox_id,
            display_name=candidate.display_name,
            email_address=candidate.email_address,
            provider_kind=candidate.provider_kind,
            default_entity_id=candidate.default_entity_id,
            enabled=candidate.enabled,
            status=candidate.status,
            connection_state=candidate.connection_state,
            created_at=candidate.created_at,
            updated_at=candidate.updated_at,
            metadata_={},
        )
        try:
            with session_scope(self._engine) as session:
                session.add(row)
        except IntegrityError as exc:
            if unique_violation_constraint(exc) == _DEDUPE_EMAIL_CONSTRAINT:
                raise ConflictError(
                    f"a MailboxSource for '{candidate.email_address}' already exists — email "
                    "uniqueness is global and never freed by retirement (see "
                    "services/mailbox/mailbox.py's own docstring)"
                ) from exc
            raise PersistenceError(f"could not create MailboxSource: {exc}") from exc
        except SQLAlchemyError as exc:
            raise PersistenceError(f"could not create MailboxSource: {exc}") from exc
        return candidate

    def _locked_row(self, session, mailbox_id: str) -> MailboxSourceRow:
        try:
            row = session.get(MailboxSourceRow, mailbox_id, with_for_update=True)
        except DataError as exc:
            if is_invalid_uuid_format(exc):
                raise NotFoundError(
                    f"no MailboxSource with mailbox_id '{mailbox_id}' (malformed identifier can never exist)"
                ) from exc
            raise
        if row is None:
            raise NotFoundError(f"no MailboxSource with mailbox_id '{mailbox_id}'")
        return row

    def update_mailbox(
        self,
        mailbox_id: str,
        *,
        display_name: str,
        email_address: str,
        provider_kind: str,
        default_entity_id: Optional[str],
    ) -> MailboxSource:
        validate_email_or_raise(email_address)
        validate_provider_kind_or_raise(provider_kind)
        normalized = normalize_email(email_address)
        try:
            with session_scope(self._engine) as session:
                row = self._locked_row(session, mailbox_id)
                current = _row_to_domain(row)
                try:
                    updated = dataclasses.replace(
                        current,
                        display_name=display_name,
                        email_address=normalized,
                        provider_kind=provider_kind,
                        default_entity_id=default_entity_id,
                        updated_at=utc_now(),
                    )
                    validate_against_contract(updated.to_dict(), _SCHEMA)
                except ValidationError:
                    raise
                except Exception as exc:  # noqa: BLE001 - never leak a raw exception
                    raise ValidationError(f"could not update MailboxSource: {exc}") from exc
                _apply_to_row(row, updated)
        except IntegrityError as exc:
            if unique_violation_constraint(exc) == _DEDUPE_EMAIL_CONSTRAINT:
                raise ConflictError(
                    f"a MailboxSource for '{normalized}' already exists — cannot reassign this "
                    "email to a different mailbox_id"
                ) from exc
            raise PersistenceError(f"could not update MailboxSource: {exc}") from exc
        except (NotFoundError, ValidationError):
            raise
        except SQLAlchemyError as exc:
            raise PersistenceError(f"could not update MailboxSource: {exc}") from exc
        return updated

    def _simple_transition(self, mailbox_id: str, new_status: str) -> MailboxSource:
        """Idempotent no-op when `mailbox_id` is already in `new_status`
        (a real, live-findable gap this delivery's own Slice 2 caught
        twice in an identical shape — see
        `services.xero.connection.InMemoryXeroConnectionRepository
        .begin_connect`'s own "already PENDING -> return unchanged"
        precedent): the GUI's Enable/Disable toggle and Retire button
        are each guarded to only render for the state they apply to,
        but a second browser tab left open on stale state (or a
        genuine double-click racing this same lock) would otherwise hit
        `InvalidStateTransitionError` -> HTTP 500 for calling
        enable/disable/retire on a mailbox already in that exact
        target state — never a real state-machine violation, just a
        redundant confirmation of the status quo. `RETIRED` is
        included: re-retiring an already-retired mailbox is a safe
        no-op, not a "transition back out of a terminal state" (which
        remains genuinely forbidden — DISABLED/ACTIVE -> RETIRED is a
        real transition; RETIRED -> RETIRED is not a transition at
        all)."""
        try:
            with session_scope(self._engine) as session:
                row = self._locked_row(session, mailbox_id)
                current = _row_to_domain(row)
                if current.status == new_status:
                    return current
                updated = mailbox_transition(current, new_status)
                _apply_to_row(row, updated)
        except (NotFoundError, InvalidStateTransitionError, ValidationError):
            raise
        except SQLAlchemyError as exc:
            raise PersistenceError(f"could not transition MailboxSource: {exc}") from exc
        return updated

    def enable_mailbox(self, mailbox_id: str) -> MailboxSource:
        return self._simple_transition(mailbox_id, "ACTIVE")

    def disable_mailbox(self, mailbox_id: str) -> MailboxSource:
        return self._simple_transition(mailbox_id, "DISABLED")

    def retire_mailbox(self, mailbox_id: str) -> MailboxSource:
        return self._simple_transition(mailbox_id, "RETIRED")

    def get_mailbox(self, mailbox_id: str) -> MailboxSource:
        try:
            with session_scope(self._engine) as session:
                row = session.get(MailboxSourceRow, mailbox_id)
                if row is None:
                    raise NotFoundError(f"no MailboxSource with mailbox_id '{mailbox_id}'")
                return _row_to_domain(row)
        except NotFoundError:
            raise
        except DataError as exc:
            if is_invalid_uuid_format(exc):
                raise NotFoundError(
                    f"no MailboxSource with mailbox_id '{mailbox_id}' (malformed identifier can never exist)"
                ) from exc
            raise PersistenceError(f"could not read MailboxSource: {exc}") from exc
        except SQLAlchemyError as exc:
            raise PersistenceError(f"could not read MailboxSource: {exc}") from exc

    def get_by_email(self, email_address: str) -> Optional[MailboxSource]:
        try:
            with session_scope(self._engine) as session:
                row = (
                    session.query(MailboxSourceRow)
                    .filter_by(email_address=normalize_email(email_address))
                    .one_or_none()
                )
                return _row_to_domain(row) if row is not None else None
        except SQLAlchemyError as exc:
            raise PersistenceError(f"could not look up MailboxSource by email_address: {exc}") from exc

    def list_mailboxes(self) -> list[MailboxSource]:
        try:
            with session_scope(self._engine) as session:
                rows = (
                    session.query(MailboxSourceRow)
                    .order_by(MailboxSourceRow.display_name, MailboxSourceRow.mailbox_id)
                    .all()
                )
                return [_row_to_domain(r) for r in rows]
        except SQLAlchemyError as exc:
            raise PersistenceError(f"could not list MailboxSource rows: {exc}") from exc
