"""``PostgresEvidenceClassificationRepository`` — durable implementation
of ``services.evidence.classification.EvidenceClassificationRepository``
(CD-6 Slice 5 WI-1).

Concurrency-safe creation — the exact transaction shape
------------------------------------------------------------------------
:meth:`PostgresEvidenceClassificationRepository.create_classification`
performs, inside ONE ``session_scope`` transaction:

1. ``SELECT ... FOR UPDATE`` on the referenced ``evidence_items`` row
   (via ``session.get(EvidenceItemRow, evidence_id, with_for_update=True)``
   — the exact same incantation
   ``PostgresEvidenceRepository.assign_entity`` already uses one layer
   up for its own "lock, then check-then-act" discipline; no other
   repository in this codebase locks a row it does not itself own the
   table for, so this is the first instance of that specific
   cross-table locking pattern — documented here for whoever looks for
   a precedent next).
2. A producer-idempotency check (inside the lock, so a genuine
   concurrent replay of the same producer identity cannot race past it
   into a duplicate insert).
3. ``supersedes_classification_id`` validation (same evidence_id +
   classification_type, branch-prevention pre-check).
4. Resolve the CURRENT classification for ``(evidence_id,
   classification_type)`` — a genuine anti-join query (see
   :func:`_current_classification_query`), never a stored flag.
5. Compare against ``expected_current_classification_id`` — raise
   ``core.errors.ConflictError`` on mismatch.
6. Insert.

Locking the ``EvidenceItem`` row for the whole sequence is what makes
step 4/5 genuinely race-safe against a SECOND concurrent caller trying
to supersede the SAME tip for the SAME evidence item — both callers
cannot hold the lock simultaneously, so the second one's step-4
resolution always sees the first one's completed insert (or the first
one's transaction has not committed yet and the second one blocks until
it does, then re-resolves against the now-current state). This is the
belt; the real database constraints below are the suspenders — a
backstop that should structurally never actually fire in correct
concurrent usage, but must still be correct if it does (mirrors
``persistence/postgres/ai_invocation_repository.py``'s own "an initial
pre-check purely to avoid an unnecessary failed insert attempt... the
constraint-violation path is what actually proves correctness under a
genuine race" doctrine, applied here to THREE different constraint
families instead of one):

* the branch-prevention partial unique index
  (``uq_evidence_classifications_supersedes``);
* the three producer-idempotency partial unique indexes;
* ordinary foreign-key violations (a referenced row was deleted, or a
  malformed id, between the application-level existence check and the
  insert — translated to ``core.errors.NotFoundError``, never a raw
  ``IntegrityError``).

See ``persistence/postgres/mailbox_domain_rule_repository.py``'s own
``upsert_rule`` for the precedent this mirrors: pre-check first (cheap,
correct in the non-racing case), IntegrityError as the real backstop
that translates to the SAME canonical errors under a genuine race —
never the pre-check alone.
"""
from __future__ import annotations

from typing import Optional, Sequence

from sqlalchemy import and_
from sqlalchemy.engine import Engine
from sqlalchemy.exc import DataError, IntegrityError, SQLAlchemyError

from core import identity
from core.contract_validation import validate_against_contract
from core.errors import ConflictError, NotFoundError, PersistenceError, ValidationError
from core.timestamps import utc_now
from persistence.postgres.db_errors import is_foreign_key_violation, is_invalid_uuid_format, unique_violation_constraint
from persistence.postgres.evidence_classification_models import EvidenceClassificationRow
from persistence.postgres.models import EvidenceItemRow
from persistence.postgres.session import get_engine, session_scope
from services.evidence.classification import (
    AI_INVOCATION_SUCCEEDED_STATUS,
    SOURCE_AI_PROPOSAL,
    SOURCE_DETERMINISTIC_RULE,
    EvidenceClassification,
    EvidenceClassificationRepository,
    validate_classification_fields_or_raise,
)

_SCHEMA = "evidence/bagman.evidence_classification.v1.schema.json"

_SUPERSEDES_CONSTRAINT = "uq_evidence_classifications_supersedes"
_RULE_PRODUCER_CONSTRAINT = "uq_evidence_classifications_rule_producer"
_AI_PRODUCER_CONSTRAINT = "uq_evidence_classifications_ai_producer"
_OPERATOR_PRODUCER_CONSTRAINT = "uq_evidence_classifications_operator_producer"
_PRODUCER_CONSTRAINTS = frozenset({_RULE_PRODUCER_CONSTRAINT, _AI_PRODUCER_CONSTRAINT, _OPERATOR_PRODUCER_CONSTRAINT})


def _row_to_classification(row: EvidenceClassificationRow) -> EvidenceClassification:
    return EvidenceClassification(
        classification_id=row.classification_id,
        evidence_id=row.evidence_id,
        classification_type=row.classification_type,
        document_type=row.document_type,
        status=row.status,
        source=row.source,
        created_at=row.created_at,
        confidence=row.confidence,
        rule_id=row.rule_id,
        ai_invocation_id=row.ai_invocation_id,
        operator_action_id=row.operator_action_id,
        reason_codes=tuple(row.reason_codes or []),
        supersedes_classification_id=row.supersedes_classification_id,
    )


def _producer_filter(
    *, source: str, evidence_id: str, classification_type: str,
    rule_id: Optional[str], ai_invocation_id: Optional[str], operator_action_id: Optional[str],
):
    """The SQLAlchemy filter expression matching one of the three
    producer-identity partial unique indexes, depending on ``source`` —
    reused for both the pre-check query and the post-IntegrityError
    replay re-fetch."""
    base = and_(
        EvidenceClassificationRow.evidence_id == evidence_id,
        EvidenceClassificationRow.classification_type == classification_type,
        EvidenceClassificationRow.source == source,
    )
    if source == SOURCE_DETERMINISTIC_RULE:
        return and_(base, EvidenceClassificationRow.rule_id == rule_id)
    if source == SOURCE_AI_PROPOSAL:
        return and_(base, EvidenceClassificationRow.ai_invocation_id == ai_invocation_id)
    return and_(base, EvidenceClassificationRow.operator_action_id == operator_action_id)


def _current_classification_query(session, evidence_id: str, classification_type: str):
    """The row no OTHER row's ``supersedes_classification_id`` points
    at, for this ``(evidence_id, classification_type)`` — a genuine
    ``NOT IN`` anti-join, never a stored flag (see
    ``services.evidence.classification`` module docstring)."""
    superseded_ids_subquery = (
        session.query(EvidenceClassificationRow.supersedes_classification_id)
        .filter(
            EvidenceClassificationRow.evidence_id == evidence_id,
            EvidenceClassificationRow.classification_type == classification_type,
            EvidenceClassificationRow.supersedes_classification_id.isnot(None),
        )
    )
    return (
        session.query(EvidenceClassificationRow)
        .filter(
            EvidenceClassificationRow.evidence_id == evidence_id,
            EvidenceClassificationRow.classification_type == classification_type,
            ~EvidenceClassificationRow.classification_id.in_(superseded_ids_subquery),
        )
    )


class PostgresEvidenceClassificationRepository(EvidenceClassificationRepository):
    """PostgreSQL-backed ``EvidenceClassificationRepository``.

    Dependencies are the SAME duck-typed shape
    ``InMemoryEvidenceClassificationRepository`` takes — normally a
    ``PostgresEvidenceRepository``/
    ``PostgresEvidenceClassificationRuleRepository``/
    ``PostgresAIInvocationRepository`` sharing the same engine, but
    never imported as a concrete type here.
    """

    def __init__(
        self,
        *,
        evidence_repository,
        rule_repository,
        ai_invocation_repository,
        engine: Optional[Engine] = None,
    ) -> None:
        self._evidence_repository = evidence_repository
        self._rule_repository = rule_repository
        self._ai_invocation_repository = ai_invocation_repository
        self._engine = engine or get_engine()

    def create_classification(
        self,
        *,
        evidence_id: str,
        classification_type: str,
        document_type: str,
        status: str,
        source: str,
        confidence: Optional[float] = None,
        rule_id: Optional[str] = None,
        ai_invocation_id: Optional[str] = None,
        operator_action_id: Optional[str] = None,
        reason_codes: Optional[Sequence[str]] = None,
        supersedes_classification_id: Optional[str] = None,
        expected_current_classification_id: Optional[str] = None,
    ) -> EvidenceClassification:
        validate_classification_fields_or_raise(
            classification_type=classification_type, document_type=document_type, status=status, source=source,
            confidence=confidence, rule_id=rule_id, ai_invocation_id=ai_invocation_id,
            operator_action_id=operator_action_id,
        )

        # Referenced-entity existence validation — "prove real before
        # trusting" — performed BEFORE the transaction below (read-only
        # checks against OTHER tables' own repositories; they do not
        # need to happen inside the evidence_items row lock).
        self._evidence_repository.get_evidence(evidence_id)
        if source == SOURCE_DETERMINISTIC_RULE:
            self._rule_repository.get_rule(rule_id)
        elif source == SOURCE_AI_PROPOSAL:
            invocation = self._ai_invocation_repository.get_invocation(ai_invocation_id)
            if invocation.status != AI_INVOCATION_SUCCEEDED_STATUS:
                raise ValidationError(
                    f"AIInvocation '{ai_invocation_id}' has status '{invocation.status}', not "
                    f"'{AI_INVOCATION_SUCCEEDED_STATUS}' — a classification may never be linked to "
                    "FAILED/REJECTED/TIMED_OUT/CANCELLED AI work"
                )

        reason_codes_list = list(reason_codes) if reason_codes else []
        now = utc_now()
        producer_filter = _producer_filter(
            source=source, evidence_id=evidence_id, classification_type=classification_type,
            rule_id=rule_id, ai_invocation_id=ai_invocation_id, operator_action_id=operator_action_id,
        )

        try:
            with session_scope(self._engine) as session:
                # 1. Lock the canonical EvidenceItem row for the life of
                # this transaction — see module docstring.
                evidence_row = session.get(EvidenceItemRow, evidence_id, with_for_update=True)
                if evidence_row is None:
                    raise NotFoundError(f"no EvidenceItem with evidence_id '{evidence_id}'")

                # 2. Producer-idempotency check, inside the lock.
                existing_row = session.query(EvidenceClassificationRow).filter(producer_filter).one_or_none()
                if existing_row is not None:
                    return _row_to_classification(existing_row)

                # 3. supersedes_classification_id validation.
                if supersedes_classification_id is not None:
                    superseded_row = session.get(EvidenceClassificationRow, supersedes_classification_id)
                    if superseded_row is None:
                        raise NotFoundError(
                            f"no EvidenceClassification with classification_id '{supersedes_classification_id}'"
                        )
                    if (
                        superseded_row.evidence_id != evidence_id
                        or superseded_row.classification_type != classification_type
                    ):
                        raise ValidationError(
                            f"supersedes_classification_id '{supersedes_classification_id}' does not share this "
                            "classification's own evidence_id/classification_type"
                        )
                    branch_exists = (
                        session.query(EvidenceClassificationRow)
                        .filter(EvidenceClassificationRow.supersedes_classification_id == supersedes_classification_id)
                        .first()
                    )
                    if branch_exists is not None:
                        raise ConflictError(
                            f"classification_id '{supersedes_classification_id}' is already superseded by "
                            f"'{branch_exists.classification_id}' — at most one row may supersede any given "
                            "classification"
                        )

                # 4/5. Resolve the current classification and compare.
                current_row = _current_classification_query(session, evidence_id, classification_type).one_or_none()
                current_id = current_row.classification_id if current_row is not None else None
                if current_id != expected_current_classification_id:
                    raise ConflictError(
                        f"expected_current_classification_id={expected_current_classification_id!r} does not "
                        f"match the actual current EvidenceClassification for (evidence_id={evidence_id!r}, "
                        f"classification_type={classification_type!r}), which is {current_id!r}"
                    )

                # 6. Insert.
                candidate = EvidenceClassification(
                    classification_id=identity.generate_id(),
                    evidence_id=evidence_id,
                    classification_type=classification_type,
                    document_type=document_type,
                    status=status,
                    source=source,
                    created_at=now,
                    confidence=confidence,
                    rule_id=rule_id,
                    ai_invocation_id=ai_invocation_id,
                    operator_action_id=operator_action_id,
                    reason_codes=tuple(reason_codes_list),
                    supersedes_classification_id=supersedes_classification_id,
                )
                validate_against_contract(candidate.to_dict(), _SCHEMA)
                session.add(
                    EvidenceClassificationRow(
                        classification_id=candidate.classification_id,
                        evidence_id=candidate.evidence_id,
                        classification_type=candidate.classification_type,
                        document_type=candidate.document_type,
                        status=candidate.status,
                        source=candidate.source,
                        confidence=candidate.confidence,
                        rule_id=candidate.rule_id,
                        ai_invocation_id=candidate.ai_invocation_id,
                        operator_action_id=candidate.operator_action_id,
                        reason_codes=list(candidate.reason_codes),
                        supersedes_classification_id=candidate.supersedes_classification_id,
                        created_at=candidate.created_at,
                    )
                )
                session.flush()
        except (NotFoundError, ValidationError, ConflictError):
            raise
        except IntegrityError as exc:
            constraint = unique_violation_constraint(exc)
            if constraint in _PRODUCER_CONSTRAINTS:
                # Someone else's concurrent replay of the SAME producer
                # identity won the race — return THEIR row (the same
                # idempotent-return semantics the pre-check above
                # provides in the non-racing case).
                with session_scope(self._engine) as retry_session:
                    winner = retry_session.query(EvidenceClassificationRow).filter(producer_filter).one()
                    return _row_to_classification(winner)
            if constraint == _SUPERSEDES_CONSTRAINT:
                raise ConflictError(
                    f"classification_id '{supersedes_classification_id}' is already superseded by another row "
                    "— resolved by the real database constraint, not merely the pre-check above"
                ) from exc
            if is_foreign_key_violation(exc):
                raise NotFoundError(f"a referenced row does not exist: {exc}") from exc
            raise PersistenceError(f"could not create EvidenceClassification: {exc}") from exc
        except SQLAlchemyError as exc:
            raise PersistenceError(f"could not create EvidenceClassification: {exc}") from exc

        return candidate

    def get_classification(self, classification_id: str) -> EvidenceClassification:
        try:
            with session_scope(self._engine) as session:
                row = session.get(EvidenceClassificationRow, classification_id)
                if row is None:
                    raise NotFoundError(f"no EvidenceClassification with classification_id '{classification_id}'")
                return _row_to_classification(row)
        except NotFoundError:
            raise
        except DataError as exc:
            if is_invalid_uuid_format(exc):
                raise NotFoundError(
                    f"no EvidenceClassification with classification_id '{classification_id}' "
                    "(malformed identifier can never exist)"
                ) from exc
            raise PersistenceError(f"could not read EvidenceClassification: {exc}") from exc
        except SQLAlchemyError as exc:
            raise PersistenceError(f"could not read EvidenceClassification: {exc}") from exc

    def get_current_classification(
        self, evidence_id: str, classification_type: str
    ) -> Optional[EvidenceClassification]:
        try:
            with session_scope(self._engine) as session:
                row = _current_classification_query(session, evidence_id, classification_type).one_or_none()
                return _row_to_classification(row) if row is not None else None
        except SQLAlchemyError as exc:
            raise PersistenceError(f"could not resolve current EvidenceClassification: {exc}") from exc

    def list_classification_history(
        self, evidence_id: str, classification_type: str
    ) -> list[EvidenceClassification]:
        try:
            with session_scope(self._engine) as session:
                rows = (
                    session.query(EvidenceClassificationRow)
                    .filter_by(evidence_id=evidence_id, classification_type=classification_type)
                    .order_by(EvidenceClassificationRow.created_at.asc(), EvidenceClassificationRow.classification_id.asc())
                    .all()
                )
                return [_row_to_classification(r) for r in rows]
        except SQLAlchemyError as exc:
            raise PersistenceError(f"could not list EvidenceClassification history: {exc}") from exc
