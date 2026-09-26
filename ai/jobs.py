"""``BackgroundJob`` — CD-6 §103 Inference Architecture Ruling's durable,
Postgres-backed job mechanism for Trinity backlog/overflow processing
(PID §103 point 7, §103.4 item 1).

Structured closely on `ai.invocation`'s own shape (frozen dataclass +
repository ABC + in-memory implementation in this module; Postgres
implementation in `persistence.postgres.background_job_models` /
`persistence.postgres.background_job_repository`, exactly mirroring the
`ai_invocation_models.py`/`ai_invocation_repository.py` split) — read
that module's docstring first if anything here is unclear on a point it
already established (the stale-recovery pattern, the audit-emission
discipline, the "single shared predicate/transition function used by
both concrete repositories" doctrine).

This is a DIFFERENT concurrency/identity doctrine from `AIInvocation`
------------------------------------------------------------------------
`ai.invocation.AIInvocationRepository.create_invocation` creates a NEW
row on every call for a given subject once the prior one reaches a
terminal state (PID §74: "retries create auditable invocation
attempts... do not overwrite the history of a failed AI request") — and
enforces "at most one ACTIVE row" via a real database constraint.
`BackgroundJobRepository.submit_job` is the deliberate OPPOSITE: it is
IDEMPOTENT on `idempotency_key` — a second `submit_job` call with the
same key returns the EXISTING row UNCHANGED, never a second row. A job
submission means "make sure this exact piece of backlog work exists
exactly once", not "record a new attempt" — the same idempotency-key
REPLAY doctrine `services.evidence.intake.intake.intake` already
establishes for `IntakeRecord` (see that module's own docstring, which
`ai.invocation`'s own module docstring explicitly names and contrasts
itself against). `BackgroundJob` does not need a second, `AIInvocation`
-style "one active per subject" guard at all: many jobs may legitimately
be `PENDING`/`CLAIMED`/`IN_PROGRESS` at once (a backlog, by definition,
is a queue of many outstanding items) — the ONLY uniqueness this module
enforces is "this exact `idempotency_key` names exactly one job, ever".

Scope: TRINITY_CORE_OVERFLOW only, for this first delivery
------------------------------------------------------------------------
CD-6 §103 point 6: the normal `MAC_LOCAL` background path stays exactly
as it is today — fully synchronous, via `ai.gateway.background
.run_background_task`, never queued. This module exists ONLY for
Trinity backlog/overflow processing; `submit_job` therefore explicitly
REJECTS `inference_backend="MAC_LOCAL"` (and any `capability_alias`
other than `"trinity-core"`) — see :func:`validate_background_job_backend_and_alias`.
A future delivery may extend this to `MAC_LOCAL` jobs if BAGMAN's own
normal path is ever itself queued; nothing in this module assumes that
will never happen, but nothing here builds it speculatively either.

Content-agnostic by construction
------------------------------------------------------------------------
`evidence_content` is accepted here as an already-resolved,
already-rendered plain-text string — mirroring
`ai.gateway.background.run_background_task`'s own identical, explicitly
documented scope limit (see that module's docstring: "Evidence-content
resolution is deliberately OUT of this function's own scope"). Whoever
submits a job (`scripts.process_background_job_overflow`'s own
`submit` mode) resolves the real content BEFORE calling `submit_job` —
this module never itself fetches/renders evidence.

Attempt-counting design (a documented judgment call)
------------------------------------------------------------------------
The dispatch text describing this module floated incrementing
`attempt_count` at `claim_next_pending` time for a `FAILED_RETRYABLE`
-origin claim, but deferring the increment to `mark_in_progress` for a
first-time `PENDING` claim, leaving the exact split as an implementer
judgment call ("your call exactly where the increment happens, just be
consistent and document it"). This module makes a DIFFERENT, simpler,
and — critically — more ROBUST choice: **`attempt_count` increments by
exactly 1 at `mark_in_progress` time only, unconditionally, for every
`CLAIMED -> IN_PROGRESS` transition; `claim_next_pending` never touches
`attempt_count` at all, regardless of whether the claimed row
originated from `PENDING` or `FAILED_RETRYABLE`.**

Why: a split-by-origin-status scheme breaks under
:func:`recover_stale_claims`. Consider a `CLAIMED` row (never reached
`IN_PROGRESS`, so under EITHER scheme its `attempt_count` has not yet
been bumped for this attempt) that goes stale and is recovered back to
`PENDING` — its `attempt_count` is UNCHANGED, exactly as if it were a
fresh `PENDING` row. Under the split scheme, a worker later re-claiming
it would see `status == "PENDING"` (not `"FAILED_RETRYABLE"`) and
therefore skip the claim-time increment — correct so far — but if a
LATER stale-`IN_PROGRESS` recovery (a genuinely different job, or the
same job on a later cycle) ever produced a recovered-to-`PENDING` row
whose `attempt_count` was already `> 0` (a real, reachable case: an
`IN_PROGRESS` row IS already-incremented, by construction, the moment
it reached that state), a naive "increment at `mark_in_progress` only
when `attempt_count == 0`" rule (the literal reading of "becomes 1 the
moment `mark_in_progress` is called") would then WRONGLY skip
incrementing on that job's next real attempt, silently undercounting
forever. The unconditional-at-`mark_in_progress` rule sidesteps this
entirely: `attempt_count` always means exactly "how many times this job
has actually STARTED being dispatched" (never "how many times it has
been claimed", which can legitimately differ under stale-claim
recovery), incremented exactly once per genuine dispatch attempt,
regardless of which state a given `CLAIMED` row's claim originated
from. `mark_failed`'s own `attempt_count < max_attempts` retry-budget
check, and :func:`recover_stale_claims`'s own identical check, both
read this same, single, unambiguous counter.
"""
from __future__ import annotations

import abc
import dataclasses
import threading
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Mapping, Optional

from ai.invocation import CAPABILITY_ALIAS_BACKENDS, STALE_RUNNING_THRESHOLD_SECONDS
from core import actor, identity
from core.audit import AuditRepository, InMemoryAuditRepository
from core.errors import InvalidStateTransitionError, NotFoundError, ValidationError
from core.timestamps import utc_now

#: The closed set of `BackgroundJob` lifecycle states (CD-6 §103,
#: PID §103.4 item 1's own literal spec).
BACKGROUND_JOB_STATUSES = frozenset(
    {"PENDING", "CLAIMED", "IN_PROGRESS", "SUCCEEDED", "FAILED_RETRYABLE", "FAILED_TERMINAL"}
)

#: States from which no further transition is possible.
BACKGROUND_JOB_TERMINAL_STATUSES = frozenset({"SUCCEEDED", "FAILED_TERMINAL"})

#: The single source of truth for valid `BackgroundJob` state
#: transitions — mirrors `ai.invocation.ALLOWED_TRANSITIONS`'s own
#: "one dict, never re-derived" doctrine exactly.
BACKGROUND_JOB_ALLOWED_TRANSITIONS: dict[str, frozenset[str]] = {
    "PENDING": frozenset({"CLAIMED"}),
    # PENDING here = an explicit release-without-executing (see
    # `release_claim`) — never the stale-recovery backstop's own
    # transition, which targets PENDING too but via a DIFFERENT call
    # path (`recover_stale_claims`), exactly analogous to how
    # `ai.invocation.ALLOWED_TRANSITIONS["RUNNING"]` names `TIMED_OUT`
    # once for two different real callers.
    "CLAIMED": frozenset({"IN_PROGRESS", "PENDING"}),
    "IN_PROGRESS": frozenset({"SUCCEEDED", "FAILED_RETRYABLE", "FAILED_TERMINAL"}),
    # Claimable again, exactly like PENDING (`claim_next_pending`
    # treats PENDING/FAILED_RETRYABLE as the same "available to claim"
    # set — see that method's own docstring).
    "FAILED_RETRYABLE": frozenset({"CLAIMED"}),
    "SUCCEEDED": frozenset(),
    "FAILED_TERMINAL": frozenset(),
}

#: The one and only backend/alias pair `submit_job` accepts in this
#: first delivery (see module docstring's "Scope" section) — reusing
#: `ai.invocation.CAPABILITY_ALIAS_BACKENDS`, the single source of
#: truth, rather than hardcoding a second, parallel pairing.
_REQUIRED_JOB_INFERENCE_BACKEND = "TRINITY_CORE_OVERFLOW"
_REQUIRED_JOB_CAPABILITY_ALIAS = "trinity-core"

#: `AuditEvent.event_type` recorded when :func:`recover_stale_claims`
#: (or a repository's own equivalent lazy pass) recovers an abandoned
#: `CLAIMED`/`IN_PROGRESS` row — mirrors
#: `ai.invocation.STALE_RECOVERY_AUDIT_EVENT_TYPE` exactly.
BACKGROUND_JOB_STALE_RECOVERY_AUDIT_EVENT_TYPE = "BACKGROUND_JOB_STALE_RECOVERED"


def validate_background_job_backend_and_alias(*, inference_backend: str, capability_alias: str) -> None:
    """Enforce this first delivery's closed scope (module docstring's
    "Scope" section): every `BackgroundJob` names EXACTLY
    `inference_backend="TRINITY_CORE_OVERFLOW"` and
    `capability_alias="trinity-core"` — never `MAC_LOCAL` (the normal
    path stays synchronous) and never any other Trinity alias.

    Raises:
        core.errors.ValidationError: on any other value, or if the two
            fields disagree with `ai.invocation.CAPABILITY_ALIAS_BACKENDS`
            (structurally impossible given the two checks above, but
            checked explicitly anyway — the same "never trust two
            hand-maintained values to silently agree" discipline
            `ai.invocation.validate_role_provider_capability_pairing`
            already establishes).
    """
    if inference_backend != _REQUIRED_JOB_INFERENCE_BACKEND:
        raise ValidationError(
            f"a BackgroundJob's inference_backend must be {_REQUIRED_JOB_INFERENCE_BACKEND!r} in this "
            f"delivery (CD-6 §103); got {inference_backend!r} — the normal MAC_LOCAL background path stays "
            "fully synchronous (ai.gateway.background.run_background_task), never queued"
        )
    if capability_alias != _REQUIRED_JOB_CAPABILITY_ALIAS:
        raise ValidationError(
            f"a BackgroundJob's capability_alias must be {_REQUIRED_JOB_CAPABILITY_ALIAS!r} in this "
            f"delivery (CD-6 §103); got {capability_alias!r} — trinity-core is the sole authorised "
            "Trinity overflow alias"
        )
    if CAPABILITY_ALIAS_BACKENDS.get(capability_alias) != inference_backend:
        raise ValidationError(
            f"capability_alias {capability_alias!r} resolves to inference_backend "
            f"{CAPABILITY_ALIAS_BACKENDS.get(capability_alias)!r} (PID §103), not {inference_backend!r}"
        )


@dataclass(frozen=True)
class BackgroundJob:
    """A single durable backlog/overflow job attempt (CD-6 §103).
    Immutable once constructed; every state transition produces a NEW
    `BackgroundJob` snapshot via :func:`transition_job` — mirrors
    `ai.invocation.AIInvocation`'s own "frozen dataclass, never mutated
    in place" discipline exactly.
    """

    job_id: str
    idempotency_key: str
    task_id: str
    task_version: int
    input_references: Mapping[str, Any]
    evidence_content: str
    inference_backend: str
    capability_alias: str
    status: str
    actor_type: str
    actor_id: str
    correlation_id: str
    created_at: datetime
    updated_at: datetime
    attempt_count: int = 0
    max_attempts: int = 3
    claimed_by: Optional[str] = None
    claimed_at: Optional[datetime] = None
    #: Populated once a real `AIInvocation` exists for a
    #: successful/attempted execution of this job — see
    #: `persistence.postgres.background_job_models.BackgroundJobRow
    #: .ai_invocation_id`'s own docstring for why this is deliberately
    #: NOT a hard foreign key.
    ai_invocation_id: Optional[str] = None
    last_error: Optional[str] = None

    def to_dict(self) -> dict:
        """Plain-dict rendering for logging/reporting (e.g.
        `scripts.process_background_job_overflow`'s own summary
        report) — no JSON Schema contract governs this shape (unlike
        `AIInvocation`); this module has no external HTTP-facing
        surface in this first delivery."""
        return {
            "job_id": self.job_id,
            "idempotency_key": self.idempotency_key,
            "task_id": self.task_id,
            "task_version": self.task_version,
            "input_references": dict(self.input_references),
            "inference_backend": self.inference_backend,
            "capability_alias": self.capability_alias,
            "status": self.status,
            "actor_type": self.actor_type,
            "actor_id": self.actor_id,
            "correlation_id": self.correlation_id,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "attempt_count": self.attempt_count,
            "max_attempts": self.max_attempts,
            "claimed_by": self.claimed_by,
            "claimed_at": self.claimed_at,
            "ai_invocation_id": self.ai_invocation_id,
            "last_error": self.last_error,
        }


def transition_job(job: BackgroundJob, new_status: str, *, now: Optional[datetime] = None, **field_updates: Any) -> BackgroundJob:
    """Move ``job`` to ``new_status``, enforcing
    :data:`BACKGROUND_JOB_ALLOWED_TRANSITIONS` — the single state-machine
    enforcement point every repository method below goes through,
    mirroring `ai.invocation.transition`'s own role exactly.

    ``updated_at`` is always stamped fresh (via ``now``, defaulting to
    :func:`core.timestamps.utc_now`) — unlike `AIInvocation.completed_at`
    (only stamped on reaching a terminal state), every `BackgroundJob`
    transition, terminal or not, counts as an update.

    Raises:
        core.errors.InvalidStateTransitionError: if ``new_status`` is
            not a valid transition from ``job.status``.
        core.errors.ValidationError: if ``field_updates`` supplies
            ``updated_at`` directly, or names something that is not a
            real `BackgroundJob` field.
    """
    allowed = BACKGROUND_JOB_ALLOWED_TRANSITIONS.get(job.status, frozenset())
    if new_status not in allowed:
        raise InvalidStateTransitionError(
            f"BackgroundJob '{job.job_id}' cannot transition from '{job.status}' to '{new_status}'; "
            f"allowed transitions from '{job.status}' are {sorted(allowed) or '(none — terminal state)'}"
        )
    if "updated_at" in field_updates:
        raise ValidationError(
            "updated_at is stamped automatically by transition_job() and must not be supplied directly "
            "in field_updates"
        )

    now = now if now is not None else utc_now()
    try:
        return dataclasses.replace(job, status=new_status, updated_at=now, **field_updates)
    except TypeError as exc:  # dataclasses.replace on an unknown field name
        raise ValidationError(f"could not transition BackgroundJob: {exc}") from exc


def is_stale_claim(job: BackgroundJob, *, staleness_threshold_seconds: float, now: Optional[datetime] = None) -> bool:
    """True if ``job`` is non-terminal (`CLAIMED`/`IN_PROGRESS`) and has
    been sitting there, unresolved, longer than
    ``staleness_threshold_seconds`` — mirrors
    `ai.invocation.is_stale_running`'s own shared-predicate role
    exactly. Staleness is measured from ``claimed_at`` (never `None`
    for a `CLAIMED`/`IN_PROGRESS` row by construction — both states are
    only ever reached via a claim, which always stamps it)."""
    if job.status not in ("CLAIMED", "IN_PROGRESS"):
        return False
    now = now if now is not None else utc_now()
    assert job.claimed_at is not None  # noqa: S101 - guaranteed by construction, see docstring
    return (now - job.claimed_at).total_seconds() > staleness_threshold_seconds


def recover_stale_claim(job: BackgroundJob, *, now: Optional[datetime] = None) -> BackgroundJob:
    """Return the recovered `BackgroundJob` a stale, abandoned
    ``job`` (see :func:`is_stale_claim`) is transitioned into — mirrors
    `ai.invocation.recover_stale_invocation`'s own "explicit, audible,
    never a silent delete" doctrine exactly, INCLUDING that doctrine's
    own "target status depends on which status it went stale FROM"
    finding (see that function's own docstring for the analogous
    `RUNNING`-vs-`REQUESTED` distinction this mirrors).

    **`job.status == "CLAIMED"`** (never reached `IN_PROGRESS` —
    `mark_in_progress` was never called, so `attempt_count` was never
    bumped for this claim cycle) -> always `PENDING`. This is always
    safe: a `CLAIMED` row's `attempt_count` is structurally guaranteed
    to be `< max_attempts` — either it is `0` (a fresh `PENDING`-origin
    claim), or it came from re-claiming a `FAILED_RETRYABLE` row, which
    `mark_failed` only ever produces when `attempt_count < max_attempts`
    held at the time of THAT failure (see `BackgroundJobRepository
    .mark_failed`'s own docstring) — so there is no case where a
    `CLAIMED` row's budget is already exhausted.

    **`job.status == "IN_PROGRESS"`** (a genuine dispatch attempt
    started — `mark_in_progress` already incremented `attempt_count`
    for it, see module docstring's "Attempt-counting design") ->
    `FAILED_RETRYABLE` if `job.attempt_count < job.max_attempts` (this
    consumed attempt failed, but the budget is not yet exhausted —
    claimable again, exactly like a real `mark_failed(...,
    retryable=True)` outcome would produce), else `FAILED_TERMINAL`
    (the retry budget is already exhausted — recovering it to anything
    reclaimable would only let a worker re-claim a job `mark_failed`
    would immediately terminal-fail anyway).

    Deliberately NEVER targets `IN_PROGRESS -> PENDING` directly (that
    edge is not in :data:`BACKGROUND_JOB_ALLOWED_TRANSITIONS` at all —
    `FAILED_RETRYABLE` is the correct, already-modelled "claimable
    again after a consumed attempt" state for a row that had genuinely
    started).
    """
    if job.status == "CLAIMED":
        target_status = "PENDING"
        last_error = (
            f"recovered by the bounded stale-claim backstop: claimed_at was older than the staleness "
            f"threshold with no terminal transition ever recorded while CLAIMED (never reached "
            f"IN_PROGRESS) — reclaimable (attempt_count={job.attempt_count} < max_attempts={job.max_attempts})"
        )
    else:
        assert job.status == "IN_PROGRESS"  # noqa: S101 - is_stale_claim only allows CLAIMED/IN_PROGRESS
        if job.attempt_count < job.max_attempts:
            target_status = "FAILED_RETRYABLE"
            last_error = (
                f"recovered by the bounded stale-claim backstop: claimed_at was older than the staleness "
                f"threshold with no terminal transition ever recorded while IN_PROGRESS — recovered as "
                f"FAILED_RETRYABLE, claimable again (attempt_count={job.attempt_count} < "
                f"max_attempts={job.max_attempts})"
            )
        else:
            target_status = "FAILED_TERMINAL"
            last_error = (
                f"recovered by the bounded stale-claim backstop: claimed_at was older than the staleness "
                f"threshold with no terminal transition ever recorded while IN_PROGRESS, and attempt_count="
                f"{job.attempt_count} had already reached max_attempts={job.max_attempts} — not reclaimed"
            )
    return transition_job(
        job,
        target_status,
        now=now,
        claimed_by=None,
        claimed_at=None,
        last_error=last_error,
    )


class BackgroundJobRepository(abc.ABC):
    """Repository abstraction for `BackgroundJob` (CD-6 §103). Concrete
    implementations (:class:`InMemoryBackgroundJobRepository`,
    `persistence.postgres.background_job_repository
    .PostgresBackgroundJobRepository`) each accept an `audit_repository`
    at construction — used ONLY to emit
    :data:`BACKGROUND_JOB_STALE_RECOVERY_AUDIT_EVENT_TYPE` when a lazy
    stale-claim recovery pass (see :func:`recover_stale_claim`) silently
    discovers and recovers an abandoned row — mirrors
    `ai.invocation.AIInvocationRepository`'s own class docstring
    exactly (the same "no external caller is ever positioned to audit
    this itself" reasoning applies here verbatim).
    """

    @abc.abstractmethod
    def submit_job(
        self,
        *,
        idempotency_key: str,
        task_id: str,
        task_version: int,
        input_references: Mapping[str, Any],
        evidence_content: str,
        inference_backend: str,
        capability_alias: str,
        actor_type: str,
        actor_id: str,
        correlation_id: Optional[str] = None,
        max_attempts: int = 3,
    ) -> BackgroundJob:
        """Idempotent job submission (module docstring's own
        contrasting doctrine vs. `AIInvocation.create_invocation`): if
        a row with this exact `idempotency_key` already exists, return
        the EXISTING row UNCHANGED — never a second row, never an
        error. A genuinely new `idempotency_key` creates a new `PENDING`
        row.

        Raises:
            core.errors.ValidationError: `inference_backend`/
                `capability_alias` is not this delivery's one
                authorised pair (see
                :func:`validate_background_job_backend_and_alias`), or
                `actor_type`/`max_attempts` is invalid.
        """
        raise NotImplementedError

    @abc.abstractmethod
    def claim_next_pending(
        self, *, limit: int, worker_id: str, now: Optional[datetime] = None
    ) -> list[BackgroundJob]:
        """Atomically claim up to `limit` rows currently `PENDING` or
        `FAILED_RETRYABLE` (an available-to-retry row, not yet
        exhausted — though `mark_failed` never produces a
        `FAILED_RETRYABLE` row past `max_attempts` in the first place),
        transitioning each to `CLAIMED` and stamping
        `claimed_by=worker_id`, `claimed_at=now`.

        Runs the stale-claim recovery pass (:func:`recover_stale_claim`
        via each repository's own equivalent) FIRST, exactly mirroring
        `ai.invocation`'s own "recovery runs lazily, at the exact
        points staleness matters" doctrine — a genuinely abandoned
        `CLAIMED`/`IN_PROGRESS` row must become claimable again (or
        terminally fail) before this method decides what is available.

        Does NOT itself change `attempt_count` — see module
        docstring's "Attempt-counting design" section for why that
        increment happens at :meth:`mark_in_progress` instead.

        Returns fewer than `limit` rows (including zero) if fewer are
        available — never raises for "nothing to claim".
        """
        raise NotImplementedError

    @abc.abstractmethod
    def recover_stale_claims(
        self, *, staleness_threshold_seconds: float = STALE_RUNNING_THRESHOLD_SECONDS, now: Optional[datetime] = None
    ) -> list[BackgroundJob]:
        """Scan every `CLAIMED`/`IN_PROGRESS` row and recover any that
        is stale per :func:`is_stale_claim`, using
        :func:`recover_stale_claim` — the bulk, repository-level
        counterpart to that single-job free function (this method is
        what can actually SEE every candidate row; the free function
        only knows how to recover one job it is handed).

        `staleness_threshold_seconds` defaults to
        `ai.invocation.STALE_RUNNING_THRESHOLD_SECONDS` — reused rather
        than a second, independent magic number, for the same
        consistency reasoning that constant's own docstring gives
        (unless a future caller has a concrete, documented reason to
        diverge, which none does today).

        Called automatically, lazily, by :meth:`claim_next_pending`
        (never as a scheduled/background poll — PID §103.4's own "no
        invented automatic thresholds" instruction applies here just as
        much as it does to backend selection) — also directly callable
        on its own (e.g. by an operator script that only wants to
        sweep abandoned claims without also claiming new work).

        Returns every `BackgroundJob` that was actually recovered (empty
        list if none were stale) — each recovery is also audited via
        :data:`BACKGROUND_JOB_STALE_RECOVERY_AUDIT_EVENT_TYPE`.
        """
        raise NotImplementedError

    @abc.abstractmethod
    def mark_in_progress(self, job_id: str) -> BackgroundJob:
        """`CLAIMED -> IN_PROGRESS`, incrementing `attempt_count` by
        exactly 1 (see module docstring's "Attempt-counting design")."""
        raise NotImplementedError

    @abc.abstractmethod
    def mark_succeeded(self, job_id: str, *, ai_invocation_id: str) -> BackgroundJob:
        """`IN_PROGRESS -> SUCCEEDED`, recording the real `AIInvocation`
        this successful attempt produced."""
        raise NotImplementedError

    @abc.abstractmethod
    def mark_failed(self, job_id: str, *, error: str, retryable: bool) -> BackgroundJob:
        """`IN_PROGRESS -> FAILED_RETRYABLE` (if `retryable` and
        `attempt_count < max_attempts`) or `IN_PROGRESS ->
        FAILED_TERMINAL` (otherwise — either a non-retryable failure,
        e.g. a structured-output/schema-validation failure that
        retrying the exact same content against the exact same model
        will never fix, or the retry budget is exhausted). Always
        stamps `last_error`."""
        raise NotImplementedError

    @abc.abstractmethod
    def release_claim(self, job_id: str) -> BackgroundJob:
        """`CLAIMED -> PENDING` — an explicit, caller-invoked "give
        this back" (e.g. a worker shutting down cleanly mid-batch).
        Distinct from the stale-claim backstop, which acts on its own
        lazily and is never invoked directly by an ordinary caller."""
        raise NotImplementedError

    @abc.abstractmethod
    def get_job(self, job_id: str) -> BackgroundJob:
        raise NotImplementedError

    @abc.abstractmethod
    def list_jobs(
        self,
        *,
        status: Optional[str] = None,
        inference_backend: Optional[str] = None,
        task_id: Optional[str] = None,
        limit: Optional[int] = None,
        offset: int = 0,
    ) -> list[BackgroundJob]:
        """List `BackgroundJob`s, most-recently-created first
        (`created_at DESC`, `job_id` as a deterministic tie-breaker) —
        the same ordering doctrine
        `ai.invocation.AIInvocationRepository.list_invocations`
        establishes. Optional equality filters; `limit=None` (default)
        returns every matching record."""
        raise NotImplementedError


def _matches_filters(
    job: BackgroundJob, *, status: Optional[str], inference_backend: Optional[str], task_id: Optional[str]
) -> bool:
    if status is not None and job.status != status:
        return False
    if inference_backend is not None and job.inference_backend != inference_backend:
        return False
    if task_id is not None and job.task_id != task_id:
        return False
    return True


class InMemoryBackgroundJobRepository(BackgroundJobRepository):
    """Narrow in-memory reference implementation, mirroring
    `ai.invocation.InMemoryAIInvocationRepository`'s own shape.
    Single-process, GIL-serialised — a plain `threading.Lock` around
    each mutating method is enough (no real cross-process concurrency
    to defend against here, unlike the Postgres implementation's
    `SELECT ... FOR UPDATE SKIP LOCKED`).
    """

    def __init__(self, *, audit_repository: Optional[AuditRepository] = None) -> None:
        self._by_id: dict[str, BackgroundJob] = {}
        self._by_idempotency_key: dict[str, str] = {}
        # RLock, not Lock: recover_stale_claims() takes this lock and is
        # itself called from within claim_next_pending()'s own
        # `with self._lock:` block — a plain Lock would deadlock a
        # single thread re-entering it.
        self._lock = threading.RLock()
        self._audit_repository: AuditRepository = audit_repository or InMemoryAuditRepository()

    # -- stale-claim recovery (module docstring's "lazy, at the exact points staleness matters") --

    def _recover_if_stale(
        self, job: BackgroundJob, *, staleness_threshold_seconds: float, now: Optional[datetime]
    ) -> Optional[BackgroundJob]:
        if not is_stale_claim(job, staleness_threshold_seconds=staleness_threshold_seconds, now=now):
            return None
        recovered = recover_stale_claim(job, now=now)
        self._by_id[recovered.job_id] = recovered
        self._audit_repository.record_audit_event(
            event_type=BACKGROUND_JOB_STALE_RECOVERY_AUDIT_EVENT_TYPE,
            actor_type=actor.SYSTEM,
            actor_id="background-job-stale-recovery",
            subject_type="BackgroundJob",
            subject_id=recovered.job_id,
            correlation_id=recovered.correlation_id,
            causation_id=None,
            payload={
                "task_id": recovered.task_id,
                "task_version": recovered.task_version,
                "recovered_status": recovered.status,
                "attempt_count": recovered.attempt_count,
                "max_attempts": recovered.max_attempts,
                "stale_threshold_seconds": staleness_threshold_seconds,
            },
        )
        return recovered

    def recover_stale_claims(
        self, *, staleness_threshold_seconds: float = STALE_RUNNING_THRESHOLD_SECONDS, now: Optional[datetime] = None
    ) -> list[BackgroundJob]:
        with self._lock:
            now = now if now is not None else utc_now()
            recovered: list[BackgroundJob] = []
            for job in list(self._by_id.values()):
                result = self._recover_if_stale(
                    job, staleness_threshold_seconds=staleness_threshold_seconds, now=now
                )
                if result is not None:
                    recovered.append(result)
            return recovered

    # -- BackgroundJobRepository -----------------------------------------

    def submit_job(
        self,
        *,
        idempotency_key: str,
        task_id: str,
        task_version: int,
        input_references: Mapping[str, Any],
        evidence_content: str,
        inference_backend: str,
        capability_alias: str,
        actor_type: str,
        actor_id: str,
        correlation_id: Optional[str] = None,
        max_attempts: int = 3,
    ) -> BackgroundJob:
        if not idempotency_key:
            raise ValidationError("idempotency_key must be a non-empty string")
        validate_background_job_backend_and_alias(
            inference_backend=inference_backend, capability_alias=capability_alias
        )
        if not actor.is_valid(actor_type):
            raise ValidationError(
                f"actor_type '{actor_type}' is not one of the closed set {sorted(actor.ALL)} (PID §14)"
            )
        if max_attempts < 1:
            raise ValidationError(f"max_attempts must be >= 1; got {max_attempts}")

        with self._lock:
            existing_id = self._by_idempotency_key.get(idempotency_key)
            if existing_id is not None:
                # Idempotent replay — return the EXISTING row unchanged
                # (module docstring's own contrasting doctrine).
                return self._by_id[existing_id]

            now = utc_now()
            job = BackgroundJob(
                job_id=identity.generate_id(),
                idempotency_key=idempotency_key,
                task_id=task_id,
                task_version=task_version,
                input_references=dict(input_references),
                evidence_content=evidence_content,
                inference_backend=inference_backend,
                capability_alias=capability_alias,
                status="PENDING",
                actor_type=actor_type,
                actor_id=actor_id,
                correlation_id=correlation_id if correlation_id is not None else identity.generate_id(),
                created_at=now,
                updated_at=now,
                max_attempts=max_attempts,
            )
            self._by_id[job.job_id] = job
            self._by_idempotency_key[idempotency_key] = job.job_id
            return job

    def claim_next_pending(
        self, *, limit: int, worker_id: str, now: Optional[datetime] = None
    ) -> list[BackgroundJob]:
        if limit <= 0:
            return []
        with self._lock:
            now = now if now is not None else utc_now()
            self.recover_stale_claims(now=now)
            candidates = sorted(
                (j for j in self._by_id.values() if j.status in ("PENDING", "FAILED_RETRYABLE")),
                key=lambda j: (j.created_at, j.job_id),
            )
            claimed: list[BackgroundJob] = []
            for job in candidates[:limit]:
                updated = transition_job(job, "CLAIMED", now=now, claimed_by=worker_id, claimed_at=now)
                self._by_id[updated.job_id] = updated
                claimed.append(updated)
            return claimed

    def mark_in_progress(self, job_id: str) -> BackgroundJob:
        with self._lock:
            job = self.get_job(job_id)
            # See module docstring's "Attempt-counting design" —
            # attempt_count increments here, unconditionally, exactly
            # once per genuine dispatch attempt.
            updated = transition_job(job, "IN_PROGRESS", attempt_count=job.attempt_count + 1)
            self._by_id[job_id] = updated
            return updated

    def mark_succeeded(self, job_id: str, *, ai_invocation_id: str) -> BackgroundJob:
        with self._lock:
            job = self.get_job(job_id)
            updated = transition_job(job, "SUCCEEDED", ai_invocation_id=ai_invocation_id)
            self._by_id[job_id] = updated
            return updated

    def mark_failed(self, job_id: str, *, error: str, retryable: bool) -> BackgroundJob:
        with self._lock:
            job = self.get_job(job_id)
            if retryable and job.attempt_count < job.max_attempts:
                target_status = "FAILED_RETRYABLE"
            else:
                target_status = "FAILED_TERMINAL"
            updated = transition_job(job, target_status, last_error=error)
            self._by_id[job_id] = updated
            return updated

    def release_claim(self, job_id: str) -> BackgroundJob:
        with self._lock:
            job = self.get_job(job_id)
            updated = transition_job(job, "PENDING", claimed_by=None, claimed_at=None)
            self._by_id[job_id] = updated
            return updated

    def get_job(self, job_id: str) -> BackgroundJob:
        try:
            return self._by_id[job_id]
        except KeyError:
            raise NotFoundError(f"no BackgroundJob with job_id '{job_id}'") from None

    def list_jobs(
        self,
        *,
        status: Optional[str] = None,
        inference_backend: Optional[str] = None,
        task_id: Optional[str] = None,
        limit: Optional[int] = None,
        offset: int = 0,
    ) -> list[BackgroundJob]:
        records = [
            j
            for j in self._by_id.values()
            if _matches_filters(j, status=status, inference_backend=inference_backend, task_id=task_id)
        ]
        records.sort(key=lambda j: (j.created_at, j.job_id), reverse=True)
        if limit is None:
            return records[offset:]
        return records[offset : offset + limit]
