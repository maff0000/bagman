"""``PostgresEntityRepository`` — durable implementation of
``core.entity.EntityRepository`` (CD-3 WI-1, PID §13).

Implements the exact same abstract interface as
``core.entity.InMemoryEntityRepository`` — same method signatures,
same return types, same canonical errors on the same conditions — so a
caller cannot tell which backend it is talking to other than
durability.
"""
from __future__ import annotations

import dataclasses
from typing import Any, Mapping, Optional

from sqlalchemy.engine import Engine
from sqlalchemy.exc import DataError, IntegrityError, SQLAlchemyError

from core import identity
from core.contract_validation import validate_against_contract
from core.entity import EntityRepository, GovernedEntity
from core.errors import ImmutabilityViolationError, NotFoundError, PersistenceError, ValidationError
from core.timestamps import utc_now
from persistence.postgres.db_errors import is_invalid_uuid_format, unique_violation_constraint
from persistence.postgres.models import GovernedEntityRow
from persistence.postgres.session import get_engine, session_scope

_SCHEMA = "entity/bagman.entity.v1.schema.json"


def _row_to_entity(row: GovernedEntityRow) -> GovernedEntity:
    return GovernedEntity(
        entity_id=row.entity_id,
        entity_type=row.entity_type,
        canonical_name=row.canonical_name,
        display_name=row.display_name,
        status=row.status,
        created_at=row.created_at,
        fiscal_year_start_month_day=row.fiscal_year_start_month_day,
        historical_floor_override_at=row.historical_floor_override_at,
        metadata=dict(row.metadata_),
    )


class PostgresEntityRepository(EntityRepository):
    """PostgreSQL-backed ``EntityRepository``. Holds no in-process
    state of its own — every method reads/writes the database directly
    (via a fresh ``Session`` per call), so durability across restarts
    or across a brand-new instance of this class is inherent, not a
    special case."""

    def __init__(self, engine: Optional[Engine] = None) -> None:
        self._engine = engine or get_engine()

    def register_entity(
        self,
        *,
        entity_type: str,
        canonical_name: str,
        display_name: str,
        status: str,
        metadata: Optional[Mapping[str, Any]] = None,
        entity_id: Optional[str] = None,
        fiscal_year_start_month_day: Optional[str] = None,
        historical_floor_override_at=None,
    ) -> GovernedEntity:
        resolved_id = entity_id if entity_id is not None else identity.generate_id()

        try:
            candidate = GovernedEntity(
                entity_id=resolved_id,
                entity_type=entity_type,
                canonical_name=canonical_name,
                display_name=display_name,
                status=status,
                created_at=utc_now(),
                fiscal_year_start_month_day=fiscal_year_start_month_day,
                historical_floor_override_at=historical_floor_override_at,
                metadata=dict(metadata) if metadata is not None else {},
            )
            validate_against_contract(candidate.to_dict(), _SCHEMA)
        except ValidationError:
            raise
        except Exception as exc:  # noqa: BLE001 - never leak a raw exception
            raise ValidationError(f"could not register GovernedEntity: {exc}") from exc

        row = GovernedEntityRow(
            entity_id=candidate.entity_id,
            entity_type=candidate.entity_type,
            canonical_name=candidate.canonical_name,
            display_name=candidate.display_name,
            status=candidate.status,
            created_at=candidate.created_at,
            fiscal_year_start_month_day=candidate.fiscal_year_start_month_day,
            historical_floor_override_at=candidate.historical_floor_override_at,
            metadata_=dict(candidate.metadata),
        )

        try:
            with session_scope(self._engine) as session:
                session.add(row)
        except IntegrityError as exc:
            # The real PK uniqueness constraint is the actual backstop
            # here (not just the in-memory dict check CD-2 used) —
            # translate its violation rather than letting it escape.
            if unique_violation_constraint(exc) is not None:
                raise ImmutabilityViolationError(
                    f"entity_id '{resolved_id}' already exists; entity_id is never reassigned"
                ) from exc
            raise PersistenceError(f"could not register GovernedEntity: {exc}") from exc
        except SQLAlchemyError as exc:
            raise PersistenceError(f"could not register GovernedEntity: {exc}") from exc

        return candidate

    def set_accounting_period_configuration(
        self,
        entity_id: str,
        *,
        fiscal_year_start_month_day: str,
        historical_floor_override_at,
    ) -> GovernedEntity:
        try:
            with session_scope(self._engine) as session:
                try:
                    row = session.get(GovernedEntityRow, entity_id, with_for_update=True)
                except DataError as exc:
                    if is_invalid_uuid_format(exc):
                        raise NotFoundError(
                            f"no GovernedEntity with entity_id '{entity_id}' "
                            "(malformed identifier can never exist)"
                        ) from exc
                    raise
                if row is None:
                    raise NotFoundError(f"no GovernedEntity with entity_id '{entity_id}'")
                current = _row_to_entity(row)
                updated = dataclasses.replace(
                    current,
                    fiscal_year_start_month_day=fiscal_year_start_month_day,
                    historical_floor_override_at=historical_floor_override_at,
                )
                validate_against_contract(updated.to_dict(), _SCHEMA)
                row.fiscal_year_start_month_day = updated.fiscal_year_start_month_day
                row.historical_floor_override_at = updated.historical_floor_override_at
        except (NotFoundError, ValidationError):
            raise
        except SQLAlchemyError as exc:
            raise PersistenceError(f"could not update GovernedEntity accounting-period configuration: {exc}") from exc
        return updated

    def get_entity(self, entity_id: str) -> GovernedEntity:
        try:
            with session_scope(self._engine) as session:
                row = session.get(GovernedEntityRow, entity_id)
                if row is None:
                    raise NotFoundError(f"no GovernedEntity with entity_id '{entity_id}'")
                return _row_to_entity(row)
        except NotFoundError:
            raise
        except DataError as exc:
            if is_invalid_uuid_format(exc):
                raise NotFoundError(
                    f"no GovernedEntity with entity_id '{entity_id}' "
                    "(malformed identifier can never exist)"
                ) from exc
            raise PersistenceError(f"could not read GovernedEntity: {exc}") from exc
        except SQLAlchemyError as exc:
            raise PersistenceError(f"could not read GovernedEntity: {exc}") from exc

    def list_entities(self) -> list[GovernedEntity]:
        try:
            with session_scope(self._engine) as session:
                rows = (
                    session.query(GovernedEntityRow)
                    .order_by(GovernedEntityRow.entity_id)
                    .all()
                )
                return [_row_to_entity(row) for row in rows]
        except SQLAlchemyError as exc:
            raise PersistenceError(f"could not list GovernedEntity rows: {exc}") from exc

    def find_by_canonical_name(self, canonical_name: str) -> Optional[GovernedEntity]:
        try:
            with session_scope(self._engine) as session:
                row = (
                    session.query(GovernedEntityRow)
                    .filter_by(canonical_name=canonical_name)
                    .first()
                )
                return _row_to_entity(row) if row is not None else None
        except SQLAlchemyError as exc:
            raise PersistenceError(f"could not look up GovernedEntity by canonical_name: {exc}") from exc
