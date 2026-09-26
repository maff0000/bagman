"""`PostgresAIInvocationRepository` — durable implementation of
`ai.invocation.AIInvocationRepository` (CD-5 WI-1, PID §27/§73-74).

Preserves, durably, the exact one-active-invocation-per-subject
concurrency semantics `ai.invocation.InMemoryAIInvocationRepository`
documents (see that module's docstring for the full "subject" and
retry-vs-conflict rationale): a `create_invocation` call for a
`(task_id, task_version, primary_input_reference)` subject that already
has a NON-terminal invocation in flight raises
`core.errors.ActiveInvocationConflictError`; once that invocation
reaches a terminal state, the same subject may be invoked again, always
producing a genuinely NEW, distinct row.

Unlike the in-memory version, this implementation never decides the
"is one already active" outcome from a separate application-level
check-then-insert alone (which could race under concurrent callers): it
always attempts the insert; the database's own partial unique index
(`uq_ai_invocations_active_subject` — see
`persistence/postgres/ai_invocation_models.py`) is the real source of
truth. An initial `find_active_invocation` pre-check is still performed
first (exactly like `PostgresIntakeRepository.create_intake_record`
does for its idempotency-key hint) purely to avoid an unnecessary
failed insert attempt in the common, non-racing case — but the
constraint-violation path below is what actually proves correctness
under a genuine race (proven against a real disposable PostgreSQL
container in `tests/persistence/test_ai_invocation_repository.py`),
never the stale pre-check alone.

Stale-`RUNNING` recovery (CD-6 reliability delta, PID §100.14/§100.16)
------------------------------------------------------------------------
See `ai.invocation`'s own module docstring ("Stale-`RUNNING` recovery")
for the full architecture/reasoning — this is the real, durable half of
that bounded backstop. `_recover_if_stale` locks the candidate row with
`SELECT ... FOR UPDATE` (the same discipline `transition_status` below
already uses) before re-checking staleness and transitioning it, so a
genuine concurrent race between two callers both discovering the same
stale row cannot double-recover/double-audit it — the loser's lock wait
resolves against an already-terminal row, and its own re-check inside
the lock (never trusting the pre-lock read alone) reports "nothing to
recover" rather than attempting a second, now-invalid transition. The
stale-recovery audit event is emitted via a separate, freshly-scoped
`AuditRepository` call AFTER the row-transitioning transaction commits
— never inside the same transaction/session — mirroring exactly how
every other `AIInvocation` audit event in this codebase is emitted
(`agent.claude_code.orchestrator`/`ai.gateway.background`'s own
"repository call, then a separate `record_audit_event` call" pattern),
not a new cross-cutting transactional coupling.
"""
from __future__ import annotations

from typing import Any, Mapping, Optional

from sqlalchemy.engine import Engine
from sqlalchemy.exc import DataError, IntegrityError, SQLAlchemyError

from ai.invocation import (
    AIInvocation,
    AIInvocationRepository,
    DEFAULT_INFERENCE_BACKEND,
    STALE_RECOVERY_AUDIT_EVENT_TYPE,
    STALE_RUNNING_THRESHOLD_SECONDS,
    derive_primary_input_reference,
    is_stale_running,
    recover_stale_invocation,
    transition,
    validate_role_provider_capability_pairing,
)
from core import actor, identity
from core.audit import AuditRepository
from core.contract_validation import validate_against_contract
from core.errors import (
    ActiveInvocationConflictError,
    InvalidStateTransitionError,
    NotFoundError,
    PersistenceError,
    ValidationError,
)
from core.timestamps import utc_now
from persistence.postgres.ai_invocation_models import AIInvocationRow
from persistence.postgres.db_errors import is_invalid_uuid_format, unique_violation_constraint
from persistence.postgres.session import get_engine, session_scope

_SCHEMA = "ai/bagman.ai_invocation.v1.schema.json"

_ACTIVE_SUBJECT_CONSTRAINT = "uq_ai_invocations_active_subject"


def _row_to_invocation(row: AIInvocationRow) -> AIInvocation:
    return AIInvocation(
        ai_invocation_id=row.ai_invocation_id,
        task_id=row.task_id,
        task_version=row.task_version,
        role=row.role,
        provider=row.provider,
        capability_alias=row.capability_alias,
        inference_backend=row.inference_backend,
        provider_model=row.provider_model,
        started_at=row.started_at,
        completed_at=row.completed_at,
        status=row.status,
        correlation_id=row.correlation_id,
        actor_type=row.actor_type,
        actor_id=row.actor_id,
        input_references=dict(row.input_references),
        prompt_contract_version=row.prompt_contract_version,
        output=dict(row.output) if row.output is not None else None,
        confidence=row.confidence,
        validation_result=dict(row.validation_result) if row.validation_result is not None else None,
        error_code=row.error_code,
        usage_metadata=dict(row.usage_metadata),
        latency_ms=row.latency_ms,
    )


def _row_from_invocation(candidate: AIInvocation) -> AIInvocationRow:
    return AIInvocationRow(
        ai_invocation_id=candidate.ai_invocation_id,
        task_id=candidate.task_id,
        task_version=candidate.task_version,
        role=candidate.role,
        provider=candidate.provider,
        capability_alias=candidate.capability_alias,
        inference_backend=candidate.inference_backend,
        provider_model=candidate.provider_model,
        started_at=candidate.started_at,
        completed_at=candidate.completed_at,
        status=candidate.status,
        correlation_id=candidate.correlation_id,
        actor_type=candidate.actor_type,
        actor_id=candidate.actor_id,
        input_references=dict(candidate.input_references),
        prompt_contract_version=candidate.prompt_contract_version,
        output=dict(candidate.output) if candidate.output is not None else None,
        confidence=candidate.confidence,
        validation_result=dict(candidate.validation_result) if candidate.validation_result is not None else None,
        error_code=candidate.error_code,
        usage_metadata=dict(candidate.usage_metadata),
        latency_ms=candidate.latency_ms,
    )


class PostgresAIInvocationRepository(AIInvocationRepository):
    """PostgreSQL-backed `AIInvocationRepository`. Stateless: every
    method reads/writes the database directly via a fresh `Session`.
    """

    def __init__(self, engine: Optional[Engine] = None, *, audit_repository: Optional[AuditRepository] = None) -> None:
        self._engine = engine or get_engine()
        # Lazy import mirrors `app/api/composition.py`'s own production
        # composition style. A fresh `PostgresAuditRepository` per call
        # is fine (unlike the in-memory repository, this one is
        # stateless — every method opens its own session against the
        # SAME database) — see module docstring's "Stale-RUNNING
        # recovery" section for why this exists at all.
        if audit_repository is None:
            from persistence.postgres.audit_repository import PostgresAuditRepository

            audit_repository = PostgresAuditRepository(self._engine)
        self._audit_repository: AuditRepository = audit_repository

    def _recover_if_stale(
        self, *, task_id: str, task_version: int, primary_input_reference: str
    ) -> Optional[AIInvocation]:
        """Lock, re-check, and (if genuinely stale) recover the active
        row for this subject, if any. Returns the recovered `AIInvocation`
        (already `TIMED_OUT`) if a recovery happened, else `None` — see
        module docstring's "Stale-RUNNING recovery" section for the
        locking discipline this relies on to stay race-safe.
        """
        try:
            with session_scope(self._engine) as session:
                row = (
                    session.query(AIInvocationRow)
                    .filter(
                        AIInvocationRow.task_id == task_id,
                        AIInvocationRow.task_version == task_version,
                        AIInvocationRow.primary_input_reference == primary_input_reference,
                        AIInvocationRow.status.in_(("REQUESTED", "RUNNING")),
                    )
                    .with_for_update()
                    .one_or_none()
                )
                if row is None:
                    return None

                current = _row_to_invocation(row)
                # Re-check UNDER THE LOCK — never trust a pre-lock read
                # alone (a concurrent recoverer, or the row's own
                # legitimate owner, could have already resolved it).
                if not is_stale_running(current):
                    return None

                recovered = recover_stale_invocation(current)
                row.status = recovered.status
                row.completed_at = recovered.completed_at
                row.error_code = recovered.error_code
                row.usage_metadata = dict(recovered.usage_metadata)
                session.flush()
        except SQLAlchemyError as exc:
            raise PersistenceError(f"could not recover stale AIInvocation: {exc}") from exc

        return recovered

    def _record_stale_recovery_audit_event(self, recovered: AIInvocation) -> None:
        # `error_code`/`status` read from `recovered` itself, never a
        # hardcoded constant — `recover_stale_invocation` targets
        # different terminal states depending on the row's pre-recovery
        # status (`TIMED_OUT` for a genuinely-dispatched `RUNNING` row,
        # `FAILED` for a `REQUESTED` row that never reached a provider
        # at all; see that function's own docstring) — hardcoding
        # `STALE_RECOVERY_ERROR_CODE` here would make this audit event
        # lie about `REQUESTED`-row recoveries.
        self._audit_repository.record_audit_event(
            event_type=STALE_RECOVERY_AUDIT_EVENT_TYPE,
            actor_type=actor.SYSTEM,
            actor_id="ai-invocation-stale-recovery",
            subject_type="AIInvocation",
            subject_id=recovered.ai_invocation_id,
            correlation_id=recovered.correlation_id,
            causation_id=None,
            payload={
                "task_id": recovered.task_id,
                "task_version": recovered.task_version,
                "recovered_status": recovered.status,
                "error_code": recovered.error_code,
                "stale_threshold_seconds": STALE_RUNNING_THRESHOLD_SECONDS,
            },
        )

    def create_invocation(
        self,
        *,
        task_id: str,
        task_version: int,
        role: str,
        provider: str,
        capability_alias: Optional[str],
        input_references: Mapping[str, Any],
        actor_type: str,
        actor_id: str,
        correlation_id: Optional[str] = None,
        prompt_contract_version: Optional[str] = None,
        inference_backend: str = DEFAULT_INFERENCE_BACKEND,
    ) -> AIInvocation:
        # Every domain-level shape/pairing/subject-derivation rule
        # lives in exactly ONE place (`ai.invocation`), reused here
        # rather than duplicated.
        validate_role_provider_capability_pairing(
            role=role, provider=provider, capability_alias=capability_alias, inference_backend=inference_backend
        )
        if not actor.is_valid(actor_type):
            raise ValidationError(
                f"actor_type '{actor_type}' is not one of the closed set {sorted(actor.ALL)} (PID §14)"
            )

        primary_ref = derive_primary_input_reference(input_references)

        # Bounded stale-RUNNING recovery backstop (CD-6 reliability
        # delta) — BEFORE the ordinary pre-check below, exactly per
        # `ai.invocation`'s module docstring: an abandoned row must
        # never permanently block this subject. Emitting the audit
        # event happens OUTSIDE `_recover_if_stale`'s own transaction —
        # see module docstring.
        recovered = self._recover_if_stale(
            task_id=task_id, task_version=task_version, primary_input_reference=primary_ref
        )
        if recovered is not None:
            self._record_stale_recovery_audit_event(recovered)

        existing = self.find_active_invocation(
            task_id=task_id, task_version=task_version, primary_input_reference=primary_ref
        )
        if existing is not None:
            raise ActiveInvocationConflictError(
                f"an active (non-terminal) AIInvocation '{existing.ai_invocation_id}' already "
                f"exists for task_id='{task_id}', task_version={task_version}, "
                f"primary_input_reference='{primary_ref}' (PID §73)"
            )

        try:
            candidate = AIInvocation(
                ai_invocation_id=identity.generate_id(),
                task_id=task_id,
                task_version=task_version,
                role=role,
                provider=provider,
                capability_alias=capability_alias,
                inference_backend=inference_backend,
                status="REQUESTED",
                started_at=utc_now(),
                correlation_id=correlation_id if correlation_id is not None else identity.generate_id(),
                actor_type=actor_type,
                actor_id=actor_id,
                input_references=dict(input_references),
                prompt_contract_version=prompt_contract_version,
            )
            validate_against_contract(candidate.to_dict(), _SCHEMA)
        except ValidationError:
            raise
        except Exception as exc:  # noqa: BLE001 - never leak a raw exception
            raise ValidationError(f"could not create AIInvocation: {exc}") from exc

        row = _row_from_invocation(candidate)

        try:
            with session_scope(self._engine) as session:
                session.add(row)
        except IntegrityError as exc:
            if unique_violation_constraint(exc) == _ACTIVE_SUBJECT_CONSTRAINT:
                raise ActiveInvocationConflictError(
                    f"an active (non-terminal) AIInvocation already exists for "
                    f"task_id='{task_id}', task_version={task_version}, "
                    f"primary_input_reference='{primary_ref}' (PID §73) — resolved by the real "
                    "database constraint, not merely the pre-check above"
                ) from exc
            raise PersistenceError(f"could not create AIInvocation: {exc}") from exc
        except SQLAlchemyError as exc:
            raise PersistenceError(f"could not create AIInvocation: {exc}") from exc

        return candidate

    def get_invocation(self, ai_invocation_id: str) -> AIInvocation:
        try:
            with session_scope(self._engine) as session:
                row = session.get(AIInvocationRow, ai_invocation_id)
                if row is None:
                    raise NotFoundError(f"no AIInvocation with ai_invocation_id '{ai_invocation_id}'")
                return _row_to_invocation(row)
        except NotFoundError:
            raise
        except DataError as exc:
            if is_invalid_uuid_format(exc):
                raise NotFoundError(
                    f"no AIInvocation with ai_invocation_id '{ai_invocation_id}' "
                    "(malformed identifier can never exist)"
                ) from exc
            raise PersistenceError(f"could not read AIInvocation: {exc}") from exc
        except SQLAlchemyError as exc:
            raise PersistenceError(f"could not read AIInvocation: {exc}") from exc

    def transition_status(self, ai_invocation_id: str, new_status: str, **field_updates: Any) -> AIInvocation:
        try:
            with session_scope(self._engine) as session:
                # SELECT ... FOR UPDATE: locks the row for the rest of
                # this transaction so a concurrent second
                # transition_status call cannot race past the
                # allowed-transition check (mirrors
                # PostgresIntakeRepository.transition_status).
                try:
                    row = session.get(AIInvocationRow, ai_invocation_id, with_for_update=True)
                except DataError as exc:
                    if is_invalid_uuid_format(exc):
                        raise NotFoundError(
                            f"no AIInvocation with ai_invocation_id '{ai_invocation_id}' "
                            "(malformed identifier can never exist)"
                        ) from exc
                    raise
                if row is None:
                    raise NotFoundError(f"no AIInvocation with ai_invocation_id '{ai_invocation_id}'")

                current = _row_to_invocation(row)
                updated = transition(current, new_status, **field_updates)

                row.status = updated.status
                row.completed_at = updated.completed_at
                row.provider_model = updated.provider_model
                row.prompt_contract_version = updated.prompt_contract_version
                row.output = dict(updated.output) if updated.output is not None else None
                row.confidence = updated.confidence
                row.validation_result = (
                    dict(updated.validation_result) if updated.validation_result is not None else None
                )
                row.error_code = updated.error_code
                row.usage_metadata = dict(updated.usage_metadata)
                row.latency_ms = updated.latency_ms
                session.flush()
        except (NotFoundError, InvalidStateTransitionError, ValidationError):
            raise
        except SQLAlchemyError as exc:
            raise PersistenceError(f"could not transition AIInvocation: {exc}") from exc

        return updated

    def find_active_invocation(
        self, *, task_id: str, task_version: int, primary_input_reference: str
    ) -> Optional[AIInvocation]:
        try:
            with session_scope(self._engine) as session:
                row = (
                    session.query(AIInvocationRow)
                    .filter(
                        AIInvocationRow.task_id == task_id,
                        AIInvocationRow.task_version == task_version,
                        AIInvocationRow.primary_input_reference == primary_input_reference,
                        AIInvocationRow.status.in_(("REQUESTED", "RUNNING")),
                    )
                    .one_or_none()
                )
                found = _row_to_invocation(row) if row is not None else None
        except SQLAlchemyError as exc:
            raise PersistenceError(f"could not look up active AIInvocation: {exc}") from exc

        if found is None:
            return None

        # Bounded stale-RUNNING recovery backstop (CD-6 reliability
        # delta) — see `ai.invocation.AIInvocationRepository
        # .find_active_invocation`'s own docstring: an abandoned row is
        # never reported as active to a caller (a GUI "grey out the
        # button" check, or `create_invocation`'s own pre-check above).
        if not is_stale_running(found):
            return found

        recovered = self._recover_if_stale(
            task_id=task_id, task_version=task_version, primary_input_reference=primary_input_reference
        )
        if recovered is not None:
            self._record_stale_recovery_audit_event(recovered)
        return None

    def list_invocations(
        self,
        *,
        task_id: Optional[str] = None,
        task_version: Optional[int] = None,
        role: Optional[str] = None,
        status: Optional[str] = None,
        correlation_id: Optional[str] = None,
        started_at_from=None,
        started_at_to=None,
        primary_input_reference: Optional[str] = None,
        limit: Optional[int] = None,
        offset: int = 0,
    ) -> list[AIInvocation]:
        try:
            with session_scope(self._engine) as session:
                query = session.query(AIInvocationRow)
                if task_id is not None:
                    query = query.filter(AIInvocationRow.task_id == task_id)
                if task_version is not None:
                    query = query.filter(AIInvocationRow.task_version == task_version)
                if role is not None:
                    query = query.filter(AIInvocationRow.role == role)
                if status is not None:
                    query = query.filter(AIInvocationRow.status == status)
                if correlation_id is not None:
                    query = query.filter(AIInvocationRow.correlation_id == correlation_id)
                if started_at_from is not None:
                    query = query.filter(AIInvocationRow.started_at >= started_at_from)
                if started_at_to is not None:
                    query = query.filter(AIInvocationRow.started_at <= started_at_to)
                if primary_input_reference is not None:
                    # The REAL stored-generated column (see
                    # persistence/postgres/ai_invocation_models.py's own
                    # "Concurrency guard" docstring section) — a plain
                    # indexed equality filter, not a derived/computed
                    # comparison as the in-memory repository must do.
                    query = query.filter(AIInvocationRow.primary_input_reference == primary_input_reference)
                query = query.order_by(
                    AIInvocationRow.started_at.desc(), AIInvocationRow.ai_invocation_id.desc()
                )
                if offset:
                    query = query.offset(offset)
                if limit is not None:
                    query = query.limit(limit)
                rows = query.all()
                return [_row_to_invocation(row) for row in rows]
        except SQLAlchemyError as exc:
            raise PersistenceError(f"could not list AIInvocation rows: {exc}") from exc
