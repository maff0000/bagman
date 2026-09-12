"""``PostgresExternalReferenceRepository`` — durable implementation of
``core.external_reference.ExternalReferenceRepository`` (CD-3 WI-1,
PID §13/§23/§34).

Preserves the exact CD-2 idempotency semantics:

* unseen tuple                       -> create a new record.
* seen tuple, same canonical target  -> idempotent replay: return the
  existing record, no duplicate created.
* seen tuple, a different target     -> ``DuplicateExternalReferenceError``.

Unlike the in-memory version, this implementation never decides the
"seen tuple" outcome from a separate application-level check-then-
insert (which could race under concurrent callers). It always attempts
the insert directly; the real, single source of truth for the
composite-uniqueness decision is the database's own
``uq_external_references_tuple`` constraint
(``persistence/postgres/models.py``). Only when that constraint fires
does this repository re-read the row that actually won the race and
decide idempotent-replay vs. genuine conflict from it.
"""
from __future__ import annotations

from typing import Any, Mapping, Optional

from sqlalchemy.engine import Engine
from sqlalchemy.exc import IntegrityError, SQLAlchemyError

from core import identity
from core.contract_validation import validate_against_contract
from core.errors import DuplicateExternalReferenceError, NotFoundError, PersistenceError, ValidationError
from core.external_reference import ExternalReference, ExternalReferenceRepository
from core.timestamps import utc_now
from persistence.postgres.db_errors import unique_violation_constraint
from persistence.postgres.models import ExternalReferenceRow
from persistence.postgres.session import get_engine, session_scope

_SCHEMA = "source/bagman.external_reference.v1.schema.json"

_TUPLE_CONSTRAINT = "uq_external_references_tuple"


def _row_to_reference(row: ExternalReferenceRow) -> ExternalReference:
    return ExternalReference(
        external_reference_id=row.external_reference_id,
        provider=row.provider,
        resource_type=row.resource_type,
        external_id=row.external_id,
        canonical_object_type=row.canonical_object_type,
        canonical_object_id=row.canonical_object_id,
        source_id=row.source_id,
        first_observed_at=row.first_observed_at,
        metadata=dict(row.metadata_),
    )


class PostgresExternalReferenceRepository(ExternalReferenceRepository):
    """PostgreSQL-backed ``ExternalReferenceRepository``. Stateless:
    every method reads/writes the database directly via a fresh
    ``Session``."""

    def __init__(self, engine: Optional[Engine] = None) -> None:
        self._engine = engine or get_engine()

    def link_external_reference(
        self,
        *,
        provider: str,
        source_id: str,
        resource_type: str,
        external_id: str,
        canonical_object_type: str,
        canonical_object_id: str,
        metadata: Optional[Mapping[str, Any]] = None,
    ) -> ExternalReference:
        try:
            candidate = ExternalReference(
                external_reference_id=identity.generate_id(),
                provider=provider,
                resource_type=resource_type,
                external_id=external_id,
                canonical_object_type=canonical_object_type,
                canonical_object_id=canonical_object_id,
                source_id=source_id,
                first_observed_at=utc_now(),
                metadata=dict(metadata) if metadata is not None else {},
            )
            validate_against_contract(candidate.to_dict(), _SCHEMA)
        except ValidationError:
            raise
        except Exception as exc:  # noqa: BLE001 - never leak a raw exception
            raise ValidationError(f"could not link ExternalReference: {exc}") from exc

        row = ExternalReferenceRow(
            external_reference_id=candidate.external_reference_id,
            provider=candidate.provider,
            resource_type=candidate.resource_type,
            external_id=candidate.external_id,
            canonical_object_type=candidate.canonical_object_type,
            canonical_object_id=candidate.canonical_object_id,
            source_id=candidate.source_id,
            first_observed_at=candidate.first_observed_at,
            metadata_=dict(candidate.metadata),
        )

        try:
            with session_scope(self._engine) as session:
                session.add(row)
            return candidate
        except IntegrityError as exc:
            if unique_violation_constraint(exc) == _TUPLE_CONSTRAINT:
                return self._resolve_tuple_conflict(
                    provider=provider,
                    source_id=source_id,
                    resource_type=resource_type,
                    external_id=external_id,
                    canonical_object_type=canonical_object_type,
                    canonical_object_id=canonical_object_id,
                    cause=exc,
                )
            raise PersistenceError(f"could not link ExternalReference: {exc}") from exc
        except SQLAlchemyError as exc:
            raise PersistenceError(f"could not link ExternalReference: {exc}") from exc

    def _resolve_tuple_conflict(
        self,
        *,
        provider: str,
        source_id: str,
        resource_type: str,
        external_id: str,
        canonical_object_type: str,
        canonical_object_id: str,
        cause: IntegrityError,
    ) -> ExternalReference:
        """Called only after the database's own uniqueness constraint
        has already fired on the (provider, source_id, resource_type,
        external_id) tuple. Re-reads whichever row actually won the
        race and decides idempotent-replay vs. genuine conflict from
        THAT row — never from a stale pre-insert read."""
        existing = self.find_by_tuple(
            provider=provider,
            source_id=source_id,
            resource_type=resource_type,
            external_id=external_id,
        )
        if (
            existing is not None
            and existing.canonical_object_type == canonical_object_type
            and existing.canonical_object_id == canonical_object_id
        ):
            return existing

        existing_target = (
            f"{existing.canonical_object_type}:{existing.canonical_object_id}"
            if existing is not None
            else "<unknown>"
        )
        raise DuplicateExternalReferenceError(
            f"external reference tuple (provider={provider!r}, source_id={source_id!r}, "
            f"resource_type={resource_type!r}, external_id={external_id!r}) already maps to "
            f"{existing_target}; cannot also map it to "
            f"{canonical_object_type}:{canonical_object_id}"
        ) from cause

    def get_external_reference(self, external_reference_id: str) -> ExternalReference:
        try:
            with session_scope(self._engine) as session:
                row = session.get(ExternalReferenceRow, external_reference_id)
                if row is None:
                    raise NotFoundError(
                        f"no ExternalReference with external_reference_id '{external_reference_id}'"
                    )
                return _row_to_reference(row)
        except NotFoundError:
            raise
        except SQLAlchemyError as exc:
            raise PersistenceError(f"could not read ExternalReference: {exc}") from exc

    def find_by_tuple(
        self, *, provider: str, source_id: str, resource_type: str, external_id: str
    ) -> Optional[ExternalReference]:
        try:
            with session_scope(self._engine) as session:
                row = (
                    session.query(ExternalReferenceRow)
                    .filter_by(
                        provider=provider,
                        source_id=source_id,
                        resource_type=resource_type,
                        external_id=external_id,
                    )
                    .one_or_none()
                )
                return _row_to_reference(row) if row is not None else None
        except SQLAlchemyError as exc:
            raise PersistenceError(f"could not look up ExternalReference by tuple: {exc}") from exc
