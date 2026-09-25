#!/usr/bin/env python3
"""scripts/evaluate_document_classifier.py — CD-6 Slice 5 WI-6 §19-32/§55.

A read-only, SHADOW evaluation tool for the governed document
classifier. It NEVER creates an `EvidenceClassification`,
`EvidenceClassificationRule`, or `NeedsYouItem` row — it calls the
EXISTING, real, governed `services.evidence.classification_orchestrator
.classify_evidence(persist=False, ...)` path (the exact function
`POST /internal/evidence/{evidence_id}/classifications/ai-preview`
already uses) for every labelled item and honestly buckets whatever
outcome comes back. It may create/reuse governed
`DOCUMENT_TYPE_PROPOSAL` v2 `AIInvocation` rows — exactly like the
real ai-preview endpoint already does — but nothing else in canonical
state.

Why `persist=False` alone is the whole safety argument (WI-6 §19/§26)
------------------------------------------------------------------------
`classify_evidence`'s own `persist=False` early-return guard makes
every write call site inside it (`create_classification_with_result`,
`ensure_classification_review_item`) structurally unreachable — this
was independently confirmed by reading that function's body in full
(see `services.evidence.classification_orchestrator`). This script
relies on that SAME guarantee, and never itself calls any of
`create_classification`, `create_classification_with_result`,
`create_classification_rule`, or `ensure_classification_review_item` —
see `tests/integration/test_architecture_boundaries.py`'s WI-6
additions for the static text-absence proof.

Deterministic-first, honestly bucketed (WI-6 §22-24)
------------------------------------------------------------------------
This script never bypasses a valid deterministic rule to force an AI
call — the single call to `classify_evidence(persist=False, ...)` per
labelled item already handles both cases uniformly (deterministic
short-circuit vs. genuine AI reach), because that is exactly what the
orchestrator itself decides internally. Every item is bucketed into
exactly one of:

* ``DETERMINISTIC`` — outcome was `DETERMINISTIC_CLASSIFIED`/
  `DETERMINISTIC_EXISTING`, OR `CURRENT_CLASSIFICATION_EXISTS` whose
  existing classification's own `source` is `DETERMINISTIC_RULE`. Graded
  `DETERMINISTIC_CORRECT`/`DETERMINISTIC_INCORRECT` against the rule's
  (or existing row's) `document_type`. WI-6 §29: any incorrect
  deterministic resolution is a STOP-worthy defect — this evaluation
  does not abort mid-run (it is read-only), but every such finding is
  surfaced loudly in `blocking_findings`, never buried.
* ``AI`` — outcome was `AI_PROPOSAL_REVIEW_REQUIRED`/
  `AI_PROPOSAL_UNCLASSIFIABLE` (a genuine model completion was reached
  and validated). Graded against `proposed_type`.
* ``ALREADY_CLASSIFIED_OTHER_SOURCE`` — `CURRENT_CLASSIFICATION_EXISTS`
  whose existing source is `AI_PROPOSAL`/`OPERATOR_ASSIGNED` (a prior,
  unrelated classification already governs this evidence). Still
  counted toward overall end-to-end accuracy, reported separately from
  both `DETERMINISTIC`/`AI` metrics.
* ``DETERMINISTIC_CONFLICT`` / ``AI_FAILURE`` / ``CONTEXT_UNSUPPORTED``
  / ``AI_IN_PROGRESS`` — never graded correct/incorrect; always visible
  in the report (WI-6 §28/§31), never silently dropped.

AI invocation reuse detection (WI-6 §27)
------------------------------------------------------------------------
Before calling `classify_evidence`, this script snapshots the set of
existing `ai_invocation_id`s for
`(task_id=DOCUMENT_TYPE_PROPOSAL, task_version=2, primary_input_reference=evidence_id)`
via `ai_invocation_repository.list_invocations(...)` — the exact same
query `classify_evidence` itself uses internally for its own reuse
check. After the call, if `result.ai_invocation_id` was already in
that snapshot -> reused; if it is new -> a genuinely new invocation,
and this script reads that invocation's own `latency_ms` (never its
own wall-clock timing — WI-6 §32) for the latency report.

Threshold doctrine (WI-6 §30)
------------------------------------------------------------------------
This script computes and reports metrics only. It contains no
READY/HOLD verdict, and no code path that adjusts or hides a threshold
based on outcomes — the activation call is made later, by the PL,
against this report plus the real production run.

Manifest (WI-6 §20-21)
------------------------------------------------------------------------
A plain JSON file: a list of
`{"evidence_id": ..., "expected_document_type": ..., "label_family": ...}`
objects, supplied via `--manifest-file`. Expected labels must be fixed
BEFORE this script ever runs — this script never modifies the manifest
file it reads. A manifest naming real production evidence IDs must
live OUTSIDE this Git repository (this script places no constraint on
where `--manifest-file` lives, and never commits anything from it —
only that file's own SHA-256 appears in the output report, never its
contents).

Usage
-----
    python3 scripts/evaluate_document_classifier.py \\
        --runtime-dir /opt/bagman/runtime/classification-runs \\
        --manifest-file /path/outside/git/eval-manifest.json \\
        --actor-type SYSTEM --actor-id bagman-pl-shadow-eval

`--runtime-dir` may also be supplied via `BAGMAN_CLASSIFICATION_RUNTIME_DIR`.
This process expects `BAGMAN_RUNTIME_ENV`/DB/object-store/LiteLLM env
vars already set — see `scripts/reprocess_evidence_classification.py`'s
own module docstring for the identical `get_composition()` convention.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import statistics
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, Optional

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import os  # noqa: E402

from core import identity  # noqa: E402
from core.errors import ActiveInvocationConflictError  # noqa: E402
from core.timestamps import to_contract_string, utc_now  # noqa: E402
from services.evidence.classification import SOURCE_DETERMINISTIC_RULE  # noqa: E402
from services.evidence.classification_orchestrator import (  # noqa: E402
    AI_TASK_ID,
    AI_TASK_VERSION,
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

_DETERMINISTIC_OUTCOMES = frozenset({OUTCOME_DETERMINISTIC_CLASSIFIED, OUTCOME_DETERMINISTIC_EXISTING})
_AI_RESULT_OUTCOMES = frozenset({OUTCOME_AI_PROPOSAL_REVIEW_REQUIRED, OUTCOME_AI_PROPOSAL_UNCLASSIFIABLE})
_AI_FAILURE_OUTCOMES = frozenset({OUTCOME_AI_INVOCATION_FAILED, OUTCOME_AI_PRIOR_FAILURE})

BUCKET_DETERMINISTIC = "DETERMINISTIC"
BUCKET_AI = "AI"
BUCKET_ALREADY_CLASSIFIED_OTHER_SOURCE = "ALREADY_CLASSIFIED_OTHER_SOURCE"
BUCKET_DETERMINISTIC_CONFLICT = "DETERMINISTIC_CONFLICT"
BUCKET_AI_FAILURE = "AI_FAILURE"
BUCKET_CONTEXT_UNSUPPORTED = "CONTEXT_UNSUPPORTED"
BUCKET_AI_IN_PROGRESS = "AI_IN_PROGRESS"
BUCKET_UNKNOWN = "UNKNOWN"


# ---------------------------------------------------------------------
# Manifest loading
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
        if not isinstance(entry, dict) or "evidence_id" not in entry or "expected_document_type" not in entry:
            raise SystemExit(
                f"error: manifest entry #{i} in '{path}' must be an object with at least "
                "'evidence_id' and 'expected_document_type'"
            )
        items.append(
            {
                "evidence_id": entry["evidence_id"],
                "expected_document_type": entry["expected_document_type"],
                "label_family": entry.get("label_family"),
            }
        )
    return items


def _manifest_file_sha256(path: str) -> str:
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


# ---------------------------------------------------------------------
# Per-item evaluation
# ---------------------------------------------------------------------


@dataclass
class ItemResult:
    evidence_id: str
    expected_document_type: str
    label_family: Optional[str]
    outcome: str
    bucket: str
    actual_document_type: Optional[str]
    correct: Optional[bool]
    ai_invocation_id: Optional[str]
    new_invocation: Optional[bool]
    latency_ms: Optional[int]
    matched_rule_id: Optional[str]
    error_code: Optional[str]
    unsupported_reason: Optional[str]

    def to_dict(self) -> dict:
        return {
            "evidence_id": self.evidence_id,
            "expected_document_type": self.expected_document_type,
            "label_family": self.label_family,
            "outcome": self.outcome,
            "bucket": self.bucket,
            "actual_document_type": self.actual_document_type,
            "correct": self.correct,
            "ai_invocation_id": self.ai_invocation_id,
            "new_invocation": self.new_invocation,
            "latency_ms": self.latency_ms,
            "matched_rule_id": self.matched_rule_id,
            "error_code": self.error_code,
            "unsupported_reason": self.unsupported_reason,
        }


def _deterministic_actual_type(result, rule_repository) -> Optional[str]:
    """WI-6 dispatch's own explicit instruction: read
    `classify_evidence`'s actual field population rather than assume
    it. Confirmed by reading `classification_orchestrator.classify_evidence`
    in full: on `DETERMINISTIC_CLASSIFIED` under `persist=False`,
    `result.classification` is always `None` (the non-mutating preview
    path never constructs one) but `result.matched_rule_id` IS
    populated — so the "would-be" document_type is read off the real
    rule itself. On `DETERMINISTIC_EXISTING`, `result.classification`
    is the real, already-existing row (a pure read, populated
    regardless of `persist`), so its own `document_type` is used
    directly and no rule lookup is needed."""
    if result.classification is not None:
        return result.classification.document_type
    if result.matched_rule_id is not None:
        return rule_repository.get_rule(result.matched_rule_id).document_type
    return None


def _bucket_result(result, expected_document_type: str, *, rule_repository) -> tuple[str, Optional[str], Optional[bool]]:
    outcome = result.outcome
    if outcome in _DETERMINISTIC_OUTCOMES:
        actual = _deterministic_actual_type(result, rule_repository)
        return BUCKET_DETERMINISTIC, actual, (actual == expected_document_type if actual is not None else None)

    if outcome == OUTCOME_CURRENT_CLASSIFICATION_EXISTS:
        actual = result.existing_document_type
        correct = (actual == expected_document_type) if actual is not None else None
        if result.existing_source == SOURCE_DETERMINISTIC_RULE:
            return BUCKET_DETERMINISTIC, actual, correct
        return BUCKET_ALREADY_CLASSIFIED_OTHER_SOURCE, actual, correct

    if outcome in _AI_RESULT_OUTCOMES:
        actual = result.proposed_type
        return BUCKET_AI, actual, (actual == expected_document_type if actual is not None else None)

    if outcome == OUTCOME_DETERMINISTIC_CONFLICT:
        return BUCKET_DETERMINISTIC_CONFLICT, None, None
    if outcome in _AI_FAILURE_OUTCOMES:
        return BUCKET_AI_FAILURE, None, None
    if outcome == OUTCOME_CONTEXT_UNSUPPORTED:
        return BUCKET_CONTEXT_UNSUPPORTED, None, None
    if outcome == OUTCOME_AI_IN_PROGRESS:
        return BUCKET_AI_IN_PROGRESS, None, None
    return BUCKET_UNKNOWN, None, None  # pragma: no cover - exhaustive in practice; defensive only


def evaluate_item(
    item: dict,
    *,
    evidence_repository,
    rule_repository,
    classification_repository,
    ai_invocation_repository,
    litellm_client,
    object_store,
    audit_repository,
    record_audit_event,
    needs_you_repository,
    actor_type: str,
    actor_id: str,
    correlation_id: Optional[str],
) -> ItemResult:
    evidence_id = item["evidence_id"]
    expected = item["expected_document_type"]
    label_family = item.get("label_family")

    # WI-6 §27 — the reuse-detection technique: snapshot BEFORE the
    # call, compare the real result's `ai_invocation_id` against it
    # AFTER. Identical query to the one `classify_evidence` uses
    # internally for its own idempotency/reuse check.
    pre_call_invocation_ids = {
        inv.ai_invocation_id
        for inv in ai_invocation_repository.list_invocations(
            task_id=AI_TASK_ID, task_version=AI_TASK_VERSION, primary_input_reference=evidence_id
        )
    }

    try:
        result = classify_evidence(
            evidence_id=evidence_id,
            persist=False,
            evidence_repository=evidence_repository,
            rule_repository=rule_repository,
            classification_repository=classification_repository,
            ai_invocation_repository=ai_invocation_repository,
            litellm_client=litellm_client,
            object_store=object_store,
            audit_repository=audit_repository,
            record_audit_event=record_audit_event,
            needs_you_repository=needs_you_repository,
            actor_type=actor_type,
            actor_id=actor_id,
            correlation_id=correlation_id,
        )
    except ActiveInvocationConflictError as exc:
        # WI-6 §35(c) — the real concurrency signal a genuine race
        # between two simultaneous shadow evaluations of the IDENTICAL
        # evidence can produce: `classify_evidence`'s own internal
        # `find_active_invocation` pre-check can miss another caller
        # that is concurrently between its own pre-check and its own
        # `create_invocation` call, so the real database unique
        # constraint (never a silent duplicate provider call) is what
        # actually arbitrates — this surfaces here as an uncaught
        # `ActiveInvocationConflictError` from `classify_evidence`
        # itself (it does not catch this internally). Handled here
        # exactly like the ORDINARY `AI_IN_PROGRESS` outcome
        # `classify_evidence` returns when its own pre-check DOES see
        # the other caller in time — same bucket, same "never crash the
        # evaluator" doctrine (WI-6 §31), no retry, no second provider
        # call.
        return ItemResult(
            evidence_id=evidence_id, expected_document_type=expected, label_family=label_family,
            outcome=OUTCOME_AI_IN_PROGRESS, bucket=BUCKET_AI_IN_PROGRESS, actual_document_type=None, correct=None,
            ai_invocation_id=None, new_invocation=None, latency_ms=None, matched_rule_id=None,
            error_code=None, unsupported_reason=str(exc),
        )

    bucket, actual_type, correct = _bucket_result(result, expected, rule_repository=rule_repository)

    new_invocation: Optional[bool] = None
    latency_ms: Optional[int] = None
    if result.ai_invocation_id is not None:
        if result.outcome in (OUTCOME_AI_IN_PROGRESS, OUTCOME_AI_PRIOR_FAILURE):
            # These two outcomes ALWAYS name a PRE-EXISTING invocation
            # `classify_evidence` merely observed (`find_active_invocation`/
            # a fingerprint-matched prior FAILED row) — never one this
            # call created — so this is never "new", by construction,
            # regardless of what the pre-call snapshot below happened to
            # contain. This also makes the snapshot technique race-safe
            # under genuine concurrency (WI-6 §35c): under a true race,
            # every LOSING caller's pre-call snapshot can be empty (taken
            # before the eventual winner's row existed), which would
            # otherwise misreport every loser as "new" too — outcome
            # alone is the correct, unambiguous signal for these two.
            new_invocation = False
        else:
            # WI-6 §27's own prescribed technique — correct for every
            # other AI-reaching outcome, where the SAME outcome shape
            # covers both "genuinely new" and "fingerprint-reused
            # SUCCEEDED" and the snapshot is what tells them apart.
            new_invocation = result.ai_invocation_id not in pre_call_invocation_ids
        if new_invocation:
            invocation = ai_invocation_repository.get_invocation(result.ai_invocation_id)
            latency_ms = invocation.latency_ms  # WI-6 §32 — the invocation's OWN recorded latency, never wall-clock here

    return ItemResult(
        evidence_id=evidence_id,
        expected_document_type=expected,
        label_family=label_family,
        outcome=result.outcome,
        bucket=bucket,
        actual_document_type=actual_type,
        correct=correct,
        ai_invocation_id=result.ai_invocation_id,
        new_invocation=new_invocation,
        latency_ms=latency_ms,
        matched_rule_id=result.matched_rule_id,
        error_code=result.error_code,
        unsupported_reason=result.unsupported_reason,
    )


# ---------------------------------------------------------------------
# Aggregation / report
# ---------------------------------------------------------------------


def _percentile(sorted_values: list[float], pct: float) -> float:
    if not sorted_values:
        return 0.0
    if len(sorted_values) == 1:
        return sorted_values[0]
    rank = pct * (len(sorted_values) - 1)
    lower = int(rank)
    upper = min(lower + 1, len(sorted_values) - 1)
    frac = rank - lower
    return sorted_values[lower] + (sorted_values[upper] - sorted_values[lower]) * frac


def _latency_stats(latencies: list[int]) -> dict:
    if not latencies:
        return {"count": 0, "min": None, "median": None, "p95": None, "max": None}
    ordered = sorted(latencies)
    return {
        "count": len(ordered),
        "min": ordered[0],
        "median": statistics.median(ordered),
        "p95": _percentile([float(v) for v in ordered], 0.95),
        "max": ordered[-1],
    }


def build_report(
    items: list[ItemResult],
    *,
    manifest_file: str,
    run_id: str,
    started_at,
    completed_at,
    task_contract_preferred_capability: Optional[str],
    prompt_contract_version: Optional[str],
) -> dict:
    overall_total = 0
    overall_correct = 0
    overall_incorrect = 0
    per_document_type: dict[str, dict[str, int]] = {}
    confusion_pairs: dict[str, int] = {}

    deterministic_total = 0
    deterministic_correct = 0
    deterministic_incorrect = 0
    deterministic_conflicts = 0

    ai_total = 0
    ai_correct = 0
    ai_incorrect = 0
    ai_review_required = 0
    ai_unclassifiable = 0

    already_classified_other_source_total = 0
    already_classified_other_source_correct = 0

    ai_invocation_failures = []
    schema_failures = []
    context_unsupported = []
    ai_in_progress_items = []

    new_invocation_count = 0
    reused_invocation_count = 0
    new_invocation_latencies: list[int] = []

    blocking_findings: list[str] = []

    for item in items:
        if item.new_invocation is True:
            new_invocation_count += 1
            if item.latency_ms is not None:
                new_invocation_latencies.append(item.latency_ms)
        elif item.new_invocation is False:
            reused_invocation_count += 1

        if item.bucket == BUCKET_DETERMINISTIC:
            deterministic_total += 1
            if item.correct is True:
                deterministic_correct += 1
            elif item.correct is False:
                deterministic_incorrect += 1
                blocking_findings.append(
                    f"DETERMINISTIC MISCLASSIFICATION: evidence_id={item.evidence_id} expected="
                    f"{item.expected_document_type!r} actual={item.actual_document_type!r} "
                    f"(rule_id={item.matched_rule_id!r}) — a deterministic rule producing the wrong "
                    "document_type is a STOP-class defect (WI-6 §29)"
                )
        elif item.bucket == BUCKET_AI:
            ai_total += 1
            if item.outcome == OUTCOME_AI_PROPOSAL_REVIEW_REQUIRED:
                ai_review_required += 1
            elif item.outcome == OUTCOME_AI_PROPOSAL_UNCLASSIFIABLE:
                ai_unclassifiable += 1
            if item.correct is True:
                ai_correct += 1
            elif item.correct is False:
                ai_incorrect += 1
        elif item.bucket == BUCKET_ALREADY_CLASSIFIED_OTHER_SOURCE:
            already_classified_other_source_total += 1
            if item.correct is True:
                already_classified_other_source_correct += 1
        elif item.bucket == BUCKET_DETERMINISTIC_CONFLICT:
            deterministic_conflicts += 1
            blocking_findings.append(
                f"DETERMINISTIC CONFLICT: evidence_id={item.evidence_id} — two or more ACTIVE rules were "
                "equally authoritative; AI must never arbitrate this, and neither may this evaluator"
            )
        elif item.bucket == BUCKET_AI_FAILURE:
            failure_record = {"evidence_id": item.evidence_id, "ai_invocation_id": item.ai_invocation_id, "outcome": item.outcome, "error_code": item.error_code}
            ai_invocation_failures.append(failure_record)
            if item.error_code and "SCHEMA" in item.error_code.upper():
                schema_failures.append(failure_record)
        elif item.bucket == BUCKET_CONTEXT_UNSUPPORTED:
            context_unsupported.append({"evidence_id": item.evidence_id, "unsupported_reason": item.unsupported_reason})
        elif item.bucket == BUCKET_AI_IN_PROGRESS:
            ai_in_progress_items.append({"evidence_id": item.evidence_id, "ai_invocation_id": item.ai_invocation_id})

        if item.correct is not None:
            overall_total += 1
            expected_type = item.expected_document_type
            bucket_for_type = per_document_type.setdefault(expected_type, {"total": 0, "correct": 0, "incorrect": 0})
            bucket_for_type["total"] += 1
            if item.correct:
                overall_correct += 1
                bucket_for_type["correct"] += 1
            else:
                overall_incorrect += 1
                bucket_for_type["incorrect"] += 1
                pair_key = f"{expected_type}->{item.actual_document_type}"
                confusion_pairs[pair_key] = confusion_pairs.get(pair_key, 0) + 1

    overall_accuracy = (overall_correct / overall_total) if overall_total else None
    deterministic_accuracy = (deterministic_correct / deterministic_total) if deterministic_total else None
    ai_accuracy = (ai_correct / ai_total) if ai_total else None

    return {
        "run_id": run_id,
        "started_at": to_contract_string(started_at),
        "completed_at": to_contract_string(completed_at),
        "deployed_git_commit": os.environ.get("BAGMAN_GIT_COMMIT", "unknown"),
        "task_id": AI_TASK_ID,
        "task_version": AI_TASK_VERSION,
        "preferred_capability": task_contract_preferred_capability,
        "prompt_contract_version": prompt_contract_version,
        "manifest_file": manifest_file,
        "manifest_file_sha256": _manifest_file_sha256(manifest_file),
        "total_labelled_items": len(items),
        "overall": {
            "total": overall_total,
            "correct": overall_correct,
            "incorrect": overall_incorrect,
            "accuracy": overall_accuracy,
        },
        "per_document_type": per_document_type,
        "confusion_pairs": confusion_pairs,
        "deterministic_metrics": {
            "rule_covered_labelled_items": deterministic_total,
            "correct": deterministic_correct,
            "incorrect": deterministic_incorrect,
            "conflicts": deterministic_conflicts,
            "accuracy": deterministic_accuracy,
        },
        "ai_metrics": {
            "total": ai_total,
            "correct": ai_correct,
            "incorrect": ai_incorrect,
            "review_required_count": ai_review_required,
            "unclassifiable_count": ai_unclassifiable,
            "accuracy": ai_accuracy,
        },
        "already_classified_other_source": {
            "total": already_classified_other_source_total,
            "correct": already_classified_other_source_correct,
        },
        "ai_invocation_failures": ai_invocation_failures,
        "schema_failures": schema_failures,
        "context_unsupported": context_unsupported,
        "ai_in_progress_items": ai_in_progress_items,
        "invocation_reuse": {
            "new_invocation_count": new_invocation_count,
            "reused_invocation_count": reused_invocation_count,
        },
        "latency_ms_new_invocations": _latency_stats(new_invocation_latencies),
        "blocking_findings": blocking_findings,
        "items": [item.to_dict() for item in items],
    }


def run_evaluation(
    *,
    items: list[dict],
    evidence_repository,
    rule_repository,
    classification_repository,
    ai_invocation_repository,
    litellm_client,
    object_store,
    audit_repository,
    record_audit_event,
    needs_you_repository,
    actor_type: str,
    actor_id: str,
    correlation_id: Optional[str],
    manifest_file: str,
    run_id: Optional[str] = None,
) -> dict:
    """Dependency-injected core (never touches `get_composition()`
    itself) — the evaluation loop, one `classify_evidence(persist=False,
    ...)` call per labelled item, followed by honest aggregation.
    Never raises on a per-item AI/provider failure (WI-6 §31) — every
    failure this function's own dependencies can report cleanly is
    captured in the returned report, never a crash."""
    run_id = run_id or identity.generate_id()
    started_at = utc_now()

    results = [
        evaluate_item(
            item,
            evidence_repository=evidence_repository,
            rule_repository=rule_repository,
            classification_repository=classification_repository,
            ai_invocation_repository=ai_invocation_repository,
            litellm_client=litellm_client,
            object_store=object_store,
            audit_repository=audit_repository,
            record_audit_event=record_audit_event,
            needs_you_repository=needs_you_repository,
            actor_type=actor_type,
            actor_id=actor_id,
            correlation_id=correlation_id,
        )
        for item in items
    ]

    completed_at = utc_now()

    preferred_capability = None
    prompt_contract_version = None
    try:
        from ai.prompts.loader import resolve_prompt_contract_version
        from ai.tasks import get_task_contract

        task_contract = get_task_contract(AI_TASK_ID, AI_TASK_VERSION)
        preferred_capability = task_contract.preferred_capability
        prompt_contract_version = resolve_prompt_contract_version(AI_TASK_ID, AI_TASK_VERSION)
    except Exception:  # noqa: BLE001 - report metadata only; never let this fail the whole evaluation
        pass

    return build_report(
        results,
        manifest_file=manifest_file,
        run_id=run_id,
        started_at=started_at,
        completed_at=completed_at,
        task_contract_preferred_capability=preferred_capability,
        prompt_contract_version=prompt_contract_version,
    )


# ---------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------


def _parse_args(argv: Optional[Iterable[str]]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument(
        "--runtime-dir",
        default=None,
        help="directory to hold the JSON report (required here, or via BAGMAN_CLASSIFICATION_RUNTIME_DIR)",
    )
    parser.add_argument(
        "--manifest-file",
        required=True,
        help="path to a JSON array of {evidence_id, expected_document_type, label_family} — must live outside Git for production runs",
    )
    parser.add_argument("--actor-type", default="SYSTEM")
    parser.add_argument("--actor-id", default="evaluate-document-classifier")
    parser.add_argument("--correlation-id", default=None)

    args = parser.parse_args(list(argv) if argv is not None else None)

    runtime_dir = args.runtime_dir or os.environ.get("BAGMAN_CLASSIFICATION_RUNTIME_DIR")
    if not runtime_dir:
        parser.error("--runtime-dir is required (or set BAGMAN_CLASSIFICATION_RUNTIME_DIR) — no default path is ever assumed")
    args.runtime_dir = runtime_dir

    return args


def main(argv: Optional[list[str]] = None) -> int:
    args = _parse_args(argv)
    runtime_dir = Path(args.runtime_dir)
    runtime_dir.mkdir(parents=True, exist_ok=True)

    items = load_manifest(args.manifest_file)

    from app.api.composition import get_composition  # local import: keeps this module importable/testable with zero env vars set

    composition = get_composition()

    report = run_evaluation(
        items=items,
        evidence_repository=composition.api.evidence_repository,
        rule_repository=composition.classification_rule_repository,
        classification_repository=composition.classification_repository,
        ai_invocation_repository=composition.ai_invocation_repository,
        litellm_client=composition.litellm_client,
        object_store=composition.object_store,
        audit_repository=composition.api.audit_repository,
        record_audit_event=composition.api.record_audit_event,
        needs_you_repository=composition.needs_you_repository,
        actor_type=args.actor_type,
        actor_id=args.actor_id,
        correlation_id=args.correlation_id,
        manifest_file=args.manifest_file,
    )

    report_path = runtime_dir / f"evaluate-document-classifier-{report['run_id']}-report.json"
    report_path.write_text(json.dumps(report, indent=2, sort_keys=True), encoding="utf-8")
    print(json.dumps(report, indent=2, sort_keys=True))
    print(f"[evaluate_document_classifier] report written to {report_path}", file=sys.stderr)

    # WI-6 §31 — this tool reports failures visibly; it does not itself
    # render a pass/fail verdict (that is the PL's call, WI-6 §55) — so
    # it always exits 0 once a report was successfully produced.
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
