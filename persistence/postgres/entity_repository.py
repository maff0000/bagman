"""``PostgresEntityRepository`` — durable implementation of
``core.entity.EntityRepository`` (CD-3 WI-1, PID §13).

Implements the exact same abstract interface as
``core.entity.InMemoryEntityRepository`` — same method signatures,
same return types, same canonical errors on the same conditions — so a
caller cannot tell which backend it is talking to other than
durability.
"""
from __future__ import annotations

from typing import Any, Mapping, Optional

from sqlalchemy.engine import Engine
from sqlalchemy.exc import IntegrityError, SQLAlchemyError

from core import identity
from core.contract_validation import validate_against_contract
from core.entity import EntityRepository, GovernedEntity
from core.errors import ImmutabilityViolationError, NotFoundError, PersistenceError, ValidationError
from core.timestamps import utc_now
from persistence.postgres.db_errors import unique_violation_constraint
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

    def get_entity(self, entity_id: str) -> GovernedEntity:
        try:
            with session_scope(self._engine) as session:
                row = session.get(GovernedEntityRow, entity_id)
                if row is None:
                    raise NotFoundError(f"no GovernedEntity with entity_id '{entity_id}'")
                return _row_to_entity(row)
        except NotFoundError:
            raise
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
