"""``PostgresNeedsYouRepository`` — durable implementation of
``services.needs_you.needs_you.NeedsYouRepository`` (CD-6 Slice 1, PID
§98.5).

Preserves, durably, the exact idempotent-creation semantics
``services.needs_you.needs_you.InMemoryNeedsYouRepository`` documents
(see that module's own docstring): a replayed producer call for the
same ``(item_type, source_object_reference)`` dedupe key resolves to
the SAME ``NeedsYouItem``, never a second row.

Like ``PostgresIntakeRepository``, this implementation never decides
the "seen key" outcome from a separate application-level
check-then-insert alone (which could race under concurrent callers): it
always attempts the insert; the database's own partial unique index
(``uq_needs_you_items_type_source_ref`` — see
``persistence/postgres/needs_you_models.py``) is the real source of
truth. An initial ``find_by_dedupe_key`` pre-check still runs first
purely to avoid an unnecessary failed insert attempt in the common,
non-racing case — the constraint-violation path below is what actually
proves correctness under a genuine race, by re-reading whichever row
actually won.
"""
from __future__ import annotations

from typing import Any, Mapping, Optional

from sqlalchemy.engine import Engine
from sqlalchemy.exc import DataError, IntegrityError, SQLAlchemyError

from core import identity
from core.contract_validation import validate_against_contract
from core.errors import (
    ConflictError,
    InvalidStateTransitionError,
    NotFoundError,
    PersistenceError,
    ValidationError,
)
from core.timestamps import utc_now
from persistence.postgres.db_errors import is_invalid_uuid_format, unique_violation_constraint
from persistence.postgres.needs_you_models import NeedsYouItemRow
from persistence.postgres.session import get_engine, session_scope
from services.needs_you.needs_you import (
    DEFAULT_PRIORITY,
    NeedsYouItem,
    NeedsYouRepository,
    transition,
)

_SCHEMA = "needs_you/bagman.needs_you_item.v1.schema.json"

_DEDUPE_CONSTRAINT = "uq_needs_you_items_type_source_ref"


def _row_to_item(row: NeedsYouItemRow) -> NeedsYouItem:
    return NeedsYouItem(
        item_id=row.item_id,
        item_type=row.item_type,
        domain=row.domain,
        source_object_reference=row.source_object_reference,
        question=row.question,
        allowed_action_type=row.allowed_action_type,
        priority=row.priority,
        status=row.status,
        created_at=row.created_at,
        resolved_at=row.resolved_at,
        resolved_by_actor_type=row.resolved_by_actor_type,
        resolved_by_actor_id=row.resolved_by_actor_id,
        resolution=dict(row.resolution) if row.resolution is not None else None,
        correlation_id=row.correlation_id,
        metadata=dict(row.metadata_),
    )


class PostgresNeedsYouRepository(NeedsYouRepository):
    """PostgreSQL-backed ``NeedsYouRepository``. Stateless: every method
    reads/writes the database directly via a fresh ``Session``."""

    def __init__(self, engine: Optional[Engine] = None) -> None:
        self._engine = engine or get_engine()

    def create_needs_you_item(
        self,
        *,
        item_type: str,
        domain: str,
        question: str,
        allowed_action_type: str,
        correlation_id: Optional[str] = None,
        source_object_reference: Optional[str] = None,
        priority: str = DEFAULT_PRIORITY,
        metadata: Optional[Mapping[str, Any]] = None,
    ) -> NeedsYouItem:
        if source_object_reference is not None:
            existing = self.find_by_dedupe_key(item_type, source_object_reference)
            if existing is not None:
                return existing

        try:
            candidate = NeedsYouItem(
                item_id=identity.generate_id(),
                item_type=item_type,
                domain=domain,
                source_object_reference=source_object_reference,
                question=question,
                allowed_action_type=allowed_action_type,
                priority=priority,
                status="OPEN",
                created_at=utc_now(),
                correlation_id=correlation_id if correlation_id is not None else identity.generate_id(),
                metadata=dict(metadata) if metadata is not None else {},
            )
            validate_against_contract(candidate.to_dict(), _SCHEMA)
        except ValidationError:
            raise
        except Exception as exc:  # noqa: BLE001 - never leak a raw exception
            raise ValidationError(f"could not create NeedsYouItem: {exc}") from exc

        row = NeedsYouItemRow(
            item_id=candidate.item_id,
            item_type=candidate.item_type,
            domain=candidate.domain,
            source_object_reference=candidate.source_object_reference,
            question=candidate.question,
            allowed_action_type=candidate.allowed_action_type,
            priority=candidate.priority,
            status=candidate.status,
            created_at=candidate.created_at,
            resolved_at=candidate.resolved_at,
            resolved_by_actor_type=candidate.resolved_by_actor_type,
            resolved_by_actor_id=candidate.resolved_by_actor_id,
            resolution=None,
            correlation_id=candidate.correlation_id,
            metadata_=dict(candidate.metadata),
        )

        try:
            with session_scope(self._engine) as session:
                session.add(row)
        except IntegrityError as exc:
            if source_object_reference is not None and unique_violation_constraint(exc) == _DEDUPE_CONSTRAINT:
                winner = self.find_by_dedupe_key(item_type, source_object_reference)
                if winner is not None:
                    return winner
                raise PersistenceError(
                    f"could not create NeedsYouItem: unique-constraint conflict on "
                    f"(item_type={item_type!r}, source_object_reference="
                    f"{source_object_reference!r}) but no winning row could be re-read: {exc}"
                ) from exc
            raise PersistenceError(f"could not create NeedsYouItem: {exc}") from exc
        except SQLAlchemyError as exc:
            raise PersistenceError(f"could not create NeedsYouItem: {exc}") from exc

        return candidate

    def get_needs_you_item(self, item_id: str) -> NeedsYouItem:
        try:
            with session_scope(self._engine) as session:
                row = session.get(NeedsYouItemRow, item_id)
                if row is None:
                    raise NotFoundError(f"no NeedsYouItem with item_id '{item_id}'")
                return _row_to_item(row)
        except NotFoundError:
            raise
        except DataError as exc:
            if is_invalid_uuid_format(exc):
                raise NotFoundError(
                    f"no NeedsYouItem with item_id '{item_id}' "
                    "(malformed identifier can never exist)"
                ) from exc
            raise PersistenceError(f"could not read NeedsYouItem: {exc}") from exc
        except SQLAlchemyError as exc:
            raise PersistenceError(f"could not read NeedsYouItem: {exc}") from exc

    def find_by_dedupe_key(self, item_type: str, source_object_reference: str) -> Optional[NeedsYouItem]:
        try:
            with session_scope(self._engine) as session:
                row = (
                    session.query(NeedsYouItemRow)
                    .filter_by(item_type=item_type, source_object_reference=source_object_reference)
                    .one_or_none()
                )
                return _row_to_item(row) if row is not None else None
        except DataError as exc:
            if is_invalid_uuid_format(exc):
                return None
            raise PersistenceError(f"could not look up NeedsYouItem by dedupe key: {exc}") from exc
        except SQLAlchemyError as exc:
            raise PersistenceError(f"could not look up NeedsYouItem by dedupe key: {exc}") from exc

    def list_needs_you_items(
        self,
        *,
        status: Optional[str] = None,
        item_type: Optional[str] = None,
        domain: Optional[str] = None,
        limit: Optional[int] = None,
        offset: int = 0,
    ) -> list[NeedsYouItem]:
        try:
            with session_scope(self._engine) as session:
                query = session.query(NeedsYouItemRow)
                if status is not None:
                    query = query.filter(NeedsYouItemRow.status == status)
                if item_type is not None:
                    query = query.filter(NeedsYouItemRow.item_type == item_type)
                if domain is not None:
                    query = query.filter(NeedsYouItemRow.domain == domain)
                rows = query.all()
        except SQLAlchemyError as exc:
            raise PersistenceError(f"could not list NeedsYouItem rows: {exc}") from exc

        items = [_row_to_item(row) for row in rows]

        # Ordering is applied in Python, not SQL (mirrors the priority-
        # rank mapping `services.needs_you.needs_you`'s in-memory
        # implementation already uses) — the OPEN-first / priority /
        # oldest-first-within-priority ordering PID §98.5's own worked
        # example implies is small-N operator-queue logic, not a
        # database-scale concern; keeping the ordering DEFINITION in one
        # place (the domain module) rather than re-deriving it as SQL
        # `CASE`/`ORDER BY` here avoids the two implementations ever
        # silently disagreeing about what "most pressing first" means.
        priority_rank = {"HIGH": 0, "NORMAL": 1, "LOW": 2}
        open_items = sorted(
            (i for i in items if i.status == "OPEN"),
            key=lambda i: (priority_rank.get(i.priority, 1), i.created_at, i.item_id),
        )
        closed_items = sorted(
            (i for i in items if i.status != "OPEN"),
            key=lambda i: (i.created_at, i.item_id),
            reverse=True,
        )
        ordered = open_items + closed_items

        if limit is None:
            return ordered[offset:]
        return ordered[offset : offset + limit]

    def resolve_needs_you_item(
        self,
        item_id: str,
        *,
        new_status: str,
        resolution: Optional[Mapping[str, Any]],
        actor_type: str,
        actor_id: str,
    ) -> NeedsYouItem:
        try:
            with session_scope(self._engine) as session:
                try:
                    row = session.get(NeedsYouItemRow, item_id, with_for_update=True)
                except DataError as exc:
                    if is_invalid_uuid_format(exc):
                        raise NotFoundError(
                            f"no NeedsYouItem with item_id '{item_id}' "
                            "(malformed identifier can never exist)"
                        ) from exc
                    raise
                if row is None:
                    raise NotFoundError(f"no NeedsYouItem with item_id '{item_id}'")

                current = _row_to_item(row)
                updated = transition(
                    current,
                    new_status,
                    resolution=resolution,
                    resolved_by_actor_type=actor_type,
                    resolved_by_actor_id=actor_id,
                )

                row.status = updated.status
                row.resolved_at = updated.resolved_at
                row.resolved_by_actor_type = updated.resolved_by_actor_type
                row.resolved_by_actor_id = updated.resolved_by_actor_id
                row.resolution = dict(updated.resolution) if updated.resolution is not None else None
                session.flush()
        except (NotFoundError, InvalidStateTransitionError, ValidationError):
            raise
        except SQLAlchemyError as exc:
            raise PersistenceError(f"could not resolve NeedsYouItem: {exc}") from exc

        return updated

    def update_item_metadata(self, item_id: str, *, metadata_updates: Mapping[str, Any]) -> NeedsYouItem:
        try:
            with session_scope(self._engine) as session:
                try:
                    row = session.get(NeedsYouItemRow, item_id, with_for_update=True)
                except DataError as exc:
                    if is_invalid_uuid_format(exc):
                        raise NotFoundError(
                            f"no NeedsYouItem with item_id '{item_id}' "
                            "(malformed identifier can never exist)"
                        ) from exc
                    raise
                if row is None:
                    raise NotFoundError(f"no NeedsYouItem with item_id '{item_id}'")
                if row.status != "OPEN":
                    raise ConflictError(
                        f"NeedsYouItem '{item_id}' is '{row.status}', not 'OPEN' — refusing to update its "
                        "metadata (a resolved/dismissed item's metadata is frozen at whatever data existed "
                        "at resolution time, since the decision was already made against it)"
                    )

                new_metadata = dict(row.metadata_)
                new_metadata.update(dict(metadata_updates))
                row.metadata_ = new_metadata
                session.flush()
                updated = _row_to_item(row)
                validate_against_contract(updated.to_dict(), _SCHEMA)
        except (NotFoundError, ConflictError, ValidationError):
            raise
        except SQLAlchemyError as exc:
            raise PersistenceError(f"could not update NeedsYouItem metadata: {exc}") from exc

        return updated
