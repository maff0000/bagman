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
second ``submit_job`` call for the SAME ``evidence_id`` returns the existing row UNCHANGED,
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

Simplified design (`evidence/classification-simplification` WO —
supersedes the enqueue-at-ingestion + reconciliation-cursor design
below)
------------------------------------------------------------------------
Three prior delivery rounds (`evidence/automatic-classification-
activation`, `evidence/classification-activation-preflight`, and this
module's own second architect-review delta) built an automatic-
classification trigger shaped as "enqueue a durable job the moment
evidence is registered, plus a durable, cyclic reconciliation cursor
(with a `lap` generation counter and a frozen-per-lap `target` upper
bound) to recover an enqueue that gets lost" — real architectural
complexity, all of it in service of one narrow problem: a second
durable write (the enqueue) at ingestion time can be lost between
`register_evidence`'s own commit and the enqueue call actually running.

This WO removes that problem at its root instead of building more
machinery to recover from it: ingestion no longer attempts a second
durable write at all. There is nothing to enqueue, so there is nothing
to lose, and therefore nothing to reconcile. Classification job
creation now happens entirely on the WORKER side, via
:func:`create_missing_classification_jobs` (below), which asks
PostgreSQL itself — fresh, every single call, via
:meth:`EvidenceClassificationJobRepository.list_missing_classification_candidates`'s
own real `NOT EXISTS`/anti-join query — "which post-activation evidence
items have neither a job nor a current classification yet". No cursor
is needed because PostgreSQL's own current state already IS the
durable state: there is nothing to remember between worker invocations
beyond what the tables already contain. A late-committing `EvidenceItem`
is found the moment it becomes query-visible (never excluded by a
stale watermark the way the old cursor design could exclude it under a
commit-order-vs-`created_at`-order inversion — see the now-deleted
`services.evidence.classification_reconciliation_cursor` module's own
former docstring for the full class of bug this simply cannot
reproduce, because there is no cursor position to invert against); a
worker outage merely delays discovery, never loses it; and a backlog
larger than one call's `limit` drains naturally across repeated calls,
because each call's own job creation shrinks the NEXT call's own
candidate set at the database level.

`enqueue_classification_job_for_evidence` (the ingestion-side trigger)
and `reconcile_missing_classification_jobs`/
`_scan_and_repair_evidence_missing_classification_jobs` (the
cursor-based recovery scan, together with the whole
`EvidenceClassificationReconciliationCursor` module/table they
depended on) are DELETED entirely by this WO — not deprecated, not
superseded-but-kept. Evidence registration
(`services/mailbox/microsoft/evidence_ingest.py`,
`app/api/routers/intake.py`) no longer references
`EvidenceClassificationJobRepository`/the activation boundary at all;
see each call site's own history for what was removed. The activation
boundary concept itself is UNCHANGED and still required — see
:func:`resolve_evidence_classification_activation_boundary` below — it
is just consumed by exactly one caller now
(`scripts/process_evidence_classification_jobs.py`'s own worker),
never by ingestion.

The activation boundary is Layer-2 runtime configuration, NEVER a
hardcoded constant (corrected 2026-09-29, architect review, WO item 1)
------------------------------------------------------------------------
An earlier version of this module defined
``AUTOMATIC_CLASSIFICATION_ACTIVATION_BOUNDARY`` as a hardcoded
``datetime`` constant, set to "today" at implementation time. That was
wrong: this feature had not yet been deployed to production at
implementation time, so the real production-activation moment is
necessarily LATER than any date fixed in source code at implementation
time — evidence received between the hardcoded date and the eventual
real activation moment would have been wrongly treated as prospective
(reconcilable) when it is actually historical (pre-activation),
violating the "no historical backfill" boundary above. This module no
longer defines any such constant. :func:`create_missing_classification_jobs`
below takes ``activation_boundary`` as an explicit, REQUIRED keyword
parameter instead — this keeps the function itself pure and directly
testable, with no environment-variable coupling of its own (consistent
with every other explicit parameter it already takes: ``actor_type``,
``actor_id``, and so on).

The REAL value is supplied by the one caller that matters —
``scripts/process_evidence_classification_jobs.py``'s ``main()`` —
which reads it from the ``BAGMAN_EVIDENCE_CLASSIFICATION_ACTIVATION_BOUNDARY``
environment variable (BAGMAN's own documented Layer 2 "runtime
environment configuration" — see ``config/README.md``, which
explicitly lists "feature flags" as a canonical Layer 2 example; an
activation boundary is the same shape of thing: environment-specific,
not secret, supplied externally, never committed as a real value). If
that variable is unset or malformed, reconciliation is SKIPPED for
that run — fail closed, never silently defaulted to "now" or to any
other value — see that script's own module docstring for the full
doctrine. This value will be set for real only at the later,
separately-authorised production-activation WO; until then, no real
value exists anywhere, including in any example config file.

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

``DEFERRED`` — an ``AI_IN_PROGRESS`` check must never consume
attempt/failure budget (architect requirement, 2026-09-29 review, WO
item 3)
------------------------------------------------------------------------
Because ``attempt_count`` increments unconditionally at
:meth:`mark_in_progress` (see above), a job that is claimed and
dispatched while ANOTHER invocation for the same evidence is genuinely
still running (``classify_evidence`` returns
``OUTCOME_AI_IN_PROGRESS`` — nothing failed, nothing completed) would,
under the plain "increment then find out the outcome" sequence, have
already spent one attempt before the worker even learns nothing was
wrong. Repeated polling of a slow-but-healthy AI invocation could
therefore exhaust ``max_attempts`` and wrongly terminate the job, even
though no real attempt ever failed. ``DEFERRED`` is the fix: a new,
non-terminal status reached only from ``IN_PROGRESS``
(:meth:`EvidenceClassificationJobRepository.mark_deferred`), which
clears ``claimed_by``/``claimed_at`` exactly like the stale-claim
recovery transitions already do, and — the actual correctness fix —
DECREMENTS ``attempt_count`` by exactly 1, undoing the increment
:meth:`mark_in_progress` applied for that one specific claim, since a
deferred check discovers nothing about whether classification itself
can succeed and is therefore not a genuine classification attempt at
all. A ``DEFERRED`` job is claimable again exactly like ``PENDING``/
``FAILED_RETRYABLE`` (see ``_CLAIMABLE_STATUSES`` in both concrete
repositories), and does NOT participate in stale-claim recovery — it
holds no claim lock to be stale (see :func:`is_stale_claim`, which
already excludes it by construction: that function only ever returns
`True` for `CLAIMED`/`IN_PROGRESS`).

Terminal AI-failure outcomes are genuinely NOT recoverable via normal
retry — Option A doctrine (corrected 2026-09-29, architect review, WO
item 4)
------------------------------------------------------------------------
An earlier round of this delivery's own documentation claimed an
operator could recover a ``FAILED_TERMINAL`` job whose
``classification_outcome`` is ``AI_INVOCATION_FAILED``/
``AI_PRIOR_FAILURE`` "by hand via the existing HTTP endpoints". That
claim is FALSE and is retracted here (see
``scripts/process_evidence_classification_jobs.py``'s own
``_AI_FAILURE_OUTCOMES`` for the full, corrected reasoning): a manual
HTTP call to any of the three existing classification endpoints, for
the SAME ``evidence_id`` with an UNCHANGED classification context
(same content, same classification-rule state, same prompt/task
version), computes the IDENTICAL
``compute_classifier_fingerprint`` result
(``services.evidence.classification_ai_fingerprint`` — a pure function
of evidence content + classification context + task/prompt version,
never of time or caller identity) and, per ``classify_evidence``'s own
§27 same-fingerprint guard
(``services.evidence.classification_orchestrator``, the
``matching_fingerprint``/``prior_failed`` branch), immediately returns
``AI_PRIOR_FAILURE`` again WITHOUT ever attempting a new model call.
Neither automatic nor manual retry can recover this state. Real
recovery requires either the classification context genuinely
changing (e.g. a prompt/task version bump, which changes the
fingerprint for every future call) or a separately-authorised future
reset/retry mechanism that does not exist yet — this delivery
deliberately does NOT build one (architect's own explicit "Option
A... this is acceptable for this WO if operational visibility is
sufficient" ruling): the job's own durable ``classification_outcome``
+ ``FAILED_TERMINAL`` status IS that operational visibility — an
operator reviewing failed jobs can see this state precisely; they just
cannot currently self-serve a fix for it through this delivery's own
tooling.
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
from services.evidence.classification import CLASSIFICATION_TYPE_DOCUMENT_TYPE
from services.evidence.classification_orchestrator import CLASSIFY_EVIDENCE_OUTCOMES
from services.evidence.evidence import EvidenceRepository

logger = logging.getLogger(__name__)

#: Layer-2 runtime environment configuration (see module docstring's
#: "The activation boundary is Layer-2 runtime configuration" section)
#: — an ISO-8601 UTC string, e.g. `2026-10-15T00:00:00Z`. RELOCATED HERE
#: from `scripts/process_evidence_classification_jobs.py` (preflight
#: review correction, item C) — as of the `evidence/classification-
#: simplification` WO this is consumed by exactly ONE real caller (that
#: same worker script's own job-discovery trigger,
#: :func:`create_missing_classification_jobs`) — evidence ingestion no
#: longer references it at all (see
#: :func:`resolve_evidence_classification_activation_boundary`'s own
#: docstring).
EVIDENCE_CLASSIFICATION_ACTIVATION_BOUNDARY_ENV_VAR = "BAGMAN_EVIDENCE_CLASSIFICATION_ACTIVATION_BOUNDARY"


def resolve_evidence_classification_activation_boundary(raw: Optional[str]) -> tuple[Optional[datetime], Optional[str]]:
    """Parse `EVIDENCE_CLASSIFICATION_ACTIVATION_BOUNDARY_ENV_VAR`'s raw
    value (or `None`, if unset) into `(activation_boundary,
    skip_reason)` — exactly one of the pair is ever non-`None`. A PURE
    function — no env/I/O of its own — deliberately taking the raw
    string rather than reading `os.environ` itself, so it is directly
    unit-testable with no environment mutation required.

    Consumed ONLY by the classification worker's own execution
    environment (`scripts/process_evidence_classification_jobs.py`'s
    `main()`/`_run_worker`) as of the `evidence/classification-
    simplification` WO — evidence ingestion
    (`services/mailbox/microsoft/evidence_ingest.py`,
    `app/api/routers/intake.py`) no longer references the activation
    boundary (or any classification-job concept) at all; it never
    attempts a classification-job enqueue in the first place, so it has
    nothing left to gate. See :func:`create_missing_classification_jobs`
    below for where this value is actually used. RELOCATED HERE
    (unchanged behavior) from `scripts/process_evidence_classification_jobs.py`'s
    own former `_resolve_activation_boundary` (preflight review
    correction, item C) — see :func:`resolve_evidence_classification_activation_boundary_from_env`
    below for the thin, env-reading wrapper real call sites actually
    use.

    Mirrors `services/xero/client.py`'s own `_parse_xero_wire_datetime`
    parsing pattern exactly (`datetime.fromisoformat(value.replace("Z",
    "+00:00"))`), but — unlike that helper, which silently returns
    `None` for an unparseable value because a single cosmetic Xero
    field is not worth failing a whole sync over — a malformed
    activation boundary here fails LOUDLY and SPECIFICALLY: the
    returned `skip_reason` names the exact env var, the exact raw value
    received, and the exact parse error, so an operator/caller never
    has to guess why activation did not happen.

    Never raises. Fails CLOSED: `raw` missing/empty, or present but not
    valid ISO-8601, or present and parseable but timezone-naive (no
    explicit offset — `datetime.fromisoformat` happily accepts this and
    silently returns a naive value, which would then compare against a
    real, timezone-aware `created_at` column ambiguously) all return
    `(None, <reason>)` — never a silent default to "now" or to any
    other value.
    """
    if not raw:
        return None, (
            f"{EVIDENCE_CLASSIFICATION_ACTIVATION_BOUNDARY_ENV_VAR} is not set — evidence classification "
            "activation skipped (fail closed; set it to a real ISO-8601 UTC value, e.g. "
            "2026-10-15T00:00:00Z, at the production-activation WO)"
        )
    try:
        parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError as exc:
        return None, (
            f"{EVIDENCE_CLASSIFICATION_ACTIVATION_BOUNDARY_ENV_VAR}={raw!r} could not be parsed as "
            f"ISO-8601 ({exc}) — evidence classification activation skipped (fail closed)"
        )
    # A value with no timezone/offset at all (e.g. "2026-10-15T00:00:00",
    # no trailing "Z") parses to a NAIVE datetime — comparing that
    # against a real, timezone-AWARE column would not raise, but would
    # be resolved ambiguously (session/server-local time), a genuine
    # silent-correctness risk directly inside the one guarantee this
    # function exists to provide ("fail loudly on malformed input,
    # never guess"). Treated as malformed too — fail closed, exactly
    # like any other parse failure, never silently accepted.
    if parsed.tzinfo is None:
        return None, (
            f"{EVIDENCE_CLASSIFICATION_ACTIVATION_BOUNDARY_ENV_VAR}={raw!r} has no timezone/offset (e.g. "
            "no trailing 'Z') — an activation boundary must be explicit and unambiguous; evidence "
            "classification activation skipped (fail closed)"
        )
    return parsed, None


#: The closed set of `EvidenceClassificationJob` lifecycle states —
#: identical vocabulary to `ai.jobs.BACKGROUND_JOB_STATUSES` (see
#: module docstring).
EVIDENCE_CLASSIFICATION_JOB_STATUSES = frozenset(
    {"PENDING", "CLAIMED", "IN_PROGRESS", "DEFERRED", "SUCCEEDED", "FAILED_RETRYABLE", "FAILED_TERMINAL"}
)

#: States from which no further transition is possible. `DEFERRED` is
#: deliberately NOT terminal (see module docstring's own "DEFERRED"
#: section) — it is an ordinary, immediately-reclaimable resting state,
#: exactly like `PENDING`/`FAILED_RETRYABLE`.
EVIDENCE_CLASSIFICATION_JOB_TERMINAL_STATUSES = frozenset({"SUCCEEDED", "FAILED_TERMINAL"})

#: The single source of truth for valid `EvidenceClassificationJob`
#: state transitions — mirrors `ai.jobs.BACKGROUND_JOB_ALLOWED_TRANSITIONS`
#: exactly (see that module's own docstring for the "PENDING is reached
#: by two different call paths" note, which applies identically here),
#: extended with `DEFERRED` (module docstring's own "DEFERRED" section,
#: architect requirement WO item 3): reachable only from `IN_PROGRESS`,
#: and itself only ever leads back to `CLAIMED` — identical shape to
#: `FAILED_RETRYABLE`'s own single `CLAIMED` target.
EVIDENCE_CLASSIFICATION_JOB_ALLOWED_TRANSITIONS: dict[str, frozenset[str]] = {
    "PENDING": frozenset({"CLAIMED"}),
    "CLAIMED": frozenset({"IN_PROGRESS", "PENDING"}),
    "IN_PROGRESS": frozenset({"SUCCEEDED", "FAILED_RETRYABLE", "FAILED_TERMINAL", "DEFERRED"}),
    "DEFERRED": frozenset({"CLAIMED"}),
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
    #: The exact `classify_evidence` outcome this job's one dispatch
    #: attempt produced (one of `services.evidence
    #: .classification_orchestrator.CLASSIFY_EVIDENCE_OUTCOMES` — never
    #: a re-typed/invented string; see module docstring's "Correct
    #: durable job outcome semantics" doctrine, architect requirement
    #: WO item 3). `None` until a genuine, non-raising `classify_evidence`
    #: call has completed for this job — i.e. for every job that has
    #: never left `PENDING`/`CLAIMED`, and for a `FAILED_RETRYABLE`/
    #: `FAILED_TERMINAL` row produced by a genuinely RAISED infrastructure
    #: exception (which never reaches `classify_evidence`'s own outcome
    #: vocabulary at all — see `mark_failed`'s own docstring).
    classification_outcome: Optional[str] = None

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
            "classification_outcome": self.classification_outcome,
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
    only ever reached via a claim, which always stamps it).

    `DEFERRED` is deliberately excluded by this same `job.status not in
    ("CLAIMED", "IN_PROGRESS")` check, with no further change needed
    (confirmed, not assumed — module docstring's own "DEFERRED"
    section): a `DEFERRED` row holds no `claimed_by`/`claimed_at` lock
    (cleared by :meth:`EvidenceClassificationJobRepository.mark_deferred`,
    exactly like the stale-claim recovery transitions already clear
    them for `PENDING`), so it is an ordinary, correctly-resting,
    immediately-reclaimable state, never a "stuck claimed" row the way
    an abandoned `CLAIMED`/`IN_PROGRESS` row genuinely is."""
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
    def mark_succeeded(self, job_id: str, *, classification_outcome: str) -> EvidenceClassificationJob:
        """`IN_PROGRESS -> SUCCEEDED`. No `ai_invocation_id` parameter
        (see module docstring — a deterministic-only success is a
        complete, valid outcome with no AI invocation at all).

        `classification_outcome` is REQUIRED — the exact
        `classify_evidence` outcome this dispatch attempt produced
        (architect requirement, WO item 3: a job may only ever be
        marked SUCCEEDED once the caller can name, honestly, which of
        `services.evidence.classification_orchestrator
        .CLASSIFY_EVIDENCE_OUTCOMES` it actually got — never an
        undifferentiated "it didn't raise").

        Raises:
            core.errors.ValidationError: `classification_outcome` is
                not one of `CLASSIFY_EVIDENCE_OUTCOMES`.
        """
        raise NotImplementedError

    @abc.abstractmethod
    def mark_failed(
        self, job_id: str, *, error: str, retryable: bool, classification_outcome: Optional[str] = None
    ) -> EvidenceClassificationJob:
        """`IN_PROGRESS -> FAILED_RETRYABLE` (if `retryable` and
        `attempt_count < max_attempts`) or `IN_PROGRESS ->
        FAILED_TERMINAL` (otherwise). Always stamps `last_error`.

        `classification_outcome` is OPTIONAL here, unlike
        `mark_succeeded` — a genuine RAISED infrastructure exception
        (object store down, database down, ...) never reaches
        `classify_evidence`'s own outcome vocabulary at all, so there
        is nothing honest to record; leave it `None` for that case.
        An outcome-driven terminal failure (`AI_INVOCATION_FAILED`/
        `AI_PRIOR_FAILURE` — see
        `scripts/process_evidence_classification_jobs.py`'s own
        outcome-mapping doctrine) DOES have a real outcome to record —
        pass it.

        Raises:
            core.errors.ValidationError: `classification_outcome` is
                supplied but is not one of `CLASSIFY_EVIDENCE_OUTCOMES`.
        """
        raise NotImplementedError

    @abc.abstractmethod
    def mark_deferred(self, job_id: str, *, classification_outcome: str) -> EvidenceClassificationJob:
        """`IN_PROGRESS -> DEFERRED` (module docstring's own "DEFERRED"
        section, architect requirement WO item 3) — used when
        `classify_evidence` reports `OUTCOME_AI_IN_PROGRESS`: another
        invocation for this evidence is genuinely still active, nothing
        failed, nothing completed. Clears `claimed_by`/`claimed_at` to
        `None` (a `DEFERRED` row is immediately reclaimable, exactly
        like `PENDING`), and — the critical correctness fix this status
        exists for — DECREMENTS `attempt_count` by exactly 1, undoing
        the increment `mark_in_progress` applied for THIS specific
        claim: a deferred check is not a genuine classification attempt
        at all, so it must never consume attempt/failure budget. This
        is a deliberate, documented compensating action, not an
        accidental double-transition — `attempt_count` is guaranteed
        >= 1 at this point (the only path into `IN_PROGRESS` is via
        `mark_in_progress`, which always increments first).

        `classification_outcome` is REQUIRED, accepted generically like
        `mark_succeeded`'s own parameter (always `OUTCOME_AI_IN_PROGRESS`
        in practice, but not hardcoded here).

        Raises:
            core.errors.ValidationError: `classification_outcome` is
                not one of `CLASSIFY_EVIDENCE_OUTCOMES`.
        """
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

    @abc.abstractmethod
    def list_missing_classification_candidates(
        self, *, activation_boundary: datetime, limit: int,
    ) -> list[str]:
        """Up to `limit` `evidence_id`s for `EvidenceItem`s whose
        `created_at` is `>= activation_boundary`, that have NEITHER an
        existing `EvidenceClassificationJob` NOR a current
        (`CLASSIFICATION_TYPE_DOCUMENT_TYPE`) `EvidenceClassification` —
        ordered `EvidenceItem.created_at ASC` (oldest-eligible-first),
        with `evidence_id ASC` as a deterministic tie-break.

        This is the entire discovery mechanism
        :func:`create_missing_classification_jobs` relies on (module
        docstring's own "Simplified design" section): a real PostgreSQL
        `NOT EXISTS`/anti-join query against tables that already exist
        (`evidence_items`, `evidence_classification_jobs`,
        `evidence_classifications`), run fresh every call — never a
        full-table Python-side scan, never a persisted cursor. Returns
        an empty list if nothing qualifies — never raises for the
        ordinary "nothing to do" case.
        """
        raise NotImplementedError


class InMemoryEvidenceClassificationJobRepository(EvidenceClassificationJobRepository):
    """Narrow in-memory reference implementation, mirroring
    `ai.jobs.InMemoryBackgroundJobRepository`'s own shape. Single-process,
    GIL-serialised — a plain `threading.RLock` around each mutating
    method is enough (no real cross-process concurrency to defend
    against here, unlike the Postgres implementation's
    `SELECT ... FOR UPDATE SKIP LOCKED`)."""

    def __init__(
        self,
        *,
        audit_repository: Optional[AuditRepository] = None,
        evidence_repository: Optional[EvidenceRepository] = None,
        classification_repository=None,
    ) -> None:
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
        # Both OPTIONAL, defaulting to `None` so every EXISTING
        # no-arg/audit-only construction call keeps working unchanged —
        # only needed by :meth:`list_missing_classification_candidates`
        # below (a duck-typed `EvidenceRepository`/
        # `EvidenceClassificationRepository`-shaped pair; never imported
        # as a concrete type here, mirroring every other cross-repository
        # dependency in this codebase).
        self._evidence_repository = evidence_repository
        self._classification_repository = classification_repository

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
                (j for j in self._by_id.values() if j.status in ("PENDING", "FAILED_RETRYABLE", "DEFERRED")),
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

    def mark_succeeded(self, job_id: str, *, classification_outcome: str) -> EvidenceClassificationJob:
        if classification_outcome not in CLASSIFY_EVIDENCE_OUTCOMES:
            raise ValidationError(
                f"classification_outcome '{classification_outcome}' is not one of "
                f"{sorted(CLASSIFY_EVIDENCE_OUTCOMES)}"
            )
        with self._lock:
            job = self.get_job(job_id)
            updated = transition_job(job, "SUCCEEDED", classification_outcome=classification_outcome)
            self._by_id[job_id] = updated
            return updated

    def mark_failed(
        self, job_id: str, *, error: str, retryable: bool, classification_outcome: Optional[str] = None
    ) -> EvidenceClassificationJob:
        if classification_outcome is not None and classification_outcome not in CLASSIFY_EVIDENCE_OUTCOMES:
            raise ValidationError(
                f"classification_outcome '{classification_outcome}' is not one of "
                f"{sorted(CLASSIFY_EVIDENCE_OUTCOMES)}"
            )
        with self._lock:
            job = self.get_job(job_id)
            target_status = "FAILED_RETRYABLE" if (retryable and job.attempt_count < job.max_attempts) else "FAILED_TERMINAL"
            updated = transition_job(job, target_status, last_error=error, classification_outcome=classification_outcome)
            self._by_id[job_id] = updated
            return updated

    def mark_deferred(self, job_id: str, *, classification_outcome: str) -> EvidenceClassificationJob:
        if classification_outcome not in CLASSIFY_EVIDENCE_OUTCOMES:
            raise ValidationError(
                f"classification_outcome '{classification_outcome}' is not one of "
                f"{sorted(CLASSIFY_EVIDENCE_OUTCOMES)}"
            )
        with self._lock:
            job = self.get_job(job_id)
            # See ABC's own mark_deferred docstring — this is a
            # deliberate compensating decrement, undoing the increment
            # mark_in_progress applied for THIS claim, never an
            # accidental double-transition. attempt_count is guaranteed
            # >= 1 here: the only path into IN_PROGRESS is via
            # mark_in_progress.
            assert job.attempt_count >= 1  # noqa: S101 - guaranteed by construction, see docstring
            updated = transition_job(
                job,
                "DEFERRED",
                classification_outcome=classification_outcome,
                claimed_by=None,
                claimed_at=None,
                attempt_count=job.attempt_count - 1,
            )
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

    def list_missing_classification_candidates(
        self, *, activation_boundary: datetime, limit: int,
    ) -> list[str]:
        """A plain, narrow, equivalent-semantics Python implementation —
        this is the IN-MEMORY reference implementation for unit tests,
        never the thing this delivery is trying to make efficient (see
        the Postgres implementation's own docstring for the real,
        single-query anti-join this mirrors). Requires both
        `evidence_repository`/`classification_repository` to have been
        supplied at construction time; raises a clear `RuntimeError`
        naming what is missing otherwise — never silently returns an
        empty/wrong result for a misconfigured repository."""
        missing: list[str] = []
        if self._evidence_repository is None:
            missing.append("evidence_repository")
        if self._classification_repository is None:
            missing.append("classification_repository")
        if missing:
            raise RuntimeError(
                "InMemoryEvidenceClassificationJobRepository.list_missing_classification_candidates "
                f"requires the following to have been supplied at construction time: {missing}"
            )
        if limit <= 0:
            return []

        with self._lock:
            eligible = sorted(
                (
                    item
                    for item in self._evidence_repository.list_evidence()
                    if item.created_at >= activation_boundary
                ),
                key=lambda item: (item.created_at, item.evidence_id),
            )
            candidate_ids: list[str] = []
            for item in eligible:
                if len(candidate_ids) >= limit:
                    break
                if self.get_by_evidence(item.evidence_id) is not None:
                    continue
                if (
                    self._classification_repository.get_current_classification(
                        item.evidence_id, CLASSIFICATION_TYPE_DOCUMENT_TYPE
                    )
                    is not None
                ):
                    continue
                candidate_ids.append(item.evidence_id)
            return candidate_ids


def create_missing_classification_jobs(
    *,
    classification_job_repository: EvidenceClassificationJobRepository,
    actor_type: str,
    actor_id: str,
    activation_boundary: datetime,
    limit: int = 200,
) -> int:
    """Discovers up to `limit` post-activation `EvidenceItem`s missing a
    classification job (via :meth:`EvidenceClassificationJobRepository
    .list_missing_classification_candidates`'s own real PostgreSQL
    anti-join — never a Python-side scan), and creates a job for each,
    idempotently.

    The real `evidence_id` unique constraint
    (`uq_evidence_classification_jobs_evidence_id`) is still the final
    safety net under a genuine concurrent race — no second,
    application-level guard is added here, consistent with this
    codebase's established doctrine (see
    :meth:`EvidenceClassificationJobRepository.submit_job`'s own module
    docstring).

    Returns the number of `submit_job` calls made, i.e. the number of
    candidates this call's own discovery query returned — NOT
    necessarily the number of rows freshly inserted: `submit_job`
    itself does not report whether it created a new row or idempotently
    returned an existing one, so if a genuine concurrent caller wins a
    race for the same `evidence_id` in between discovery and submission,
    this call's own count still includes that candidate even though no
    new row resulted. In ordinary (non-racing) operation this never
    diverges from "jobs created", since a candidate this function was
    just handed, by definition, had no job yet at the moment it was
    selected — the divergence is a narrow, honestly-disclosed edge case,
    never a correctness issue (the real constraint still prevents any
    duplicate row).

    No cursor, no scan state, nothing to remember between calls (module
    docstring's own "Simplified design" section): PostgreSQL's own
    current state (which evidence has a job, which has a current
    classification) is queried fresh every single call — a
    late-committing `EvidenceItem` is found the moment it becomes
    query-visible, a worker outage merely delays discovery (never loses
    it), and a backlog larger than `limit` drains naturally across
    repeated calls because each call's own job creation shrinks the
    NEXT call's own candidate set at the database level.

    A single bounded pass per call — deliberately no limit-exhaustion
    loop, no retry logic, no cursor of any kind. `limit` default (200)
    matches the WO's own established convention for this family of
    bounded operator-invoked passes
    (`scripts/process_evidence_classification_jobs.py`'s own
    `claim_next_pending` default usage).
    """
    evidence_ids = classification_job_repository.list_missing_classification_candidates(
        activation_boundary=activation_boundary, limit=limit,
    )
    created = 0
    for evidence_id in evidence_ids:
        classification_job_repository.submit_job(
            evidence_id=evidence_id, actor_type=actor_type, actor_id=actor_id,
        )
        created += 1
    return created
