#!/usr/bin/env python3
"""scripts/process_background_job_overflow.py — CD-6 §103 Inference
Architecture Ruling, PID §103.4 item 6.

The ONE, bounded, explicit, human-invoked mechanism for submitting and
processing Trinity backlog/overflow work (`ai.jobs`). There is no
daemon, no scheduler, and no automatic threshold anywhere in this file
— running it is how an operator manually starts or continues backlog
processing; running it again later is how they continue it further.
This is deliberately the "bounded operator/maintenance-mode procedure"
CD-6 §103 point 6 / §103.4 item 2 requires, never a background service.

Mirrors `scripts/reprocess_evidence_classification.py`'s own
conventions (argparse, `RawDescriptionHelpFormatter`, `main(argv=None)
-> int`, `if __name__ == "__main__": raise SystemExit(main())`) and
`scripts/evaluate_document_classifier.py`'s own `load_manifest`
shape/style for the manifest this tool's `--submit` mode reads.

Two independent modes (never both in one invocation)
------------------------------------------------------------------------
`--submit --manifest-file <path>`: given a manifest — a JSON array of
`{"evidence_id": ..., "task_id": ..., "task_version": ...}` objects —
resolves each item's REAL evidence content via
`services.evidence.classification_context.build_evidence_classification_context`
(the SAME governed, bounded context builder
`services/evidence/classification_orchestrator.py` already uses — this
script never re-implements content resolution, and never does the
unsafe best-effort raw-byte UTF-8 decode
`app/api/routers/ai.py::_resolve_evidence_content` uses for its own
generic HTTP surface) and calls `ai.jobs.BackgroundJobRepository
.submit_job(...)` for each, always with
`inference_backend="TRINITY_CORE_OVERFLOW"`,
`capability_alias="trinity-core"` — the only pair this delivery's
`ai.jobs` module authorises (`ai.jobs.validate_background_job_backend_and_alias`).
Idempotent per manifest entry (`ai.jobs.BackgroundJobRepository
.submit_job`'s own idempotency-key-replay doctrine): re-running
`--submit` with the same manifest is always safe and never creates
duplicate jobs. Per-entry failures (an unknown `task_id`/`task_version`,
an `evidence_id` with no stored content, an evidence body this repo's
governed context builder cannot support) are recorded in this run's own
report and SKIPPED — they do not abort the whole batch, since each
manifest entry is an independent submission with no cross-entry
invariant to protect (a deliberate contrast with
`scripts/reprocess_evidence_classification.py`'s own "any anomaly stops
the whole run" doctrine, which exists there to protect a SHARED
manifest-hash gate this tool has no equivalent of).

`--process --limit N [--worker-id ...]`: claims up to `N` pending/
retryable jobs (`BackgroundJobRepository.claim_next_pending`) and
actually executes each one — routing it at `trinity-core`/
`TRINITY_CORE_OVERFLOW` via `ai.gateway.background.run_background_task`'s
new, optional `capability_alias_override`/`inference_backend`
parameters (see that module's own docstring's "Backend-override
parameters" section — added specifically so this script can reuse the
exact same provider-call/invocation-lifecycle/structured-output-
validation/audit-event machinery the normal MAC_LOCAL path already
uses, never a second, parallel reimplementation). One bounded pass over
exactly the jobs that exist right now, then exits — running it again
later is how an operator continues.

Retryable-vs-terminal failure classification (this script's own
judgment call, documented here)
------------------------------------------------------------------------
A transport/timeout/rate-limit-shaped `ai.providers.litellm.client
.LiteLLMOutcomeStatus` (surfaced on the resulting `AIInvocation` as
`error_code` `LITELLM_TRANSPORT_ERROR`/`LITELLM_TIMEOUT`/
`LITELLM_RATE_LIMITED`) is classified RETRYABLE — a genuinely transient
condition, safe to attempt again. Every other failure — a structured-
output/schema-validation failure (`OUTPUT_NOT_JSON`/
`OUTPUT_SCHEMA_INVALID` — retrying the exact same content against the
exact same model will not fix a schema mismatch), an auth failure
(`LITELLM_AUTH_ERROR` — a credential problem, not a transient one), a
local config failure (`LITELLM_CONFIG_ERROR`), or a generic provider
error (`LITELLM_PROVIDER_ERROR` — deliberately NOT classified
retryable here, even though this codebase's own history records a real
recurring `400 no_db_connection` PROVIDER_ERROR condition that was, in
fact, transient infrastructure — a PL judgment call to revisit if that
pattern recurs against `trinity-core`, flagged explicitly in this
delivery's own report rather than silently guessed at) — is classified
NON-retryable and goes straight to `FAILED_TERMINAL`. A concurrency
conflict (`core.errors.ActiveInvocationConflictError` — another
invocation, of ANY origin, is already in flight for this exact
`(task_id, task_version, evidence_id)` subject) is treated as
RETRYABLE: it is a genuinely transient state, not a defect in the job
or its content.

Usage
-----
    # Submit a backlog batch (safe to re-run — idempotent per entry):
    python3 scripts/process_background_job_overflow.py \\
        --runtime-dir /opt/bagman/runtime/background-job-overflow \\
        --submit --manifest-file /path/outside/git/overflow-manifest.json \\
        --actor-type SYSTEM --actor-id bagman-overflow-operator

    # Process up to 10 pending/retryable jobs right now:
    python3 scripts/process_background_job_overflow.py \\
        --runtime-dir /opt/bagman/runtime/background-job-overflow \\
        --process --limit 10 --worker-id operator-manual-run-1

`--runtime-dir` may also be supplied via
`BAGMAN_BACKGROUND_JOB_OVERFLOW_RUNTIME_DIR`. This process expects to
run with `BAGMAN_RUNTIME_ENV`/DB/object-store env vars already set
(typically `docker exec bagman-api python3
scripts/process_background_job_overflow.py ...`), via
`app.api.composition.get_composition()` — exactly like every other
operator script in this directory.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import uuid
from pathlib import Path
from typing import Any, Iterable, Optional

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from ai.gateway.background import run_background_task  # noqa: E402
from ai.jobs import BackgroundJob  # noqa: E402
from ai.tasks import get_task_contract  # noqa: E402
from core import identity  # noqa: E402
from core.errors import ActiveInvocationConflictError, NotFoundError, ValidationError  # noqa: E402
from core.contract_validation import describe_schema_errors  # noqa: E402
from core.timestamps import to_contract_string, utc_now  # noqa: E402
from services.evidence.classification_context import (  # noqa: E402
    OUTCOME_BUILT,
    build_evidence_classification_context,
)

#: The one authorised Trinity overflow alias/backend pair (CD-6 §103) —
#: reused verbatim, never re-derived, from `ai.jobs`'s own closed scope.
_OVERFLOW_INFERENCE_BACKEND = "TRINITY_CORE_OVERFLOW"
_OVERFLOW_CAPABILITY_ALIAS = "trinity-core"

#: See module docstring's "Retryable-vs-terminal failure classification"
#: section for the full reasoning.
_RETRYABLE_ERROR_CODES = frozenset({"LITELLM_TRANSPORT_ERROR", "LITELLM_TIMEOUT", "LITELLM_RATE_LIMITED"})

#: Bounded ceiling for `--limit` (never "unlimited") — generous headroom
#: for a manual operator batch while still being a genuine bound.
_MAX_PROCESS_LIMIT = 200


# ---------------------------------------------------------------------
# Manifest loading (mirrors scripts/evaluate_document_classifier.py's
# own load_manifest shape/style — see module docstring)
# ---------------------------------------------------------------------


def load_manifest(path: str) -> list[dict]:
    raw_text = Path(path).read_text(encoding="utf-8")
    try:
        raw = json.loads(raw_text)
    except json.JSONDecodeError as exc:
        raise SystemExit(f"error: manifest file '{path}' is not valid JSON: {exc}") from exc
    if not isinstance(raw, list) or not raw:
        raise SystemExit(f"error: manifest file '{path}' must contain a non-empty JSON array")

    items = []
    for i, entry in enumerate(raw):
        if (
            not isinstance(entry, dict)
            or "evidence_id" not in entry
            or "task_id" not in entry
            or "task_version" not in entry
        ):
            raise SystemExit(
                f"error: manifest entry #{i} in '{path}' must be an object with 'evidence_id', "
                "'task_id', and 'task_version'"
            )
        items.append(
            {
                "evidence_id": entry["evidence_id"],
                "task_id": entry["task_id"],
                "task_version": entry["task_version"],
            }
        )
    return items


# ---------------------------------------------------------------------
# --submit
# ---------------------------------------------------------------------


def _submit_one(
    entry: dict,
    *,
    evidence_repository,
    object_store,
    background_job_repository,
    actor_type: str,
    actor_id: str,
) -> dict:
    """Resolve one manifest entry's real evidence content and submit it
    as a `BackgroundJob`. Returns a plain-dict report record — never
    raises for an ordinary, expected per-entry failure (see module
    docstring's "per-entry failures... recorded... and SKIPPED").
    """
    evidence_id = entry["evidence_id"]
    task_id = entry["task_id"]
    task_version = entry["task_version"]

    try:
        task_contract = get_task_contract(task_id, task_version)
    except NotFoundError as exc:
        return {"evidence_id": evidence_id, "task_id": task_id, "task_version": task_version, "outcome": "UNKNOWN_TASK", "detail": str(exc)}

    try:
        evidence = evidence_repository.get_evidence(evidence_id)
    except NotFoundError as exc:
        return {"evidence_id": evidence_id, "task_id": task_id, "task_version": task_version, "outcome": "UNKNOWN_EVIDENCE", "detail": str(exc)}

    if not evidence.storage_reference:
        return {
            "evidence_id": evidence_id, "task_id": task_id, "task_version": task_version,
            "outcome": "CONTEXT_UNSUPPORTED", "detail": "EvidenceItem has no stored content",
        }

    raw_content = object_store.get(evidence.storage_reference)
    context_result = build_evidence_classification_context(evidence=evidence, raw_content=raw_content)
    if context_result.outcome != OUTCOME_BUILT:
        return {
            "evidence_id": evidence_id, "task_id": task_id, "task_version": task_version,
            "outcome": "CONTEXT_UNSUPPORTED", "detail": context_result.unsupported_reason,
        }
    context = context_result.context
    assert context is not None  # noqa: S101 - guaranteed by OUTCOME_BUILT

    input_references: dict[str, Any] = {"evidence_id": evidence_id}
    schema_errors = describe_schema_errors(dict(input_references), dict(task_contract.input_schema))
    if schema_errors:
        return {
            "evidence_id": evidence_id, "task_id": task_id, "task_version": task_version,
            "outcome": "INPUT_SCHEMA_MISMATCH",
            "detail": (
                f"task '{task_id}' v{task_version} requires input_references this script's fixed "
                f"{{'evidence_id': ...}} shape does not satisfy: {'; '.join(schema_errors)} — this tool "
                "only supports tasks whose input_schema accepts exactly {'evidence_id': <str>} "
                "(DOCUMENT_SUMMARY v1 / DOCUMENT_TYPE_PROPOSAL v1 / ENTITY_PROPOSAL v1 today)"
            ),
        }

    idempotency_key = f"{task_id}:{task_version}:{evidence_id}"
    job = background_job_repository.submit_job(
        idempotency_key=idempotency_key,
        task_id=task_id,
        task_version=task_version,
        input_references=input_references,
        evidence_content=context.rendered_context,
        inference_backend=_OVERFLOW_INFERENCE_BACKEND,
        capability_alias=_OVERFLOW_CAPABILITY_ALIAS,
        actor_type=actor_type,
        actor_id=actor_id,
    )
    return {
        "evidence_id": evidence_id, "task_id": task_id, "task_version": task_version,
        "outcome": "SUBMITTED", "job_id": job.job_id, "status": job.status,
    }


def run_submit(
    *,
    manifest_items: list[dict],
    evidence_repository,
    object_store,
    background_job_repository,
    actor_type: str,
    actor_id: str,
) -> dict:
    records = [
        _submit_one(
            entry,
            evidence_repository=evidence_repository,
            object_store=object_store,
            background_job_repository=background_job_repository,
            actor_type=actor_type,
            actor_id=actor_id,
        )
        for entry in manifest_items
    ]
    per_outcome_counts: dict[str, int] = {}
    for record in records:
        per_outcome_counts[record["outcome"]] = per_outcome_counts.get(record["outcome"], 0) + 1

    return {
        "mode": "SUBMIT",
        "manifest_entry_count": len(manifest_items),
        "records": records,
        "per_outcome_counts": per_outcome_counts,
    }


# ---------------------------------------------------------------------
# --process
# ---------------------------------------------------------------------


def _is_retryable(error_code: Optional[str]) -> bool:
    return error_code in _RETRYABLE_ERROR_CODES


def _process_one(
    job: BackgroundJob,
    *,
    background_job_repository,
    ai_invocation_repository,
    litellm_client,
    record_audit_event,
) -> dict:
    background_job_repository.mark_in_progress(job.job_id)

    try:
        invocation = run_background_task(
            task_id=job.task_id,
            task_version=job.task_version,
            input_references=job.input_references,
            evidence_content=job.evidence_content,
            actor_type=job.actor_type,
            actor_id=job.actor_id,
            correlation_id=job.correlation_id,
            repository=ai_invocation_repository,
            litellm_client=litellm_client,
            record_audit_event=record_audit_event,
            capability_alias_override=job.capability_alias,
            inference_backend=job.inference_backend,
        )
    except ActiveInvocationConflictError as exc:
        # See module docstring's own classification section — treated
        # as retryable: a transient state, not a defect in this job.
        updated = background_job_repository.mark_failed(job.job_id, error=f"ACTIVE_INVOCATION_CONFLICT: {exc}", retryable=True)
        return {"job_id": job.job_id, "outcome": "RETRYABLE_CONFLICT", "job_status": updated.status}
    except (ValidationError, NotFoundError) as exc:
        # A caller/config-shape defect (bad task_id/version, or
        # input_references that no longer matches the task's own
        # input_schema) — retrying identical content will not fix this.
        updated = background_job_repository.mark_failed(job.job_id, error=f"{type(exc).__name__}: {exc}", retryable=False)
        return {"job_id": job.job_id, "outcome": "TERMINAL_DEFECT", "job_status": updated.status}

    if invocation.status == "SUCCEEDED":
        updated = background_job_repository.mark_succeeded(job.job_id, ai_invocation_id=invocation.ai_invocation_id)
        return {"job_id": job.job_id, "outcome": "SUCCEEDED", "job_status": updated.status, "ai_invocation_id": invocation.ai_invocation_id}

    retryable = _is_retryable(invocation.error_code)
    updated = background_job_repository.mark_failed(
        job.job_id, error=invocation.error_code or f"AI_INVOCATION_{invocation.status}", retryable=retryable
    )
    return {
        "job_id": job.job_id,
        "outcome": "RETRYABLE_FAILURE" if retryable else "TERMINAL_FAILURE",
        "job_status": updated.status,
        "ai_invocation_id": invocation.ai_invocation_id,
        "error_code": invocation.error_code,
    }


def run_process(
    *,
    limit: int,
    worker_id: str,
    background_job_repository,
    ai_invocation_repository,
    litellm_client,
    record_audit_event,
) -> dict:
    claimed = background_job_repository.claim_next_pending(limit=limit, worker_id=worker_id)
    records = [
        _process_one(
            job,
            background_job_repository=background_job_repository,
            ai_invocation_repository=ai_invocation_repository,
            litellm_client=litellm_client,
            record_audit_event=record_audit_event,
        )
        for job in claimed
    ]
    per_outcome_counts: dict[str, int] = {}
    for record in records:
        per_outcome_counts[record["outcome"]] = per_outcome_counts.get(record["outcome"], 0) + 1

    return {
        "mode": "PROCESS",
        "worker_id": worker_id,
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
            "BAGMAN_BACKGROUND_JOB_OVERFLOW_RUNTIME_DIR — never hardcoded by this tool)"
        ),
    )

    mode_group = parser.add_mutually_exclusive_group(required=True)
    mode_group.add_argument("--submit", action="store_true", help="submit a manifest's worth of new backlog jobs")
    mode_group.add_argument("--process", action="store_true", help="claim and execute pending/retryable jobs right now")

    parser.add_argument("--manifest-file", default=None, help="required with --submit: JSON array of {evidence_id, task_id, task_version}")
    parser.add_argument("--actor-type", default="SYSTEM")
    parser.add_argument("--actor-id", default="background-job-overflow-operator")

    parser.add_argument("--limit", type=int, default=None, help=f"required with --process: bounded claim count (1-{_MAX_PROCESS_LIMIT})")
    parser.add_argument("--worker-id", default=None, help="worker identity stamped on claimed jobs; defaults to a generated id")

    args = parser.parse_args(list(argv) if argv is not None else None)

    if args.submit:
        if not args.manifest_file:
            parser.error("--submit requires --manifest-file <path>")
    else:
        if args.limit is None:
            parser.error(f"--process requires --limit <1-{_MAX_PROCESS_LIMIT}>")
        if not (1 <= args.limit <= _MAX_PROCESS_LIMIT):
            parser.error(f"--limit must be between 1 and {_MAX_PROCESS_LIMIT} (got {args.limit})")

    runtime_dir = args.runtime_dir or os.environ.get("BAGMAN_BACKGROUND_JOB_OVERFLOW_RUNTIME_DIR")
    if not runtime_dir:
        parser.error(
            "--runtime-dir is required (or set BAGMAN_BACKGROUND_JOB_OVERFLOW_RUNTIME_DIR) — no default "
            "path is ever assumed"
        )
    args.runtime_dir = runtime_dir

    return args


def main(argv: Optional[list[str]] = None) -> int:
    args = _parse_args(argv)
    runtime_dir = Path(args.runtime_dir)
    runtime_dir.mkdir(parents=True, exist_ok=True)

    from app.api.composition import get_composition  # local import: keeps this module importable/testable with zero env vars set

    composition = get_composition()

    run_id = identity.generate_id()
    started_at = utc_now()

    try:
        if args.submit:
            manifest_items = load_manifest(args.manifest_file)
            result = run_submit(
                manifest_items=manifest_items,
                evidence_repository=composition.api.evidence_repository,
                object_store=composition.object_store,
                background_job_repository=composition.background_job_repository,
                actor_type=args.actor_type,
                actor_id=args.actor_id,
            )
            has_terminal_failure = False
        else:
            worker_id = args.worker_id or f"overflow-worker-{uuid.uuid4()}"
            result = run_process(
                limit=args.limit,
                worker_id=worker_id,
                background_job_repository=composition.background_job_repository,
                ai_invocation_repository=composition.ai_invocation_repository,
                litellm_client=composition.litellm_client,
                record_audit_event=composition.api.record_audit_event,
            )
            has_terminal_failure = any(
                r["outcome"] in ("TERMINAL_FAILURE", "TERMINAL_DEFECT") for r in result["records"]
            )
    except SystemExit:
        raise

    report = {
        "run_id": run_id,
        **result,
        "started_at": to_contract_string(started_at),
        "completed_at": to_contract_string(utc_now()),
    }

    mode_tag = "submit" if args.submit else "process"
    report_path = runtime_dir / f"background-job-overflow-{mode_tag}-{run_id}-report.json"
    report_path.write_text(json.dumps(report, indent=2, sort_keys=True, default=str), encoding="utf-8")
    print(json.dumps(report, indent=2, sort_keys=True, default=str))
    print(f"[process_background_job_overflow] report written to {report_path}", file=sys.stderr)

    return 1 if has_terminal_failure else 0


if __name__ == "__main__":
    raise SystemExit(main())
