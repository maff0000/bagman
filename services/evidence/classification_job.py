"""``EvidenceClassificationJob`` — the durable, asynchronous trigger for
``services.evidence.classification_orchestrator.classify_evidence``
(BAGMAN accounting platform, `evidence/automatic-classification-activation`
WO).

BAGMAN's evidence-classification subsystem (``classify_evidence`` —
deterministic-rule-first, AI-fallback, human review via
``CLASSIFICATION_REVIEW`` Needs You items) has always been fully
governed but had NO automatic trigger: it only ran when a
human/process explicitly called one of three HTTP endpoints
(``app/api/routers/evidence_classification.py``). This module is the
missing trigger's DURABLE QUEUE half — the actual classification work
is still, and will always be, run by the exact same, unmodified
``classify_evidence`` (see ``scripts/process_evidence_classification_jobs.py``
for the bounded, operator/cron-invoked worker that claims and runs
these jobs).

Structured closely on ``ai.jobs.BackgroundJob``'s own shape (frozen
dataclass + repository ABC + in-memory implementation in this module;
Postgres implementation in ``persistence.postgres
.evidence_classification_job_models``/``persistence.postgres
.evidence_classification_job_repository``, exactly mirroring the
``background_job_models.py``/``background_job_repository.py`` split) —
read that module's own docstring first if anything here is unclear on
a point it already established (the "attempt_count increments at
mark_in_progress only" attempt-counting design, the lazy
stale-claim-recovery discipline, the audit-emission discipline).

A DIFFERENT identity/idempotency doctrine from ``BackgroundJob``
------------------------------------------------------------------------
``ai.jobs.BackgroundJobRepository.submit_job`` is idempotent on a
caller-supplied ``idempotency_key`` — appropriate there because one
``evidence_id`` can legitimately need several DIFFERENT Trinity-overflow
jobs over time (different tasks, different fingerprints). This module
makes the DELIBERATELY SIMPLER, stricter choice the WO's own spec
requires: :meth:`EvidenceClassificationJobRepository.submit_job` is
idempotent directly on ``evidence_id`` itself — AT MOST ONE
``EvidenceClassificationJob`` may ever exist for a given
``evidence_id``, full stop, because ``classify_evidence`` itself is
the single governed classification attempt for that evidence (its own
top-of-function "current classification already exists" guard, WI-3
§31/§38, already makes a second classification attempt a no-op) — a
second job would never do anything ``classify_evidence`` had not
already made idempotent, so there is no reason to ever create one. A
second :func:`enqueue_classification_job_for_evidence`/``submit_job``
call for the SAME ``evidence_id`` returns the existing row UNCHANGED,
never a second row — enforced by a REAL database-level unique
constraint on ``evidence_id`` in the Postgres implementation (mirrors
the concurrency-safety fix
``services.xero.account_suggestion.XeroAccountSuggestionRepository``'s
own class docstring documents: an ``IntegrityError``-to-idempotent-
return translation, never an application-level pre-check alone — see
`tests/persistence/test_evidence_classification_job_concurrency.py`
for the real, disposable-PostgreSQL, genuine-threads proof).

No ``ai_invocation_id`` field (unlike ``BackgroundJob``)
------------------------------------------------------------------------
A ``BackgroundJob``'s whole purpose IS an AI invocation, so recording
which ``AIInvocation`` it produced is central to its own record. An
``EvidenceClassificationJob``'s job is narrower and different: "run
``classify_evidence`` once for this evidence_id" — and a
deterministic-rule match (zero AI calls made at all) is just as
complete and successful an outcome as an AI proposal is. Recording a
single ``ai_invocation_id`` on this row would misrepresent the
deterministic-only-success case (no invocation exists at all) and
would in any case be redundant: ``classify_evidence``'s own returned
outcome, together with the ``EvidenceClassification``/``AIInvocation``
rows it may have written, is already the durable record of what
actually happened — this job row exists only to answer "was this
evidence handed to the classifier at least once, and did that attempt
complete without an infrastructure-level exception", nothing more.

The known, accepted, NOT-closed-here gap: enqueue can be skipped
------------------------------------------------------------------------
There is no outbox/LISTEN-NOTIFY/DB-trigger/backfill-reconciliation
mechanism ANYWHERE in this codebase (verified: neither
``ai.jobs``/``BackgroundJob`` nor any other durable-job mechanism in
this repository has one either) — so if the process enqueuing a job
crashes between ``core.api.BagmanCanonicalAPI.register_evidence``'s own
transaction committing and :func:`enqueue_classification_job_for_evidence`
actually being called (or if that call itself fails and its own
exception is swallowed — see that function's own docstring), that one
``EvidenceItem`` never gets an automatic job, ever, unless something
else later independently triggers classification for it (e.g. an
operator hitting one of the existing HTTP endpoints by hand). This is a
disclosed, accepted, narrow limitation of THIS delivery — building a
backfill/reconciliation sweep to close it is explicitly OUT OF SCOPE
(the WO's own "no historical bulk classification" non-goal) and is not
attempted here. The registration transaction itself
(``persistence/postgres/evidence_repository.py``'s own
``session_scope`` covering the ``EvidenceItem`` + external-reference
rows) is unaffected either way — this gap is scoped ENTIRELY to "does a
job get enqueued", never to evidence durability itself.

Attempt-counting / stale-claim-recovery / status machine
------------------------------------------------------------------------
Identical doctrine to ``ai.jobs.BackgroundJob`` — reused verbatim, not
reinvented: ``attempt_count`` increments by exactly 1 at
:meth:`EvidenceClassificationJobRepository.mark_in_progress` time only
(see ``ai.jobs``'s own module docstring, "Attempt-counting design",
for the full "why not at claim time" reasoning — it applies here
identically). The staleness threshold reused for lazy stale-claim
recovery is ``ai.invocation.STALE_RUNNING_THRESHOLD_SECONDS`` (600s) —
the SAME constant ``ai.jobs.BackgroundJobRepository.recover_stale_claims``
defaults to, for the identical "one shared magic number, not a second
independently-tuned one" reasoning that constant's own docstring
gives; a documented judgment call, not a WO-mandated value (there is
nothing evidence-classification-specific that would justify a
different number).
"""
from __future__ import annotations

import abc
import dataclasses
import logging
import threading
from dataclasses import dataclass
from datetime import datetime
from typing import Optional

from ai.invocation import STALE_RUNNING_THRESHOLD_SECONDS
from core import actor, identity
from core.audit import AuditRepository, InMemoryAuditRepository
from core.errors import InvalidStateTransitionError, NotFoundError, ValidationError
from core.timestamps import utc_now

logger = logging.getLogger(__name__)

#: The closed set of `EvidenceClassificationJob` lifecycle states —
#: identical vocabulary to `ai.jobs.BACKGROUND_JOB_STATUSES` (see
#: module docstring).
EVIDENCE_CLASSIFICATION_JOB_STATUSES = frozenset(
    {"PENDING", "CLAIMED", "IN_PROGRESS", "SUCCEEDED", "FAILED_RETRYABLE", "FAILED_TERMINAL"}
)

#: States from which no further transition is possible.
EVIDENCE_CLASSIFICATION_JOB_TERMINAL_STATUSES = frozenset({"SUCCEEDED", "FAILED_TERMINAL"})

#: The single source of truth for valid `EvidenceClassificationJob`
#: state transitions — mirrors `ai.jobs.BACKGROUND_JOB_ALLOWED_TRANSITIONS`
#: exactly (see that module's own docstring for the "PENDING is reached
#: by two different call paths" note, which applies identically here).
EVIDENCE_CLASSIFICATION_JOB_ALLOWED_TRANSITIONS: dict[str, frozenset[str]] = {
    "PENDING": frozenset({"CLAIMED"}),
    "CLAIMED": frozenset({"IN_PROGRESS", "PENDING"}),
    "IN_PROGRESS": frozenset({"SUCCEEDED", "FAILED_RETRYABLE", "FAILED_TERMINAL"}),
    "FAILED_RETRYABLE": frozenset({"CLAIMED"}),
    "SUCCEEDED": frozenset(),
    "FAILED_TERMINAL": frozenset(),
}

#: `AuditEvent.event_type` recorded when the lazy stale-claim-recovery
#: pass recovers an abandoned `CLAIMED`/`IN_PROGRESS` row — mirrors
#: `ai.jobs.BACKGROUND_JOB_STALE_RECOVERY_AUDIT_EVENT_TYPE` exactly.
EVIDENCE_CLASSIFICATION_JOB_STALE_RECOVERY_AUDIT_EVENT_TYPE = "EVIDENCE_CLASSIFICATION_JOB_STALE_RECOVERED"


@dataclass(frozen=True)
class EvidenceClassificationJob:
    """A single durable "run `classify_evidence` once for this
    `evidence_id`" job. Immutable once constructed; every state
    transition produces a NEW snapshot via :func:`transition_job` —
    mirrors `ai.jobs.BackgroundJob`'s own "frozen dataclass, never
    mutated in place" discipline exactly."""

    job_id: str
    evidence_id: str
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
    last_error: Optional[str] = None

    def to_dict(self) -> dict:
        """Plain-dict rendering for logging/reporting (e.g.
        `scripts.process_evidence_classification_jobs`'s own summary
        report) — no JSON Schema contract governs this shape (mirrors
        `ai.jobs.BackgroundJob.to_dict`'s own identical note): this
        module has no external HTTP-facing surface."""
        return {
            "job_id": self.job_id,
            "evidence_id": self.evidence_id,
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
            "last_error": self.last_error,
        }


def transition_job(job: EvidenceClassificationJob, new_status: str, *, now: Optional[datetime] = None, **field_updates) -> EvidenceClassificationJob:
    """Move ``job`` to ``new_status``, enforcing
    :data:`EVIDENCE_CLASSIFICATION_JOB_ALLOWED_TRANSITIONS` — the
    single state-machine enforcement point every repository method
    below goes through, mirroring `ai.jobs.transition_job` exactly.

    Raises:
        core.errors.InvalidStateTransitionError: if ``new_status`` is
            not a valid transition from ``job.status``.
        core.errors.ValidationError: if ``field_updates`` supplies
            ``updated_at`` directly, or names something that is not a
            real field.
    """
    allowed = EVIDENCE_CLASSIFICATION_JOB_ALLOWED_TRANSITIONS.get(job.status, frozenset())
    if new_status not in allowed:
        raise InvalidStateTransitionError(
            f"EvidenceClassificationJob '{job.job_id}' cannot transition from '{job.status}' to "
            f"'{new_status}'; allowed transitions from '{job.status}' are "
            f"{sorted(allowed) or '(none — terminal state)'}"
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
        raise ValidationError(f"could not transition EvidenceClassificationJob: {exc}") from exc


def is_stale_claim(job: EvidenceClassificationJob, *, staleness_threshold_seconds: float, now: Optional[datetime] = None) -> bool:
    """True if ``job`` is non-terminal (`CLAIMED`/`IN_PROGRESS`) and has
    been sitting there, unresolved, longer than
    ``staleness_threshold_seconds`` — mirrors `ai.jobs.is_stale_claim`
    exactly. Staleness is measured from ``claimed_at`` (never `None`
    for a `CLAIMED`/`IN_PROGRESS` row by construction — both states are
    only ever reached via a claim, which always stamps it)."""
    if job.status not in ("CLAIMED", "IN_PROGRESS"):
        return False
    now = now if now is not None else utc_now()
    assert job.claimed_at is not None  # noqa: S101 - guaranteed by construction, see docstring
    return (now - job.claimed_at).total_seconds() > staleness_threshold_seconds


def recover_stale_claim(job: EvidenceClassificationJob, *, now: Optional[datetime] = None) -> EvidenceClassificationJob:
    """Return the recovered `EvidenceClassificationJob` a stale,
    abandoned ``job`` (see :func:`is_stale_claim`) is transitioned
    into — mirrors `ai.jobs.recover_stale_claim`'s own doctrine
    exactly, INCLUDING that function's own "target status depends on
    which status it went stale FROM" finding: see that function's own
    docstring for the full reasoning (a `CLAIMED` row always -> `PENDING`;
    an `IN_PROGRESS` row -> `FAILED_RETRYABLE` if budget remains, else
    `FAILED_TERMINAL`)."""
    if job.status == "CLAIMED":
        target_status = "PENDING"
        last_error = (
            "recovered by the bounded stale-claim backstop: claimed_at was older than the staleness "
            "threshold with no terminal transition ever recorded while CLAIMED (never reached "
            f"IN_PROGRESS) — reclaimable (attempt_count={job.attempt_count} < max_attempts={job.max_attempts})"
        )
    else:
        assert job.status == "IN_PROGRESS"  # noqa: S101 - is_stale_claim only allows CLAIMED/IN_PROGRESS
        if job.attempt_count < job.max_attempts:
            target_status = "FAILED_RETRYABLE"
            last_error = (
                "recovered by the bounded stale-claim backstop: claimed_at was older than the staleness "
                "threshold with no terminal transition ever recorded while IN_PROGRESS — recovered as "
                f"FAILED_RETRYABLE, claimable again (attempt_count={job.attempt_count} < "
                f"max_attempts={job.max_attempts})"
            )
        else:
            target_status = "FAILED_TERMINAL"
            last_error = (
                "recovered by the bounded stale-claim backstop: claimed_at was older than the staleness "
                "threshold with no terminal transition ever recorded while IN_PROGRESS, and "
                f"attempt_count={job.attempt_count} had already reached max_attempts={job.max_attempts} — "
                "not reclaimed"
            )
    return transition_job(job, target_status, now=now, claimed_by=None, claimed_at=None, last_error=last_error)


class EvidenceClassificationJobRepository(abc.ABC):
    """Repository abstraction for `EvidenceClassificationJob`. Concrete
    implementations (:class:`InMemoryEvidenceClassificationJobRepository`,
    `persistence.postgres.evidence_classification_job_repository
    .PostgresEvidenceClassificationJobRepository`) each accept an
    `audit_repository` at construction — used ONLY to emit
    :data:`EVIDENCE_CLASSIFICATION_JOB_STALE_RECOVERY_AUDIT_EVENT_TYPE`
    when a lazy stale-claim recovery pass silently discovers and
    recovers an abandoned row — mirrors
    `ai.jobs.BackgroundJobRepository`'s own class docstring exactly
    (the same "no external caller is ever positioned to audit this
    itself" reasoning applies here verbatim).
    """

    @abc.abstractmethod
    def submit_job(
        self, *, evidence_id: str, actor_type: str, actor_id: str, correlation_id: Optional[str] = None,
        max_attempts: int = 3,
    ) -> EvidenceClassificationJob:
        """Idempotent job submission, keyed directly on `evidence_id`
        (see module docstring's own "A DIFFERENT identity/idempotency
        doctrine" section — NOT a caller-supplied idempotency key): if
        a row already exists for `evidence_id`, return the EXISTING row
        UNCHANGED — never a second row, never an error.

        Raises:
            core.errors.ValidationError: `actor_type`/`max_attempts` is
                invalid.
        """
        raise NotImplementedError

    @abc.abstractmethod
    def claim_next_pending(
        self, *, limit: int, claimed_by: str, now: Optional[datetime] = None
    ) -> list[EvidenceClassificationJob]:
        """Atomically claim up to `limit` rows currently `PENDING` or
        `FAILED_RETRYABLE` (an available-to-retry row, not yet
        exhausted), transitioning each to `CLAIMED` and stamping
        `claimed_by`, `claimed_at=now`.

        Runs the lazy stale-claim recovery pass FIRST — mirrors
        `ai.jobs.BackgroundJobRepository.claim_next_pending`'s own
        documented "recovery runs lazily, at the exact points staleness
        matters" doctrine exactly.

        Returns fewer than `limit` rows (including zero) if fewer are
        available — never raises for "nothing to claim".
        """
        raise NotImplementedError

    @abc.abstractmethod
    def mark_in_progress(self, job_id: str) -> EvidenceClassificationJob:
        """`CLAIMED -> IN_PROGRESS`, incrementing `attempt_count` by
        exactly 1 (see module docstring's "Attempt-counting" section)."""
        raise NotImplementedError

    @abc.abstractmethod
    def mark_succeeded(self, job_id: str) -> EvidenceClassificationJob:
        """`IN_PROGRESS -> SUCCEEDED`. No `ai_invocation_id` parameter
        (see module docstring — a deterministic-only success is a
        complete, valid outcome with no AI invocation at all)."""
        raise NotImplementedError

    @abc.abstractmethod
    def mark_failed(self, job_id: str, *, error: str, retryable: bool) -> EvidenceClassificationJob:
        """`IN_PROGRESS -> FAILED_RETRYABLE` (if `retryable` and
        `attempt_count < max_attempts`) or `IN_PROGRESS ->
        FAILED_TERMINAL` (otherwise). Always stamps `last_error`."""
        raise NotImplementedError

    @abc.abstractmethod
    def get_job(self, job_id: str) -> EvidenceClassificationJob:
        raise NotImplementedError

    @abc.abstractmethod
    def get_by_evidence(self, evidence_id: str) -> Optional[EvidenceClassificationJob]:
        """Read-only lookup, never `NotFoundError` — an evidence item
        with no job yet is an ordinary, expected state. At most one row
        can ever exist for `evidence_id` (see module docstring)."""
        raise NotImplementedError


class InMemoryEvidenceClassificationJobRepository(EvidenceClassificationJobRepository):
    """Narrow in-memory reference implementation, mirroring
    `ai.jobs.InMemoryBackgroundJobRepository`'s own shape. Single-process,
    GIL-serialised — a plain `threading.RLock` around each mutating
    method is enough (no real cross-process concurrency to defend
    against here, unlike the Postgres implementation's
    `SELECT ... FOR UPDATE SKIP LOCKED`)."""

    def __init__(self, *, audit_repository: Optional[AuditRepository] = None) -> None:
        self._by_id: dict[str, EvidenceClassificationJob] = {}
        self._by_evidence: dict[str, str] = {}
        # RLock, not Lock: recover_stale_claims() (called internally by
        # claim_next_pending()) takes this lock and is itself invoked
        # from within claim_next_pending()'s own `with self._lock:`
        # block — a plain Lock would deadlock a single thread
        # re-entering it (mirrors InMemoryBackgroundJobRepository's own
        # identical reasoning).
        self._lock = threading.RLock()
        self._audit_repository: AuditRepository = audit_repository or InMemoryAuditRepository()

    def _recover_if_stale(
        self, job: EvidenceClassificationJob, *, staleness_threshold_seconds: float, now: Optional[datetime]
    ) -> Optional[EvidenceClassificationJob]:
        if not is_stale_claim(job, staleness_threshold_seconds=staleness_threshold_seconds, now=now):
            return None
        recovered = recover_stale_claim(job, now=now)
        self._by_id[recovered.job_id] = recovered
        self._audit_repository.record_audit_event(
            event_type=EVIDENCE_CLASSIFICATION_JOB_STALE_RECOVERY_AUDIT_EVENT_TYPE,
            actor_type=actor.SYSTEM,
            actor_id="evidence-classification-job-stale-recovery",
            subject_type="EvidenceClassificationJob",
            subject_id=recovered.job_id,
            correlation_id=recovered.correlation_id,
            causation_id=None,
            payload={
                "evidence_id": recovered.evidence_id,
                "recovered_status": recovered.status,
                "attempt_count": recovered.attempt_count,
                "max_attempts": recovered.max_attempts,
                "stale_threshold_seconds": staleness_threshold_seconds,
            },
        )
        return recovered

    def _recover_stale_claims(
        self, *, staleness_threshold_seconds: float, now: Optional[datetime] = None
    ) -> list[EvidenceClassificationJob]:
        with self._lock:
            now = now if now is not None else utc_now()
            recovered: list[EvidenceClassificationJob] = []
            for job in list(self._by_id.values()):
                result = self._recover_if_stale(job, staleness_threshold_seconds=staleness_threshold_seconds, now=now)
                if result is not None:
                    recovered.append(result)
            return recovered

    def submit_job(
        self, *, evidence_id: str, actor_type: str, actor_id: str, correlation_id: Optional[str] = None,
        max_attempts: int = 3,
    ) -> EvidenceClassificationJob:
        if not evidence_id:
            raise ValidationError("evidence_id must be a non-empty string")
        if not actor.is_valid(actor_type):
            raise ValidationError(
                f"actor_type '{actor_type}' is not one of the closed set {sorted(actor.ALL)} (PID §14)"
            )
        if max_attempts < 1:
            raise ValidationError(f"max_attempts must be >= 1; got {max_attempts}")

        with self._lock:
            existing_id = self._by_evidence.get(evidence_id)
            if existing_id is not None:
                # Idempotent replay — return the EXISTING row unchanged
                # (module docstring's own idempotency doctrine).
                return self._by_id[existing_id]

            now = utc_now()
            job = EvidenceClassificationJob(
                job_id=identity.generate_id(),
                evidence_id=evidence_id,
                status="PENDING",
                actor_type=actor_type,
                actor_id=actor_id,
                correlation_id=correlation_id if correlation_id is not None else identity.generate_id(),
                created_at=now,
                updated_at=now,
                max_attempts=max_attempts,
            )
            self._by_id[job.job_id] = job
            self._by_evidence[evidence_id] = job.job_id
            return job

    def claim_next_pending(
        self, *, limit: int, claimed_by: str, now: Optional[datetime] = None
    ) -> list[EvidenceClassificationJob]:
        if limit <= 0:
            return []
        with self._lock:
            now = now if now is not None else utc_now()
            self._recover_stale_claims(staleness_threshold_seconds=STALE_RUNNING_THRESHOLD_SECONDS, now=now)
            candidates = sorted(
                (j for j in self._by_id.values() if j.status in ("PENDING", "FAILED_RETRYABLE")),
                key=lambda j: (j.created_at, j.job_id),
            )
            claimed: list[EvidenceClassificationJob] = []
            for job in candidates[:limit]:
                updated = transition_job(job, "CLAIMED", now=now, claimed_by=claimed_by, claimed_at=now)
                self._by_id[updated.job_id] = updated
                claimed.append(updated)
            return claimed

    def mark_in_progress(self, job_id: str) -> EvidenceClassificationJob:
        with self._lock:
            job = self.get_job(job_id)
            # See module docstring's "Attempt-counting" section —
            # increments here, unconditionally, exactly once per
            # genuine dispatch attempt.
            updated = transition_job(job, "IN_PROGRESS", attempt_count=job.attempt_count + 1)
            self._by_id[job_id] = updated
            return updated

    def mark_succeeded(self, job_id: str) -> EvidenceClassificationJob:
        with self._lock:
            job = self.get_job(job_id)
            updated = transition_job(job, "SUCCEEDED")
            self._by_id[job_id] = updated
            return updated

    def mark_failed(self, job_id: str, *, error: str, retryable: bool) -> EvidenceClassificationJob:
        with self._lock:
            job = self.get_job(job_id)
            target_status = "FAILED_RETRYABLE" if (retryable and job.attempt_count < job.max_attempts) else "FAILED_TERMINAL"
            updated = transition_job(job, target_status, last_error=error)
            self._by_id[job_id] = updated
            return updated

    def get_job(self, job_id: str) -> EvidenceClassificationJob:
        try:
            return self._by_id[job_id]
        except KeyError:
            raise NotFoundError(f"no EvidenceClassificationJob with job_id '{job_id}'") from None

    def get_by_evidence(self, evidence_id: str) -> Optional[EvidenceClassificationJob]:
        existing_id = self._by_evidence.get(evidence_id)
        return self._by_id[existing_id] if existing_id is not None else None


def enqueue_classification_job_for_evidence(
    evidence_id: str,
    *,
    classification_job_repository: EvidenceClassificationJobRepository,
    actor_type: str,
    actor_id: str,
    correlation_id: Optional[str] = None,
) -> None:
    """The ONE new integration point both real evidence-creation call
    sites invoke, immediately after their own
    `core.api.BagmanCanonicalAPI.register_evidence` call has returned
    successfully (see `services/mailbox/microsoft/evidence_ingest.py`
    and `app/api/routers/intake.py`) — enqueues a durable
    `EvidenceClassificationJob` for `evidence_id`, idempotently (see
    module docstring's own idempotency doctrine: a replayed
    `register_evidence` call, or any other repeat call for the same
    `evidence_id`, is always safe).

    **Never raises.** The invariant "no classification failure may
    corrupt evidence ingestion" (already true of `classify_evidence`
    itself — it never raises for an ordinary AI/context failure)
    extends here to "no ENQUEUE failure may corrupt evidence ingestion
    either": `EvidenceItem` registration is the primary, already-durably
    -committed operation (see module docstring's "known, accepted,
    NOT-closed-here gap" section — the transaction that commits the
    `EvidenceItem` has ALREADY completed by the time either call site
    reaches this function); losing the automatic classification trigger
    for one evidence item is a real but bounded degradation (an
    operator can always re-trigger classification for it by hand via
    the existing HTTP endpoints), never a reason to fail an otherwise-
    successful evidence observation. There is no established
    "log a non-fatal side-effect failure" helper elsewhere in this
    codebase to reuse (checked: neither `services/` nor `core/` defines
    one — only `app/api/` configures the stdlib `logging` module, which
    is an application-layer concern this module deliberately does not
    depend on) — this function's own documented judgment call is a
    plain, module-level `logging.getLogger(__name__)` call (stdlib
    only, no new dependency, no app-layer coupling), logged at `ERROR`
    with `exc_info=True` so the failure is fully visible in ordinary
    process logs/log aggregation without ever propagating to the
    caller.
    """
    try:
        classification_job_repository.submit_job(
            evidence_id=evidence_id, actor_type=actor_type, actor_id=actor_id, correlation_id=correlation_id,
        )
    except Exception:  # noqa: BLE001 - must never propagate; see docstring
        logger.error(
            "failed to enqueue EvidenceClassificationJob for evidence_id=%s — evidence registration "
            "itself is unaffected; this evidence item will not be automatically classified unless an "
            "operator re-triggers classification for it by hand (see module docstring's known gap)",
            evidence_id,
            exc_info=True,
        )
