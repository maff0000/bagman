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

The enqueue-loss gap, and how it is closed (architect requirement,
2026-09-29 review of this WO)
------------------------------------------------------------------------
There is no outbox/LISTEN-NOTIFY/DB-trigger mechanism ANYWHERE in this
codebase (verified: neither ``ai.jobs``/``BackgroundJob`` nor any other
durable-job mechanism in this repository has one either) — so if the
process enqueuing a job crashes between
``core.api.BagmanCanonicalAPI.register_evidence``'s own transaction
committing and :func:`enqueue_classification_job_for_evidence` actually
being called (or if that call itself fails and its own exception is
swallowed — see that function's own docstring), that one
``EvidenceItem`` would never get an automatic job on its own. An
earlier version of this delivery disclosed this as an accepted, narrow,
NOT-closed-here limitation and left it there. The architect's review of
this WO REQUIRED that gap be closed — narrowly, prospectively, never as
a historical bulk backfill (the WO's own "no historical bulk
classification" non-goal is unchanged and remains absolute).
:func:`reconcile_missing_classification_jobs`, below, is that closure:
a bounded, activation-boundary-scoped, genuinely-paging scan (reusing
:meth:`services.evidence.evidence.EvidenceRepository.list_evidence`'s
own existing ``received_at_from``/``limit``/``offset`` filtering — no
new query surface anywhere) that finds evidence with `received_at` on
or after an explicit, caller-supplied ``activation_boundary`` that has
neither a job nor a current classification yet, and submits the
missing job for it — see that function's own docstring for the full
paging doctrine, and ``scripts/process_evidence_classification_jobs.py``'s
module docstring for how it is wired into the worker's own run
sequence and where ``activation_boundary`` actually comes from.
Evidence received BEFORE the activation boundary remains permanently
untouched by anything in this module, at any scan size, on any call —
that is the explicit, durable boundary between "this WO's own narrow,
prospective repair" and "a historical bulk backfill", which stays out
of scope. The registration transaction itself
(``persistence/postgres/evidence_repository.py``'s own ``session_scope``
covering the ``EvidenceItem`` + external-reference rows) is unaffected
either way — this gap was always scoped ENTIRELY to "does a job get
enqueued", never to evidence durability itself.

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
longer defines any such constant. :func:`reconcile_missing_classification_jobs`
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
import os
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
from services.evidence.classification_reconciliation_cursor import (
    EvidenceClassificationReconciliationCursorRepository,
)

logger = logging.getLogger(__name__)

#: Layer-2 runtime environment configuration (see module docstring's
#: "The activation boundary is Layer-2 runtime configuration" section)
#: — an ISO-8601 UTC string, e.g. `2026-10-15T00:00:00Z`. RELOCATED HERE
#: from `scripts/process_evidence_classification_jobs.py` (preflight
#: review correction, item C) — this is now the ONE, public, canonical
#: name both the worker script's reconciliation trigger AND the two
#: real enqueue call sites (`services/mailbox/microsoft/evidence_ingest.py`,
#: `app/api/routers/intake.py`) resolve against, so "unset/malformed"
#: means exactly the same thing everywhere it is checked — "one
#: coherent activation contract", never two independently-maintained
#: copies of the same parsing logic.
EVIDENCE_CLASSIFICATION_ACTIVATION_BOUNDARY_ENV_VAR = "BAGMAN_EVIDENCE_CLASSIFICATION_ACTIVATION_BOUNDARY"


def resolve_evidence_classification_activation_boundary(raw: Optional[str]) -> tuple[Optional[datetime], Optional[str]]:
    """Parse `EVIDENCE_CLASSIFICATION_ACTIVATION_BOUNDARY_ENV_VAR`'s raw
    value (or `None`, if unset) into `(activation_boundary,
    skip_reason)` — exactly one of the pair is ever non-`None`. A PURE
    function — no env/I/O of its own — deliberately taking the raw
    string rather than reading `os.environ` itself, so it is directly
    unit-testable with no environment mutation required. RELOCATED HERE
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


def resolve_evidence_classification_activation_boundary_from_env() -> tuple[Optional[datetime], Optional[str]]:
    """Thin, env-reading convenience wrapper around
    :func:`resolve_evidence_classification_activation_boundary` — reads
    `EVIDENCE_CLASSIFICATION_ACTIVATION_BOUNDARY_ENV_VAR` from
    `os.environ` fresh on every call (cheap; no caching — the value is
    operator/deployment configuration, never expected to change
    mid-process, but re-reading costs nothing and avoids any staleness
    question) and delegates. This is what every REAL call site actually
    calls — :func:`enqueue_classification_job_for_evidence`'s own two
    real callers (`services/mailbox/microsoft/evidence_ingest.py`,
    `app/api/routers/intake.py`) and
    `scripts/process_evidence_classification_jobs.py`'s own worker —
    keeping :func:`resolve_evidence_classification_activation_boundary`
    itself independently unit-testable with no environment coupling at
    all.
    """
    return resolve_evidence_classification_activation_boundary(
        os.environ.get(EVIDENCE_CLASSIFICATION_ACTIVATION_BOUNDARY_ENV_VAR)
    )

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


def enqueue_classification_job_for_evidence(
    evidence_id: str,
    *,
    classification_job_repository: EvidenceClassificationJobRepository,
    actor_type: str,
    actor_id: str,
    evidence_created_at: datetime,
    activation_boundary: Optional[datetime],
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

    The activation gate (preflight review correction, item C — "one
    coherent activation contract")
    ------------------------------------------------------------------
    `evidence_created_at`/`activation_boundary` are REQUIRED keyword
    parameters: if `activation_boundary is None` (unset/malformed —
    see :func:`resolve_evidence_classification_activation_boundary`)
    OR `evidence_created_at < activation_boundary`, this function does
    NOT enqueue a job at all — it logs at `INFO` (never `ERROR`: this
    is normal, expected, not-yet-activated-or-pre-boundary behaviour,
    never a failure) naming exactly which of the two conditions
    applied, then returns. This closes the gap an earlier version of
    this delivery left open: before this correction, BOTH real call
    sites invoked this function UNCONDITIONALLY (the mailbox call site
    was gated only on `classification_job_repository is not None`, a
    wiring concern, not an activation concern; the intake call site had
    no gating at all), so the moment this code ran in any environment —
    even with the activation boundary env var completely unset — every
    new mailbox/manual-upload evidence registration immediately created
    a durable `EvidenceClassificationJob` that would sit `PENDING`
    forever with no worker ever configured to process it. Now, exactly
    one function (this one) decides whether a given evidence item is
    "activated" for automatic classification, used identically by both
    real call sites and driven by the SAME
    `EVIDENCE_CLASSIFICATION_ACTIVATION_BOUNDARY_ENV_VAR`/
    `resolve_evidence_classification_activation_boundary_from_env`
    :func:`reconcile_missing_classification_jobs` (below) also reads —
    "one coherent activation contract", never two independently-tuned
    gates.

    Critically, this enqueue-side gate does NOT make
    `reconcile_missing_classification_jobs` any less necessary:
    reconciliation receives its own explicit `activation_boundary`
    parameter and is the sole recovery path for anything this gate
    causes to be skipped — by design, "a missed enqueue after
    activation remains recoverable" (see
    `tests/persistence/test_evidence_classification_job_reconciliation.py
    ::test_reconciliation_finds_evidence_the_enqueue_side_gate_skipped`).

    **Never raises**, for the ordinary "the repository call itself
    failed" case (unchanged from before this correction). The invariant
    "no classification failure may corrupt evidence ingestion" (already
    true of `classify_evidence` itself — it never raises for an
    ordinary AI/context failure) extends here to "no ENQUEUE failure
    may corrupt evidence ingestion either": `EvidenceItem` registration
    is the primary, already-durably-committed operation (see module
    docstring's "The enqueue-loss gap, and how it is closed" section —
    the transaction that commits the `EvidenceItem` has ALREADY
    completed by the time either call site reaches this function);
    losing the automatic classification trigger for one evidence item
    is a real but bounded degradation — closed, prospectively, by
    :func:`reconcile_missing_classification_jobs` (below), and an
    operator can always re-trigger classification for it by hand via
    the existing HTTP endpoints too — never a reason to fail an
    otherwise-successful evidence observation. There is no established
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
    if activation_boundary is None:
        logger.info(
            "skipping EvidenceClassificationJob enqueue for evidence_id=%s — no activation boundary is "
            "configured (%s is unset/malformed); this evidence item remains recoverable later by "
            "reconcile_missing_classification_jobs once a real boundary is configured and this "
            "evidence's own created_at qualifies",
            evidence_id,
            EVIDENCE_CLASSIFICATION_ACTIVATION_BOUNDARY_ENV_VAR,
        )
        return
    if evidence_created_at < activation_boundary:
        logger.info(
            "skipping EvidenceClassificationJob enqueue for evidence_id=%s — evidence_created_at=%s is "
            "strictly before the configured activation_boundary=%s (historical evidence; this WO's own "
            "'no historical bulk backfill' boundary applies identically to the enqueue-side gate)",
            evidence_id,
            evidence_created_at,
            activation_boundary,
        )
        return

    try:
        classification_job_repository.submit_job(
            evidence_id=evidence_id, actor_type=actor_type, actor_id=actor_id, correlation_id=correlation_id,
        )
    except Exception:  # noqa: BLE001 - must never propagate; see docstring
        logger.error(
            "failed to enqueue EvidenceClassificationJob for evidence_id=%s — evidence registration "
            "itself is unaffected; this evidence item will be picked up by the next bounded "
            "reconciliation pass (reconcile_missing_classification_jobs, if created_at is on/after "
            "the operator-configured activation boundary — see "
            "scripts/process_evidence_classification_jobs.py's own module docstring for where that "
            "value comes from) or can be re-triggered by hand via the existing HTTP endpoints",
            evidence_id,
            exc_info=True,
        )


def _scan_and_repair_evidence_missing_classification_jobs(
    *,
    evidence_repository,
    classification_job_repository: EvidenceClassificationJobRepository,
    classification_repository,
    cursor_repository: EvidenceClassificationReconciliationCursorRepository,
    actor_type: str,
    actor_id: str,
    cursor_key: str,
    scan_created_at_from: datetime,
    created_at_to: Optional[datetime],
    already_inspected_ids_at_boundary: set[str],
    has_existing_cursor: bool,
    repair_limit: int,
    page_size: int,
    max_rows_scanned: int,
    lap: int,
) -> int:
    """ONE bounded scan-and-repair pass, in REGISTRATION (`created_at
    ASC, evidence_id ASC`) order, over `[scan_created_at_from,
    created_at_to]` (`created_at_to=None` means unbounded above) —
    factored out so `reconcile_missing_classification_jobs` can run
    the IDENTICAL scan/skip/inspect/submit/track-watermark logic twice
    per call (once for the forward pass, once for the "safety sweep"
    pass — see that function's own docstring) without two
    near-duplicate loops silently drifting apart over time.

    `cursor_key`/`scan_created_at_from`/`already_inspected_ids_at_boundary`/
    `has_existing_cursor` together describe where THIS pass's own
    cursor currently sits (the set-membership tie-break discipline is
    identical regardless of which cursor_key is passed — see
    `services.evidence.classification_reconciliation_cursor`'s own
    docstring, "Why the tie-break is SET membership" section); this
    pass advances `cursor_repository`'s own `cursor_key` row via
    `advance_cursor`'s merge-on-write contract (see that module's own
    "Why advance_cursor merges instead of overwrites" AND "lap"
    docstring sections) if, and only if, it actually inspected anything
    new — exactly the existing single-pass discipline, unchanged.

    `lap` is the generation counter THIS call proposes for
    `cursor_key` (see `services.evidence
    .classification_reconciliation_cursor`'s own "lap" docstring
    section) — this helper never computes it itself, only threads the
    caller's own decision through to `advance_cursor`: the forward pass
    always passes its existing cursor's own `lap` back unchanged; the
    sweep pass passes either its existing cursor's own `lap` (continuing
    a lap) or that value plus one (starting a new one) — see
    `reconcile_missing_classification_jobs`'s own docstring for the
    full reasoning.

    Returns the number of jobs submitted by THIS pass alone (never
    includes the other pass's own count — the caller sums both).
    """
    offset = 0
    submitted = 0
    rows_scanned = 0
    running_created_at: Optional[datetime] = scan_created_at_from if has_existing_cursor else None
    running_ids: set[str] = set(already_inspected_ids_at_boundary)
    any_inspected = False

    while submitted < repair_limit and rows_scanned < max_rows_scanned:
        page = evidence_repository.list_evidence(
            created_at_from=scan_created_at_from,
            created_at_to=created_at_to,
            limit=page_size,
            offset=offset,
            order_by_created_at=True,
        )
        if not page:
            break
        for item in page:
            if submitted >= repair_limit or rows_scanned >= max_rows_scanned:
                # Stop INSPECTING entirely — never advance the running
                # watermark past a row we never actually
                # resolved-or-submitted.
                break
            if (
                has_existing_cursor
                and item.created_at == scan_created_at_from
                and item.evidence_id in already_inspected_ids_at_boundary
            ):
                # Already fully handled by a previous call at this
                # exact cursor_key — a cheap set-membership check,
                # never counted against rows_scanned/repair_limit. Only
                # ever matters for rows sharing the exact
                # scan_created_at_from timestamp.
                continue

            rows_scanned += 1
            any_inspected = True
            if running_created_at is None or item.created_at > running_created_at:
                # The watermark has moved to a new, later timestamp —
                # the old timestamp's ids are irrelevant to any future
                # call at this cursor_key, so do NOT carry them forward.
                running_created_at = item.created_at
                running_ids = {item.evidence_id}
            else:
                # item.created_at == running_created_at (it can never
                # be less — order_by_created_at=True and the skip
                # condition above already excluded anything at or
                # behind scan_created_at_from that was already
                # inspected). Add to, never replace, the running set.
                running_ids.add(item.evidence_id)

            if classification_job_repository.get_by_evidence(item.evidence_id) is not None:
                continue
            if classification_repository.get_current_classification(
                item.evidence_id, CLASSIFICATION_TYPE_DOCUMENT_TYPE
            ) is not None:
                continue
            # submit_job's own idempotency (the real evidence_id unique
            # constraint) is the real safety net if a race occurs
            # mid-scan — no second guard is added here.
            classification_job_repository.submit_job(
                evidence_id=item.evidence_id, actor_type=actor_type, actor_id=actor_id,
            )
            submitted += 1
        offset += len(page)
        if len(page) < page_size:
            break

    if any_inspected:
        cursor_repository.advance_cursor(
            cursor_key, lap=lap, last_created_at=running_created_at, last_evidence_ids=running_ids,
        )

    return submitted


def reconcile_missing_classification_jobs(
    *,
    evidence_repository,
    classification_job_repository: EvidenceClassificationJobRepository,
    classification_repository,
    cursor_repository: EvidenceClassificationReconciliationCursorRepository,
    actor_type: str,
    actor_id: str,
    activation_boundary: datetime,
    repair_limit: int = 200,
    page_size: int = 200,
    max_rows_scanned: int = 5000,
    sweep_repair_limit: int = 50,
    sweep_page_size: int = 200,
    sweep_max_rows_scanned: int = 1000,
) -> int:
    """The bounded, prospective repair path for the disclosed
    "post-commit enqueue can be skipped" gap (see
    :func:`enqueue_classification_job_for_evidence`'s own docstring —
    this function is what actually closes that gap, superseding the
    earlier "accepted, not-closed-here" framing) — now ALSO the sole
    recovery path for evidence that function's own preflight-review
    activation gate (item C) skipped.

    ``activation_boundary`` is an explicit, REQUIRED parameter — see
    module docstring's own "The activation boundary is Layer-2 runtime
    configuration" section. This function itself has no knowledge of
    where the value comes from (no env-var coupling here — that lives
    entirely in ``scripts/process_evidence_classification_jobs.py``'s
    ``main()``), which keeps it pure and directly testable: evidence
    with `created_at` strictly BEFORE ``activation_boundary`` can NEVER
    be found by this scan, at any `repair_limit`/`page_size`, no matter
    how many times it runs — the `created_at_from` filter is
    unconditional and applies to every page of every call.

    Eligibility is now `created_at`-based, not `received_at`-based
    (preflight review correction, item A) — CORRECTED
    ------------------------------------------------------------------
    An earlier version of this function filtered on
    `received_at_from=activation_boundary`. That was wrong:
    `received_at` is the (possibly much earlier) time the underlying
    artifact was actually received/observed, never the time BAGMAN
    itself registered it — an operator flipping the activation boundary
    "on" prospectively governs "what gets registered from now on", not
    "what was historically received". Evidence whose `received_at` is
    old but whose `created_at` (registration time) is on/after the
    boundary is a genuine, recoverable late registration and MUST be
    reconciled; evidence whose `created_at` is genuinely historical
    (before the boundary) must NEVER be touched, regardless of what its
    own `received_at` happens to be (even a `received_at` that looks
    "prospective"). `created_at` — server-assigned, monotonic with
    registration order — is the sole authority here now.

    A durable, per-activation-boundary cursor closes a genuine
    starvation bug (preflight review correction, item B) — CORRECTED
    ------------------------------------------------------------------
    An earlier version of this function kept NO persisted cross-call
    state at all: every call started scanning from `offset=0` again
    (ordered `received_at DESC, evidence_id DESC`), relying on
    already-resolved evidence being "cheaply skipped" to make forward
    progress over repeated calls. That reasoning is WRONG in general —
    it is exactly the bug this correction fixes: if more than
    `max_rows_scanned` already-resolved rows sort ahead of a genuinely
    orphaned row in that fixed ordering, the orphan can NEVER be
    reached by any number of repeated calls, because every call
    re-scans exactly the same window and stops there. Worse, under
    `received_at DESC` ordering, an orphan's position only ever gets
    WORSE over time as more evidence is registered — a permanent,
    unrecoverable starvation, not merely a slow-convergence issue (see
    `tests/persistence/test_evidence_classification_job_reconciliation.py
    ::test_old_fixed_window_scan_would_starve_an_orphan_behind_more_than_max_rows_scanned_resolved_rows`
    for the regression proof).

    The fix is two things together: (1) `cursor_repository`
    (:mod:`services.evidence.classification_reconciliation_cursor`) — a
    REQUIRED parameter — persists, per `cursor_key =
    activation_boundary.isoformat()` (see that module's own docstring
    for why the key is the boundary's own value, not a constant), the
    `(created_at, evidence_id)` of the highest-ordered evidence row this
    function has actually INSPECTED so far; and (2) every page is now
    fetched via `evidence_repository.list_evidence(created_at_from=...,
    order_by_created_at=True, ...)` — REGISTRATION order, the only
    ordering under which an orphan's scan position is FIXED forever
    once it exists (new evidence only ever appends after it, never in
    front of it). Together these guarantee genuine, monotonic,
    PERMANENT forward progress with a FIXED `max_rows_scanned` no
    matter how large the resolved-evidence corpus grows — the property
    a single-fixed-window scan (in either ordering) can never provide.

    Concretely: `scan_created_at_from` starts at the cursor's own
    `last_created_at` if one already exists for this `cursor_key`, else
    at `activation_boundary` itself (this cursor's very first call).
    `already_inspected_ids_at_boundary` (the cursor's own
    `last_evidence_ids`) is used ONLY to skip rows that share that
    exact `created_at` timestamp AND whose `evidence_id` is a MEMBER of
    that set — pure set membership, never an ordering comparison — this
    only ever matters for the handful of rows sharing the exact
    boundary timestamp: a narrow, correctly-handled edge case, not the
    common case. After the scan loop, IF any row was actually inspected
    this call (not merely cursor-skipped), the cursor is advanced to
    `(last_seen_created_at, ids_seen_at_last_created_at)` — the highest
    `created_at` this call actually inspected, paired with EVERY
    `evidence_id` inspected (across this call and, if the watermark
    never moved past `scan_created_at_from`, every prior call too) at
    that exact timestamp — so the NEXT call's window starts exactly
    where this one left off.

    Why the tie-break is set membership, never `evidence_id <=
    scan_after_evidence_id` (post-merge audit correction)
    ------------------------------------------------------------------
    An earlier version of this function compared a single scalar
    cursor value (`evidence_id <= scan_after_evidence_id`, plain string
    comparison) to decide whether a row sharing the exact boundary
    timestamp had "already" been inspected. That is unsound:
    `evidence_id` is a UUIDv7 (`core.identity.generate_id`), and that
    module's own docstring is explicit that its monotonic-counter
    guarantee ("later-generated sorts later") holds only WITHIN one
    process — BAGMAN registers evidence from at least two separate OS
    processes (the API server and the mailbox-ingestion worker), and
    nothing guarantees ordering between ids minted by two different
    ones. If two evidence rows registered by different processes
    happened to share the exact same microsecond-precision
    `created_at`, and the later-registered row's id happened to sort
    `<=` the earlier one's, the old `<=` skip condition would silently
    and PERMANENTLY discard it — never inspected, never jobbed, with no
    error and no log (see `tests/persistence
    /test_evidence_classification_job_reconciliation.py
    ::test_cross_process_created_at_tie_evidence_id_inversion_is_not_permanently_lost`
    for the regression proof). Set membership has no such assumption:
    correctness depends only on exact-id membership in "the ids already
    confirmed-inspected at this exact timestamp", never on how any two
    ids sharing that timestamp happen to compare as strings.

    Genuine paging within one call is unchanged from the previous
    correction (architect review, WO item 2): `repair_limit` (default
    200) means "the maximum number of missing jobs to CREATE this
    run", not "how many rows to look at"; this function pages through
    `EvidenceRepository.list_evidence`'s own `limit`+`offset`
    parameters (real DB-level `OFFSET` in the Postgres implementation —
    no new query surface), `page_size` (default 200) rows at a time,
    until either `repair_limit` jobs have been submitted or the
    evidence set (bounded by `max_rows_scanned`, see below) is
    exhausted.

    `max_rows_scanned` (default 5000) bounds how many rows a single
    call actually INSPECTS (resolves-or-submits) across all its pages —
    a cursor-skipped row costs only a cheap in-memory string comparison,
    never a real resolution attempt, so it does NOT count against this
    bound (nor against `repair_limit`) — a defensive, generous, narrow
    safety backstop against a genuinely pathological corpus, not a
    tuned operational parameter (unchanged reasoning from the previous
    correction).

    For each row this function actually inspects (i.e. not
    cursor-skipped) that has NEITHER an existing
    `EvidenceClassificationJob` NOR a current classification already,
    it submits the missing job — idempotently, relying on the real
    `evidence_id` unique constraint exactly like the normal enqueue path
    (a genuine race between this function and a normal
    `enqueue_classification_job_for_evidence` call for the same
    evidence resolves to exactly one row, never two — same DB-level
    guarantee, not a new one); no second, application-level guard is
    added here.

    A second, bounded, cyclic "safety sweep" cursor closes a genuine
    commit-order-vs-created_at-order inversion gap (architect-confirmed,
    this round's own correction)
    ------------------------------------------------------------------
    `PostgresEvidenceRepository.register_evidence` assigns
    `EvidenceItem.created_at` in Python (`utc_now()`) BEFORE its own
    transaction commits. Under Postgres's default READ COMMITTED
    isolation, two concurrent registrations can COMMIT in an order
    DIFFERENT from their `created_at` VALUE order: transaction A can
    start first (an earlier `created_at`) but commit LATER than
    transaction B (a later `created_at`, committed first). If a forward
    reconciliation pass runs while A is still uncommitted (invisible to
    that scan's snapshot), sees B, and advances the forward cursor's
    watermark to B's timestamp, then A commits afterward — A's
    `created_at` is now STRICTLY BEFORE the forward cursor's watermark,
    so `created_at_from` excludes it from the forward pass's own scan
    forever, even though it only just became visible and may never have
    gotten its immediate-enqueue job either (that enqueue call happens
    inside the SAME window this gap exploits). The forward cursor above
    is correct and remains the primary, low-latency discovery mechanism
    for the common case (evidence usually commits promptly, in roughly
    `created_at` order) — it is NOT removed or weakened here.

    The safety sweep is a SEPARATE, independent cursor, keyed
    `f"{activation_boundary.isoformat()}::sweep"` under the exact SAME
    `EvidenceClassificationReconciliationCursor` dataclass/repository/
    Postgres table the forward cursor already uses — a distinct,
    deterministic key under the exact same persistence, no new table,
    no new repository class, no new migration. Each call:

    1. Reads the forward cursor's CURRENT position (re-read AFTER the
       forward pass above has run, so it reflects whatever the forward
       pass may have JUST advanced it to this same call) as
       `forward_frontier`. If the forward cursor has never advanced at
       all yet (`forward_frontier is None` — nothing has ever been
       forward-scanned), the sweep has nothing to safety-net yet and
       does zero work this call.
    2. Derives `effective_sweep_start`, purely at read time, from the
       sweep cursor's own persisted position: if no sweep cursor exists
       yet, OR the sweep has already fully caught up to
       `forward_frontier` (`sweep_cursor.last_created_at >=
       forward_frontier` — it completed a full lap), START A NEW LAP at
       `activation_boundary`; otherwise CONTINUE the current lap from
       `sweep_cursor.last_created_at`. This is a derived-at-read-time
       decision only — no separate "reset" WRITE operation exists; the
       persisted sweep position is simply reinterpreted as "start over"
       once it has caught up.
    3. Scans `[effective_sweep_start, forward_frontier]` — the SAME
       `_scan_and_repair_evidence_missing_classification_jobs` helper
       the forward pass uses, with its own, smaller `sweep_repair_limit`
       /`sweep_page_size`/`sweep_max_rows_scanned` budget (the sweep is
       a background safety net, not the primary discovery path) — the
       upper bound (`created_at_to=forward_frontier`) means the sweep
       NEVER races ahead into territory the forward pass has not
       reached yet (redundant: the forward pass will get there on its
       own).

    `sweep_repair_limit=50`/`sweep_page_size=200`/
    `sweep_max_rows_scanned=1000` (vs. the forward pass's own 200/200/
    5000): a documented judgment call, not architect-mandated values —
    generous enough that a normal-sized backlog between the forward
    cursor and the sweep converges in a handful of calls, small enough
    that the sweep's own inherently-redundant re-scanning of
    already-resolved evidence (a cheap `get_by_evidence`/
    `get_current_classification` lookup per row, never a full
    resolution) never dominates a reconciliation call's own cost next
    to the forward pass's primary work.

    What this backstop actually guarantees, and what it costs (honest,
    not overclaimed — this delivery's own established documentation
    discipline)
    ------------------------------------------------------------------
    Guarantees: no post-activation `EvidenceItem` is EVER permanently
    unreachable once its own transaction commits, regardless of any
    commit-order-vs-created_at-order inversion — "eventually, within
    finitely many calls, never permanently", NOT a specific bounded
    latency. Job submission happens DURING each scan pass itself, the
    instant a row is actually inspected — it is never contingent on the
    sweep cursor's own write succeeding afterward, so an orphan within
    reach of a single sweep pass's budget gets its job that same call
    regardless of what happens to cursor bookkeeping next.

    Costs: a bounded amount of extra, mostly-cheap-skip re-scanning
    work per call (the sweep's own budget, on top of the forward pass's
    own); and a discovery LATENCY for a late-committing row that
    depends on how long the sweep takes to complete its current lap —
    which GROWS as the eligible evidence volume grows, since each
    call's sweep budget is fixed.

    The `lap` generation counter — corrected this round; the previous
    "self-resolves" claim below was FALSE and has been retracted
    ------------------------------------------------------------------
    An earlier version of this function's own docstring claimed that a
    newly-started lap whose first, budget-bounded call does not yet
    reach the previous lap's completion point "self-resolves the
    moment the forward frontier advances again even slightly". That
    claim was FALSE — independently confirmed both by an external audit
    and by direct reproduction (see
    `tests/persistence/test_evidence_classification_job_reconciliation_sweep.py`'s
    own bug-reproduction test): under the single-dimension, plain
    `created_at` merge-on-write comparison `advance_cursor` used to
    apply, a newly-started lap's own first write is compared directly
    against the OLD, already-completed lap's much-further-along
    persisted watermark and is rejected as "behind" — and this rejection
    does NOT depend on the forward frontier at all, so it never
    self-resolves; every subsequent call re-proposes the same early,
    already-rejected position, forever. Any evidence in the unswept
    remainder of that window — precisely the case this whole mechanism
    exists to catch — was never found.

    The actual fix: `EvidenceClassificationReconciliationCursor` now
    carries a second, independent ordering dimension, `lap` — a
    monotonically non-decreasing generation counter compared BEFORE
    `created_at` (lexicographic `(lap, created_at)` ordering; see
    `services.evidence.classification_reconciliation_cursor`'s own
    docstring, "lap" section, for the full mechanism and its
    concurrency-safety argument). A genuinely NEWER lap's `(lap,
    created_at)` proposal always supersedes an OLDER lap's persisted
    position entirely — regardless of where the new lap's own first,
    partial scan happens to land — because the two are no longer
    compared on `created_at` at all once `lap` differs. This function
    threads `lap` through both passes below:

    * Forward pass: always proposes `lap = existing_forward_cursor.lap`
      (or `0` if no forward cursor exists yet) — a pure read-and-pass-
      through, never invented or incremented; the forward cursor's own
      `lap` dimension is a permanent non-event (see that module's own
      docstring).
    * Sweep pass: proposes `existing_sweep_cursor.lap + 1` (or `0` if no
      sweep cursor exists yet) exactly when it decides to start a NEW
      lap (the existing "caught up to `forward_frontier`" condition,
      unchanged); proposes `existing_sweep_cursor.lap` unchanged when
      CONTINUING an already-in-progress lap (also unchanged from
      today's `created_at`/`last_evidence_ids` continuation logic).

    This genuinely closes the permanent-stall class of bug: a lap
    transition's own cursor write is no longer contingent on catching
    up to where the previous lap ended before it is allowed to persist
    any progress at all.

    Returns the SUM of jobs submitted by both passes this call (the
    forward pass's own count plus the sweep pass's own count) —
    unchanged external contract: still a single `int`, "number of jobs
    submitted this call". 0 in the common case — most evidence already
    has a job via the immediate-enqueue path; this function exists for
    the rare gap, not as the normal path.
    """
    forward_cursor_key = activation_boundary.isoformat()
    existing_forward_cursor = cursor_repository.get_cursor(forward_cursor_key)
    forward_scan_from = (
        existing_forward_cursor.last_created_at if existing_forward_cursor is not None else activation_boundary
    )
    forward_already_inspected: set[str] = (
        set(existing_forward_cursor.last_evidence_ids) if existing_forward_cursor is not None else set()
    )
    # The forward cursor's own `lap` dimension is a permanent non-event
    # — always read-and-passed-through unchanged, never invented or
    # incremented here (see docstring's own "lap" section above).
    forward_lap = existing_forward_cursor.lap if existing_forward_cursor is not None else 0

    forward_submitted = _scan_and_repair_evidence_missing_classification_jobs(
        evidence_repository=evidence_repository,
        classification_job_repository=classification_job_repository,
        classification_repository=classification_repository,
        cursor_repository=cursor_repository,
        actor_type=actor_type,
        actor_id=actor_id,
        cursor_key=forward_cursor_key,
        scan_created_at_from=forward_scan_from,
        created_at_to=None,
        already_inspected_ids_at_boundary=forward_already_inspected,
        has_existing_cursor=existing_forward_cursor is not None,
        repair_limit=repair_limit,
        page_size=page_size,
        max_rows_scanned=max_rows_scanned,
        lap=forward_lap,
    )

    # --- The "safety sweep" backstop pass (see docstring above) ---
    sweep_submitted = 0
    # Re-read fresh: the forward pass above may have JUST advanced this
    # same call, so the sweep's own upper bound must reflect the MOST
    # current forward position, never a stale pre-call snapshot.
    forward_frontier_cursor = cursor_repository.get_cursor(forward_cursor_key)
    forward_frontier = forward_frontier_cursor.last_created_at if forward_frontier_cursor is not None else None

    if forward_frontier is not None:
        sweep_cursor_key = f"{forward_cursor_key}::sweep"
        existing_sweep_cursor = cursor_repository.get_cursor(sweep_cursor_key)
        if existing_sweep_cursor is None or existing_sweep_cursor.last_created_at >= forward_frontier:
            # No sweep cursor yet, or it already completed a full lap
            # (caught up to forward_frontier) — start a NEW lap. Purely
            # derived at read time; no "reset" write is ever performed
            # (see docstring). A NEW lap always proposes a STRICTLY
            # HIGHER `lap` than whatever is currently persisted (see
            # docstring's own "lap" section) — this is what lets this
            # call's write land even though its own bounded scan cannot
            # yet reach the OLD lap's much-further-along position.
            sweep_scan_from = activation_boundary
            sweep_already_inspected: set[str] = set()
            sweep_has_existing_cursor = False
            sweep_lap = (existing_sweep_cursor.lap + 1) if existing_sweep_cursor is not None else 0
        else:
            sweep_scan_from = existing_sweep_cursor.last_created_at
            sweep_already_inspected = set(existing_sweep_cursor.last_evidence_ids)
            sweep_has_existing_cursor = True
            # Continuing an in-progress lap — propose the SAME `lap`
            # value unchanged (falls into _resolve_cursor_advance's own
            # "proposed.lap == existing.lap" branch, the ordinary,
            # already-safe created_at/ids merge).
            sweep_lap = existing_sweep_cursor.lap

        sweep_submitted = _scan_and_repair_evidence_missing_classification_jobs(
            evidence_repository=evidence_repository,
            classification_job_repository=classification_job_repository,
            classification_repository=classification_repository,
            cursor_repository=cursor_repository,
            actor_type=actor_type,
            actor_id=actor_id,
            cursor_key=sweep_cursor_key,
            scan_created_at_from=sweep_scan_from,
            created_at_to=forward_frontier,
            already_inspected_ids_at_boundary=sweep_already_inspected,
            has_existing_cursor=sweep_has_existing_cursor,
            repair_limit=sweep_repair_limit,
            page_size=sweep_page_size,
            max_rows_scanned=sweep_max_rows_scanned,
            lap=sweep_lap,
        )

    return forward_submitted + sweep_submitted
