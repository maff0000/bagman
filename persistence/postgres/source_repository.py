"""``PostgresSourceRepository`` — durable implementation of
``core.source.SourceRepository`` (CD-3 WI-1, PID §13).

``governed_entity_hint`` is stored as a plain nullable string column
(see ``persistence/postgres/models.py::SourceRow`` and its module
docstring) — deliberately NOT a foreign key, per PID §9: a source's
entity hint is a hint, never an ownership assertion.
"""
from __future__ import annotations

from typing import Any, Mapping, Optional

from sqlalchemy.engine import Engine
from sqlalchemy.exc import DataError, SQLAlchemyError

from core import identity
from core.contract_validation import validate_against_contract
from core.errors import NotFoundError, PersistenceError, ValidationError
from core.source import Source, SourceRepository
from persistence.postgres.db_errors import is_invalid_uuid_format
from persistence.postgres.models import SourceRow
from persistence.postgres.session import get_engine, session_scope

_SCHEMA = "source/bagman.source.v1.schema.json"


def _row_to_source(row: SourceRow) -> Source:
    return Source(
        source_id=row.source_id,
        source_type=row.source_type,
        provider=row.provider,
        status=row.status,
        external_source_ref=row.external_source_ref,
        governed_entity_hint=row.governed_entity_hint,
        metadata=dict(row.metadata_),
    )


class PostgresSourceRepository(SourceRepository):
    """PostgreSQL-backed ``SourceRepository``. Stateless: every method
    reads/writes the database directly via a fresh ``Session``."""

    def __init__(self, engine: Optional[Engine] = None) -> None:
        self._engine = engine or get_engine()

    def register_source(
        self,
        *,
        source_type: str,
        provider: str,
        status: str,
        external_source_ref: Optional[str] = None,
        governed_entity_hint: Optional[str] = None,
        metadata: Optional[Mapping[str, Any]] = None,
    ) -> Source:
        try:
            candidate = Source(
                source_id=identity.generate_id(),
                source_type=source_type,
                provider=provider,
                status=status,
                external_source_ref=external_source_ref,
                governed_entity_hint=governed_entity_hint,
                metadata=dict(metadata) if metadata is not None else {},
            )
            validate_against_contract(candidate.to_dict(), _SCHEMA)
        except ValidationError:
            raise
        except Exception as exc:  # noqa: BLE001 - never leak a raw exception
            raise ValidationError(f"could not register Source: {exc}") from exc

        row = SourceRow(
            source_id=candidate.source_id,
            source_type=candidate.source_type,
            provider=candidate.provider,
            status=candidate.status,
            external_source_ref=candidate.external_source_ref,
            governed_entity_hint=candidate.governed_entity_hint,
            metadata_=dict(candidate.metadata),
        )

        try:
            with session_scope(self._engine) as session:
                session.add(row)
        except SQLAlchemyError as exc:
            raise PersistenceError(f"could not register Source: {exc}") from exc

        return candidate

    def get_source(self, source_id: str) -> Source:
        try:
            with session_scope(self._engine) as session:
                row = session.get(SourceRow, source_id)
                if row is None:
                    raise NotFoundError(f"no Source with source_id '{source_id}'")
                return _row_to_source(row)
        except NotFoundError:
            raise
        except DataError as exc:
            if is_invalid_uuid_format(exc):
                raise NotFoundError(
                    f"no Source with source_id '{source_id}' (malformed identifier can never exist)"
                ) from exc
            raise PersistenceError(f"could not read Source: {exc}") from exc
        except SQLAlchemyError as exc:
            raise PersistenceError(f"could not read Source: {exc}") from exc

    def list_sources(self) -> list[Source]:
        try:
            with session_scope(self._engine) as session:
                rows = session.query(SourceRow).order_by(SourceRow.source_id).all()
                return [_row_to_source(row) for row in rows]
        except SQLAlchemyError as exc:
            raise PersistenceError(f"could not list Source rows: {exc}") from exc
