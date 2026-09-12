"""``PostgresProvenanceRepository`` — durable implementation of
``core.provenance.ProvenanceRepository`` (CD-3 WI-1, PID §13/§22/§31).

Append-only: exposes only ``record_provenance()`` plus read methods —
no update or delete method exists.

Rejects an orphan ``evidence_id`` via a pre-check against the
``EvidenceRepository`` it is constructed with (immediate, clear error
for the overwhelmingly common case), with the real foreign key
(``provenance.evidence_id -> evidence_items.evidence_id``) as the
actual backstop — its violation is still caught and translated, never
left to leak a raw driver exception.
"""
from __future__ import annotations

from typing import Any, Mapping, Optional

from sqlalchemy.engine import Engine
from sqlalchemy.exc import IntegrityError, SQLAlchemyError

from core import identity
from core.contract_validation import validate_against_contract
from core.errors import InvalidProvenanceError, NotFoundError, PersistenceError, ValidationError
from core.provenance import Provenance, ProvenanceRepository
from core.timestamps import utc_now
from persistence.postgres.db_errors import is_foreign_key_violation
from persistence.postgres.models import ProvenanceRow
from persistence.postgres.session import get_engine, session_scope

_SCHEMA = "provenance/bagman.provenance.v1.schema.json"


def _row_to_provenance(row: ProvenanceRow) -> Provenance:
    return Provenance(
        provenance_id=row.provenance_id,
        subject_type=row.subject_type,
        subject_id=row.subject_id,
        evidence_id=row.evidence_id,
        relationship=row.relationship,
        created_at=row.created_at,
        transform_id=row.transform_id,
        metadata=dict(row.metadata_),
    )


class PostgresProvenanceRepository(ProvenanceRepository):
    """PostgreSQL-backed ``ProvenanceRepository``. Stateless: every
    method reads/writes the database directly via a fresh ``Session``;
    constructed with an ``EvidenceRepository`` exactly like
    ``InMemoryProvenanceRepository`` (normally a
    ``PostgresEvidenceRepository`` sharing the same engine)."""

    def __init__(self, evidence_repository, engine: Optional[Engine] = None) -> None:
        self._evidence_repository = evidence_repository
        self._engine = engine or get_engine()

    def record_provenance(
        self,
        *,
        subject_type: str,
        subject_id: str,
        evidence_id: str,
        relationship: str,
        transform_id: Optional[str] = None,
        metadata: Optional[Mapping[str, Any]] = None,
    ) -> Provenance:
        try:
            self._evidence_repository.get_evidence(evidence_id)
        except NotFoundError:
            raise InvalidProvenanceError(
                f"cannot record provenance: evidence_id '{evidence_id}' does not exist "
                "(invalid/orphan provenance rejected per PID §31)"
            ) from None

        try:
            candidate = Provenance(
                provenance_id=identity.generate_id(),
                subject_type=subject_type,
                subject_id=subject_id,
                evidence_id=evidence_id,
                relationship=relationship,
                transform_id=transform_id,
                created_at=utc_now(),
                metadata=dict(metadata) if metadata is not None else {},
            )
            validate_against_contract(candidate.to_dict(), _SCHEMA)
        except ValidationError:
            raise
        except Exception as exc:  # noqa: BLE001 - never leak a raw exception
            raise ValidationError(f"could not record Provenance: {exc}") from exc

        row = ProvenanceRow(
            provenance_id=candidate.provenance_id,
            subject_type=candidate.subject_type,
            subject_id=candidate.subject_id,
            evidence_id=candidate.evidence_id,
            relationship=candidate.relationship,
            transform_id=candidate.transform_id,
            created_at=candidate.created_at,
            metadata_=dict(candidate.metadata),
        )

        try:
            with session_scope(self._engine) as session:
                session.add(row)
        except IntegrityError as exc:
            # Real FK backstop (PID §31): even if the pre-check above
            # somehow missed an orphan (e.g. a genuine race), the
            # database itself rejects it and we translate rather than
            # leak the raw driver exception.
            if is_foreign_key_violation(exc):
                raise InvalidProvenanceError(
                    f"cannot record provenance: evidence_id '{evidence_id}' does not exist "
                    "(foreign-key backstop; invalid/orphan provenance rejected per PID §31)"
                ) from exc
            raise PersistenceError(f"could not record Provenance: {exc}") from exc
        except SQLAlchemyError as exc:
            raise PersistenceError(f"could not record Provenance: {exc}") from exc

        return candidate

    def get_provenance(self, provenance_id: str) -> Provenance:
        try:
            with session_scope(self._engine) as session:
                row = session.get(ProvenanceRow, provenance_id)
                if row is None:
                    raise NotFoundError(f"no Provenance with provenance_id '{provenance_id}'")
                return _row_to_provenance(row)
        except NotFoundError:
            raise
        except SQLAlchemyError as exc:
            raise PersistenceError(f"could not read Provenance: {exc}") from exc

    def trace_provenance(self, *, subject_type: str, subject_id: str) -> list[Provenance]:
        try:
            with session_scope(self._engine) as session:
                rows = (
                    session.query(ProvenanceRow)
                    .filter_by(subject_type=subject_type, subject_id=subject_id)
                    .order_by(ProvenanceRow.provenance_id)
                    .all()
                )
                return [_row_to_provenance(row) for row in rows]
        except SQLAlchemyError as exc:
            raise PersistenceError(f"could not trace Provenance: {exc}") from exc
