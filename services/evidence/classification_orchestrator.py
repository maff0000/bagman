"""``classify_evidence`` — the deterministic-first, AI-fallback
governed classification orchestrator (CD-6 Slice 5 WI-3 §29-38).

Sequence (WI-3 §29)
------------------------------------------------------------------------
1. Retrieve the current `DOCUMENT_TYPE` classification for this evidence.
2. If a current classification already exists (of ANY source —
   deterministic, AI, or operator-assigned) -> STOP
   (`OUTCOME_CURRENT_CLASSIFICATION_EXISTS`, WI-3 §31/§38). This guard
   runs BEFORE the deterministic classifier is even invoked, because
   `classify_evidence_deterministically` (WI-2) only itself re-checks
   "current" on the rule-MATCH path — a NO_MATCH/NO_APPLICABLE_RULE_INPUT
   outcome from WI-2 never looks at "current" at all, so without this
   explicit pre-check an evidence item that already has an AI or
   operator-assigned classification (but no matching deterministic
   rule) would incorrectly fall through to a second, redundant AI call.
3. Invoke the deterministic classifier. `persist=True` (WI-3 §40) calls
   WI-2's own, real, WRITING
   `services.evidence.classification_service.classify_evidence_deterministically`
   directly — never reimplemented. `persist=False` (WI-3 §39's
   `ai-preview` endpoint, which must create NO `EvidenceClassification`
   row under ANY outcome) instead calls this module's own
   `_preview_deterministic_outcome` — a NON-MUTATING mirror that reuses
   the exact SAME `services.evidence.classification_matcher
   .match_evidence_to_rule` precedence logic (never a second
   implementation of matching/precedence rules) but never calls
   `create_classification`. See `_preview_deterministic_outcome`'s own
   docstring for the real defect this fixes — an earlier version of
   this module called the writing function unconditionally, so the
   'preview, never mutates' endpoint could silently create a genuine
   row whenever a real ACTIVE rule happened to match.
4. `CLASSIFIED`/`EXISTING` -> STOP (deterministic rule truth already
   governs this evidence; WI-3 never overrides it).
5. `CONFLICT` -> STOP, fail closed (WI-3 §30: AI must NEVER arbitrate a
   deterministic rule conflict — that is an operator-resolution matter).
6. `CURRENT_CLASSIFICATION_EXISTS` (from WI-2's OWN internal check, on
   the rare path where a rule matched but a DIFFERENT producer already
   holds current) -> STOP.
7. `NO_MATCH`/`NO_APPLICABLE_RULE_INPUT` -> build the bounded AI
   evidence-classification context
   (`services.evidence.classification_context`).
8. Context `CONTEXT_UNSUPPORTED` -> STOP; the AI is NEVER invoked
   merely to ask it to classify meaningless/unsupported bytes (WI-3
   §21).
9. Resolve/reuse/run the `DOCUMENT_TYPE_PROPOSAL` v2 AI invocation —
   idempotency/reuse (§25-26), active-invocation guard (§28), and
   prior-failure guard (§27), all against
   `ai.invocation.AIInvocationRepository`; the actual model call, when
   genuinely new, goes through `ai.gateway.background.run_background_task`
   (WI-2's own gateway) — never reimplemented here either.
10. Validate output — `run_background_task` already performs full
    JSON/schema validation; this module only interprets the ALREADY
    validated `SUCCEEDED` output.
11. Optionally persist the AI proposal, depending on `persist` (WI-3
    §32-38) — the SAME function backs both the preview endpoint
    (`persist=False`, WI-3 §39) and the orchestrated endpoint
    (`persist=True`, WI-3 §40), sharing every step above verbatim so
    the deterministic-first/context/fingerprint/reuse logic is never
    duplicated.

Dependency-injected, not composition-coupled
------------------------------------------------------------------------
Every dependency (`evidence_repository`, `rule_repository`,
`classification_repository`, `ai_invocation_repository`,
`litellm_client`, `object_store`, `audit_repository`,
`record_audit_event`) is a plain constructor/call parameter — this
module never imports `app.api.composition`, mirroring
`ai.gateway.background.run_background_task`'s own documented
dependency-injection discipline. Only
`app/api/routers/evidence_classification.py` (the HTTP layer) unpacks
`get_composition()` onto these parameters.

Never supersedes, never mutates EvidenceItem, never touches entity
ownership (WI-3 §31/§43)
------------------------------------------------------------------------
This module never calls `EvidenceClassificationRepository.create_classification`
with a non-null `supersedes_classification_id`, never calls
`EvidenceRepository.update_status`/`assign_entity`, and never
references the separate entity-ownership-hint AI task at all —
document classification and entity ownership remain independent
authorities.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Mapping, Optional

from ai.gateway.background import run_background_task
from ai.invocation import TERMINAL_STATUSES
from ai.prompts.loader import resolve_prompt_contract_version
from ai.tasks import get_task_contract
from core.errors import ConflictError
from services.evidence.classification import (
    CLASSIFICATION_TYPE_DOCUMENT_TYPE,
    DOCUMENT_TYPE_UNKNOWN,
    SOURCE_AI_PROPOSAL,
    SOURCE_DETERMINISTIC_RULE,
    STATUS_REVIEW_REQUIRED,
    STATUS_UNCLASSIFIABLE,
    EvidenceClassification,
)
from services.evidence.classification_ai_fingerprint import compute_classifier_fingerprint
from services.evidence.classification_context import (
    OUTCOME_BUILT as _CONTEXT_OUTCOME_BUILT,
    build_evidence_classification_context,
)
from services.evidence.classification_matcher import (
    OUTCOME_CONFLICT as _MATCHER_OUTCOME_CONFLICT,
    OUTCOME_MATCH as _MATCHER_OUTCOME_MATCH,
    match_evidence_to_rule,
)
from services.evidence.classification_rule import RULE_STATUS_ACTIVE
from services.evidence.classification_service import (
    OUTCOME_CLASSIFIED as _DET_OUTCOME_CLASSIFIED,
    OUTCOME_CONFLICT as _DET_OUTCOME_CONFLICT,
    OUTCOME_CURRENT_CLASSIFICATION_EXISTS as _DET_OUTCOME_CURRENT_CLASSIFICATION_EXISTS,
    OUTCOME_EXISTING as _DET_OUTCOME_EXISTING,
    DeterministicClassificationResult,
    classify_evidence_deterministically,
)

#: The exact `(task_id, task_version)` this orchestrator governs (WI-3
#: §4) — never a caller-supplied parameter; the governed classification
#: surface is deliberately not generic (see WI-3 §22 and
#: `app/api/routers/ai.py`'s own generic-path rejection of this exact
#: pair).
AI_TASK_ID = "DOCUMENT_TYPE_PROPOSAL"
AI_TASK_VERSION = 2

#: Bounded, system-owned reason codes (WI-3 §34) — never arbitrary
#: model prose. The complete model signals/warnings remain on
#: `AIInvocation.output`, never copied into `EvidenceClassification
#: .reason_codes`.
REASON_CODE_AI_PROPOSAL_PENDING_REVIEW = "AI_PROPOSAL_PENDING_REVIEW"
REASON_CODE_AI_PROPOSAL_UNKNOWN = "AI_PROPOSAL_UNKNOWN"
REASON_CODE_CONTEXT_BODY_TRUNCATED = "CONTEXT_BODY_TRUNCATED"
REASON_CODE_ATTACHMENT_CONTENT_NOT_EXTRACTED = "ATTACHMENT_CONTENT_NOT_EXTRACTED"

#: The closed set of outcomes :func:`classify_evidence` may return —
#: see that function's own docstring for what each one means.
OUTCOME_CURRENT_CLASSIFICATION_EXISTS = "CURRENT_CLASSIFICATION_EXISTS"
OUTCOME_DETERMINISTIC_CLASSIFIED = "DETERMINISTIC_CLASSIFIED"
OUTCOME_DETERMINISTIC_EXISTING = "DETERMINISTIC_EXISTING"
OUTCOME_DETERMINISTIC_CONFLICT = "DETERMINISTIC_CONFLICT"
OUTCOME_CONTEXT_UNSUPPORTED = "CONTEXT_UNSUPPORTED"
OUTCOME_AI_IN_PROGRESS = "AI_IN_PROGRESS"
OUTCOME_AI_PRIOR_FAILURE = "AI_PRIOR_FAILURE"
OUTCOME_AI_INVOCATION_FAILED = "AI_INVOCATION_FAILED"
OUTCOME_AI_PROPOSAL_REVIEW_REQUIRED = "AI_PROPOSAL_REVIEW_REQUIRED"
OUTCOME_AI_PROPOSAL_UNCLASSIFIABLE = "AI_PROPOSAL_UNCLASSIFIABLE"

CLASSIFY_EVIDENCE_OUTCOMES = frozenset(
    {
        OUTCOME_CURRENT_CLASSIFICATION_EXISTS,
        OUTCOME_DETERMINISTIC_CLASSIFIED,
        OUTCOME_DETERMINISTIC_EXISTING,
        OUTCOME_DETERMINISTIC_CONFLICT,
        OUTCOME_CONTEXT_UNSUPPORTED,
        OUTCOME_AI_IN_PROGRESS,
        OUTCOME_AI_PRIOR_FAILURE,
        OUTCOME_AI_INVOCATION_FAILED,
        OUTCOME_AI_PROPOSAL_REVIEW_REQUIRED,
        OUTCOME_AI_PROPOSAL_UNCLASSIFIABLE,
    }
)

#: Terminal AIInvocation statuses that represent a genuine prior
#: failure (WI-3 §27) — every terminal status except SUCCEEDED.
_FAILURE_TERMINAL_STATUSES = TERMINAL_STATUSES - {"SUCCEEDED"}


@dataclass(frozen=True)
class ClassifyEvidenceResult:
    """Result of :func:`classify_evidence` — one of
    :data:`CLASSIFY_EVIDENCE_OUTCOMES`. Fields are populated according
    to which outcome this is; see :func:`classify_evidence`'s own
    docstring for the full outcome-by-outcome contract."""

    outcome: str
    #: The current/deterministic/AI EvidenceClassification most
    #: relevant to this outcome (populated for
    #: CURRENT_CLASSIFICATION_EXISTS, DETERMINISTIC_CLASSIFIED,
    #: DETERMINISTIC_EXISTING, and AI_PROPOSAL_* when `persist=True`).
    classification: Optional[EvidenceClassification] = None
    #: True if THIS call caused a genuinely new EvidenceClassification
    #: row to be written (only meaningful when `classification` is
    #: populated under `persist=True`); False for a replay/reuse; None
    #: when no persistence was attempted at all (`persist=False`, or an
    #: outcome that never reaches persistence).
    was_created: Optional[bool] = None
    deterministic_outcome: Optional[str] = None
    matched_rule_id: Optional[str] = None
    conflicting_rule_ids: tuple[str, ...] = field(default_factory=tuple)
    existing_classification_id: Optional[str] = None
    existing_source: Optional[str] = None
    existing_document_type: Optional[str] = None
    unsupported_reason: Optional[str] = None
    ai_invocation_id: Optional[str] = None
    error_code: Optional[str] = None
    proposed_type: Optional[str] = None
    confidence: Optional[float] = None
    signals: tuple[str, ...] = field(default_factory=tuple)
    warnings: tuple[str, ...] = field(default_factory=tuple)
    classifier_fingerprint: Optional[str] = None
    classification_context_version: Optional[str] = None

    def __post_init__(self) -> None:
        if self.outcome not in CLASSIFY_EVIDENCE_OUTCOMES:
            raise ValueError(f"'{self.outcome}' is not one of {sorted(CLASSIFY_EVIDENCE_OUTCOMES)}")


def _preview_deterministic_outcome(*, evidence, rule_repository, classification_repository) -> DeterministicClassificationResult:
    """Non-mutating mirror of
    `services.evidence.classification_service.classify_evidence_deterministically`'s
    own outcome determination — reuses the SAME matcher WI-2's service
    uses (`match_evidence_to_rule`, never a fourth reimplementation of
    matching/precedence logic), but NEVER calls
    `classification_repository.create_classification`.

    Used ONLY by `classify_evidence`'s `persist=False` path (the
    `ai-preview` endpoint, WI-3 §39, which must create NO
    `EvidenceClassification` row regardless of outcome). The real,
    persisting `classify_evidence_deterministically` remains the sole
    write path, used only when `persist=True` (WI-3 §40) — a defect
    found in independent PL review: an earlier version of this module
    called the real (writing) function unconditionally, which meant the
    'preview, never mutates' endpoint could silently create a genuine
    deterministic `EvidenceClassification` row whenever a real ACTIVE
    rule happened to match. Every non-CLASSIFIED outcome below
    (`EXISTING`/`CONFLICT`/`CURRENT_CLASSIFICATION_EXISTS`/`NO_MATCH`/
    `NO_APPLICABLE_RULE_INPUT`) is already a pure read in WI-2's own
    function too — this helper exists purely to keep the one genuinely
    write-capable case (`CLASSIFIED`) from ever being reached without
    `persist=True`; `classify_evidence` itself still gates
    `was_created`/`classification` on `persist` for that case (see
    below), never trusting this helper alone to be the only safeguard.
    """
    sender_address = evidence.metadata.get("sender_address")
    subject = evidence.metadata.get("subject")
    active_rules = rule_repository.list_rules(status=RULE_STATUS_ACTIVE)
    result = match_evidence_to_rule(sender_address=sender_address, subject=subject, active_rules=active_rules)

    if result.outcome == "NO_APPLICABLE_RULE_INPUT":
        return DeterministicClassificationResult(outcome="NO_APPLICABLE_RULE_INPUT")
    if result.outcome == "NO_MATCH":
        return DeterministicClassificationResult(outcome="NO_MATCH")
    if result.outcome == _MATCHER_OUTCOME_CONFLICT:
        return DeterministicClassificationResult(
            outcome=_DET_OUTCOME_CONFLICT, conflicting_rule_ids=result.conflicting_rule_ids
        )

    assert result.outcome == _MATCHER_OUTCOME_MATCH  # noqa: S101 - exhaustive outcome switch

    current = classification_repository.get_current_classification(
        evidence.evidence_id, CLASSIFICATION_TYPE_DOCUMENT_TYPE
    )
    if current is None:
        # A real rule WOULD classify this evidence — but this is the
        # non-persisting preview path, so NOTHING is written and no
        # `classification` object exists to return. `matched_rule_id`
        # alone communicates "a deterministic match exists" to the caller.
        return DeterministicClassificationResult(
            outcome=_DET_OUTCOME_CLASSIFIED, classification=None, matched_rule_id=result.rule_id,
            matching_tier=result.matching_tier,
        )
    if current.source == SOURCE_DETERMINISTIC_RULE and current.rule_id == result.rule_id:
        return DeterministicClassificationResult(
            outcome=_DET_OUTCOME_EXISTING, classification=current, matched_rule_id=result.rule_id,
            matching_tier=result.matching_tier,
        )
    return DeterministicClassificationResult(
        outcome=_DET_OUTCOME_CURRENT_CLASSIFICATION_EXISTS,
        existing_classification_id=current.classification_id,
        existing_source=current.source,
        existing_document_type=current.document_type,
    )


def _evidence_content_hash(evidence) -> str:
    content_hash = evidence.content_hash
    if isinstance(content_hash, Mapping):
        return str(content_hash.get("value"))
    return str(content_hash)


def classify_evidence(
    *,
    evidence_id: str,
    persist: bool,
    evidence_repository,
    rule_repository,
    classification_repository,
    ai_invocation_repository,
    litellm_client,
    object_store,
    audit_repository,
    record_audit_event,
    actor_type: str,
    actor_id: str,
    correlation_id: Optional[str] = None,
) -> ClassifyEvidenceResult:
    """Run the full deterministic-first, AI-fallback classification
    sequence for one `evidence_id` (WI-3 §29). See module docstring for
    the numbered sequence.

    `persist=False` (the preview endpoint, WI-3 §39): builds context,
    resolves/reuses/runs the AI invocation, and returns the proposal —
    creates NO `EvidenceClassification` row. `persist=True` (the
    orchestrated endpoint, WI-3 §40): additionally persists the AI
    proposal per WI-3 §32-38.

    Raises:
        core.errors.NotFoundError: no such `evidence_id`.
    """
    evidence = evidence_repository.get_evidence(evidence_id)

    # Steps 1-2 — the current-classification guard (WI-3 §31/§38). Runs
    # BEFORE the deterministic classifier — see module docstring for why.
    current = classification_repository.get_current_classification(evidence_id, CLASSIFICATION_TYPE_DOCUMENT_TYPE)
    if current is not None:
        return ClassifyEvidenceResult(
            outcome=OUTCOME_CURRENT_CLASSIFICATION_EXISTS,
            classification=current,
            existing_classification_id=current.classification_id,
            existing_source=current.source,
            existing_document_type=current.document_type,
        )

    # Steps 3-6 — deterministic-first (WI-2's own classifier, never
    # reimplemented). `persist=False` (the ai-preview endpoint, WI-3
    # §39) MUST create no EvidenceClassification row regardless of
    # outcome — classify_evidence_deterministically itself WRITES on
    # its own CLASSIFIED outcome, so only `persist=True` may call the
    # real (writing) function; `persist=False` uses the non-mutating
    # `_preview_deterministic_outcome` mirror instead (every other
    # outcome — EXISTING/CONFLICT/CURRENT_CLASSIFICATION_EXISTS/
    # NO_MATCH/NO_APPLICABLE_RULE_INPUT — is already a pure read in
    # WI-2's own function too, so both variants agree on those).
    if persist:
        det_result = classify_evidence_deterministically(
            evidence_id=evidence_id,
            evidence_repository=evidence_repository,
            rule_repository=rule_repository,
            classification_repository=classification_repository,
            audit_repository=audit_repository,
            actor_type=actor_type,
            actor_id=actor_id,
        )
    else:
        det_result = _preview_deterministic_outcome(
            evidence=evidence, rule_repository=rule_repository, classification_repository=classification_repository,
        )

    if det_result.outcome == _DET_OUTCOME_CLASSIFIED:
        return ClassifyEvidenceResult(
            outcome=OUTCOME_DETERMINISTIC_CLASSIFIED,
            # `det_result.classification`/`was_created` are only ever
            # non-None/True when `persist=True` (the real, writing path
            # was used) — `_preview_deterministic_outcome` always
            # returns `classification=None` for this outcome, and
            # `persist` is False exactly when that helper was used, so
            # gating directly on `persist` here is equivalent to (and
            # clearer than) re-deriving it from `det_result` itself.
            classification=det_result.classification,
            was_created=persist,
            matched_rule_id=det_result.matched_rule_id,
            deterministic_outcome=det_result.outcome,
        )
    if det_result.outcome == _DET_OUTCOME_EXISTING:
        return ClassifyEvidenceResult(
            outcome=OUTCOME_DETERMINISTIC_EXISTING,
            classification=det_result.classification,
            was_created=False,
            matched_rule_id=det_result.matched_rule_id,
            deterministic_outcome=det_result.outcome,
        )
    if det_result.outcome == _DET_OUTCOME_CONFLICT:
        # WI-3 §30 — AI must never arbitrate a deterministic conflict.
        return ClassifyEvidenceResult(
            outcome=OUTCOME_DETERMINISTIC_CONFLICT,
            conflicting_rule_ids=det_result.conflicting_rule_ids,
            deterministic_outcome=det_result.outcome,
        )
    if det_result.outcome == _DET_OUTCOME_CURRENT_CLASSIFICATION_EXISTS:
        return ClassifyEvidenceResult(
            outcome=OUTCOME_CURRENT_CLASSIFICATION_EXISTS,
            existing_classification_id=det_result.existing_classification_id,
            existing_source=det_result.existing_source,
            existing_document_type=det_result.existing_document_type,
            deterministic_outcome=det_result.outcome,
        )

    # Only NO_MATCH / NO_APPLICABLE_RULE_INPUT reach here (step 7).
    assert det_result.outcome in ("NO_MATCH", "NO_APPLICABLE_RULE_INPUT")  # noqa: S101 - documents the exhaustive switch

    # Step 7-8 — build the bounded AI context.
    if not evidence.storage_reference:
        return ClassifyEvidenceResult(
            outcome=OUTCOME_CONTEXT_UNSUPPORTED,
            unsupported_reason="EvidenceItem has no stored content (storage_reference is empty)",
            deterministic_outcome=det_result.outcome,
        )
    raw_content = object_store.get(evidence.storage_reference)
    context_result = build_evidence_classification_context(evidence=evidence, raw_content=raw_content)
    if context_result.outcome != _CONTEXT_OUTCOME_BUILT:
        return ClassifyEvidenceResult(
            outcome=OUTCOME_CONTEXT_UNSUPPORTED,
            unsupported_reason=context_result.unsupported_reason,
            deterministic_outcome=det_result.outcome,
        )
    context = context_result.context
    assert context is not None  # noqa: S101 - guaranteed by OUTCOME_BUILT

    # Step 9 — resolve task contract / prompt version / fingerprint.
    task_contract = get_task_contract(AI_TASK_ID, AI_TASK_VERSION)
    prompt_contract_version = resolve_prompt_contract_version(AI_TASK_ID, AI_TASK_VERSION)
    evidence_content_hash = _evidence_content_hash(evidence)
    fingerprint = compute_classifier_fingerprint(
        task_id=AI_TASK_ID,
        task_version=AI_TASK_VERSION,
        prompt_contract_version=prompt_contract_version,
        preferred_capability=str(task_contract.preferred_capability),
        classification_context_version=context.context_contract_version,
        evidence_id=evidence_id,
        evidence_content_hash=evidence_content_hash,
        classification_context_hash=context.context_sha256,
    )
    input_references: dict[str, Any] = {
        "evidence_id": evidence_id,
        "evidence_content_hash": evidence_content_hash,
        "classification_context_version": context.context_contract_version,
        "classification_context_hash": context.context_sha256,
        "classifier_fingerprint": fingerprint,
    }

    # §25 — automatic-invocation idempotency: search prior invocations
    # for this exact subject, filtered client-side by fingerprint (no
    # fingerprint filter param exists on list_invocations — see
    # ai.invocation.AIInvocationRepository.list_invocations's own
    # docstring).
    prior_candidates = ai_invocation_repository.list_invocations(
        task_id=AI_TASK_ID, task_version=AI_TASK_VERSION, primary_input_reference=evidence_id,
    )
    matching_fingerprint = [
        inv for inv in prior_candidates if inv.input_references.get("classifier_fingerprint") == fingerprint
    ]
    succeeded = next((inv for inv in matching_fingerprint if inv.status == "SUCCEEDED"), None)

    if succeeded is not None:
        # §25/§26 — reuse; no second model call, including the
        # crash-recovery case (AIInvocation SUCCEEDED but the
        # EvidenceClassification write never happened).
        invocation = succeeded
    else:
        # §28 — active-invocation guard (subject-scoped, not
        # fingerprint-scoped: at most one non-terminal invocation can
        # ever exist for this (task_id, task_version, evidence_id)
        # subject at all, by ai.invocation's own concurrency guard).
        active = ai_invocation_repository.find_active_invocation(
            task_id=AI_TASK_ID, task_version=AI_TASK_VERSION, primary_input_reference=evidence_id,
        )
        if active is not None:
            return ClassifyEvidenceResult(
                outcome=OUTCOME_AI_IN_PROGRESS,
                ai_invocation_id=active.ai_invocation_id,
                classifier_fingerprint=fingerprint,
                classification_context_version=context.context_contract_version,
                deterministic_outcome=det_result.outcome,
            )

        # §27 — do not silently retry a matching FAILED/TIMED_OUT/
        # REJECTED/CANCELLED invocation for this exact fingerprint.
        prior_failed = next(
            (inv for inv in matching_fingerprint if inv.status in _FAILURE_TERMINAL_STATUSES), None
        )
        if prior_failed is not None:
            return ClassifyEvidenceResult(
                outcome=OUTCOME_AI_PRIOR_FAILURE,
                ai_invocation_id=prior_failed.ai_invocation_id,
                error_code=prior_failed.error_code,
                classifier_fingerprint=fingerprint,
                classification_context_version=context.context_contract_version,
                deterministic_outcome=det_result.outcome,
            )

        # A genuinely new call — the actual model invocation, via
        # WI-2's own gateway (never reimplemented here).
        invocation = run_background_task(
            task_id=AI_TASK_ID,
            task_version=AI_TASK_VERSION,
            input_references=input_references,
            evidence_content=context.rendered_context,
            actor_type=actor_type,
            actor_id=actor_id,
            correlation_id=correlation_id,
            repository=ai_invocation_repository,
            litellm_client=litellm_client,
            record_audit_event=record_audit_event,
        )

    if invocation.status != "SUCCEEDED":
        return ClassifyEvidenceResult(
            outcome=OUTCOME_AI_INVOCATION_FAILED,
            ai_invocation_id=invocation.ai_invocation_id,
            error_code=invocation.error_code,
            classifier_fingerprint=fingerprint,
            classification_context_version=context.context_contract_version,
            deterministic_outcome=det_result.outcome,
        )

    # Step 10 — output was already schema-validated by
    # run_background_task; this only interprets the validated shape.
    output: Mapping[str, Any] = invocation.output or {}
    proposed_type = output.get("proposed_type")
    confidence = output.get("confidence")
    signals = tuple(output.get("signals") or ())
    warnings = tuple(output.get("warnings") or ())
    is_unknown = proposed_type == DOCUMENT_TYPE_UNKNOWN

    result_outcome = OUTCOME_AI_PROPOSAL_UNCLASSIFIABLE if is_unknown else OUTCOME_AI_PROPOSAL_REVIEW_REQUIRED

    if not persist:
        return ClassifyEvidenceResult(
            outcome=result_outcome,
            ai_invocation_id=invocation.ai_invocation_id,
            proposed_type=proposed_type,
            confidence=confidence,
            signals=signals,
            warnings=warnings,
            classifier_fingerprint=fingerprint,
            classification_context_version=context.context_contract_version,
            deterministic_outcome=det_result.outcome,
        )

    # Step 11 — persist (WI-3 §32-38).
    reason_codes = [REASON_CODE_AI_PROPOSAL_UNKNOWN if is_unknown else REASON_CODE_AI_PROPOSAL_PENDING_REVIEW]
    if context.body_truncated:
        reason_codes.append(REASON_CODE_CONTEXT_BODY_TRUNCATED)
    if context.attachment_content_not_extracted and context.attachment_count > 0:
        reason_codes.append(REASON_CODE_ATTACHMENT_CONTENT_NOT_EXTRACTED)

    existing_ids_before = {
        c.classification_id
        for c in classification_repository.list_classification_history(evidence_id, CLASSIFICATION_TYPE_DOCUMENT_TYPE)
    }

    try:
        created = classification_repository.create_classification(
            evidence_id=evidence_id,
            classification_type=CLASSIFICATION_TYPE_DOCUMENT_TYPE,
            document_type=DOCUMENT_TYPE_UNKNOWN if is_unknown else proposed_type,
            status=STATUS_UNCLASSIFIABLE if is_unknown else STATUS_REVIEW_REQUIRED,
            source=SOURCE_AI_PROPOSAL,
            confidence=confidence,
            rule_id=None,
            ai_invocation_id=invocation.ai_invocation_id,
            operator_action_id=None,
            reason_codes=reason_codes,
            supersedes_classification_id=None,
            expected_current_classification_id=None,
        )
    except ConflictError:
        # §38 — a race: another producer established current truth
        # while the model was running. The AI result must never
        # supersede it; report CURRENT_CLASSIFICATION_EXISTS honestly.
        recheck = classification_repository.get_current_classification(evidence_id, CLASSIFICATION_TYPE_DOCUMENT_TYPE)
        return ClassifyEvidenceResult(
            outcome=OUTCOME_CURRENT_CLASSIFICATION_EXISTS,
            classification=recheck,
            existing_classification_id=recheck.classification_id if recheck else None,
            existing_source=recheck.source if recheck else None,
            existing_document_type=recheck.document_type if recheck else None,
            ai_invocation_id=invocation.ai_invocation_id,
            classifier_fingerprint=fingerprint,
            classification_context_version=context.context_contract_version,
            deterministic_outcome=det_result.outcome,
        )

    was_created = created.classification_id not in existing_ids_before

    if was_created:
        # §35 — bounded AI audit event, only for a genuinely NEW row
        # (§37 — an ordinary producer-identity replay must never
        # double-audit).
        record_audit_event(
            event_type="EVIDENCE_CLASSIFIED",
            actor_type=actor_type,
            actor_id=actor_id,
            subject_type="EvidenceClassification",
            subject_id=created.classification_id,
            correlation_id=correlation_id or created.classification_id,
            causation_id=None,
            payload={
                "evidence_id": evidence_id,
                "ai_invocation_id": invocation.ai_invocation_id,
                "document_type": created.document_type,
                "status": created.status,
                "confidence": created.confidence,
                "classification_context_version": context.context_contract_version,
            },
        )

    return ClassifyEvidenceResult(
        outcome=result_outcome,
        classification=created,
        was_created=was_created,
        ai_invocation_id=invocation.ai_invocation_id,
        proposed_type=proposed_type,
        confidence=confidence,
        signals=signals,
        warnings=warnings,
        classifier_fingerprint=fingerprint,
        classification_context_version=context.context_contract_version,
        deterministic_outcome=det_result.outcome,
    )
