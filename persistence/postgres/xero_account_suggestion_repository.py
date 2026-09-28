"""PostgreSQL-backed implementations of the Xero Account Suggestion
Producer's two repository abstractions (BAGMAN accounting platform,
`xero/account-suggestion-producer` WO). Follows the exact same
"stateless, fresh `Session` per call, translate constraint violations to
the matching canonical error, never let a raw SQLAlchemy/psycopg
exception escape" discipline every other
`persistence/postgres/*_repository.py` module in this codebase already
establishes — see `persistence/postgres/xero_repository.py` for the
closest structural precedent.
"""
from __future__ import annotations

from typing import Optional, Sequence

from sqlalchemy.engine import Engine
from sqlalchemy.exc import DataError, IntegrityError, SQLAlchemyError

from core import identity
from core.contract_validation import validate_against_contract
from core.errors import ConflictError, NotFoundError, PersistenceError, ValidationError
from core.timestamps import utc_now
from persistence.postgres.db_errors import is_invalid_uuid_format, unique_violation_constraint
from persistence.postgres.session import get_engine, session_scope
from persistence.postgres.xero_account_suggestion_models import (
    XeroAccountAssignmentRow,
    XeroAccountSuggestionRow,
)
from services.xero.account_assignment import (
    XeroAccountAssignment,
    XeroAccountAssignmentRepository,
    validate_assignment_fields_or_raise,
)
from services.xero.account_suggestion import (
    XeroAccountSuggestion,
    XeroAccountSuggestionRepository,
)

_SUGGESTION_SCHEMA = "xero/bagman.xero_account_suggestion.v1.schema.json"
_ASSIGNMENT_SCHEMA = "xero/bagman.xero_account_assignment.v1.schema.json"

_ASSIGNMENT_DEDUPE_CONSTRAINT = "uq_xero_account_assignments_evidence_id"


# ---------------------------------------------------------------------
# XeroAccountSuggestion
# ---------------------------------------------------------------------


def _suggestion_row_to_domain(row: XeroAccountSuggestionRow) -> XeroAccountSuggestion:
    return XeroAccountSuggestion(
        suggestion_id=row.suggestion_id,
        evidence_id=row.evidence_id,
        entity_id=row.entity_id,
        tenant_id=row.tenant_id,
        suggested_account_id=row.suggested_account_id,
        confidence=row.confidence,
        signals=tuple(row.signals or []),
        ai_invocation_id=row.ai_invocation_id,
        status=row.status,
        created_at=row.created_at,
    )


class PostgresXeroAccountSuggestionRepository(XeroAccountSuggestionRepository):
    """PostgreSQL-backed `XeroAccountSuggestionRepository`."""

    def __init__(self, engine: Optional[Engine] = None) -> None:
        self._engine = engine or get_engine()

    def create_suggestion(
        self,
        *,
        evidence_id: str,
        entity_id: str,
        tenant_id: str,
        suggested_account_id: str,
        confidence: Optional[float],
        signals: Sequence[str],
        ai_invocation_id: str,
    ) -> XeroAccountSuggestion:
        try:
            candidate = XeroAccountSuggestion(
                suggestion_id=identity.generate_id(),
                evidence_id=evidence_id,
                entity_id=entity_id,
                tenant_id=tenant_id,
                suggested_account_id=suggested_account_id,
                confidence=confidence,
                signals=tuple(signals),
                ai_invocation_id=ai_invocation_id,
                created_at=utc_now(),
            )
            validate_against_contract(candidate.to_dict(), _SUGGESTION_SCHEMA)
        except ValidationError:
            raise
        except Exception as exc:  # noqa: BLE001 - never leak a raw exception
            raise ValidationError(f"could not create XeroAccountSuggestion: {exc}") from exc

        row = XeroAccountSuggestionRow(
            suggestion_id=candidate.suggestion_id,
            evidence_id=candidate.evidence_id,
            entity_id=candidate.entity_id,
            tenant_id=candidate.tenant_id,
            suggested_account_id=candidate.suggested_account_id,
            confidence=candidate.confidence,
            signals=list(candidate.signals),
            ai_invocation_id=candidate.ai_invocation_id,
            status=candidate.status,
            created_at=candidate.created_at,
        )
        try:
            with session_scope(self._engine) as session:
                session.add(row)
        except IntegrityError as exc:
            raise PersistenceError(f"could not create XeroAccountSuggestion: {exc}") from exc
        except SQLAlchemyError as exc:
            raise PersistenceError(f"could not create XeroAccountSuggestion: {exc}") from exc
        return candidate

    def get_suggestion(self, suggestion_id: str) -> XeroAccountSuggestion:
        try:
            with session_scope(self._engine) as session:
                row = session.get(XeroAccountSuggestionRow, suggestion_id)
                if row is None:
                    raise NotFoundError(f"no XeroAccountSuggestion with suggestion_id '{suggestion_id}'")
                return _suggestion_row_to_domain(row)
        except NotFoundError:
            raise
        except DataError as exc:
            if is_invalid_uuid_format(exc):
                raise NotFoundError(
                    f"no XeroAccountSuggestion with suggestion_id '{suggestion_id}' "
                    "(malformed identifier can never exist)"
                ) from exc
            raise PersistenceError(f"could not read XeroAccountSuggestion: {exc}") from exc
        except SQLAlchemyError as exc:
            raise PersistenceError(f"could not read XeroAccountSuggestion: {exc}") from exc

    def get_by_evidence(self, evidence_id: str) -> Optional[XeroAccountSuggestion]:
        try:
            with session_scope(self._engine) as session:
                row = (
                    session.query(XeroAccountSuggestionRow)
                    .filter_by(evidence_id=evidence_id)
                    .order_by(XeroAccountSuggestionRow.created_at.desc())
                    .first()
                )
                return _suggestion_row_to_domain(row) if row is not None else None
        except DataError as exc:
            if is_invalid_uuid_format(exc):
                return None
            raise PersistenceError(f"could not look up XeroAccountSuggestion by evidence_id: {exc}") from exc
        except SQLAlchemyError as exc:
            raise PersistenceError(f"could not look up XeroAccountSuggestion by evidence_id: {exc}") from exc


# ---------------------------------------------------------------------
# XeroAccountAssignment
# ---------------------------------------------------------------------


def _assignment_row_to_domain(row: XeroAccountAssignmentRow) -> XeroAccountAssignment:
    return XeroAccountAssignment(
        assignment_id=row.assignment_id,
        evidence_id=row.evidence_id,
        entity_id=row.entity_id,
        tenant_id=row.tenant_id,
        account_id=row.account_id,
        source=row.source,
        suggestion_id=row.suggestion_id,
        assigned_by_actor_type=row.assigned_by_actor_type,
        assigned_by_actor_id=row.assigned_by_actor_id,
        assigned_at=row.assigned_at,
    )


class PostgresXeroAccountAssignmentRepository(XeroAccountAssignmentRepository):
    """PostgreSQL-backed `XeroAccountAssignmentRepository`. Never
    decides the "already assigned" outcome from a separate
    application-level check-then-insert alone (which could race under
    concurrent callers): it always attempts the insert; the database's
    own unique constraint (`uq_xero_account_assignments_evidence_id`) is
    the real source of truth — mirrors
    `PostgresNeedsYouRepository.create_needs_you_item`'s identical
    discipline exactly."""

    def __init__(self, engine: Optional[Engine] = None) -> None:
        self._engine = engine or get_engine()

    def create_assignment(
        self,
        *,
        evidence_id: str,
        entity_id: str,
        tenant_id: str,
        account_id: str,
        source: str,
        suggestion_id: Optional[str],
        assigned_by_actor_type: str,
        assigned_by_actor_id: str,
    ) -> XeroAccountAssignment:
        validate_assignment_fields_or_raise(
            source=source, account_id=account_id, evidence_id=evidence_id, entity_id=entity_id, tenant_id=tenant_id,
        )

        # Pre-check purely to avoid an unnecessary failed insert attempt
        # in the common, non-racing case — the constraint-violation path
        # below is what actually proves correctness under a genuine race.
        existing = self.get_by_evidence(evidence_id)
        if existing is not None:
            if existing.account_id == account_id:
                return existing
            raise ConflictError(
                f"evidence '{evidence_id}' already has a DIFFERENT account_id "
                f"('{existing.account_id}') assigned via XeroAccountAssignment "
                f"'{existing.assignment_id}'; reassignment to '{account_id}' is an explicit, separate, "
                "NOT-built-here governed correction workflow — refusing to silently overwrite an "
                "already-resolved coding decision"
            )

        try:
            candidate = XeroAccountAssignment(
                assignment_id=identity.generate_id(),
                evidence_id=evidence_id,
                entity_id=entity_id,
                tenant_id=tenant_id,
                account_id=account_id,
                source=source,
                suggestion_id=suggestion_id,
                assigned_by_actor_type=assigned_by_actor_type,
                assigned_by_actor_id=assigned_by_actor_id,
                assigned_at=utc_now(),
            )
            validate_against_contract(candidate.to_dict(), _ASSIGNMENT_SCHEMA)
        except ValidationError:
            raise
        except Exception as exc:  # noqa: BLE001 - never leak a raw exception
            raise ValidationError(f"could not create XeroAccountAssignment: {exc}") from exc

        row = XeroAccountAssignmentRow(
            assignment_id=candidate.assignment_id,
            evidence_id=candidate.evidence_id,
            entity_id=candidate.entity_id,
            tenant_id=candidate.tenant_id,
            account_id=candidate.account_id,
            source=candidate.source,
            suggestion_id=candidate.suggestion_id,
            assigned_by_actor_type=candidate.assigned_by_actor_type,
            assigned_by_actor_id=candidate.assigned_by_actor_id,
            assigned_at=candidate.assigned_at,
        )
        try:
            with session_scope(self._engine) as session:
                session.add(row)
        except IntegrityError as exc:
            if unique_violation_constraint(exc) == _ASSIGNMENT_DEDUPE_CONSTRAINT:
                winner = self.get_by_evidence(evidence_id)
                if winner is not None:
                    if winner.account_id == account_id:
                        return winner
                    raise ConflictError(
                        f"evidence '{evidence_id}' already has a DIFFERENT account_id "
                        f"('{winner.account_id}') assigned via XeroAccountAssignment "
                        f"'{winner.assignment_id}'; reassignment to '{account_id}' is an explicit, "
                        "separate, NOT-built-here governed correction workflow"
                    ) from exc
                raise PersistenceError(
                    f"could not create XeroAccountAssignment: unique-constraint conflict on "
                    f"evidence_id={evidence_id!r} but no winning row could be re-read: {exc}"
                ) from exc
            raise PersistenceError(f"could not create XeroAccountAssignment: {exc}") from exc
        except SQLAlchemyError as exc:
            raise PersistenceError(f"could not create XeroAccountAssignment: {exc}") from exc
        return candidate

    def get_assignment(self, assignment_id: str) -> XeroAccountAssignment:
        try:
            with session_scope(self._engine) as session:
                row = session.get(XeroAccountAssignmentRow, assignment_id)
                if row is None:
                    raise NotFoundError(f"no XeroAccountAssignment with assignment_id '{assignment_id}'")
                return _assignment_row_to_domain(row)
        except NotFoundError:
            raise
        except DataError as exc:
            if is_invalid_uuid_format(exc):
                raise NotFoundError(
                    f"no XeroAccountAssignment with assignment_id '{assignment_id}' "
                    "(malformed identifier can never exist)"
                ) from exc
            raise PersistenceError(f"could not read XeroAccountAssignment: {exc}") from exc
        except SQLAlchemyError as exc:
            raise PersistenceError(f"could not read XeroAccountAssignment: {exc}") from exc

    def get_by_evidence(self, evidence_id: str) -> Optional[XeroAccountAssignment]:
        try:
            with session_scope(self._engine) as session:
                row = session.query(XeroAccountAssignmentRow).filter_by(evidence_id=evidence_id).one_or_none()
                return _assignment_row_to_domain(row) if row is not None else None
        except DataError as exc:
            if is_invalid_uuid_format(exc):
                return None
            raise PersistenceError(f"could not look up XeroAccountAssignment by evidence_id: {exc}") from exc
        except SQLAlchemyError as exc:
            raise PersistenceError(f"could not look up XeroAccountAssignment by evidence_id: {exc}") from exc
