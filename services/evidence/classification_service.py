"""Deterministic (non-AI) evidence classification service (CD-6 Slice 5
WI-2) — the business logic behind
``POST /internal/evidence/{evidence_id}/classifications/deterministic``
(``app/api/routers/evidence_classification.py``, kept thin).

Manually invoked only — never wired into any automatic path
------------------------------------------------------------------------
This module, and the endpoint that calls it, are an explicit,
manually-invoked surface only (WI-2's own bounded testing/operations).
Nothing under ``services/mailbox/``, ``services/evidence/intake/``, or
``app/api/routers/intake.py`` calls anything in this module — see
``tests/integration/test_architecture_boundaries.py``'s WI-2 additions
for the grep-based proof. There is no bulk/"classify-all" endpoint and
no historical-backfill mechanism.

Transaction/audit boundary — a known, pre-existing limitation
------------------------------------------------------------------------
Exactly the same sequential, non-atomic write-then-audit pattern
``services.evidence.classification_rule_service`` documents (see that
module's own docstring) — ``PostgresAuditRepository.record_audit_event``
is not composable into an external caller's transaction. The
classification row is written first (durable, producer-identity
idempotent); the audit event is emitted second.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional

from services.evidence.classification import (
    CLASSIFICATION_TYPE_DOCUMENT_TYPE,
    DOCUMENT_TYPE_UNKNOWN,
    SOURCE_DETERMINISTIC_RULE,
    STATUS_CLASSIFIED,
    STATUS_UNCLASSIFIABLE,
    EvidenceClassification,
)
from services.evidence.classification_matcher import (
    OUTCOME_CONFLICT,
    OUTCOME_MATCH,
    OUTCOME_NO_APPLICABLE_RULE_INPUT,
    OUTCOME_NO_MATCH,
    match_evidence_to_rule,
)
from services.evidence.classification_rule import RULE_STATUS_ACTIVE

#: The closed set of outcomes `DeterministicClassificationResult` may
#: carry — see that class's own docstring for what each one means.
OUTCOME_CLASSIFIED = "CLASSIFIED"
OUTCOME_EXISTING = "EXISTING"
OUTCOME_CURRENT_CLASSIFICATION_EXISTS = "CURRENT_CLASSIFICATION_EXISTS"
DETERMINISTIC_OUTCOMES = frozenset(
    {
        OUTCOME_CLASSIFIED,
        OUTCOME_EXISTING,
        OUTCOME_NO_MATCH,
        OUTCOME_NO_APPLICABLE_RULE_INPUT,
        OUTCOME_CURRENT_CLASSIFICATION_EXISTS,
        OUTCOME_CONFLICT,
    }
)


@dataclass(frozen=True)
class DeterministicClassificationResult:
    """Result of :func:`classify_evidence_deterministically` — one of
    six mutually-exclusive, real, typed outcomes:

    * ``CLASSIFIED`` — a genuinely NEW ``EvidenceClassification`` row was
      created. ``classification``/``matched_rule_id``/``matching_tier``
      populated.
    * ``EXISTING`` — an exact same-rule replay: the current
      classification was ALREADY produced by this exact rule.
      ``classification``/``matched_rule_id``/``matching_tier``
      populated; nothing was written.
    * ``NO_MATCH`` — sender/subject were eligible but no ACTIVE rule
      matched. Nothing written.
    * ``NO_APPLICABLE_RULE_INPUT`` — evidence has no usable
      sender_address/subject metadata at all. Nothing written.
    * ``CURRENT_CLASSIFICATION_EXISTS`` — a rule matched, but a DIFFERENT
      producer already holds the current classification for this
      evidence — ``existing_classification_id``/``existing_source``/
      ``existing_document_type`` populated so the caller can see what is
      blocking it. Nothing written (WI-2 never supersedes).
    * ``CONFLICT`` — two or more ACTIVE rules are equally authoritative
      (see ``services.evidence.classification_matcher``).
      ``conflicting_rule_ids`` populated. Nothing written.
    """

    outcome: str
    classification: Optional[EvidenceClassification] = None
    matched_rule_id: Optional[str] = None
    matching_tier: Optional[str] = None
    conflicting_rule_ids: tuple[str, ...] = field(default_factory=tuple)
    existing_classification_id: Optional[str] = None
    existing_source: Optional[str] = None
    existing_document_type: Optional[str] = None

    def __post_init__(self) -> None:
        if self.outcome not in DETERMINISTIC_OUTCOMES:
            raise ValueError(f"'{self.outcome}' is not one of {sorted(DETERMINISTIC_OUTCOMES)}")


def classify_evidence_deterministically(
    *,
    evidence_id: str,
    evidence_repository,
    rule_repository,
    classification_repository,
    audit_repository,
    actor_type: str,
    actor_id: str,
) -> DeterministicClassificationResult:
    """CD-6 Slice 5 WI-2 §8. See module docstring for the transaction/
    audit-boundary caveat, and ``services.evidence.classification_matcher
    .match_evidence_to_rule`` for the precedence/ambiguity logic this
    delegates to (the SAME matcher the observed-evidence guard/preview
    service use — never a fourth reimplementation).

    ``EvidenceItem.entity_id`` is NEVER touched anywhere in this
    function — this module never calls
    ``EvidenceRepository.update_status``/``assign_entity`` (see
    ``tests/integration/test_architecture_boundaries.py``'s WI-2
    additions for the static proof, and this module's own test suite
    for a direct before/after ``entity_id`` equality proof).

    Raises:
        core.errors.NotFoundError: no such ``evidence_id``.
    """
    evidence = evidence_repository.get_evidence(evidence_id)
    sender_address = evidence.metadata.get("sender_address")
    subject = evidence.metadata.get("subject")

    active_rules = rule_repository.list_rules(status=RULE_STATUS_ACTIVE)
    result = match_evidence_to_rule(sender_address=sender_address, subject=subject, active_rules=active_rules)

    if result.outcome == OUTCOME_NO_APPLICABLE_RULE_INPUT:
        return DeterministicClassificationResult(outcome=OUTCOME_NO_APPLICABLE_RULE_INPUT)
    if result.outcome == OUTCOME_NO_MATCH:
        return DeterministicClassificationResult(outcome=OUTCOME_NO_MATCH)
    if result.outcome == OUTCOME_CONFLICT:
        return DeterministicClassificationResult(
            outcome=OUTCOME_CONFLICT, conflicting_rule_ids=result.conflicting_rule_ids
        )

    assert result.outcome == OUTCOME_MATCH  # noqa: S101 - exhaustive outcome switch, documents the invariant

    current = classification_repository.get_current_classification(evidence_id, CLASSIFICATION_TYPE_DOCUMENT_TYPE)

    if current is None:
        # A rule's own document_type is validated against the shared
        # DOCUMENT_TYPES vocabulary at rule-creation time (including
        # DOCUMENT_TYPE_UNKNOWN, which classification_rule.py does not
        # itself exclude) — mirror EvidenceClassification's own
        # status/document_type cross-field invariant here rather than
        # assuming every matched rule targets a non-UNKNOWN type: a
        # STATUS_CLASSIFIED row can never carry document_type=UNKNOWN
        # (services.evidence.classification.validate_classification_fields_or_raise
        # would reject it), so a rule that resolves to UNKNOWN produces
        # a STATUS_UNCLASSIFIABLE row instead.
        status = STATUS_UNCLASSIFIABLE if result.document_type == DOCUMENT_TYPE_UNKNOWN else STATUS_CLASSIFIED

        created = classification_repository.create_classification(
            evidence_id=evidence_id,
            classification_type=CLASSIFICATION_TYPE_DOCUMENT_TYPE,
            document_type=result.document_type,
            status=status,
            source=SOURCE_DETERMINISTIC_RULE,
            confidence=None,
            rule_id=result.rule_id,
            ai_invocation_id=None,
            operator_action_id=None,
            reason_codes=(),
            supersedes_classification_id=None,
            expected_current_classification_id=None,
        )

        # Sequential, non-atomic write-then-audit — see module docstring.
        audit_repository.record_audit_event(
            event_type="EVIDENCE_CLASSIFIED",
            actor_type=actor_type,
            actor_id=actor_id,
            subject_type="EvidenceClassification",
            subject_id=created.classification_id,
            correlation_id=created.classification_id,
            causation_id=None,
            payload={
                "evidence_id": evidence_id,
                "rule_id": result.rule_id,
                "document_type": result.document_type,
                "matching_tier": result.matching_tier,
            },
        )
        return DeterministicClassificationResult(
            outcome=OUTCOME_CLASSIFIED,
            classification=created,
            matched_rule_id=result.rule_id,
            matching_tier=result.matching_tier,
        )

    if current.source == SOURCE_DETERMINISTIC_RULE and current.rule_id == result.rule_id:
        # Exact producer-identity replay — the current row already IS
        # this exact rule's own output. Never call create_classification
        # again (redundant — WI-1's own producer-idempotency would just
        # return the same row) and never emit a second audit event.
        return DeterministicClassificationResult(
            outcome=OUTCOME_EXISTING,
            classification=current,
            matched_rule_id=result.rule_id,
            matching_tier=result.matching_tier,
        )

    # A DIFFERENT producer (different source, or the same source but a
    # different rule_id) already holds the current classification — WI-2
    # never supersedes; nothing is written.
    return DeterministicClassificationResult(
        outcome=OUTCOME_CURRENT_CLASSIFICATION_EXISTS,
        existing_classification_id=current.classification_id,
        existing_source=current.source,
        existing_document_type=current.document_type,
    )
