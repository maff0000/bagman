"""``PostgresEvidenceClassificationRuleRepository`` — durable
implementation of
``services.evidence.classification_rule.EvidenceClassificationRuleRepository``
(CD-6 Slice 5 WI-1).

Preserves, durably, the exact active-identity semantics
``InMemoryEvidenceClassificationRuleRepository`` documents (see that
module's own docstring): a ``create_rule`` call at an identity that
already has an ACTIVE rule with the SAME ``document_type`` returns the
existing row unchanged (idempotent replay); the SAME identity with a
DIFFERENT ``document_type`` raises ``core.errors.ConflictError``.

Mirrors ``persistence/postgres/mailbox_domain_rule_repository.py``'s own
"lock the candidate row with ``with_for_update()``, then decide
create-vs-replace-vs-conflict inside that same transaction" discipline,
and its own "pre-check + IntegrityError backstop, never the pre-check
alone" duality for the real database constraint
(``uq_evidence_classification_rules_active_identity``).
"""
from __future__ import annotations

from typing import Optional

from sqlalchemy.engine import Engine
from sqlalchemy.exc import DataError, IntegrityError, SQLAlchemyError

from core import identity
from core.contract_validation import validate_against_contract
from core.errors import ConflictError, InvalidStateTransitionError, NotFoundError, PersistenceError, ValidationError
from core.timestamps import utc_now
from persistence.postgres.db_errors import is_invalid_uuid_format, unique_violation_constraint
from persistence.postgres.evidence_classification_rule_models import EvidenceClassificationRuleRow
from persistence.postgres.session import get_engine, session_scope
from services.evidence.classification_rule import (
    RULE_STATUS_ACTIVE,
    RULE_STATUS_RETIRED,
    EvidenceClassificationRule,
    EvidenceClassificationRuleRepository,
    normalize_sender_scope_value,
    validate_rule_fields_or_raise,
)
from services.mailbox.domain_rule import normalize_subject_for_policy

_SCHEMA = "evidence/bagman.evidence_classification_rule.v1.schema.json"

_ACTIVE_IDENTITY_CONSTRAINT = "uq_evidence_classification_rules_active_identity"


def _row_to_rule(row: EvidenceClassificationRuleRow) -> EvidenceClassificationRule:
    return EvidenceClassificationRule(
        rule_id=row.rule_id,
        sender_scope_type=row.sender_scope_type,
        sender_scope_value=row.sender_scope_value,
        subject_predicate_type=row.subject_predicate_type,
        subject_predicate_value=row.subject_predicate_value,
        document_type=row.document_type,
        status=row.status,
        source=row.source,
        supersedes_rule_id=row.supersedes_rule_id,
        created_at=row.created_at,
        approved_at=row.approved_at,
        retired_at=row.retired_at,
    )


class PostgresEvidenceClassificationRuleRepository(EvidenceClassificationRuleRepository):
    def __init__(self, engine: Optional[Engine] = None) -> None:
        self._engine = engine or get_engine()

    def create_rule(
        self,
        *,
        sender_scope_type: str,
        sender_scope_value: str,
        subject_predicate_type: str,
        subject_predicate_value: str,
        document_type: str,
        source: str,
        supersedes_rule_id: Optional[str] = None,
    ) -> EvidenceClassificationRule:
        normalized_scope_value = normalize_sender_scope_value(sender_scope_type, sender_scope_value)
        normalized_subject_value = normalize_subject_for_policy(subject_predicate_value) or ""

        validate_rule_fields_or_raise(
            sender_scope_type=sender_scope_type, sender_scope_value=normalized_scope_value,
            subject_predicate_type=subject_predicate_type, subject_predicate_value=normalized_subject_value,
            document_type=document_type, status=RULE_STATUS_ACTIVE, source=source, retired_at=None,
        )

        try:
            with session_scope(self._engine) as session:
                if supersedes_rule_id is not None:
                    superseded = session.get(EvidenceClassificationRuleRow, supersedes_rule_id)
                    if superseded is None:
                        raise NotFoundError(f"no EvidenceClassificationRule with rule_id '{supersedes_rule_id}'")

                existing_row = (
                    session.query(EvidenceClassificationRuleRow)
                    .filter_by(
                        sender_scope_type=sender_scope_type,
                        sender_scope_value=normalized_scope_value,
                        subject_predicate_type=subject_predicate_type,
                        subject_predicate_value=normalized_subject_value,
                        status=RULE_STATUS_ACTIVE,
                    )
                    .with_for_update()
                    .one_or_none()
                )
                if existing_row is not None:
                    if existing_row.document_type == document_type:
                        return _row_to_rule(existing_row)
                    raise ConflictError(
                        f"an ACTIVE EvidenceClassificationRule already exists at this identity with a "
                        f"different document_type ('{existing_row.document_type}' != '{document_type}') — "
                        "retire the existing rule first, then create the replacement"
                    )

                now = utc_now()
                candidate = EvidenceClassificationRule(
                    rule_id=identity.generate_id(),
                    sender_scope_type=sender_scope_type,
                    sender_scope_value=normalized_scope_value,
                    subject_predicate_type=subject_predicate_type,
                    subject_predicate_value=normalized_subject_value,
                    document_type=document_type,
                    status=RULE_STATUS_ACTIVE,
                    source=source,
                    supersedes_rule_id=supersedes_rule_id,
                    created_at=now,
                    approved_at=now,
                    retired_at=None,
                )
                validate_against_contract(candidate.to_dict(), _SCHEMA)
                session.add(
                    EvidenceClassificationRuleRow(
                        rule_id=candidate.rule_id,
                        sender_scope_type=candidate.sender_scope_type,
                        sender_scope_value=candidate.sender_scope_value,
                        subject_predicate_type=candidate.subject_predicate_type,
                        subject_predicate_value=candidate.subject_predicate_value,
                        document_type=candidate.document_type,
                        status=candidate.status,
                        source=candidate.source,
                        supersedes_rule_id=candidate.supersedes_rule_id,
                        created_at=candidate.created_at,
                        approved_at=candidate.approved_at,
                        retired_at=candidate.retired_at,
                    )
                )
                session.flush()
        except (NotFoundError, ValidationError, ConflictError):
            raise
        except IntegrityError as exc:
            if unique_violation_constraint(exc) == _ACTIVE_IDENTITY_CONSTRAINT:
                # Lost a genuine race to another concurrent create at the
                # same identity — re-resolve and apply the same
                # idempotent-vs-conflict decision the pre-check above
                # makes, against whichever row actually won.
                with session_scope(self._engine) as retry_session:
                    winner = (
                        retry_session.query(EvidenceClassificationRuleRow)
                        .filter_by(
                            sender_scope_type=sender_scope_type,
                            sender_scope_value=normalized_scope_value,
                            subject_predicate_type=subject_predicate_type,
                            subject_predicate_value=normalized_subject_value,
                            status=RULE_STATUS_ACTIVE,
                        )
                        .one()
                    )
                    if winner.document_type == document_type:
                        return _row_to_rule(winner)
                    raise ConflictError(
                        f"an ACTIVE EvidenceClassificationRule already exists at this identity with a "
                        f"different document_type ('{winner.document_type}' != '{document_type}') — resolved "
                        "by the real database constraint, not merely the pre-check above"
                    ) from exc
            raise PersistenceError(f"could not create EvidenceClassificationRule: {exc}") from exc
        except SQLAlchemyError as exc:
            raise PersistenceError(f"could not create EvidenceClassificationRule: {exc}") from exc

        return candidate

    def get_rule(self, rule_id: str) -> EvidenceClassificationRule:
        try:
            with session_scope(self._engine) as session:
                row = session.get(EvidenceClassificationRuleRow, rule_id)
                if row is None:
                    raise NotFoundError(f"no EvidenceClassificationRule with rule_id '{rule_id}'")
                return _row_to_rule(row)
        except NotFoundError:
            raise
        except DataError as exc:
            if is_invalid_uuid_format(exc):
                raise NotFoundError(
                    f"no EvidenceClassificationRule with rule_id '{rule_id}' (malformed identifier can never exist)"
                ) from exc
            raise PersistenceError(f"could not read EvidenceClassificationRule: {exc}") from exc
        except SQLAlchemyError as exc:
            raise PersistenceError(f"could not read EvidenceClassificationRule: {exc}") from exc

    def retire_rule(self, rule_id: str) -> EvidenceClassificationRule:
        try:
            with session_scope(self._engine) as session:
                try:
                    row = session.get(EvidenceClassificationRuleRow, rule_id, with_for_update=True)
                except DataError as exc:
                    if is_invalid_uuid_format(exc):
                        raise NotFoundError(
                            f"no EvidenceClassificationRule with rule_id '{rule_id}' "
                            "(malformed identifier can never exist)"
                        ) from exc
                    raise
                if row is None:
                    raise NotFoundError(f"no EvidenceClassificationRule with rule_id '{rule_id}'")
                if row.status != RULE_STATUS_ACTIVE:
                    raise InvalidStateTransitionError(
                        f"EvidenceClassificationRule '{rule_id}' cannot be retired from status '{row.status}' — "
                        "only an ACTIVE rule may be retired (one-way ACTIVE -> RETIRED transition)"
                    )
                row.status = RULE_STATUS_RETIRED
                row.retired_at = utc_now()
                session.flush()
                updated = _row_to_rule(row)
                validate_against_contract(updated.to_dict(), _SCHEMA)
        except (NotFoundError, InvalidStateTransitionError, ValidationError):
            raise
        except SQLAlchemyError as exc:
            raise PersistenceError(f"could not retire EvidenceClassificationRule: {exc}") from exc

        return updated

    def find_active_rule_at_identity(
        self,
        sender_scope_type: str,
        sender_scope_value: str,
        subject_predicate_type: str,
        subject_predicate_value: str,
    ) -> Optional[EvidenceClassificationRule]:
        normalized_scope_value = normalize_sender_scope_value(sender_scope_type, sender_scope_value)
        normalized_subject_value = normalize_subject_for_policy(subject_predicate_value) or ""
        try:
            with session_scope(self._engine) as session:
                row = (
                    session.query(EvidenceClassificationRuleRow)
                    .filter_by(
                        sender_scope_type=sender_scope_type,
                        sender_scope_value=normalized_scope_value,
                        subject_predicate_type=subject_predicate_type,
                        subject_predicate_value=normalized_subject_value,
                        status=RULE_STATUS_ACTIVE,
                    )
                    .one_or_none()
                )
                return _row_to_rule(row) if row is not None else None
        except SQLAlchemyError as exc:
            raise PersistenceError(f"could not look up active EvidenceClassificationRule: {exc}") from exc

    def list_rules(
        self,
        *,
        status: Optional[str] = None,
        sender_scope_type: Optional[str] = None,
        document_type: Optional[str] = None,
        limit: Optional[int] = None,
        offset: int = 0,
    ) -> list[EvidenceClassificationRule]:
        try:
            with session_scope(self._engine) as session:
                query = session.query(EvidenceClassificationRuleRow)
                if status is not None:
                    query = query.filter(EvidenceClassificationRuleRow.status == status)
                if sender_scope_type is not None:
                    query = query.filter(EvidenceClassificationRuleRow.sender_scope_type == sender_scope_type)
                if document_type is not None:
                    query = query.filter(EvidenceClassificationRuleRow.document_type == document_type)
                query = query.order_by(
                    EvidenceClassificationRuleRow.created_at.asc(), EvidenceClassificationRuleRow.rule_id.asc()
                )
                if offset:
                    query = query.offset(offset)
                if limit is not None:
                    query = query.limit(limit)
                rows = query.all()
                return [_row_to_rule(row) for row in rows]
        except SQLAlchemyError as exc:
            raise PersistenceError(f"could not list EvidenceClassificationRule rows: {exc}") from exc
