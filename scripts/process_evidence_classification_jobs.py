#!/usr/bin/env python3
"""scripts/process_evidence_classification_jobs.py — the bounded,
operator/cron-invoked worker for `EvidenceClassificationJob`
(BAGMAN accounting platform, `evidence/automatic-classification-activation`
WO).

BAGMAN's evidence-classification subsystem
(`services.evidence.classification_orchestrator.classify_evidence` —
deterministic-rule-first, AI-fallback, human review via
`CLASSIFICATION_REVIEW` Needs You items) has always been fully
governed but had NO automatic trigger. This delivery's other half
(`services/evidence/classification_job.py`'s
`enqueue_classification_job_for_evidence`, wired into both real
evidence-creation call sites — `services/mailbox/microsoft/evidence_ingest.py`
and `app/api/routers/intake.py`) enqueues a durable
`EvidenceClassificationJob` the moment new evidence is created. THIS
script is what claims and actually processes those jobs — the one,
bounded, explicit mechanism for doing so.

Mirrors `scripts/process_background_job_overflow.py`'s own structure
closely (that module's own docstring states plainly: *"There is no
daemon, no scheduler, and no automatic threshold anywhere in this
file — running it is how an operator manually starts or continues
backlog processing."* — identically true here): argument parsing,
`get_composition()` (local import, keeps this module importable/
testable with zero env vars set), a bounded claim-and-process loop,
per-job try/except, a real JSON summary report. Genuinely different in
one respect: there is no `--submit` mode here at all (unlike
`process_background_job_overflow.py`'s own two-mode CLI) — jobs are
submitted automatically, by `enqueue_classification_job_for_evidence`,
never manually via this script; `--process --limit N` is this script's
only mode.

Per-job outcome doctrine — corrected 2026-09-29 (architect review, WO
item 3)
------------------------------------------------------------------------
A claimed job's SOLE responsibility is "run `classify_evidence` once
for this `evidence_id`", never "guarantee a definitive classification
resulted" — `classify_evidence` already has its own complete internal
failure semantics (WI-3's own governed outcome vocabulary,
`CLASSIFY_EVIDENCE_OUTCOMES`) and NEVER raises for an ordinary
AI/context/deterministic-conflict/current-classification-exists
condition. An earlier version of this worker treated **any**
`ClassifyEvidenceResult` `classify_evidence` returned WITHOUT RAISING —
including `AI_IN_PROGRESS`/`AI_INVOCATION_FAILED`/`AI_PRIOR_FAILURE` —
as an undifferentiated job `SUCCEEDED`, with no durable record of which
outcome actually happened. The architect's review of this WO REQUIRED
that be replaced with an HONEST, EXPLICIT mapping from each of the 10
real `CLASSIFY_EVIDENCE_OUTCOMES` values to job status +
`EvidenceClassificationJob.classification_outcome` (see
`services.evidence.classification_job`'s own module docstring) — see
`_COMPLETE_OUTCOMES`/`_COMPLETE_BUT_UNRESOLVED_OUTCOMES`/
`_AI_FAILURE_OUTCOMES`/`_RETRY_DEFER_OUTCOMES` below and `_process_one`'s
own outcome-dispatch for the full, exhaustive mapping — never a coarse
"it didn't raise" bucket again. `classification_outcome` is durably
recorded on the job row for EVERY one of these four categories, so an
operator can always distinguish a genuinely-classified success from a
governed-but-unresolved or AI-failed one, even though the job's own
`status` is `SUCCEEDED` for two of the four categories (see those
constants' own docstrings for why).

Only a genuinely RAISED exception is a job failure — an actual
infrastructure fault (object store down, database down, an unexpected
bug), never an ordinary classification outcome. Retryable-vs-terminal
classification of a raised exception (this script's own judgment call,
mirroring `process_background_job_overflow.py`'s own identically-named
docstring section and identical reasoning):

* `core.errors.NotFoundError` on the evidence lookup (evidence row
  genuinely gone) — TERMINAL. Structurally near-impossible given the
  real `evidence_id` foreign key on `evidence_classification_jobs`
  (a job can only ever be created for a real, already-committed
  `EvidenceItem`, and `EvidenceItem` rows are never deleted — CD-2's
  own immutability doctrine), but handled defensively rather than
  assumed impossible; retrying an evidence lookup that will never
  resolve is pointless.
* `core.errors.PersistenceError` (a database/session-layer failure
  surfaced by any of the several repositories `classify_evidence`
  touches) — RETRYABLE. Textbook transient infrastructure.
* `TimeoutError`/`ConnectionError`/`OSError` (the object store, or any
  other network-shaped dependency, unreachable/slow) — RETRYABLE. Same
  reasoning as `process_background_job_overflow.py`'s own
  `LITELLM_TRANSPORT_ERROR`/`LITELLM_TIMEOUT` classification (a
  transient condition, safe to attempt again) — note this is a
  DIFFERENT failure surface from an AI provider error, which
  `classify_evidence` already absorbs internally as an ordinary,
  non-raising `OUTCOME_AI_INVOCATION_FAILED` (see above); this branch
  only ever fires for something classify_evidence's own call to
  `run_background_task` did NOT itself catch.
* Anything else (a genuine, unexpected bug — a `TypeError`/
  `AttributeError`/`KeyError` this worker did not anticipate) —
  RETRYABLE, on the same "fail open toward eventual recovery, let the
  bounded `max_attempts` budget be the real backstop" reasoning
  `process_background_job_overflow.py` does NOT itself apply
  uniformly (it deliberately does distinguish a schema-validation
  failure as non-retryable) — but this worker has no equivalent
  "retrying will deterministically reproduce the exact same failure"
  signal available to it the way a `LiteLLMOutcomeStatus` error code
  gives that script; an unrecognised exception here is treated as
  possibly-transient rather than guessed to be permanent. A future
  revision that discovers a specific, deterministically-non-retryable
  exception shape reachable from this call path should name it
  explicitly, mirroring `process_background_job_overflow.py`'s own
  named, itemised exceptions — not invent a second implicit default.

Reconciliation is wired in BEFORE claiming (architect requirement, WO
item 1)
------------------------------------------------------------------------
`main()` (never `run_process()` — see that decision's own note at the
call site below) calls
`services.evidence.classification_job.reconcile_missing_classification_jobs`
exactly once, BEFORE `classification_job_repository.claim_next_pending`,
matching the architect's required flow: normal evidence creation ->
best-effort immediate enqueue -> worker invocation -> bounded
prospective reconciliation -> claim/process jobs. Reconciliation runs
with its own distinct `actor_id`
(`bagman-evidence-classification-reconciliation`, vs. this worker's own
`bagman-evidence-classification-worker`) so a job's `actor_id` alone
tells an operator whether it was submitted by the immediate-enqueue
path or recovered by reconciliation — `EvidenceClassificationJob
.actor_id` is a plain persisted column, queryable like any other, so
this distinction costs nothing and is worth keeping (a documented,
non-blocking judgment call — no dedicated reporting surface for it
exists yet). The reconciled count is folded into this script's own
summary report as `reconciled_count`, alongside `claimed_count`.
`run_process()` itself is deliberately NOT changed to call
reconciliation internally — it is exercised directly, without
reconciliation, by `tests/integration/test_evidence_classification_job_worker.py
::test_run_process_never_creates_jobs_itself` (a pre-existing, still-
authoritative proof that `--process` alone never conjures a job into
existence for un-enqueued evidence); wiring reconciliation into
`run_process()` itself would make that specific, deliberate guarantee
false. `main()` is the correct seam: it is the one, real, operator-
invoked entrypoint that always wants both steps, and it is what
production imports/runs. (Nothing prevents a future revision from
splitting this into two explicit CLI flags — not attempted here, the
WO's own spec asks for exactly one wired-in call.)

The activation boundary is operator-set Layer-2 configuration, and
reconciliation fails CLOSED, never silently, when it is unavailable
(corrected 2026-09-29, architect review, WO item 1)
------------------------------------------------------------------------
`reconcile_missing_classification_jobs` now takes `activation_boundary`
as a required parameter (see
`services.evidence.classification_job`'s own module docstring — an
earlier version of that module hardcoded a hidden
`AUTOMATIC_CLASSIFICATION_ACTIVATION_BOUNDARY` constant instead, which
was wrong given this feature had not yet been deployed to production).
THIS script is the one real caller that supplies it, reading the
`BAGMAN_EVIDENCE_CLASSIFICATION_ACTIVATION_BOUNDARY` environment
variable — BAGMAN's own documented Layer 2 "runtime environment
configuration" (`config/README.md`, which lists "feature flags" as a
canonical Layer 2 example) — as an ISO-8601 UTC string (e.g.
`2026-10-15T00:00:00Z`), parsed with the exact same
`datetime.fromisoformat(value.replace("Z", "+00:00"))` pattern already
established at `services/xero/client.py`'s own `_parse_xero_wire_datetime`,
wrapped so a malformed value fails loudly and specifically (see
`_resolve_activation_boundary` below) rather than with a bare
traceback.

Deliberately env-var-only, NEVER a CLI flag: per the architect's own
explicit "set during the later production-activation WO" instruction,
this value is an OPERATOR-set deployment/environment concern, never a
per-invocation argument that could accidentally be passed differently
on different runs.

If the variable is unset, or set to something that does not parse,
reconciliation is SKIPPED ENTIRELY for that run — fail closed, never a
silent default to "now" or to any other value. This is reported
explicitly in the run's own summary JSON as
`reconciliation_skipped_reason` (a human-readable string; `None` when
reconciliation actually ran). Critically, a skipped reconciliation NEVER
blocks the worker's OTHER responsibility — claiming and processing
already-existing `PENDING`/`FAILED_RETRYABLE`/`DEFERRED`/reclaimed jobs
proceeds completely normally regardless of whether reconciliation ran.
`main()` delegates this whole "resolve boundary, maybe reconcile, then
always process" sequence to `_run_worker` (below) — a small,
dependency-injected extraction (`composition` passed in explicitly,
rather than fetched via `get_composition()` internally) added
specifically so this dispatch logic is directly unit-testable without
real env vars or a real database (this codebase has no pre-existing
"inject a fake composition into main()" test pattern for any script in
this directory, so this is a documented, narrow, test-motivated
addition — `main()` itself remains the one, thin, real entrypoint that
calls `get_composition()` and does file/stdout I/O).

Reconciliation's own bound is a separate CLI concern from the worker's
claim count
------------------------------------------------------------------------
`--limit` continues to govern only `claim_next_pending`'s own claim
count (unrelated to reconciliation). A new, distinct
`--reconciliation-repair-limit` flag (default 200, matching
`reconcile_missing_classification_jobs`'s own default `repair_limit`)
governs how many missing jobs a single reconciliation pass may create —
deliberately NOT conflated with `--limit`, since the two bound
genuinely different things (jobs claimed-and-processed vs. jobs
newly-created-by-repair) that an operator may reasonably want to tune
independently.

Usage
-----
    python3 scripts/process_evidence_classification_jobs.py \\
        --runtime-dir /opt/bagman/runtime/evidence-classification-jobs \\
        --process --limit 10 --reconciliation-repair-limit 200 \\
        --worker-id operator-manual-run-1

`--runtime-dir` may also be supplied via
`BAGMAN_EVIDENCE_CLASSIFICATION_JOB_RUNTIME_DIR`. This process expects
to run with `BAGMAN_RUNTIME_ENV`/DB/object-store env vars already set
(typically `docker exec bagman-api python3
scripts/process_evidence_classification_jobs.py ...`), via
`app.api.composition.get_composition()` — exactly like every other
operator script in this directory. Never installed as a live periodic
job (cron/systemd timer) by this delivery — that is a separate, future
production-activation decision (WO non-goal).

Reconciliation only actually runs once
`BAGMAN_EVIDENCE_CLASSIFICATION_ACTIVATION_BOUNDARY` is set to a real
value (see "The activation boundary is operator-set Layer-2
configuration" section above) — deliberately NOT set by this delivery;
that is a separate, later, production-activation decision. Until then,
every run's own summary JSON reports `reconciliation_skipped_reason`
explaining why, and ordinary claim/process work is entirely unaffected.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import uuid
from datetime import datetime
from pathlib import Path
from typing import Iterable, Optional

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from core import identity  # noqa: E402
from core.errors import NotFoundError, PersistenceError  # noqa: E402
from core.timestamps import to_contract_string, utc_now  # noqa: E402
from services.evidence.classification_job import (  # noqa: E402
    EvidenceClassificationJob,
    reconcile_missing_classification_jobs,
)
from services.evidence.classification_orchestrator import (  # noqa: E402
    OUTCOME_AI_IN_PROGRESS,
    OUTCOME_AI_INVOCATION_FAILED,
    OUTCOME_AI_PRIOR_FAILURE,
    OUTCOME_AI_PROPOSAL_REVIEW_REQUIRED,
    OUTCOME_AI_PROPOSAL_UNCLASSIFIABLE,
    OUTCOME_CONTEXT_UNSUPPORTED,
    OUTCOME_CURRENT_CLASSIFICATION_EXISTS,
    OUTCOME_DETERMINISTIC_CLASSIFIED,
    OUTCOME_DETERMINISTIC_CONFLICT,
    OUTCOME_DETERMINISTIC_EXISTING,
    classify_evidence,
)

#: Bounded ceiling for `--limit` (never "unlimited") — mirrors
#: `process_background_job_overflow.py`'s own identical bound.
_MAX_PROCESS_LIMIT = 200

_ACTOR_TYPE = "SYSTEM"
_WORKER_ACTOR_ID = "bagman-evidence-classification-worker"
#: Distinct from `_WORKER_ACTOR_ID` (see module docstring's
#: "Reconciliation is wired in BEFORE claiming" section) — lets a job's
#: own `actor_id` tell an operator whether it was submitted by the
#: immediate-enqueue path or recovered by the reconciliation pass.
_RECONCILIATION_ACTOR_ID = "bagman-evidence-classification-reconciliation"

#: Layer-2 runtime environment configuration (see module docstring's
#: "The activation boundary is operator-set Layer-2 configuration"
#: section) — an ISO-8601 UTC string, e.g. `2026-10-15T00:00:00Z`.
#: Deliberately env-var-only, never a CLI flag (see that section).
_ACTIVATION_BOUNDARY_ENV_VAR = "BAGMAN_EVIDENCE_CLASSIFICATION_ACTIVATION_BOUNDARY"

#: Default `--reconciliation-repair-limit`, matching
#: `services.evidence.classification_job.reconcile_missing_classification_jobs`'s
#: own default `repair_limit`.
_DEFAULT_RECONCILIATION_REPAIR_LIMIT = 200


def _resolve_activation_boundary(raw: Optional[str]) -> tuple[Optional[datetime], Optional[str]]:
    """Parse `_ACTIVATION_BOUNDARY_ENV_VAR`'s raw value (or `None`, if
    unset) into `(activation_boundary, skip_reason)` — exactly one of
    the pair is ever non-`None`. A pure function, deliberately taking
    the raw string rather than reading `os.environ` itself, so it is
    directly unit-testable with no environment mutation required (see
    module docstring's "The activation boundary is operator-set Layer-2
    configuration" section).

    Mirrors `services/xero/client.py`'s own `_parse_xero_wire_datetime`
    parsing pattern exactly (`datetime.fromisoformat(value.replace("Z",
    "+00:00"))`), but — unlike that helper, which silently returns
    `None` for an unparseable value because a single cosmetic Xero
    field is not worth failing a whole sync over — a malformed
    activation boundary here fails LOUDLY and SPECIFICALLY: the
    returned `skip_reason` names the exact env var, the exact raw value
    received, and the exact parse error, so an operator reading a run's
    own summary JSON never has to guess why reconciliation did not run.

    Never raises. Fails CLOSED: `raw` missing/empty, or present but not
    valid ISO-8601, both return `(None, <reason>)` — never a silent
    default to "now" or to any other value.
    """
    if not raw:
        return None, (
            f"{_ACTIVATION_BOUNDARY_ENV_VAR} is not set — reconciliation skipped for this run "
            "(fail closed; set it to a real ISO-8601 UTC value, e.g. 2026-10-15T00:00:00Z, at the "
            "production-activation WO)"
        )
    try:
        parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError as exc:
        return None, (
            f"{_ACTIVATION_BOUNDARY_ENV_VAR}={raw!r} could not be parsed as ISO-8601 ({exc}) — "
            "reconciliation skipped for this run (fail closed)"
        )
    # Independent-audit finding (third re-audit round): `fromisoformat`
    # happily accepts a value with no timezone/offset at all (e.g.
    # "2026-10-15T00:00:00", no trailing "Z") and silently returns a
    # NAIVE datetime — comparing that against `received_at`'s own real,
    # timezone-AWARE column would not raise, but would be resolved
    # ambiguously (session/server-local time), a genuine silent-
    # correctness risk directly inside the one guarantee this function
    # exists to provide ("fail loudly on malformed input, never guess").
    # A naive value is therefore treated as malformed too — fail closed,
    # exactly like any other parse failure, never silently accepted.
    if parsed.tzinfo is None:
        return None, (
            f"{_ACTIVATION_BOUNDARY_ENV_VAR}={raw!r} has no timezone/offset (e.g. no trailing 'Z') — "
            "an activation boundary must be explicit and unambiguous; reconciliation skipped for this "
            "run (fail closed)"
        )
    return parsed, None

#: See module docstring's "Per-job outcome doctrine" section for the
#: full reasoning behind this classification.
_RETRYABLE_EXCEPTION_TYPES = (PersistenceError, TimeoutError, ConnectionError, OSError)

#: A genuinely, completely classified/governed-terminal outcome — the
#: evidence item is DONE, no further automatic action is useful. Job
#: status `SUCCEEDED`, `classification_outcome` records exactly which
#: of these it was.
_COMPLETE_OUTCOMES = frozenset({
    OUTCOME_CURRENT_CLASSIFICATION_EXISTS,
    OUTCOME_DETERMINISTIC_CLASSIFIED,
    OUTCOME_DETERMINISTIC_EXISTING,
    OUTCOME_AI_PROPOSAL_REVIEW_REQUIRED,
    OUTCOME_AI_PROPOSAL_UNCLASSIFIABLE,
})
#: A governed, non-retryable terminal condition was reached — genuinely
#: classified? NO. But no automatic retry is useful either (a
#: deterministic-rule conflict needs a human to fix the conflicting
#: rules; unsupported context needs different evidence content — never
#: something a retry produces). Job status is still SUCCEEDED (its
#: narrow responsibility — "ran classify_evidence once" — is complete),
#: but `classification_outcome` durably records that no real
#: classification resulted, so operators can distinguish this from
#: genuine success (architect requirement, WO item 3).
_COMPLETE_BUT_UNRESOLVED_OUTCOMES = frozenset({
    OUTCOME_DETERMINISTIC_CONFLICT,
    OUTCOME_CONTEXT_UNSUPPORTED,
})
#: classify_evidence's own same-fingerprint prior-failure guard means a
#: job-level retry of either of these would NEVER make real progress —
#: a second `classify_evidence` call for the identical evidence+context
#: either finds the same FAILED AIInvocation again (AI_PRIOR_FAILURE,
#: no new model call attempted at all) or, for a fresh
#: AI_INVOCATION_FAILED, immediately becomes exactly that
#: AI_PRIOR_FAILURE state on any subsequent call. Retrying at the job
#: level would therefore only ever loop into AI_PRIOR_FAILURE, never
#: recover — architect's own explicit "do not paper over that"
#: instruction. Both go straight to FAILED_TERMINAL, durably recording
#: which one via `classification_outcome`, so this evidence item is
#: visible in the existing operational review/recovery model.
#:
#: CORRECTED 2026-09-29 (architect review, WO item 4 — Option A): an
#: earlier version of this comment claimed an operator could recover
#: one of these "by hand via the existing HTTP endpoints once/if the
#: underlying condition is addressed" — that is FALSE and is retracted
#: here. `compute_classifier_fingerprint`
#: (`services.evidence.classification_ai_fingerprint`) is a pure
#: function of evidence content + classification-context + task/prompt
#: version — never of time or caller identity — so a manual HTTP call
#: to any of the three existing classification endpoints, for the SAME
#: evidence_id with an UNCHANGED classification context, computes the
#: IDENTICAL fingerprint and, per classify_evidence's own §27
#: same-fingerprint guard (the `matching_fingerprint`/`prior_failed`
#: branch in `services.evidence.classification_orchestrator`),
#: immediately returns AI_PRIOR_FAILURE again WITHOUT ever attempting a
#: new model call — normal retry, automatic OR manual, genuinely cannot
#: recover this state. Real recovery requires either the classification
#: context itself changing (e.g. a prompt/task version bump, which
#: changes the fingerprint for every future call) or a
#: separately-authorised future reset/retry mechanism that does not
#: exist yet and is explicitly OUT OF SCOPE for this delivery — this is
#: a documentation-only correction, no reset mechanism is built here
#: (architect's own explicit "Option A... this is acceptable for this
#: WO if operational visibility is sufficient" ruling: the job's own
#: durable `classification_outcome` + `FAILED_TERMINAL` status IS that
#: operational visibility — an operator reviewing failed jobs can SEE
#: this state precisely; they just cannot currently self-serve a fix
#: for it through this delivery's own tooling).
_AI_FAILURE_OUTCOMES = frozenset({
    OUTCOME_AI_INVOCATION_FAILED,
    OUTCOME_AI_PRIOR_FAILURE,
})
#: Another invocation for this evidence_id is genuinely still running
#: (a concurrent manual HTTP call, or another worker/reconciliation
#: race) — nothing failed, nothing completed. Must NOT disappear into
#: terminal SUCCEEDED, and must NEVER consume real attempt/failure
#: budget either (corrected 2026-09-29, architect review, WO item 3 —
#: an earlier version of this worker mapped this outcome to
#: FAILED_RETRYABLE, which DOES consume an attempt_count slot at
#: mark_in_progress time even though nothing about this outcome is a
#: genuine failed attempt; repeated polling of a slow-but-healthy AI
#: invocation could therefore exhaust max_attempts and wrongly
#: terminate the job). Mapped instead to the new `DEFERRED` status
#: (`services.evidence.classification_job`'s own module docstring,
#: "DEFERRED" section) via `mark_deferred`, which decrements
#: attempt_count by exactly 1 — undoing mark_in_progress's own
#: increment for this specific claim — so the net effect of a deferred
#: check is a wash: the job is reclaimable exactly like
#: PENDING/FAILED_RETRYABLE, and a LATER worker run naturally re-checks
#: once the other invocation finishes, with attempt_count never
#: net-increasing beyond what a single genuine attempt would show.
_RETRY_DEFER_OUTCOMES = frozenset({OUTCOME_AI_IN_PROGRESS})


def _process_one(
    job: EvidenceClassificationJob,
    *,
    classification_job_repository,
    evidence_repository,
    rule_repository,
    classification_repository,
    ai_invocation_repository,
    litellm_client,
    object_store,
    audit_repository,
    record_audit_event,
    needs_you_repository,
) -> dict:
    """Claim-to-terminal processing of exactly one job. Never raises —
    every outcome (including a genuine infrastructure exception) is
    caught here and translated into a `mark_succeeded`/`mark_failed`
    call plus a plain-dict report record (see module docstring's own
    "Per-job outcome doctrine")."""
    classification_job_repository.mark_in_progress(job.job_id)

    try:
        # Step 2 — prove the evidence genuinely exists before handing
        # it to classify_evidence (which would itself raise NotFoundError
        # anyway, but this makes the "evidence row is genuinely gone"
        # case explicit and separately reported — see module docstring).
        evidence_repository.get_evidence(job.evidence_id)

        result = classify_evidence(
            evidence_id=job.evidence_id,
            persist=True,
            evidence_repository=evidence_repository,
            rule_repository=rule_repository,
            classification_repository=classification_repository,
            ai_invocation_repository=ai_invocation_repository,
            litellm_client=litellm_client,
            object_store=object_store,
            audit_repository=audit_repository,
            record_audit_event=record_audit_event,
            needs_you_repository=needs_you_repository,
            actor_type=_ACTOR_TYPE,
            actor_id=_WORKER_ACTOR_ID,
            correlation_id=job.correlation_id,
        )
    except NotFoundError as exc:
        # Structurally near-impossible given the real evidence_id FK on
        # evidence_classification_jobs (see module docstring) — handled
        # defensively, never assumed impossible. A raised exception
        # never reaches classify_evidence's own outcome vocabulary at
        # all, so no `classification_outcome` is recorded (see
        # `mark_failed`'s own docstring).
        updated = classification_job_repository.mark_failed(
            job.job_id, error=f"NotFoundError: {exc}", retryable=False
        )
        return {"job_id": job.job_id, "evidence_id": job.evidence_id, "outcome": "EVIDENCE_NOT_FOUND", "job_status": updated.status, "classification_outcome": updated.classification_outcome}
    except _RETRYABLE_EXCEPTION_TYPES as exc:
        updated = classification_job_repository.mark_failed(
            job.job_id, error=f"{type(exc).__name__}: {exc}", retryable=True
        )
        return {"job_id": job.job_id, "evidence_id": job.evidence_id, "outcome": "RETRYABLE_FAILURE", "job_status": updated.status, "classification_outcome": updated.classification_outcome, "error": str(exc)}
    except Exception as exc:  # noqa: BLE001 - see module docstring's "Per-job outcome doctrine" (unrecognised -> retryable)
        updated = classification_job_repository.mark_failed(
            job.job_id, error=f"{type(exc).__name__}: {exc}", retryable=True
        )
        return {"job_id": job.job_id, "evidence_id": job.evidence_id, "outcome": "RETRYABLE_FAILURE", "job_status": updated.status, "classification_outcome": updated.classification_outcome, "error": str(exc)}

    # classify_evidence RETURNED without raising — map its own outcome
    # honestly onto job status + classification_outcome (see module
    # docstring's "Per-job outcome doctrine", corrected 2026-09-29;
    # architect requirement WO item 3). Never a coarse "it didn't raise
    # -> SUCCEEDED" bucket again.
    if result.outcome in _COMPLETE_OUTCOMES or result.outcome in _COMPLETE_BUT_UNRESOLVED_OUTCOMES:
        updated = classification_job_repository.mark_succeeded(job.job_id, classification_outcome=result.outcome)
        report_outcome = "SUCCEEDED"
    elif result.outcome in _AI_FAILURE_OUTCOMES:
        # Always FAILED_TERMINAL regardless of attempt_count — the
        # same-fingerprint prior-failure guard means a job-level retry
        # can never make progress (see _AI_FAILURE_OUTCOMES's own
        # docstring); `retryable=False` already forces this outcome
        # via `mark_failed`'s own budget logic.
        updated = classification_job_repository.mark_failed(
            job.job_id,
            error=f"classify_evidence returned {result.outcome} (error_code={result.error_code})",
            retryable=False,
            classification_outcome=result.outcome,
        )
        report_outcome = "TERMINAL_FAILURE"
    elif result.outcome in _RETRY_DEFER_OUTCOMES:
        # mark_deferred, never mark_failed — see _RETRY_DEFER_OUTCOMES's
        # own docstring (architect requirement, WO item 3): this is not
        # a failed attempt, and must not consume attempt/failure
        # budget. mark_deferred itself performs the compensating
        # attempt_count decrement.
        updated = classification_job_repository.mark_deferred(
            job.job_id, classification_outcome=result.outcome,
        )
        report_outcome = "DEFERRED"
    else:
        # Structurally impossible given CLASSIFY_EVIDENCE_OUTCOMES is a
        # closed, exhaustive set fully partitioned across the four sets
        # above — raise loudly rather than silently defaulting, so a
        # future new outcome value added to classify_evidence can never
        # silently fall through unclassified (see module docstring).
        raise AssertionError(
            f"classify_evidence returned outcome '{result.outcome}', which is not covered by any of "
            "_COMPLETE_OUTCOMES/_COMPLETE_BUT_UNRESOLVED_OUTCOMES/_AI_FAILURE_OUTCOMES/_RETRY_DEFER_OUTCOMES "
            "— this worker's outcome mapping must be extended to cover it before it can ever be marked "
            "succeeded or failed"
        )

    return {
        "job_id": job.job_id,
        "evidence_id": job.evidence_id,
        "outcome": report_outcome,
        "job_status": updated.status,
        "classify_evidence_outcome": result.outcome,
        "classification_outcome": updated.classification_outcome,
        "deterministic_outcome": result.deterministic_outcome,
        "ai_invocation_id": result.ai_invocation_id,
    }


def run_process(
    *,
    limit: int,
    claimed_by: str,
    classification_job_repository,
    evidence_repository,
    rule_repository,
    classification_repository,
    ai_invocation_repository,
    litellm_client,
    object_store,
    audit_repository,
    record_audit_event,
    needs_you_repository,
) -> dict:
    claimed = classification_job_repository.claim_next_pending(limit=limit, claimed_by=claimed_by)
    records = [
        _process_one(
            job,
            classification_job_repository=classification_job_repository,
            evidence_repository=evidence_repository,
            rule_repository=rule_repository,
            classification_repository=classification_repository,
            ai_invocation_repository=ai_invocation_repository,
            litellm_client=litellm_client,
            object_store=object_store,
            audit_repository=audit_repository,
            record_audit_event=record_audit_event,
            needs_you_repository=needs_you_repository,
        )
        for job in claimed
    ]
    per_outcome_counts: dict[str, int] = {}
    for record in records:
        per_outcome_counts[record["outcome"]] = per_outcome_counts.get(record["outcome"], 0) + 1

    return {
        "mode": "PROCESS",
        "claimed_by": claimed_by,
        "claimed_count": len(claimed),
        "records": records,
        "per_outcome_counts": per_outcome_counts,
    }


# ---------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------


def _parse_args(argv: Optional[Iterable[str]]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)

    parser.add_argument(
        "--runtime-dir",
        default=None,
        help=(
            "directory to hold this run's own JSON report (required here, or via "
            "BAGMAN_EVIDENCE_CLASSIFICATION_JOB_RUNTIME_DIR — never hardcoded by this tool)"
        ),
    )
    parser.add_argument("--process", action="store_true", required=True, help="claim and execute pending/retryable jobs right now (the only mode)")
    parser.add_argument("--limit", type=int, required=True, help=f"bounded claim count (1-{_MAX_PROCESS_LIMIT})")
    parser.add_argument(
        "--reconciliation-repair-limit",
        type=int,
        default=_DEFAULT_RECONCILIATION_REPAIR_LIMIT,
        help=(
            "bounded max number of missing jobs a single reconciliation pass may create this run "
            f"(default {_DEFAULT_RECONCILIATION_REPAIR_LIMIT}) — a separate concern from --limit, "
            "which governs claim_next_pending's own claim count; has no effect at all if "
            f"{_ACTIVATION_BOUNDARY_ENV_VAR} is unset/malformed (reconciliation is skipped entirely — "
            "see module docstring)"
        ),
    )
    parser.add_argument("--worker-id", default=None, help="worker identity stamped on claimed jobs; defaults to a generated id")

    args = parser.parse_args(list(argv) if argv is not None else None)

    if not (1 <= args.limit <= _MAX_PROCESS_LIMIT):
        parser.error(f"--limit must be between 1 and {_MAX_PROCESS_LIMIT} (got {args.limit})")
    if args.reconciliation_repair_limit < 1:
        parser.error(f"--reconciliation-repair-limit must be >= 1 (got {args.reconciliation_repair_limit})")

    runtime_dir = args.runtime_dir or os.environ.get("BAGMAN_EVIDENCE_CLASSIFICATION_JOB_RUNTIME_DIR")
    if not runtime_dir:
        parser.error(
            "--runtime-dir is required (or set BAGMAN_EVIDENCE_CLASSIFICATION_JOB_RUNTIME_DIR) — no "
            "default path is ever assumed"
        )
    args.runtime_dir = runtime_dir

    return args


def _run_worker(
    *,
    args: argparse.Namespace,
    composition,
    run_id: str,
    started_at,
    worker_id: str,
    env: Optional[dict] = None,
) -> dict:
    """Everything `main()` does once it has a `composition` and parsed
    `args`: resolve the activation boundary, reconcile-or-skip, always
    process, and assemble the run's own summary report. Extracted as a
    small, dependency-injected function (`composition` passed in
    explicitly, `env` defaulting to `os.environ`) specifically so this
    dispatch logic — most importantly, "an unset/malformed activation
    boundary skips reconciliation but never blocks ordinary claim/
    process work" — is directly unit-testable with a fake/in-memory
    `composition` and no real env vars or database (see module
    docstring's "The activation boundary is operator-set Layer-2
    configuration" section for why this extraction exists; this
    codebase has no pre-existing "inject a fake composition into
    main()" pattern for any script in this directory, so this is a
    documented, narrow, test-motivated addition). `main()` itself stays
    the one, thin, real entrypoint that calls `get_composition()` and
    performs file/stdout I/O.

    Returns `{"report": <dict>, "exit_code": <int>}`.
    """
    env = env if env is not None else os.environ
    activation_boundary, skip_reason = _resolve_activation_boundary(env.get(_ACTIVATION_BOUNDARY_ENV_VAR))

    # Bounded, prospective reconciliation FIRST — see module docstring's
    # "Reconciliation is wired in BEFORE claiming" section. Deliberately
    # called here, never inside run_process() itself (that would
    # falsify test_run_process_never_creates_jobs_itself's own, still-
    # authoritative "--process alone never conjures a job into
    # existence" guarantee — see that section for the full reasoning).
    # A skipped reconciliation (activation_boundary is None) NEVER
    # blocks the ordinary claim/process work below — see module
    # docstring's own "fails CLOSED, never silently" section.
    if activation_boundary is not None:
        reconciled_count = reconcile_missing_classification_jobs(
            evidence_repository=composition.api.evidence_repository,
            classification_job_repository=composition.classification_job_repository,
            classification_repository=composition.classification_repository,
            actor_type=_ACTOR_TYPE,
            actor_id=_RECONCILIATION_ACTOR_ID,
            activation_boundary=activation_boundary,
            repair_limit=args.reconciliation_repair_limit,
        )
        reconciliation_skipped_reason = None
    else:
        reconciled_count = 0
        reconciliation_skipped_reason = skip_reason

    result = run_process(
        limit=args.limit,
        claimed_by=worker_id,
        classification_job_repository=composition.classification_job_repository,
        evidence_repository=composition.api.evidence_repository,
        rule_repository=composition.classification_rule_repository,
        classification_repository=composition.classification_repository,
        ai_invocation_repository=composition.ai_invocation_repository,
        litellm_client=composition.litellm_client,
        object_store=composition.object_store,
        audit_repository=composition.api.audit_repository,
        record_audit_event=composition.api.record_audit_event,
        needs_you_repository=composition.needs_you_repository,
    )
    has_retryable_or_terminal_failure = any(
        r["outcome"] in ("RETRYABLE_FAILURE", "EVIDENCE_NOT_FOUND", "TERMINAL_FAILURE", "DEFERRED")
        for r in result["records"]
    )

    report = {
        "run_id": run_id,
        "reconciled_count": reconciled_count,
        "reconciliation_skipped_reason": reconciliation_skipped_reason,
        **result,
        "started_at": to_contract_string(started_at),
        "completed_at": to_contract_string(utc_now()),
    }

    return {"report": report, "exit_code": 1 if has_retryable_or_terminal_failure else 0}


def main(argv: Optional[list[str]] = None) -> int:
    args = _parse_args(argv)
    runtime_dir = Path(args.runtime_dir)
    runtime_dir.mkdir(parents=True, exist_ok=True)

    from app.api.composition import get_composition  # local import: keeps this module importable/testable with zero env vars set

    composition = get_composition()

    run_id = identity.generate_id()
    started_at = utc_now()
    worker_id = args.worker_id or f"evidence-classification-worker-{uuid.uuid4()}"

    outcome = _run_worker(
        args=args, composition=composition, run_id=run_id, started_at=started_at, worker_id=worker_id,
    )
    report = outcome["report"]

    report_path = runtime_dir / f"evidence-classification-jobs-process-{run_id}-report.json"
    report_path.write_text(json.dumps(report, indent=2, sort_keys=True, default=str), encoding="utf-8")
    print(json.dumps(report, indent=2, sort_keys=True, default=str))
    print(f"[process_evidence_classification_jobs] report written to {report_path}", file=sys.stderr)

    return outcome["exit_code"]


if __name__ == "__main__":
    raise SystemExit(main())
