"""The human-control loop for AI document classification (CD-6 Slice 5
WI-4): AI_PROPOSAL classification -> ``CLASSIFICATION_REVIEW`` Needs You
item -> operator confirms/corrects -> a new ``OPERATOR_ASSIGNED``
classification that supersedes the AI proposal -> optional explicit
deterministic rule teaching.

This module owns exactly two operations:

* :func:`ensure_classification_review_item` — the PRODUCER. Given one
  ``AI_PROPOSAL`` :class:`~services.evidence.classification.EvidenceClassification`
  that requires operator review (``REVIEW_REQUIRED`` or
  ``UNCLASSIFIABLE``), ensures exactly one OPEN
  ``services.needs_you.needs_you.ITEM_TYPE_CLASSIFICATION_REVIEW``
  item exists for it — idempotent via
  ``NeedsYouRepository.create_needs_you_item``'s OWN
  ``(item_type, source_object_reference)`` dedupe mechanism (never a
  second dedupe mechanism built here).
* :func:`resolve_classification_review` — the RESOLVER. Given the
  ``NeedsYouItem`` and the operator's resolution payload
  (``{"document_type": ..., "teach_rule": ...}``), creates the new
  ``OPERATOR_ASSIGNED`` classification that supersedes the AI proposal,
  determines CONFIRMED vs CORRECTED, optionally teaches a deterministic
  rule, and emits the bounded classification audit event. This function
  does NOT itself transition the ``NeedsYouItem`` to ``RESOLVED`` — the
  caller (``app/api/routers/needs_you.py``) does that AFTER this
  function succeeds, exactly mirroring the existing
  ``ITEM_TYPE_COMPANY_REQUIRED`` branch's own division of labour (real
  business-logic mutation first, generic Needs You state transition +
  its own ``NEEDS_YOU_ITEM_RESOLVED`` audit event second).

Human authority doctrine (WI-4 §2)
------------------------------------------------------------------------
AI proposes. Operator decides. An ``AI_PROPOSAL``/``REVIEW_REQUIRED`` or
``AI_PROPOSAL``/``UNCLASSIFIABLE`` classification is never treated as
final accepted truth. Operator resolution always creates a NEW,
immutable ``OPERATOR_ASSIGNED`` classification row — the AI row is
NEVER mutated, not even on a plain confirmation (see
:func:`resolve_classification_review`'s own "Confirmation is still a
new row" doctrine, WI-4 §17).

Idempotency reuses the SAME producer-identity mechanism WI-1 already
built
------------------------------------------------------------------------
:func:`resolve_classification_review` creates the operator
classification via
``EvidenceClassificationRepository.create_classification_with_result``
with ``source=OPERATOR_ASSIGNED`` and
``operator_action_id=needs_you_item.item_id`` — WI-1's own
producer-identity replay mechanism (see
``services.evidence.classification._producer_key``) makes THIS the
entire idempotency story for "the same operator resolution submitted
twice": a retry with the same ``needs_you_item.item_id`` returns the
SAME existing row (``was_created=False``), never a duplicate, and never
re-validates ``expected_current_classification_id`` against a possibly
stale view (see WI-1's own module docstring, "Producer idempotency — a
SEPARATE concern from supersession-chain integrity").

Current-tip protection (WI-4 §21) is the SAME
``expected_current_classification_id`` mechanism WI-1 already built —
this module passes the AI proposal's own ``classification_id`` as
``expected_current_classification_id``; if some other producer has
superseded it in the meantime, ``create_classification_with_result``
raises ``core.errors.ConflictError`` on its own, which this function
lets propagate uncaught (never marking the Needs You item resolved,
never silently superseding the newer classification).

DISMISSED is rejected one layer up (this module never sees it)
------------------------------------------------------------------------
``app/api/routers/needs_you.py`` rejects
``CLASSIFICATION_REVIEW``/``DISMISSED`` with ``ValidationError`` BEFORE
calling :func:`resolve_classification_review` at all (WI-4 §26) — this
module has no DISMISSED-handling code of its own.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping, Optional

from core.errors import ValidationError
from core.text_matching import normalize_subject_for_policy
from core.timestamps import utc_now
from services.evidence.classification import (
    CLASSIFICATION_TYPE_DOCUMENT_TYPE,
    DOCUMENT_TYPE_UNKNOWN,
    DOCUMENT_TYPES,
    SOURCE_AI_PROPOSAL,
    SOURCE_OPERATOR_ASSIGNED,
    STATUS_CLASSIFIED,
    STATUS_UNCLASSIFIABLE,
    EvidenceClassification,
)
from services.evidence.classification_matcher import OUTCOME_MATCH, match_evidence_to_rule
from services.evidence.classification_rule import (
    RULE_SOURCE_OPERATOR,
    RULE_STATUS_ACTIVE,
    SENDER_SCOPE_TYPES,
    SUBJECT_PREDICATE_TYPES,
    EvidenceClassificationRule,
    normalize_sender_scope_value,
)
from services.evidence.classification_rule_service import create_classification_rule
from services.needs_you.needs_you import (
    ALLOWED_ACTION_CLASSIFICATION_REVIEW,
    ITEM_TYPE_CLASSIFICATION_REVIEW,
    NeedsYouItem,
)

#: WI-4 §10 — the fixed ``domain`` every ``CLASSIFICATION_REVIEW`` item
#: carries (the field is an open string in the Needs You contract, but
#: this is the one real value this producer ever uses).
DOMAIN_EVIDENCE = "EVIDENCE"

#: WI-4 §23 — bounded, system-owned classification-resolution audit
#: event types (never arbitrary operator/model prose).
EVIDENCE_CLASSIFICATION_CONFIRMED = "EVIDENCE_CLASSIFICATION_CONFIRMED"
EVIDENCE_CLASSIFICATION_CORRECTED = "EVIDENCE_CLASSIFICATION_CORRECTED"

#: WI-4 §15 — the two possible resolution decisions.
DECISION_CONFIRMED = "CONFIRMED"
DECISION_CORRECTED = "CORRECTED"


def _review_question(*, proposed_type: str, is_unknown: bool) -> str:
    if is_unknown:
        return (
            "BAGMAN could not determine this document type. Choose the correct "
            "classification or confirm that it is unclassifiable."
        )
    return f"BAGMAN classified this document as {proposed_type}. Confirm or correct it."


def ensure_classification_review_item(
    *,
    classification: EvidenceClassification,
    evidence,
    ai_invocation_id: str,
    needs_you_repository,
    correlation_id: Optional[str] = None,
) -> NeedsYouItem:
    """WI-4 §5/§7/§8/§10/§11/§12/§39 — ensure exactly one OPEN
    ``CLASSIFICATION_REVIEW`` Needs You item exists for ``classification``
    (an ``AI_PROPOSAL`` classification requiring review — concrete
    ``REVIEW_REQUIRED`` or ``UNCLASSIFIABLE``, both need a review item
    per WI-4 §5).

    Idempotent via ``NeedsYouRepository.create_needs_you_item``'s own
    ``(item_type, source_object_reference)`` dedupe — calling this
    twice for the SAME ``classification.classification_id`` returns the
    SAME item, never a second row (this is the crash-recovery mechanism
    WI-4 §8 requires: a retry after "classification committed, process
    died before the review item was created" simply calls this again).

    ``source_object_reference`` = ``classification.classification_id``
    (WI-4 §4 — one review item anchored to ONE AI
    EvidenceClassification; never deduped merely by ``evidence_id``, so
    a genuinely new future proposal for the same evidence after a
    deliberate reprocessing cycle gets its OWN review item).

    Metadata is bounded (WI-4 §11): ``evidence_id``,
    ``ai_classification_id``, ``ai_invocation_id``, ``proposed_type``,
    ``confidence``, and — only when already present on the reviewed
    ``EvidenceItem``'s own canonical metadata — ``sender_address``/
    ``subject``. Never body, MIME, or attachment content.
    """
    is_unknown = classification.status == STATUS_UNCLASSIFIABLE
    proposed_type = classification.document_type

    metadata: dict[str, Any] = {
        "evidence_id": classification.evidence_id,
        "ai_classification_id": classification.classification_id,
        "ai_invocation_id": ai_invocation_id,
        "proposed_type": proposed_type,
        "confidence": classification.confidence,
    }
    sender_address = evidence.metadata.get("sender_address") if evidence is not None else None
    subject = evidence.metadata.get("subject") if evidence is not None else None
    if sender_address:
        metadata["sender_address"] = sender_address
    if subject:
        metadata["subject"] = subject

    return needs_you_repository.create_needs_you_item(
        item_type=ITEM_TYPE_CLASSIFICATION_REVIEW,
        domain=DOMAIN_EVIDENCE,
        question=_review_question(proposed_type=proposed_type, is_unknown=is_unknown),
        allowed_action_type=ALLOWED_ACTION_CLASSIFICATION_REVIEW,
        correlation_id=correlation_id or classification.classification_id,
        source_object_reference=classification.classification_id,
        metadata=metadata,
    )


@dataclass(frozen=True)
class ClassificationReviewResolutionResult:
    """Result of :func:`resolve_classification_review`."""

    #: The (possibly replayed) ``OPERATOR_ASSIGNED`` classification.
    classification: EvidenceClassification
    #: True only if THIS call genuinely created the operator
    #: classification row (WI-4 §20 — repository-authoritative, never
    #: inferred).
    classification_was_created: bool
    #: ``DECISION_CONFIRMED`` or ``DECISION_CORRECTED`` (WI-4 §15).
    decision: str
    #: The taught rule, if `resolution["teach_rule"]` was supplied and
    #: teaching succeeded (replay-safe — may be an existing row).
    rule: Optional[EvidenceClassificationRule] = None
    #: True only if THIS call genuinely created the rule row; ``None``
    #: when no rule teaching was requested at all.
    rule_was_created: Optional[bool] = None


def _validate_resolution_or_raise(resolution: Optional[Mapping[str, Any]]) -> tuple[str, Optional[Mapping[str, Any]]]:
    """WI-4 §13/§14/§25 step 1 — validate the operator's resolution
    payload shape, BEFORE any state is touched. Returns
    ``(document_type, teach_rule)``.

    Raises:
        core.errors.ValidationError: missing/malformed ``document_type``,
            a ``document_type`` outside the closed WI-1 vocabulary, a
            malformed ``teach_rule`` shape, or a ``teach_rule`` request
            against a final ``document_type`` of ``UNKNOWN`` (WI-4 §30 —
            a deterministic rule may never be taught to UNKNOWN).
    """
    if not resolution:
        raise ValidationError(
            "a CLASSIFICATION_REVIEW resolution requires a non-empty `resolution` object carrying at "
            "least `document_type`"
        )
    document_type = resolution.get("document_type")
    if not document_type or document_type not in DOCUMENT_TYPES:
        raise ValidationError(
            f"resolution.document_type must be exactly one of {sorted(DOCUMENT_TYPES)} (got {document_type!r})"
        )

    teach_rule = resolution.get("teach_rule")
    if teach_rule is not None:
        if not isinstance(teach_rule, Mapping):
            raise ValidationError("resolution.teach_rule must be an object (or null/omitted for no teaching)")
        required_fields = (
            "sender_scope_type", "sender_scope_value", "subject_predicate_type", "subject_predicate_value",
        )
        missing = [f for f in required_fields if not teach_rule.get(f)]
        if missing:
            raise ValidationError(f"resolution.teach_rule is missing required field(s): {missing}")
        if document_type == DOCUMENT_TYPE_UNKNOWN:
            raise ValidationError(
                "resolution.teach_rule may never be supplied when the final document_type is UNKNOWN "
                "(WI-4 §30) — if the operator confirms UNKNOWN, resolve the evidence classification "
                "only, with teach_rule omitted/null"
            )

    return document_type, teach_rule


def _build_candidate_rule(
    *, sender_scope_type: str, sender_scope_value: str, subject_predicate_type: str, subject_predicate_value: str,
) -> EvidenceClassificationRule:
    """WI-4 §32 — a local, never-persisted candidate rule shaped exactly
    like ``services.evidence.classification_observation
    ._synthetic_candidate_rule`` (that helper is module-private there,
    so this is this module's own equivalent, never an import of a
    private symbol from another module). ``document_type`` is a neutral
    placeholder — irrelevant to whether ``match_evidence_to_rule``
    reports a MATCH, which is the only thing this module inspects."""
    if sender_scope_type not in SENDER_SCOPE_TYPES:
        raise ValidationError(f"'{sender_scope_type}' is not one of {sorted(SENDER_SCOPE_TYPES)}")
    if subject_predicate_type not in SUBJECT_PREDICATE_TYPES:
        raise ValidationError(f"'{subject_predicate_type}' is not one of {sorted(SUBJECT_PREDICATE_TYPES)}")

    normalized_scope_value = normalize_sender_scope_value(sender_scope_type, sender_scope_value)
    normalized_subject_value = normalize_subject_for_policy(subject_predicate_value) or ""
    if not normalized_subject_value:
        raise ValidationError("resolution.teach_rule.subject_predicate_value must normalise to a non-empty string")

    now = utc_now()
    return EvidenceClassificationRule(
        rule_id="__classification_review_synthetic_candidate__",
        sender_scope_type=sender_scope_type,
        sender_scope_value=normalized_scope_value,
        subject_predicate_type=subject_predicate_type,
        subject_predicate_value=normalized_subject_value,
        document_type=DOCUMENT_TYPE_UNKNOWN,
        status=RULE_STATUS_ACTIVE,
        source=RULE_SOURCE_OPERATOR,
        created_at=now,
        approved_at=now,
    )


def resolve_classification_review(
    *,
    needs_you_item: NeedsYouItem,
    resolution: Optional[Mapping[str, Any]],
    actor_type: str,
    actor_id: str,
    evidence_repository,
    classification_repository,
    rule_repository,
    audit_repository,
    record_audit_event,
) -> ClassificationReviewResolutionResult:
    """WI-4 §13-36/§38 — resolve one ``CLASSIFICATION_REVIEW`` Needs You
    item. Does NOT transition ``needs_you_item`` itself — the caller
    (``app/api/routers/needs_you.py``) does that AFTER this call
    succeeds (WI-4 §25 step 6, mirroring the existing
    ``COMPANY_REQUIRED`` branch's own division of labour).

    Sequence (WI-4 §25 steps 1-5, step 6 is the caller's job):

    1. Validate ``resolution`` shape (:func:`_validate_resolution_or_raise`).
    2. Prove ``needs_you_item.source_object_reference`` is a real AI
       ``EvidenceClassification`` (``classification_repository
       .get_classification`` — its own ``NotFoundError`` propagates
       honestly; a ``ValidationError`` if it is not genuinely an
       ``AI_PROPOSAL`` row).
    3. Prove it belongs to a real ``EvidenceItem``
       (``evidence_repository.get_evidence`` — its own ``NotFoundError``
       propagates honestly).
    4. Create/reuse the ``OPERATOR_ASSIGNED`` classification (WI-4
       §16/§19/§20/§21/§22) — supersedes the AI proposal;
       ``expected_current_classification_id`` = the AI proposal's own
       id, so a genuine current-tip race raises
       ``core.errors.ConflictError`` (WI-4 §21/§52), uncaught.
    5. Optionally teach a deterministic rule (WI-4 §28-36) — AFTER the
       operator classification exists (§35), so a rule-teaching failure
       never rolls back or hides the operator's own real decision.

    Raises:
        core.errors.ValidationError: malformed resolution shape, a
            ``teach_rule`` targeting UNKNOWN, or a ``teach_rule`` that
            does not match the reviewed ``EvidenceItem`` itself (§32).
        core.errors.NotFoundError: ``source_object_reference``/
            ``evidence_id`` does not resolve to a real row.
        core.errors.ConflictError: the AI proposal is no longer the
            current classification tip (§21/§52), or an ACTIVE rule
            already exists at the taught identity with a DIFFERENT
            document_type (§34/§57).
    """
    document_type, teach_rule = _validate_resolution_or_raise(resolution)

    ai_classification_id = needs_you_item.source_object_reference
    if not ai_classification_id:
        raise ValidationError(
            f"NeedsYouItem '{needs_you_item.item_id}' has no source_object_reference — there is no AI "
            "EvidenceClassification for this CLASSIFICATION_REVIEW item to resolve against"
        )
    ai_classification = classification_repository.get_classification(ai_classification_id)
    if ai_classification.source != SOURCE_AI_PROPOSAL:
        raise ValidationError(
            f"EvidenceClassification '{ai_classification_id}' referenced by NeedsYouItem "
            f"'{needs_you_item.item_id}' has source={ai_classification.source!r}, not AI_PROPOSAL — "
            "a CLASSIFICATION_REVIEW item must always anchor to a real AI proposal"
        )

    evidence = evidence_repository.get_evidence(ai_classification.evidence_id)

    is_unknown = document_type == DOCUMENT_TYPE_UNKNOWN
    decision = DECISION_CONFIRMED if document_type == ai_classification.document_type else DECISION_CORRECTED
    new_status = STATUS_UNCLASSIFIABLE if is_unknown else STATUS_CLASSIFIED

    creation_result = classification_repository.create_classification_with_result(
        evidence_id=ai_classification.evidence_id,
        classification_type=CLASSIFICATION_TYPE_DOCUMENT_TYPE,
        document_type=document_type,
        status=new_status,
        source=SOURCE_OPERATOR_ASSIGNED,
        operator_action_id=needs_you_item.item_id,
        supersedes_classification_id=ai_classification_id,
        expected_current_classification_id=ai_classification_id,
    )
    operator_classification = creation_result.classification
    classification_was_created = creation_result.was_created

    if classification_was_created:
        event_type = EVIDENCE_CLASSIFICATION_CONFIRMED if decision == DECISION_CONFIRMED else EVIDENCE_CLASSIFICATION_CORRECTED
        record_audit_event(
            event_type=event_type,
            actor_type=actor_type,
            actor_id=actor_id,
            subject_type="EvidenceClassification",
            subject_id=operator_classification.classification_id,
            correlation_id=needs_you_item.correlation_id,
            causation_id=None,
            payload={
                "evidence_id": ai_classification.evidence_id,
                "needs_you_item_id": needs_you_item.item_id,
                "previous_classification_id": ai_classification.classification_id,
                "previous_document_type": ai_classification.document_type,
                "new_classification_id": operator_classification.classification_id,
                "new_document_type": operator_classification.document_type,
            },
        )

    rule: Optional[EvidenceClassificationRule] = None
    rule_was_created: Optional[bool] = None
    if teach_rule is not None:
        # WI-4 §32 — the reviewed-evidence-specific teaching guard,
        # STRONGER than "matches some evidence somewhere": the proposed
        # rule must match THIS reviewed EvidenceItem itself, via the
        # authoritative WI-2 matcher.
        candidate_rule = _build_candidate_rule(
            sender_scope_type=teach_rule["sender_scope_type"], sender_scope_value=teach_rule["sender_scope_value"],
            subject_predicate_type=teach_rule["subject_predicate_type"],
            subject_predicate_value=teach_rule["subject_predicate_value"],
        )
        match_result = match_evidence_to_rule(
            sender_address=evidence.metadata.get("sender_address"), subject=evidence.metadata.get("subject"),
            active_rules=[candidate_rule],
        )
        if match_result.outcome != OUTCOME_MATCH:
            raise ValidationError(
                f"resolution.teach_rule (sender_scope_type={teach_rule['sender_scope_type']!r}, "
                f"sender_scope_value={teach_rule['sender_scope_value']!r}, "
                f"subject_predicate_type={teach_rule['subject_predicate_type']!r}, "
                f"subject_predicate_value={teach_rule['subject_predicate_value']!r}) does not match the "
                f"reviewed EvidenceItem '{evidence.evidence_id}' itself — refusing to teach a rule that "
                "was never proven against the very evidence the operator just reviewed"
            )

        # WI-4 §33 — the existing governed WI-2 rule-creation service:
        # observed-evidence guard, identity normalization, conflict
        # checking, repository-authoritative was_created, audit exactly
        # once. `document_type` is ALWAYS the final operator-selected
        # value (§29) — never an independently caller-supplied one.
        create_rule_result = create_classification_rule(
            sender_scope_type=teach_rule["sender_scope_type"], sender_scope_value=teach_rule["sender_scope_value"],
            subject_predicate_type=teach_rule["subject_predicate_type"],
            subject_predicate_value=teach_rule["subject_predicate_value"],
            document_type=document_type, source=RULE_SOURCE_OPERATOR,
            actor_type=actor_type, actor_id=actor_id,
            rule_repository=rule_repository, evidence_repository=evidence_repository,
            audit_repository=audit_repository,
        )
        rule = create_rule_result.rule
        rule_was_created = create_rule_result.was_created

    return ClassificationReviewResolutionResult(
        classification=operator_classification,
        classification_was_created=classification_was_created,
        decision=decision,
        rule=rule,
        rule_was_created=rule_was_created,
    )
