"""PostgreSQL-backed implementations of every ``services.xero``
repository abstraction (CD-6 Slice 2, PID §98.4).

Follows the exact same "stateless, fresh `Session` per call, translate
constraint violations to the matching canonical error, never let a raw
SQLAlchemy/psycopg exception escape" discipline every other
``persistence/postgres/*_repository.py`` module in this codebase
already establishes — see ``persistence/postgres/needs_you_repository.py``
for the closest structural precedent (same partial-unique-index /
"application check first, database constraint is the real proof under
a race" pattern, applied here to `entity_id`/`tenant_id`/
`(tenant_id, account_id)`).
"""
from __future__ import annotations

import dataclasses
from datetime import datetime
from typing import Optional, Sequence

from sqlalchemy.engine import Engine
from sqlalchemy.exc import DataError, IntegrityError, SQLAlchemyError

from core import identity
from core.contract_validation import validate_against_contract
from core.errors import (
    ConflictError,
    InvalidStateTransitionError,
    NotFoundError,
    OAuthStateError,
    PersistenceError,
    ValidationError,
)
from core.timestamps import utc_now
from persistence.postgres.db_errors import is_invalid_uuid_format, unique_violation_constraint
from persistence.postgres.session import get_engine, session_scope
from persistence.postgres.xero_models import (
    XeroAccountRow,
    XeroConnectionRow,
    XeroOAuthStateRow,
    XeroSyncRunRow,
)
from services.xero.account import RawXeroAccount, XeroAccount, XeroAccountRepository
from services.xero.connection import (
    PROVIDER_XERO,
    XeroConnection,
    XeroConnectionRepository,
    transition as connection_transition,
)
from services.xero.oauth_state import OAuthState, OAuthStateRepository, STATE_TTL_SECONDS, generate_state_value
from services.xero.sync import XeroSyncRun, XeroSyncRunRepository, transition as sync_run_transition

#: Contract paths and constraint names duplicated here as local literal
#: constants (never imported as a "private" `_SCHEMA` name from the
#: owning domain module) — the same convention
#: `persistence/postgres/needs_you_repository.py`'s own
#: `_DEDUPE_CONSTRAINT` local constant already establishes.
_CONNECTION_SCHEMA = "xero/bagman.xero_connection.v1.schema.json"
_ACCOUNT_SCHEMA = "xero/bagman.xero_account.v1.schema.json"
_SYNC_RUN_SCHEMA = "xero/bagman.xero_sync_run.v1.schema.json"

_CONNECTION_DEDUPE_ENTITY_CONSTRAINT = "uq_xero_connections_entity_id"
_CONNECTION_DEDUPE_TENANT_CONSTRAINT = "uq_xero_connections_tenant_id"
_ACCOUNT_DEDUPE_CONSTRAINT = "uq_xero_accounts_tenant_account"


# ---------------------------------------------------------------------
# XeroConnection
# ---------------------------------------------------------------------


def _connection_row_to_domain(row: XeroConnectionRow) -> XeroConnection:
    return XeroConnection(
        xero_connection_id=row.xero_connection_id,
        entity_id=row.entity_id,
        provider=row.provider,
        tenant_id=row.tenant_id,
        tenant_name=row.tenant_name,
        status=row.status,
        connected_at=row.connected_at,
        last_successful_sync_at=row.last_successful_sync_at,
        last_attempted_sync_at=row.last_attempted_sync_at,
        token_expires_at=row.token_expires_at,
        created_at=row.created_at,
        updated_at=row.updated_at,
        error_detail=row.error_detail,
        metadata=dict(row.metadata_),
    )


def _apply_connection_to_row(row: XeroConnectionRow, connection: XeroConnection) -> None:
    row.tenant_id = connection.tenant_id
    row.tenant_name = connection.tenant_name
    row.status = connection.status
    row.connected_at = connection.connected_at
    row.last_successful_sync_at = connection.last_successful_sync_at
    row.last_attempted_sync_at = connection.last_attempted_sync_at
    row.token_expires_at = connection.token_expires_at
    row.updated_at = connection.updated_at
    row.error_detail = connection.error_detail
    row.metadata_ = dict(connection.metadata)


class PostgresXeroConnectionRepository(XeroConnectionRepository):
    """PostgreSQL-backed ``XeroConnectionRepository``."""

    def __init__(self, engine: Optional[Engine] = None) -> None:
        self._engine = engine or get_engine()

    def begin_connect(self, *, entity_id: str) -> XeroConnection:
        try:
            with session_scope(self._engine) as session:
                row = session.query(XeroConnectionRow).filter_by(entity_id=entity_id).with_for_update().one_or_none()
                if row is None:
                    now = utc_now()
                    candidate = XeroConnection(
                        xero_connection_id=identity.generate_id(),
                        entity_id=entity_id,
                        provider=PROVIDER_XERO,
                        tenant_id=None,
                        tenant_name=None,
                        status="PENDING",
                        connected_at=None,
                        last_successful_sync_at=None,
                        last_attempted_sync_at=None,
                        token_expires_at=None,
                        created_at=now,
                        updated_at=now,
                        error_detail=None,
                        metadata={},
                    )
                    validate_against_contract(candidate.to_dict(), _CONNECTION_SCHEMA)
                    new_row = XeroConnectionRow(
                        xero_connection_id=candidate.xero_connection_id,
                        entity_id=candidate.entity_id,
                        provider=candidate.provider,
                        created_at=candidate.created_at,
                        updated_at=candidate.updated_at,
                        status=candidate.status,
                        metadata_={},
                    )
                    session.add(new_row)
                    return candidate

                current = _connection_row_to_domain(row)
                if current.status == "PENDING":
                    return current
                updated = connection_transition(current, "PENDING")
                _apply_connection_to_row(row, updated)
                return updated
        except IntegrityError as exc:
            if unique_violation_constraint(exc) == _CONNECTION_DEDUPE_ENTITY_CONSTRAINT:
                # Lost a genuine concurrent-first-connect race (see this
                # method's own docstring note above) — a second
                # transaction won the INSERT; re-read and hand back
                # WHATEVER row now exists rather than erroring, exactly
                # the same "constraint is the real proof under a race"
                # recovery `persistence.postgres.needs_you_repository`
                # already documents for its own dedupe constraint.
                existing = self.get_by_entity(entity_id)
                if existing is not None:
                    return existing
            raise PersistenceError(f"could not begin XeroConnection connect: {exc}") from exc
        except SQLAlchemyError as exc:
            raise PersistenceError(f"could not begin XeroConnection connect: {exc}") from exc

    def complete_connect(
        self,
        xero_connection_id: str,
        *,
        tenant_id: str,
        tenant_name: Optional[str],
        token_expires_at: Optional[datetime],
    ) -> XeroConnection:
        try:
            with session_scope(self._engine) as session:
                row = self._locked_row(session, xero_connection_id)
                current = _connection_row_to_domain(row)
                updated = connection_transition(
                    current,
                    "CONNECTED",
                    tenant_id=tenant_id,
                    tenant_name=tenant_name,
                    token_expires_at=token_expires_at,
                    error_detail=None,
                )
                _apply_connection_to_row(row, updated)
        except IntegrityError as exc:
            if unique_violation_constraint(exc) == _CONNECTION_DEDUPE_TENANT_CONSTRAINT:
                existing = self.get_by_tenant(tenant_id)
                other = f" ('{existing.entity_id}')" if existing is not None else ""
                raise ConflictError(
                    f"Xero tenant '{tenant_id}' is already connected to a different BAGMAN "
                    f"entity{other} — refusing to map the same Xero organisation to a second "
                    "BAGMAN company (architect spec §17)"
                ) from exc
            raise PersistenceError(f"could not complete XeroConnection connect: {exc}") from exc
        except (NotFoundError, InvalidStateTransitionError, ValidationError):
            raise
        except SQLAlchemyError as exc:
            raise PersistenceError(f"could not complete XeroConnection connect: {exc}") from exc
        return updated

    def _locked_row(self, session, xero_connection_id: str) -> XeroConnectionRow:
        try:
            row = session.get(XeroConnectionRow, xero_connection_id, with_for_update=True)
        except DataError as exc:
            if is_invalid_uuid_format(exc):
                raise NotFoundError(
                    f"no XeroConnection with xero_connection_id '{xero_connection_id}' "
                    "(malformed identifier can never exist)"
                ) from exc
            raise
        if row is None:
            raise NotFoundError(f"no XeroConnection with xero_connection_id '{xero_connection_id}'")
        return row

    def _simple_transition(self, xero_connection_id: str, new_status: str, **field_updates) -> XeroConnection:
        try:
            with session_scope(self._engine) as session:
                row = self._locked_row(session, xero_connection_id)
                current = _connection_row_to_domain(row)
                updated = connection_transition(current, new_status, **field_updates)
                _apply_connection_to_row(row, updated)
        except (NotFoundError, InvalidStateTransitionError, ValidationError):
            raise
        except SQLAlchemyError as exc:
            raise PersistenceError(f"could not transition XeroConnection: {exc}") from exc
        return updated

    def fail_connect(self, xero_connection_id: str, *, error_detail: str) -> XeroConnection:
        return self._simple_transition(xero_connection_id, "ERROR", error_detail=error_detail)

    def disconnect(self, xero_connection_id: str) -> XeroConnection:
        return self._simple_transition(xero_connection_id, "DISCONNECTED", error_detail=None)

    def fail_refresh(self, xero_connection_id: str, *, error_detail: str) -> XeroConnection:
        return self._simple_transition(xero_connection_id, "ERROR", error_detail=error_detail)

    def fail_auth_revoked(self, xero_connection_id: str, *, error_detail: str) -> XeroConnection:
        return self._simple_transition(xero_connection_id, "REVOKED", error_detail=error_detail)

    def record_sync_attempt(self, xero_connection_id: str) -> XeroConnection:
        try:
            with session_scope(self._engine) as session:
                row = self._locked_row(session, xero_connection_id)
                current = _connection_row_to_domain(row)
                now = utc_now()
                updated = dataclasses.replace(current, last_attempted_sync_at=now, updated_at=now)
                validate_against_contract(updated.to_dict(), _CONNECTION_SCHEMA)
                _apply_connection_to_row(row, updated)
        except NotFoundError:
            raise
        except SQLAlchemyError as exc:
            raise PersistenceError(f"could not record XeroConnection sync attempt: {exc}") from exc
        return updated

    def record_sync_success(
        self,
        xero_connection_id: str,
        *,
        tenant_name: Optional[str],
        token_expires_at: Optional[datetime],
    ) -> XeroConnection:
        try:
            with session_scope(self._engine) as session:
                row = self._locked_row(session, xero_connection_id)
                current = _connection_row_to_domain(row)
                now = utc_now()
                if current.status == "ERROR":
                    updated = connection_transition(
                        current,
                        "CONNECTED",
                        tenant_name=tenant_name if tenant_name is not None else current.tenant_name,
                        token_expires_at=token_expires_at,
                        last_successful_sync_at=now,
                        last_attempted_sync_at=now,
                        error_detail=None,
                    )
                else:
                    updated = dataclasses.replace(
                        current,
                        tenant_name=tenant_name if tenant_name is not None else current.tenant_name,
                        token_expires_at=token_expires_at,
                        last_successful_sync_at=now,
                        last_attempted_sync_at=now,
                        updated_at=now,
                    )
                    validate_against_contract(updated.to_dict(), _CONNECTION_SCHEMA)
                _apply_connection_to_row(row, updated)
        except (NotFoundError, InvalidStateTransitionError, ValidationError):
            raise
        except SQLAlchemyError as exc:
            raise PersistenceError(f"could not record XeroConnection sync success: {exc}") from exc
        return updated

    def get_connection(self, xero_connection_id: str) -> XeroConnection:
        try:
            with session_scope(self._engine) as session:
                row = session.get(XeroConnectionRow, xero_connection_id)
                if row is None:
                    raise NotFoundError(f"no XeroConnection with xero_connection_id '{xero_connection_id}'")
                return _connection_row_to_domain(row)
        except NotFoundError:
            raise
        except DataError as exc:
            if is_invalid_uuid_format(exc):
                raise NotFoundError(
                    f"no XeroConnection with xero_connection_id '{xero_connection_id}' "
                    "(malformed identifier can never exist)"
                ) from exc
            raise PersistenceError(f"could not read XeroConnection: {exc}") from exc
        except SQLAlchemyError as exc:
            raise PersistenceError(f"could not read XeroConnection: {exc}") from exc

    def get_by_entity(self, entity_id: str) -> Optional[XeroConnection]:
        try:
            with session_scope(self._engine) as session:
                row = session.query(XeroConnectionRow).filter_by(entity_id=entity_id).one_or_none()
                return _connection_row_to_domain(row) if row is not None else None
        except DataError as exc:
            if is_invalid_uuid_format(exc):
                return None
            raise PersistenceError(f"could not look up XeroConnection by entity_id: {exc}") from exc
        except SQLAlchemyError as exc:
            raise PersistenceError(f"could not look up XeroConnection by entity_id: {exc}") from exc

    def get_by_tenant(self, tenant_id: str) -> Optional[XeroConnection]:
        try:
            with session_scope(self._engine) as session:
                row = session.query(XeroConnectionRow).filter_by(tenant_id=tenant_id).one_or_none()
                return _connection_row_to_domain(row) if row is not None else None
        except SQLAlchemyError as exc:
            raise PersistenceError(f"could not look up XeroConnection by tenant_id: {exc}") from exc

    def list_connections(self) -> list[XeroConnection]:
        try:
            with session_scope(self._engine) as session:
                rows = session.query(XeroConnectionRow).order_by(XeroConnectionRow.created_at).all()
                return [_connection_row_to_domain(r) for r in rows]
        except SQLAlchemyError as exc:
            raise PersistenceError(f"could not list XeroConnection rows: {exc}") from exc


# ---------------------------------------------------------------------
# XeroAccount
# ---------------------------------------------------------------------


def _account_row_to_domain(row: XeroAccountRow) -> XeroAccount:
    return XeroAccount(
        xero_account_row_id=row.xero_account_row_id,
        entity_id=row.entity_id,
        tenant_id=row.tenant_id,
        account_id=row.account_id,
        code=row.code,
        name=row.name,
        type=row.type,
        account_class=row.account_class,
        tax_type=row.tax_type,
        status=row.status,
        show_in_expense_claims=row.show_in_expense_claims,
        reporting_code=row.reporting_code,
        reporting_code_name=row.reporting_code_name,
        source_updated_date_utc=row.source_updated_date_utc,
        first_synced_at=row.first_synced_at,
        last_synced_at=row.last_synced_at,
        last_sync_run_id=row.last_sync_run_id,
    )


class PostgresXeroAccountRepository(XeroAccountRepository):
    """PostgreSQL-backed ``XeroAccountRepository``."""

    def __init__(self, engine: Optional[Engine] = None) -> None:
        self._engine = engine or get_engine()

    def upsert_account(
        self,
        *,
        entity_id: str,
        tenant_id: str,
        raw: RawXeroAccount,
        sync_run_id: str,
    ) -> XeroAccount:
        self.upsert_accounts(entity_id=entity_id, tenant_id=tenant_id, raws=[raw], sync_run_id=sync_run_id)
        result = self.get_by_external_id(tenant_id=tenant_id, account_id=raw.account_id)
        assert result is not None  # the upsert above guarantees this row now exists
        return result

    def upsert_accounts(
        self,
        *,
        entity_id: str,
        tenant_id: str,
        raws: Sequence[RawXeroAccount],
        sync_run_id: str,
    ) -> tuple[int, int]:
        """ONE database transaction for the whole batch (see
        `services.xero.account.XeroAccountRepository.upsert_accounts`'s
        own ABC docstring for the atomicity guarantee this gives): if
        anything raises partway through, `session_scope`'s own rollback
        (`persistence/postgres/session.py`) discards EVERY upsert
        attempted so far in this call — the previous sync's rows are
        never touched, and none of this run's rows are left half-
        written either."""
        now = utc_now()
        created = 0
        updated = 0
        try:
            with session_scope(self._engine) as session:
                for raw in raws:
                    row = (
                        session.query(XeroAccountRow)
                        .filter_by(tenant_id=tenant_id, account_id=raw.account_id)
                        .with_for_update()
                        .one_or_none()
                    )
                    if row is not None:
                        candidate = XeroAccount(
                            xero_account_row_id=row.xero_account_row_id,
                            entity_id=entity_id,
                            tenant_id=tenant_id,
                            account_id=raw.account_id,
                            code=raw.code,
                            name=raw.name,
                            type=raw.type,
                            account_class=raw.account_class,
                            tax_type=raw.tax_type,
                            status=raw.status,
                            show_in_expense_claims=raw.show_in_expense_claims,
                            reporting_code=raw.reporting_code,
                            reporting_code_name=raw.reporting_code_name,
                            source_updated_date_utc=raw.updated_date_utc,
                            first_synced_at=row.first_synced_at,
                            last_synced_at=now,
                            last_sync_run_id=sync_run_id,
                        )
                        validate_against_contract(candidate.to_dict(), _ACCOUNT_SCHEMA)
                        row.entity_id = entity_id
                        row.code = raw.code
                        row.name = raw.name
                        row.type = raw.type
                        row.account_class = raw.account_class
                        row.tax_type = raw.tax_type
                        row.status = raw.status
                        row.show_in_expense_claims = raw.show_in_expense_claims
                        row.reporting_code = raw.reporting_code
                        row.reporting_code_name = raw.reporting_code_name
                        row.source_updated_date_utc = raw.updated_date_utc
                        row.last_synced_at = now
                        row.last_sync_run_id = sync_run_id
                        updated += 1
                    else:
                        candidate = XeroAccount(
                            xero_account_row_id=identity.generate_id(),
                            entity_id=entity_id,
                            tenant_id=tenant_id,
                            account_id=raw.account_id,
                            code=raw.code,
                            name=raw.name,
                            type=raw.type,
                            account_class=raw.account_class,
                            tax_type=raw.tax_type,
                            status=raw.status,
                            show_in_expense_claims=raw.show_in_expense_claims,
                            reporting_code=raw.reporting_code,
                            reporting_code_name=raw.reporting_code_name,
                            source_updated_date_utc=raw.updated_date_utc,
                            first_synced_at=now,
                            last_synced_at=now,
                            last_sync_run_id=sync_run_id,
                        )
                        validate_against_contract(candidate.to_dict(), _ACCOUNT_SCHEMA)
                        session.add(
                            XeroAccountRow(
                                xero_account_row_id=candidate.xero_account_row_id,
                                entity_id=entity_id,
                                tenant_id=tenant_id,
                                account_id=raw.account_id,
                                code=raw.code,
                                name=raw.name,
                                type=raw.type,
                                account_class=raw.account_class,
                                tax_type=raw.tax_type,
                                status=raw.status,
                                show_in_expense_claims=raw.show_in_expense_claims,
                                reporting_code=raw.reporting_code,
                                reporting_code_name=raw.reporting_code_name,
                                source_updated_date_utc=raw.updated_date_utc,
                                first_synced_at=now,
                                last_synced_at=now,
                                last_sync_run_id=sync_run_id,
                            )
                        )
                        created += 1
        except IntegrityError as exc:
            raise PersistenceError(f"could not upsert XeroAccount batch: {exc}") from exc
        except ValidationError:
            raise
        except SQLAlchemyError as exc:
            raise PersistenceError(f"could not upsert XeroAccount batch: {exc}") from exc
        return created, updated

    def get_account(self, xero_account_row_id: str) -> XeroAccount:
        try:
            with session_scope(self._engine) as session:
                row = session.get(XeroAccountRow, xero_account_row_id)
                if row is None:
                    raise NotFoundError(f"no XeroAccount with xero_account_row_id '{xero_account_row_id}'")
                return _account_row_to_domain(row)
        except NotFoundError:
            raise
        except DataError as exc:
            if is_invalid_uuid_format(exc):
                raise NotFoundError(
                    f"no XeroAccount with xero_account_row_id '{xero_account_row_id}' "
                    "(malformed identifier can never exist)"
                ) from exc
            raise PersistenceError(f"could not read XeroAccount: {exc}") from exc
        except SQLAlchemyError as exc:
            raise PersistenceError(f"could not read XeroAccount: {exc}") from exc

    def get_by_external_id(self, *, tenant_id: str, account_id: str) -> Optional[XeroAccount]:
        try:
            with session_scope(self._engine) as session:
                row = (
                    session.query(XeroAccountRow)
                    .filter_by(tenant_id=tenant_id, account_id=account_id)
                    .one_or_none()
                )
                return _account_row_to_domain(row) if row is not None else None
        except SQLAlchemyError as exc:
            raise PersistenceError(f"could not look up XeroAccount by external id: {exc}") from exc

    def list_accounts(self, *, entity_id: str) -> list[XeroAccount]:
        try:
            with session_scope(self._engine) as session:
                rows = (
                    session.query(XeroAccountRow)
                    .filter_by(entity_id=entity_id)
                    .order_by(XeroAccountRow.code, XeroAccountRow.name)
                    .all()
                )
                return [_account_row_to_domain(r) for r in rows]
        except DataError as exc:
            if is_invalid_uuid_format(exc):
                return []
            raise PersistenceError(f"could not list XeroAccount rows: {exc}") from exc
        except SQLAlchemyError as exc:
            raise PersistenceError(f"could not list XeroAccount rows: {exc}") from exc


# ---------------------------------------------------------------------
# XeroSyncRun
# ---------------------------------------------------------------------


def _sync_run_row_to_domain(row: XeroSyncRunRow) -> XeroSyncRun:
    return XeroSyncRun(
        sync_run_id=row.sync_run_id,
        entity_id=row.entity_id,
        tenant_id=row.tenant_id,
        status=row.status,
        started_at=row.started_at,
        completed_at=row.completed_at,
        accounts_seen_count=row.accounts_seen_count,
        accounts_created_count=row.accounts_created_count,
        accounts_updated_count=row.accounts_updated_count,
        error_code=row.error_code,
        error_detail=row.error_detail,
    )


class PostgresXeroSyncRunRepository(XeroSyncRunRepository):
    """PostgreSQL-backed ``XeroSyncRunRepository``."""

    def __init__(self, engine: Optional[Engine] = None) -> None:
        self._engine = engine or get_engine()

    def create_run(self, *, entity_id: str, tenant_id: str) -> XeroSyncRun:
        try:
            candidate = XeroSyncRun(
                sync_run_id=identity.generate_id(),
                entity_id=entity_id,
                tenant_id=tenant_id,
                status="RUNNING",
                started_at=utc_now(),
            )
            validate_against_contract(candidate.to_dict(), _SYNC_RUN_SCHEMA)
        except ValidationError:
            raise
        except Exception as exc:  # noqa: BLE001 - never leak a raw exception
            raise ValidationError(f"could not create XeroSyncRun: {exc}") from exc

        row = XeroSyncRunRow(
            sync_run_id=candidate.sync_run_id,
            entity_id=candidate.entity_id,
            tenant_id=candidate.tenant_id,
            status=candidate.status,
            started_at=candidate.started_at,
        )
        try:
            with session_scope(self._engine) as session:
                session.add(row)
        except SQLAlchemyError as exc:
            raise PersistenceError(f"could not create XeroSyncRun: {exc}") from exc
        return candidate

    def _terminal_transition(self, sync_run_id: str, new_status: str, **field_updates) -> XeroSyncRun:
        try:
            with session_scope(self._engine) as session:
                try:
                    row = session.get(XeroSyncRunRow, sync_run_id, with_for_update=True)
                except DataError as exc:
                    if is_invalid_uuid_format(exc):
                        raise NotFoundError(
                            f"no XeroSyncRun with sync_run_id '{sync_run_id}' "
                            "(malformed identifier can never exist)"
                        ) from exc
                    raise
                if row is None:
                    raise NotFoundError(f"no XeroSyncRun with sync_run_id '{sync_run_id}'")

                current = _sync_run_row_to_domain(row)
                updated = sync_run_transition(current, new_status, **field_updates)

                row.status = updated.status
                row.completed_at = updated.completed_at
                row.accounts_seen_count = updated.accounts_seen_count
                row.accounts_created_count = updated.accounts_created_count
                row.accounts_updated_count = updated.accounts_updated_count
                row.error_code = updated.error_code
                row.error_detail = updated.error_detail
        except (NotFoundError, InvalidStateTransitionError, ValidationError):
            raise
        except SQLAlchemyError as exc:
            raise PersistenceError(f"could not transition XeroSyncRun: {exc}") from exc
        return updated

    def succeed_run(
        self,
        sync_run_id: str,
        *,
        accounts_seen_count: int,
        accounts_created_count: int,
        accounts_updated_count: int,
    ) -> XeroSyncRun:
        return self._terminal_transition(
            sync_run_id,
            "SUCCEEDED",
            accounts_seen_count=accounts_seen_count,
            accounts_created_count=accounts_created_count,
            accounts_updated_count=accounts_updated_count,
        )

    def fail_run(self, sync_run_id: str, *, error_code: str, error_detail: str) -> XeroSyncRun:
        return self._terminal_transition(sync_run_id, "FAILED", error_code=error_code, error_detail=error_detail)

    def get_run(self, sync_run_id: str) -> XeroSyncRun:
        try:
            with session_scope(self._engine) as session:
                row = session.get(XeroSyncRunRow, sync_run_id)
                if row is None:
                    raise NotFoundError(f"no XeroSyncRun with sync_run_id '{sync_run_id}'")
                return _sync_run_row_to_domain(row)
        except NotFoundError:
            raise
        except DataError as exc:
            if is_invalid_uuid_format(exc):
                raise NotFoundError(
                    f"no XeroSyncRun with sync_run_id '{sync_run_id}' (malformed identifier can never exist)"
                ) from exc
            raise PersistenceError(f"could not read XeroSyncRun: {exc}") from exc
        except SQLAlchemyError as exc:
            raise PersistenceError(f"could not read XeroSyncRun: {exc}") from exc

    def list_runs(self, *, entity_id: str, limit: Optional[int] = None) -> list[XeroSyncRun]:
        try:
            with session_scope(self._engine) as session:
                query = (
                    session.query(XeroSyncRunRow)
                    .filter_by(entity_id=entity_id)
                    .order_by(XeroSyncRunRow.started_at.desc())
                )
                if limit is not None:
                    query = query.limit(limit)
                rows = query.all()
                return [_sync_run_row_to_domain(r) for r in rows]
        except DataError as exc:
            if is_invalid_uuid_format(exc):
                return []
            raise PersistenceError(f"could not list XeroSyncRun rows: {exc}") from exc
        except SQLAlchemyError as exc:
            raise PersistenceError(f"could not list XeroSyncRun rows: {exc}") from exc


# ---------------------------------------------------------------------
# OAuthState
# ---------------------------------------------------------------------


def _oauth_state_row_to_domain(row: XeroOAuthStateRow) -> OAuthState:
    return OAuthState(
        state=row.state,
        entity_id=row.entity_id,
        created_at=row.created_at,
        expires_at=row.expires_at,
        consumed_at=row.consumed_at,
    )


class PostgresOAuthStateRepository(OAuthStateRepository):
    """PostgreSQL-backed ``OAuthStateRepository``."""

    def __init__(self, engine: Optional[Engine] = None) -> None:
        self._engine = engine or get_engine()

    def create_state(self, *, entity_id: str) -> OAuthState:
        from datetime import timedelta

        now = utc_now()
        candidate = OAuthState(
            state=generate_state_value(),
            entity_id=entity_id,
            created_at=now,
            expires_at=now + timedelta(seconds=STATE_TTL_SECONDS),
            consumed_at=None,
        )
        row = XeroOAuthStateRow(
            state=candidate.state,
            entity_id=candidate.entity_id,
            created_at=candidate.created_at,
            expires_at=candidate.expires_at,
        )
        try:
            with session_scope(self._engine) as session:
                session.add(row)
        except SQLAlchemyError as exc:
            raise PersistenceError(f"could not create OAuthState: {exc}") from exc
        return candidate

    def get_state(self, state: str) -> Optional[OAuthState]:
        try:
            with session_scope(self._engine) as session:
                row = session.get(XeroOAuthStateRow, state)
                return _oauth_state_row_to_domain(row) if row is not None else None
        except SQLAlchemyError as exc:
            raise PersistenceError(f"could not read OAuthState: {exc}") from exc

    def mark_consumed(self, state: str, *, now: Optional[datetime] = None) -> OAuthState:
        resolved_now = now if now is not None else utc_now()
        try:
            with session_scope(self._engine) as session:
                row = session.get(XeroOAuthStateRow, state, with_for_update=True)
                if row is None:
                    raise NotFoundError(f"no OAuthState with state '{state}'")
                # Re-validated INSIDE the lock — see this method's own
                # abstract docstring (services/xero/oauth_state.py) for
                # why the caller's earlier, unlocked pre-check in
                # consume_state() is not sufficient on its own: two
                # near-simultaneous callbacks presenting the same
                # `state` could both pass that pre-check before either
                # acquires this lock.
                if row.consumed_at is not None:
                    raise OAuthStateError(
                        f"OAuth state value was already consumed at {row.consumed_at.isoformat()} — "
                        "refusing a replayed callback"
                    )
                if resolved_now > row.expires_at:
                    raise OAuthStateError(
                        f"OAuth state value expired at {row.expires_at.isoformat()} "
                        f"(now {resolved_now.isoformat()}) — refusing a stale callback"
                    )
                row.consumed_at = resolved_now
                return _oauth_state_row_to_domain(row)
        except (NotFoundError, OAuthStateError):
            raise
        except SQLAlchemyError as exc:
            raise PersistenceError(f"could not consume OAuthState: {exc}") from exc
