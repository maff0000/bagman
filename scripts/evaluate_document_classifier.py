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

AI-only mode — `--ai-only` (CD-6 follow-up, testing v3 in isolation)
------------------------------------------------------------------------
A second, real deterministic rule (created separately, not this
module's concern) can now short-circuit `classify_evidence` for exactly
the corporate-event family v3's own prompt fix targets — which makes
the ordinary shadow path above the WRONG tool for judging whether
`DOCUMENT_TYPE_PROPOSAL` v3 (or any other `task_version`) itself
resolved that boundary correctly: a deterministic match would report a
`DETERMINISTIC` outcome and never reach the model at all.

`--ai-only` adds a second, additive evaluation mode that bypasses
`classify_evidence` ENTIRELY — it never calls it, never touches
deterministic rules, and never persists anything. It instead: builds
the bounded evidence-classification context
(`services.evidence.classification_context.build_evidence_classification_context`
— the exact same builder the real orchestrator uses), resolves the task
contract for the given `--task-version` (`ai.tasks.get_task_contract`),
resolves that version's own prompt asset (`ai.prompts.loader`), calls
`litellm_client.complete(...)` directly, and validates the response with
the exact same `ai.tasks.validate_task_output` the real gateway uses.
It never calls `ai.gateway.background.run_background_task` and never
creates an `AIInvocation`/`EvidenceClassification`/
`EvidenceClassificationRule`/`NeedsYouItem` row of any kind, for either
of its two accepted input shapes:

* `--manifest-file` — the SAME `{evidence_id, expected_document_type,
  label_family}` manifest shape as the shadow mode above, naming real,
  already-registered `EvidenceItem`s; their content is read via the same
  `evidence_repository`/`object_store` the orchestrator itself uses.
* `--challenge-set-file` — a NEW, purely synthetic/inline shape: a JSON
  object with an `"items"` array of `{sender, subject, body,
  expected_document_type, label_family}` entries that do not correspond
  to any real, registered `EvidenceItem` at all (see
  `tests/fixtures/document_type_proposal_v3_challenge_set_synthetic.json`)
  — evaluated by building a synthetic, duck-typed evidence input and
  driving it straight through the same context builder's `text/plain`
  path, with zero repository/object-store reach.

Exactly one of `--manifest-file`/`--challenge-set-file` must be supplied
together with `--ai-only`; `--task-version` selects which
`DOCUMENT_TYPE_PROPOSAL` contract version to invoke (default: the
shadow mode's own `AI_TASK_VERSION`, currently 2) — e.g.:

    python3 scripts/evaluate_document_classifier.py \\
        --runtime-dir /opt/bagman/runtime/classification-runs \\
        --ai-only --task-version 3 \\
        --challenge-set-file tests/fixtures/document_type_proposal_v3_challenge_set_synthetic.json
"""
from __future__ import annotations

import argparse
import hashlib
import json
import statistics
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable, Mapping, Optional

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import os  # noqa: E402

from ai.prompts.loader import load_system_prompt, resolve_prompt_contract_version  # noqa: E402
from ai.providers.litellm.client import LiteLLMOutcomeStatus  # noqa: E402
from ai.tasks import get_task_contract, validate_task_output  # noqa: E402
from core import identity  # noqa: E402
from core.errors import ActiveInvocationConflictError  # noqa: E402
from core.timestamps import to_contract_string, utc_now  # noqa: E402
from services.evidence.classification import DOCUMENT_TYPE_UNKNOWN, SOURCE_DETERMINISTIC_RULE  # noqa: E402
from services.evidence.classification_context import (  # noqa: E402
    OUTCOME_BUILT as _CONTEXT_OUTCOME_BUILT,
    build_evidence_classification_context,
)
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


def load_challenge_set(path: str) -> list[dict]:
    """Load a synthetic/inline challenge-set fixture file for `--ai-only`
    mode (Task 4/5) — a JSON OBJECT (never a bare array, so a genuinely
    synthetic-content marker can live in the same file — see
    `tests/fixtures/document_type_proposal_v3_challenge_set_synthetic.json`)
    with an `"items"` array of `{sender, subject, body,
    expected_document_type}` entries (`label_family` optional). These
    entries do NOT name any real, registered `EvidenceItem` — deliberately
    a distinct, honestly-named loader from `load_manifest` above rather
    than one loader silently guessing the caller's intent from shape.
    """
    raw_text = Path(path).read_text(encoding="utf-8")
    try:
        raw = json.loads(raw_text)
    except json.JSONDecodeError as exc:
        raise SystemExit(f"error: challenge-set file '{path}' is not valid JSON: {exc}") from exc
    if not isinstance(raw, dict) or not isinstance(raw.get("items"), list) or not raw["items"]:
        raise SystemExit(
            f"error: challenge-set file '{path}' must be a JSON object with a non-empty 'items' array"
        )

    required_keys = ("sender", "subject", "body", "expected_document_type")
    items = []
    for i, entry in enumerate(raw["items"]):
        if not isinstance(entry, dict) or not all(key in entry for key in required_keys):
            raise SystemExit(
                f"error: challenge-set entry #{i} in '{path}' must be an object with at least {required_keys}"
            )
        items.append(
            {
                "sender": entry["sender"],
                "subject": entry["subject"],
                "body": entry["body"],
                "expected_document_type": entry["expected_document_type"],
                "label_family": entry.get("label_family"),
            }
        )
    return items


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
# AI-only evaluation (`--ai-only`) — Task 4: a version-aware evaluation
# path that bypasses `classify_evidence`'s deterministic-first check
# ENTIRELY, so a real deterministic rule for the exact family under
# test can never mask whether the AI TASK ITSELF resolved a
# classification boundary correctly. Reuses the SAME context builder,
# task contract lookup, prompt loader, LiteLLM client, and
# `validate_task_output` structured-output validation the real
# gateway/orchestrator use — never a second, hand-rolled prompt or
# classifier. Deliberately never calls `ai.gateway.background
# .run_background_task` (which would create a real, durable
# `AIInvocation` row via a repository) — every call in this section is
# fully disposable, for both a real registered `EvidenceItem` and a
# synthetic/inline challenge-set fixture alike, which is a strictly
# safer (and simpler) property than the WO's own "may create real
# AIInvocation rows" allowance for the real-evidence case; see this
# script's own architecture-boundary test for the proof.
# ---------------------------------------------------------------------


@dataclass(frozen=True)
class _SyntheticEvidenceInput:
    """A minimal, duck-typed evidence-shaped input for AI-only
    evaluation of inline synthetic content — never a real, registered
    `EvidenceItem`. Carries only the two attributes
    `services.evidence.classification_context.build_evidence_classification_context`
    actually reads (`.mime_type`/`.metadata`), so that exact context
    builder is reused verbatim for synthetic content too, with zero
    second/hand-rolled context-rendering logic."""

    mime_type: str
    metadata: Mapping[str, Any]


def _is_synthetic_item(item: dict) -> bool:
    return "evidence_id" not in item


def _build_ai_only_context(item: dict, *, evidence_repository, object_store):
    """Build the bounded classification context for one `--ai-only`-mode
    item — either a real, already-registered `EvidenceItem`
    (`evidence_id` present, resolved via the SAME
    `evidence_repository`/`object_store`/`build_evidence_classification_context`
    the real orchestrator uses), or a synthetic/inline challenge-set
    fixture (`sender`/`subject`/`body` present, no real evidence row or
    object-store read at all)."""
    if _is_synthetic_item(item):
        synthetic = _SyntheticEvidenceInput(
            mime_type="text/plain",
            metadata={"sender_address": item["sender"], "subject": item["subject"]},
        )
        raw_content = item["body"].encode("utf-8")
        return build_evidence_classification_context(evidence=synthetic, raw_content=raw_content)

    evidence = evidence_repository.get_evidence(item["evidence_id"])
    if not evidence.storage_reference:
        from services.evidence.classification_context import ClassificationContextBuildResult, OUTCOME_CONTEXT_UNSUPPORTED

        return ClassificationContextBuildResult(
            outcome=OUTCOME_CONTEXT_UNSUPPORTED,
            unsupported_reason="EvidenceItem has no stored content (storage_reference is empty)",
        )
    raw_content = object_store.get(evidence.storage_reference)
    return build_evidence_classification_context(evidence=evidence, raw_content=raw_content)


def evaluate_item_ai_only(
    item: dict,
    *,
    task_version: int,
    litellm_client,
    evidence_repository=None,
    object_store=None,
) -> ItemResult:
    """Task 4's per-item core: never calls `classify_evidence` (so a
    deterministic rule can never short-circuit this path) — builds
    context, resolves `(AI_TASK_ID, task_version)`'s task contract and
    prompt asset, calls `litellm_client.complete()` directly, and
    validates the result with `ai.tasks.validate_task_output`. `item` is
    either the existing `{evidence_id, expected_document_type,
    label_family}` manifest shape, or the new synthetic
    `{sender, subject, body, expected_document_type, label_family}`
    challenge-set shape (see `load_challenge_set`) — `evidence_repository`/
    `object_store` are only ever consulted for the former.
    """
    evidence_id = item.get("evidence_id")
    expected = item["expected_document_type"]
    label_family = item.get("label_family")
    identifier = evidence_id if evidence_id is not None else f"synthetic:{item.get('subject', '?')!r}"

    context_result = _build_ai_only_context(item, evidence_repository=evidence_repository, object_store=object_store)
    if context_result.outcome != _CONTEXT_OUTCOME_BUILT:
        return ItemResult(
            evidence_id=identifier, expected_document_type=expected, label_family=label_family,
            outcome=OUTCOME_CONTEXT_UNSUPPORTED, bucket=BUCKET_CONTEXT_UNSUPPORTED, actual_document_type=None,
            correct=None, ai_invocation_id=None, new_invocation=None, latency_ms=None, matched_rule_id=None,
            error_code=None, unsupported_reason=context_result.unsupported_reason,
        )
    context = context_result.context

    task_contract = get_task_contract(AI_TASK_ID, task_version)
    prompt_contract_version = resolve_prompt_contract_version(AI_TASK_ID, task_version)
    system_instructions = load_system_prompt(AI_TASK_ID, prompt_contract_version)

    result = litellm_client.complete(
        capability_alias=task_contract.preferred_capability,
        system_instructions=system_instructions,
        evidence_content=context.rendered_context,
        output_schema=task_contract.output_schema,
        timeout_seconds=float(task_contract.timeout_seconds),
    )

    if result.status != LiteLLMOutcomeStatus.OK:
        return ItemResult(
            evidence_id=identifier, expected_document_type=expected, label_family=label_family,
            outcome=OUTCOME_AI_INVOCATION_FAILED, bucket=BUCKET_AI_FAILURE, actual_document_type=None,
            correct=None, ai_invocation_id=None, new_invocation=None, latency_ms=result.latency_ms,
            matched_rule_id=None, error_code=f"LITELLM_{result.status.value}", unsupported_reason=None,
        )

    try:
        parsed_output = json.loads(result.content or "")
    except (json.JSONDecodeError, TypeError) as exc:
        return ItemResult(
            evidence_id=identifier, expected_document_type=expected, label_family=label_family,
            outcome=OUTCOME_AI_INVOCATION_FAILED, bucket=BUCKET_AI_FAILURE, actual_document_type=None,
            correct=None, ai_invocation_id=None, new_invocation=None, latency_ms=result.latency_ms,
            matched_rule_id=None, error_code="OUTPUT_NOT_JSON", unsupported_reason=str(exc),
        )

    validation_result = validate_task_output(task_contract, parsed_output)
    if not validation_result.valid:
        return ItemResult(
            evidence_id=identifier, expected_document_type=expected, label_family=label_family,
            outcome=OUTCOME_AI_INVOCATION_FAILED, bucket=BUCKET_AI_FAILURE, actual_document_type=None,
            correct=None, ai_invocation_id=None, new_invocation=None, latency_ms=result.latency_ms,
            matched_rule_id=None, error_code="OUTPUT_SCHEMA_INVALID",
            unsupported_reason="; ".join(validation_result.errors),
        )

    proposed_type = parsed_output.get("proposed_type")
    is_unknown = proposed_type == DOCUMENT_TYPE_UNKNOWN
    outcome = OUTCOME_AI_PROPOSAL_UNCLASSIFIABLE if is_unknown else OUTCOME_AI_PROPOSAL_REVIEW_REQUIRED

    return ItemResult(
        evidence_id=identifier,
        expected_document_type=expected,
        label_family=label_family,
        outcome=outcome,
        bucket=BUCKET_AI,
        actual_document_type=proposed_type,
        correct=(proposed_type == expected),
        ai_invocation_id=None,
        new_invocation=None,
        latency_ms=result.latency_ms,
        matched_rule_id=None,
        error_code=None,
        unsupported_reason=None,
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
    task_version: int = AI_TASK_VERSION,
    mode: str = "SHADOW_CLASSIFY_EVIDENCE",
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
        "mode": mode,
        "task_id": AI_TASK_ID,
        "task_version": task_version,
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


def run_ai_only_evaluation(
    *,
    items: list[dict],
    task_version: int,
    litellm_client,
    evidence_repository=None,
    object_store=None,
    manifest_file: str,
    run_id: Optional[str] = None,
) -> dict:
    """Task 4's dependency-injected core for `--ai-only` mode — the
    version-aware, deterministic-bypassing sibling of `run_evaluation`
    above. Never calls `classify_evidence`, never touches
    `get_composition()` itself, and (unlike `run_evaluation`) needs no
    `rule_repository`/`classification_repository`/`ai_invocation_repository`/
    `audit_repository`/`record_audit_event`/`needs_you_repository` at
    all — this function structurally cannot write any of those rows,
    because it never accepts a dependency capable of doing so.
    `evidence_repository`/`object_store` are only required when `items`
    names real, already-registered `EvidenceItem`s (manifest shape); a
    purely synthetic challenge-set run needs neither (both stay `None`).
    Reuses `build_report` unchanged beyond its own additive
    `task_version`/`mode` parameters, so the report shape stays
    identical to the shadow-mode report above."""
    run_id = run_id or identity.generate_id()
    started_at = utc_now()

    results = [
        evaluate_item_ai_only(
            item,
            task_version=task_version,
            litellm_client=litellm_client,
            evidence_repository=evidence_repository,
            object_store=object_store,
        )
        for item in items
    ]

    completed_at = utc_now()

    preferred_capability = None
    prompt_contract_version = None
    try:
        task_contract = get_task_contract(AI_TASK_ID, task_version)
        preferred_capability = task_contract.preferred_capability
        prompt_contract_version = resolve_prompt_contract_version(AI_TASK_ID, task_version)
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
        task_version=task_version,
        mode="AI_ONLY_DIRECT",
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
        default=None,
        help=(
            "path to a JSON array of {evidence_id, expected_document_type, label_family} — must live "
            "outside Git for production runs. Required in the default shadow mode; in --ai-only mode, "
            "supply exactly one of --manifest-file/--challenge-set-file"
        ),
    )
    parser.add_argument(
        "--challenge-set-file",
        default=None,
        help=(
            "--ai-only mode only: path to a JSON object with an 'items' array of {sender, subject, body, "
            "expected_document_type, label_family} synthetic fixtures that do NOT name any real, "
            "registered EvidenceItem (see tests/fixtures/document_type_proposal_v3_challenge_set_synthetic.json)"
        ),
    )
    parser.add_argument(
        "--ai-only",
        action="store_true",
        help=(
            "Task 4: bypass classify_evidence's deterministic-first check entirely and invoke "
            "DOCUMENT_TYPE_PROPOSAL at --task-version directly (litellm_client.complete() + "
            "validate_task_output only — creates no AIInvocation/EvidenceClassification/"
            "EvidenceClassificationRule/NeedsYouItem row of any kind)"
        ),
    )
    parser.add_argument(
        "--task-version",
        type=int,
        default=AI_TASK_VERSION,
        help="DOCUMENT_TYPE_PROPOSAL task_version to invoke in --ai-only mode (default: %(default)s) — ignored outside --ai-only mode",
    )
    parser.add_argument("--actor-type", default="SYSTEM")
    parser.add_argument("--actor-id", default="evaluate-document-classifier")
    parser.add_argument("--correlation-id", default=None)

    args = parser.parse_args(list(argv) if argv is not None else None)

    runtime_dir = args.runtime_dir or os.environ.get("BAGMAN_CLASSIFICATION_RUNTIME_DIR")
    if not runtime_dir:
        parser.error("--runtime-dir is required (or set BAGMAN_CLASSIFICATION_RUNTIME_DIR) — no default path is ever assumed")
    args.runtime_dir = runtime_dir

    if args.ai_only:
        if bool(args.manifest_file) == bool(args.challenge_set_file):
            parser.error("--ai-only requires exactly one of --manifest-file or --challenge-set-file")
    else:
        if args.challenge_set_file:
            parser.error("--challenge-set-file is only valid together with --ai-only")
        if not args.manifest_file:
            parser.error("--manifest-file is required (unless --ai-only is used with --challenge-set-file)")

    return args


def main(argv: Optional[list[str]] = None) -> int:
    args = _parse_args(argv)
    runtime_dir = Path(args.runtime_dir)
    runtime_dir.mkdir(parents=True, exist_ok=True)

    from app.api.composition import get_composition  # local import: keeps this module importable/testable with zero env vars set

    composition = get_composition()

    if args.ai_only:
        if args.challenge_set_file:
            items = load_challenge_set(args.challenge_set_file)
            source_file = args.challenge_set_file
            evidence_repository = None
            object_store = None
        else:
            items = load_manifest(args.manifest_file)
            source_file = args.manifest_file
            evidence_repository = composition.api.evidence_repository
            object_store = composition.object_store

        report = run_ai_only_evaluation(
            items=items,
            task_version=args.task_version,
            litellm_client=composition.litellm_client,
            evidence_repository=evidence_repository,
            object_store=object_store,
            manifest_file=source_file,
        )
    else:
        items = load_manifest(args.manifest_file)
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
