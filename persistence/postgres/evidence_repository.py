"""``PostgresEvidenceRepository`` — durable implementation of
``services.evidence.evidence.EvidenceRepository`` (CD-3 WI-1, PID
§13/§23/§34/§55).

Preserves, durably, the exact WI-2-repaired CD-2 idempotency behaviour
of ``services.evidence.evidence.InMemoryEvidenceRepository.register_evidence``:
when an ``external_reference`` hint is given and the tuple has already
been observed (through this exact repository, or via a bare process
restart followed by a retry — this is now durable), the SAME
``EvidenceItem`` is returned and no new row/audit event is created.
When the tuple is genuinely new, this repository writes BOTH the
``EvidenceItem`` row and its ``ExternalReference`` row in ONE database
transaction (PID §55) — so a caller that retries the same call with no
separate ``link_external_reference`` call of its own is durably
self-sufficient, and a mid-way failure never leaves one row without
its corresponding partner.

Also preserves: immutable core fields (no update path except the two
narrow ones below), ``update_status``, and ``assign_entity`` (rejecting
a second call once resolved — enforced inside one transaction with
``SELECT ... FOR UPDATE`` so a concurrent second call cannot race past
the "already resolved" check).
"""
from __future__ import annotations

import dataclasses
from datetime import datetime
from typing import Any, Mapping, Optional, Union

import sqlalchemy as sa
from sqlalchemy.engine import Engine
from sqlalchemy.exc import DataError, IntegrityError, SQLAlchemyError

from core import identity
from core.contract_validation import validate_against_contract
from core.errors import (
    DuplicateExternalReferenceError,
    ImmutabilityViolationError,
    NotFoundError,
    PersistenceError,
    ValidationError,
)
from core.external_reference import ExternalReferenceRepository
from core.text_matching import normalize_address, normalize_domain
from core.timestamps import utc_now
from persistence.postgres.db_errors import (
    is_foreign_key_violation,
    is_invalid_uuid_format,
    unique_violation_constraint,
)
from persistence.postgres.models import EvidenceItemRow, ExternalReferenceRow
from persistence.postgres.session import get_engine, session_scope
from services.evidence.evidence import (
    MAILBOX_EVIDENCE_TYPES,
    STATUSES,
    EvidenceItem,
    EvidenceRepository,
    ExternalReferenceHint,
    _DEFAULT_CANDIDATE_LIMIT,
    _normalize_content_hash,
)

_SCHEMA = "evidence/bagman.evidence.v1.schema.json"

_TUPLE_CONSTRAINT = "uq_external_references_tuple"


def _row_to_evidence(row: EvidenceItemRow) -> EvidenceItem:
    return EvidenceItem(
        evidence_id=row.evidence_id,
        entity_id=row.entity_id,
        evidence_type=row.evidence_type,
        source_id=row.source_id,
        observed_at=row.observed_at,
        received_at=row.received_at,
        content_hash=dict(row.content_hash),
        mime_type=row.mime_type,
        size_bytes=row.size_bytes,
        status=row.status,
        created_at=row.created_at,
        original_name=row.original_name,
        storage_reference=row.storage_reference,
        metadata=dict(row.metadata_),
    )


class PostgresEvidenceRepository(EvidenceRepository):
    """PostgreSQL-backed ``EvidenceRepository``. Stateless: every
    method reads/writes the database directly via a fresh ``Session``;
    constructed with an ``ExternalReferenceRepository`` exactly like
    ``InMemoryEvidenceRepository`` (normally a
    ``PostgresExternalReferenceRepository`` sharing the same engine)."""

    def __init__(
        self,
        external_reference_repository: ExternalReferenceRepository,
        engine: Optional[Engine] = None,
    ) -> None:
        self._external_reference_repository = external_reference_repository
        self._engine = engine or get_engine()

    def register_evidence(
        self,
        *,
        entity_id: Optional[str],
        evidence_type: str,
        source_id: str,
        observed_at: datetime,
        received_at: datetime,
        content_hash: Union[str, Mapping[str, str]],
        mime_type: str,
        size_bytes: int,
        original_name: Optional[str] = None,
        storage_reference: Optional[str] = None,
        status: str = "OBSERVED",
        metadata: Optional[Mapping[str, Any]] = None,
        external_reference: Optional[ExternalReferenceHint] = None,
    ) -> EvidenceItem:
        if external_reference is not None:
            provider, resource_type, external_id = external_reference
            existing_ref = self._external_reference_repository.find_by_tuple(
                provider=provider,
                source_id=source_id,
                resource_type=resource_type,
                external_id=external_id,
            )
            replay = self._replay_if_evidence(existing_ref)
            if replay is not None:
                return replay

        try:
            candidate = EvidenceItem(
                evidence_id=identity.generate_id(),
                entity_id=entity_id,
                evidence_type=evidence_type,
                source_id=source_id,
                observed_at=observed_at,
                received_at=received_at,
                content_hash=_normalize_content_hash(content_hash),
                mime_type=mime_type,
                size_bytes=size_bytes,
                status=status,
                created_at=utc_now(),
                original_name=original_name,
                storage_reference=storage_reference,
                metadata=dict(metadata) if metadata is not None else {},
            )
            validate_against_contract(candidate.to_dict(), _SCHEMA)
        except ValidationError:
            raise
        except Exception as exc:  # noqa: BLE001 - never leak a raw exception
            raise ValidationError(f"could not register EvidenceItem: {exc}") from exc

        evidence_row = EvidenceItemRow(
            evidence_id=candidate.evidence_id,
            entity_id=candidate.entity_id,
            evidence_type=candidate.evidence_type,
            source_id=candidate.source_id,
            observed_at=candidate.observed_at,
            received_at=candidate.received_at,
            content_hash=dict(candidate.content_hash),
            mime_type=candidate.mime_type,
            size_bytes=candidate.size_bytes,
            status=candidate.status,
            created_at=candidate.created_at,
            original_name=candidate.original_name,
            storage_reference=candidate.storage_reference,
            metadata_=dict(candidate.metadata),
        )

        reference_row: Optional[ExternalReferenceRow] = None
        if external_reference is not None:
            provider, resource_type, external_id = external_reference
            reference_row = ExternalReferenceRow(
                external_reference_id=identity.generate_id(),
                provider=provider,
                resource_type=resource_type,
                external_id=external_id,
                canonical_object_type="EvidenceItem",
                canonical_object_id=candidate.evidence_id,
                source_id=source_id,
                first_observed_at=utc_now(),
                metadata_={},
            )

        try:
            # One transaction covers BOTH rows (PID §55): a mid-way
            # failure (in particular the external-reference tuple
            # uniquely conflicting) never leaves an EvidenceItem
            # without its corresponding reference, or vice versa.
            with session_scope(self._engine) as session:
                session.add(evidence_row)
                if reference_row is not None:
                    session.add(reference_row)
                    session.flush()
        except IntegrityError as exc:
            if reference_row is not None and unique_violation_constraint(exc) == _TUPLE_CONSTRAINT:
                provider, resource_type, external_id = external_reference
                winner = self._external_reference_repository.find_by_tuple(
                    provider=provider,
                    source_id=source_id,
                    resource_type=resource_type,
                    external_id=external_id,
                )
                replay = self._replay_if_evidence(winner)
                if replay is not None:
                    return replay
                raise DuplicateExternalReferenceError(
                    f"external reference tuple (provider={provider!r}, source_id={source_id!r}, "
                    f"resource_type={resource_type!r}, external_id={external_id!r}) already maps "
                    "to a different canonical object; cannot also map it to the new EvidenceItem"
                ) from exc
            raise PersistenceError(f"could not register EvidenceItem: {exc}") from exc
        except SQLAlchemyError as exc:
            raise PersistenceError(f"could not register EvidenceItem: {exc}") from exc

        return candidate

    def _replay_if_evidence(self, reference) -> Optional[EvidenceItem]:
        """If `reference` points at an EvidenceItem that genuinely
        exists, return it (idempotent replay); else None."""
        if reference is None or reference.canonical_object_type != "EvidenceItem":
            return None
        try:
            return self.get_evidence(reference.canonical_object_id)
        except NotFoundError:
            return None

    def get_evidence(self, evidence_id: str) -> EvidenceItem:
        try:
            with session_scope(self._engine) as session:
                row = session.get(EvidenceItemRow, evidence_id)
                if row is None:
                    raise NotFoundError(f"no EvidenceItem with evidence_id '{evidence_id}'")
                return _row_to_evidence(row)
        except NotFoundError:
            raise
        except DataError as exc:
            if is_invalid_uuid_format(exc):
                raise NotFoundError(
                    f"no EvidenceItem with evidence_id '{evidence_id}' "
                    "(malformed identifier can never exist)"
                ) from exc
            raise PersistenceError(f"could not read EvidenceItem: {exc}") from exc
        except SQLAlchemyError as exc:
            raise PersistenceError(f"could not read EvidenceItem: {exc}") from exc

    def list_evidence(
        self,
        *,
        entity_id: Optional[str] = None,
        evidence_type: Optional[str] = None,
        received_at_from=None,
        received_at_to=None,
        limit: Optional[int] = None,
        offset: int = 0,
    ) -> list[EvidenceItem]:
        try:
            with session_scope(self._engine) as session:
                query = session.query(EvidenceItemRow)
                if entity_id is not None:
                    query = query.filter(EvidenceItemRow.entity_id == entity_id)
                if evidence_type is not None:
                    query = query.filter(EvidenceItemRow.evidence_type == evidence_type)
                if received_at_from is not None:
                    query = query.filter(EvidenceItemRow.received_at >= received_at_from)
                if received_at_to is not None:
                    query = query.filter(EvidenceItemRow.received_at <= received_at_to)
                query = query.order_by(
                    EvidenceItemRow.received_at.desc(), EvidenceItemRow.evidence_id.desc()
                )
                if offset:
                    query = query.offset(offset)
                if limit is not None:
                    query = query.limit(limit)
                rows = query.all()
                return [_row_to_evidence(row) for row in rows]
        except SQLAlchemyError as exc:
            raise PersistenceError(f"could not list EvidenceItem rows: {exc}") from exc

    def update_status(self, evidence_id: str, new_status: str) -> EvidenceItem:
        if new_status not in STATUSES:
            raise ValidationError(
                f"'{new_status}' is not one of the documented PID §25 evidence status "
                f"values {sorted(STATUSES)}"
            )

        try:
            with session_scope(self._engine) as session:
                row = session.get(EvidenceItemRow, evidence_id)
                if row is None:
                    raise NotFoundError(f"no EvidenceItem with evidence_id '{evidence_id}'")
                row.status = new_status
                session.flush()
                updated = _row_to_evidence(row)
                validate_against_contract(updated.to_dict(), _SCHEMA)
        except (NotFoundError, ValidationError):
            raise
        except DataError as exc:
            if is_invalid_uuid_format(exc):
                raise NotFoundError(
                    f"no EvidenceItem with evidence_id '{evidence_id}' "
                    "(malformed identifier can never exist)"
                ) from exc
            raise PersistenceError(f"could not update EvidenceItem status: {exc}") from exc
        except SQLAlchemyError as exc:
            raise PersistenceError(f"could not update EvidenceItem status: {exc}") from exc

        return updated

    def assign_entity(self, evidence_id: str, entity_id: str) -> EvidenceItem:
        try:
            with session_scope(self._engine) as session:
                # SELECT ... FOR UPDATE: locks the row for the rest of
                # this transaction so a concurrent second assign_entity
                # call cannot race past the "already resolved" check.
                try:
                    row = session.get(EvidenceItemRow, evidence_id, with_for_update=True)
                except DataError as exc:
                    # A malformed evidence_id (not a genuinely broken
                    # persistence layer) — see db_errors.is_invalid_uuid_format.
                    # Deliberately scoped to just this lookup so a
                    # DataError from the row.entity_id write below (a
                    # malformed entity_id, a different case entirely)
                    # is never mislabelled as "no such EvidenceItem".
                    if is_invalid_uuid_format(exc):
                        raise NotFoundError(
                            f"no EvidenceItem with evidence_id '{evidence_id}' "
                            "(malformed identifier can never exist)"
                        ) from exc
                    raise
                if row is None:
                    raise NotFoundError(f"no EvidenceItem with evidence_id '{evidence_id}'")
                if row.entity_id is not None:
                    raise ImmutabilityViolationError(
                        f"EvidenceItem '{evidence_id}' already has entity_id '{row.entity_id}' "
                        "assigned; reassignment is out of CD-2 scope (a later delivery's "
                        "correction pattern, not a mutation this repository performs)"
                    )
                row.entity_id = entity_id
                session.flush()
                updated = _row_to_evidence(row)
                validate_against_contract(updated.to_dict(), _SCHEMA)
        except (NotFoundError, ImmutabilityViolationError, ValidationError):
            raise
        except IntegrityError as exc:
            if is_foreign_key_violation(exc):
                raise NotFoundError(f"no GovernedEntity with entity_id '{entity_id}'") from exc
            raise PersistenceError(f"could not assign entity to EvidenceItem: {exc}") from exc
        except SQLAlchemyError as exc:
            raise PersistenceError(f"could not assign entity to EvidenceItem: {exc}") from exc

        return updated

    def find_candidate_evidence_for_sender(
        self,
        *,
        sender_domain: str,
        sender_address: Optional[str] = None,
        limit: int = _DEFAULT_CANDIDATE_LIMIT,
    ) -> list[EvidenceItem]:
        normalized_domain = normalize_domain(sender_domain)
        normalized_address = normalize_address(sender_address) if sender_address else None

        try:
            with session_scope(self._engine) as session:
                query = session.query(EvidenceItemRow).filter(
                    EvidenceItemRow.metadata_.has_key("sender_address"),
                    EvidenceItemRow.metadata_.has_key("subject"),
                    # CD-6 Slice 5 WI-2 — a documented, non-authoritative
                    # narrowing judgment call (see
                    # services.evidence.evidence.MAILBOX_EVIDENCE_TYPES'
                    # own docstring): restricts the SQL scan to
                    # evidence_type == "EMAIL" (the only mailbox-shaped
                    # evidence_type any real ingestion path writes today).
                    # This is purely a performance narrowing — every
                    # returned row is still re-tested in Python by the
                    # caller (the observed-evidence guard) via
                    # core.text_matching, never trusted as final truth.
                    EvidenceItemRow.evidence_type.in_(MAILBOX_EVIDENCE_TYPES),
                )
                if normalized_address is not None:
                    # Exact (case-insensitive) match — real narrowing,
                    # not just a prefilter, but still re-verified in
                    # Python by the caller.
                    query = query.filter(
                        sa.func.lower(EvidenceItemRow.metadata_["sender_address"].astext) == normalized_address
                    )
                else:
                    # Loose SQL-level ILIKE prefilter only (never the
                    # source of match truth — see this method's own ABC
                    # docstring): "<anything>@<normalized_domain>",
                    # escaped so a literal '%'/'_' in the domain (should
                    # never occur for a real domain, but defends against
                    # one anyway) is not treated as an ILIKE wildcard.
                    escaped = normalized_domain.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
                    query = query.filter(
                        EvidenceItemRow.metadata_["sender_address"].astext.ilike(f"%@{escaped}", escape="\\")
                    )
                query = query.order_by(
                    EvidenceItemRow.received_at.desc(), EvidenceItemRow.evidence_id.desc()
                ).limit(limit)
                rows = query.all()
                return [_row_to_evidence(row) for row in rows]
        except SQLAlchemyError as exc:
            raise PersistenceError(f"could not query candidate EvidenceItem rows for sender: {exc}") from exc
