"""Governed create/retire operations for ``EvidenceClassificationRule``
(CD-6 Slice 5 WI-2) — the business logic behind
``POST /internal/evidence-classification/rules`` and
``POST /internal/evidence-classification/rules/{rule_id}/retire``
(``app/api/routers/evidence_classification.py``, kept thin — see that
router's own module docstring).

Why this is a separate module from ``services.evidence.classification_rule``
------------------------------------------------------------------------
``services.evidence.classification_rule`` is WI-1's own domain
model/repository module (frozen dataclass + ABC + two repository
implementations) — it stays exactly as WI-1 left it, its existing
methods/invariants unchanged. This module is WI-2's own, ADDITIVE
governed-operation layer on top of it: the observed-evidence guard
gate, audit-event emission, and idempotent-replay-vs-genuine-creation
detection a real HTTP caller needs, none of which belongs inside the
narrower repository abstraction itself.

Transaction/audit boundary — a known, pre-existing limitation, not
introduced here
------------------------------------------------------------------------
``persistence.postgres.audit_repository.PostgresAuditRepository
.record_audit_event`` opens its OWN ``session_scope`` internally — it is
NOT composable into an external caller's transaction (see that module's
own docstring). Every write in this module therefore follows
``app/api/routers/intake.py``'s own established SEQUENTIAL, non-atomic
pattern: persist the durable, idempotent canonical row FIRST, emit the
audit event SECOND. A crash between the two leaves a real, durable rule
with no matching audit row — a known, documented gap, not something
this WI introduces or is in scope to fix (see each call site's own
inline comment).
"""
from __future__ import annotations

from dataclasses import dataclass

from core.errors import InvalidStateTransitionError, ValidationError
from core.text_matching import normalize_subject_for_policy
from services.evidence.classification_observation import observed_evidence_guard
from services.evidence.classification_rule import (
    RULE_SOURCE_OPERATOR,
    RULE_STATUS_ACTIVE,
    RULE_STATUS_RETIRED,
    EvidenceClassificationRule,
    normalize_sender_scope_value,
    validate_rule_fields_or_raise,
)


@dataclass(frozen=True)
class CreateRuleResult:
    """Result of :func:`create_classification_rule`."""

    rule: EvidenceClassificationRule
    #: The observed-evidence guard's own match_count (see
    #: `services.evidence.classification_observation.observed_evidence_guard`'s
    #: own "bounded, never a full scan" doctrine) — always >= 1 (a
    #: `match_count == 0` never reaches this far; see below).
    match_count: int
    #: True only for a GENUINE first creation (an
    #: `EVIDENCE_CLASSIFICATION_RULE_CREATED` audit event was emitted).
    #: False for an exact-identity, exact-document_type replay (no new
    #: audit event — see module docstring's own idempotency doctrine).
    was_created: bool


@dataclass(frozen=True)
class RetireRuleResult:
    """Result of :func:`retire_classification_rule`."""

    rule: EvidenceClassificationRule
    #: True only when THIS call performed the ACTIVE -> RETIRED
    #: transition (an `EVIDENCE_CLASSIFICATION_RULE_RETIRED` audit event
    #: was emitted). False for a safe no-op replay against an
    #: already-RETIRED rule.
    was_retired_now: bool


def create_classification_rule(
    *,
    sender_scope_type: str,
    sender_scope_value: str,
    subject_predicate_type: str,
    subject_predicate_value: str,
    document_type: str,
    source: str,
    actor_type: str,
    actor_id: str,
    rule_repository,
    evidence_repository,
    audit_repository,
) -> CreateRuleResult:
    """Governed rule creation (CD-6 Slice 5 WI-2 §5). Sequence:

    1. Reject ``source`` outright unless it is exactly
       ``RULE_SOURCE_OPERATOR`` — in particular
       ``RULE_SOURCE_BAGMAN_PROPOSED`` is explicitly rejected (no
       producer for it exists yet; this API must never silently accept
       a value the system is not ready to honour).
    2. ``validate_rule_fields_or_raise`` (reused from
       ``services.evidence.classification_rule``, never re-implemented).
    3. Normalise the identity (``normalize_sender_scope_value`` +
       ``normalize_subject_for_policy``).
    4. Run the observed-evidence guard (§3) — ``ValidationError`` if
       ``match_count == 0``, BEFORE calling the repository at all.
    5. ``rule_repository.create_rule_with_result(...)`` — the
       DATABASE-AUTHORITATIVE creation call (CD-6 correctness delta,
       2026-09-25; see the inline comment at the call site and
       ``RuleCreationResult``'s own docstring). A same-identity/
       different-document_type ``ConflictError`` propagates as-is
       (already mapped to 409 by ``app/api/main.py``'s central error
       handler). This deliberately does NOT use a
       ``find_active_rule_at_identity`` pre-check as audit-decision
       authority — a caller-side pre-check can never be concurrency-safe
       (two genuinely concurrent identical callers could both observe
       "nothing exists yet" before either write lands, and both would
       incorrectly believe they created the rule).
    6. On genuine creation only (``creation_result.was_created is True``
       — the real DB winner, never a caller-side guess): record
       ``EVIDENCE_CLASSIFICATION_RULE_CREATED`` exactly once.

    Never touches ``EvidenceClassificationRepository`` — no
    classification row is ever created merely because a rule was
    created.
    """
    if source != RULE_SOURCE_OPERATOR:
        raise ValidationError(
            f"source must be '{RULE_SOURCE_OPERATOR}' (got {source!r}) — no producer for any other "
            "classification-rule source exists yet; this API must never silently accept a value the "
            "system is not ready to honour"
        )

    normalized_scope_value = normalize_sender_scope_value(sender_scope_type, sender_scope_value)
    normalized_subject_value = normalize_subject_for_policy(subject_predicate_value) or ""

    validate_rule_fields_or_raise(
        sender_scope_type=sender_scope_type,
        sender_scope_value=normalized_scope_value,
        subject_predicate_type=subject_predicate_type,
        subject_predicate_value=normalized_subject_value,
        document_type=document_type,
        status=RULE_STATUS_ACTIVE,
        source=source,
        retired_at=None,
    )

    match_count = observed_evidence_guard(
        sender_scope_type=sender_scope_type,
        sender_scope_value=normalized_scope_value,
        subject_predicate_type=subject_predicate_type,
        subject_predicate_value=normalized_subject_value,
        evidence_repository=evidence_repository,
    )
    if match_count == 0:
        raise ValidationError(
            f"candidate EvidenceClassificationRule identity (sender_scope_type={sender_scope_type!r}, "
            f"sender_scope_value={normalized_scope_value!r}, subject_predicate_type={subject_predicate_type!r}, "
            f"subject_predicate_value={normalized_subject_value!r}) has never matched a real, persisted "
            "EvidenceItem — refusing to create a rule with zero observed evidence"
        )

    # CD-6 correctness delta (2026-09-25): the creation-vs-replay
    # decision comes from the REPOSITORY's own database-authoritative
    # result, never a service-layer `find_active_rule_at_identity`
    # pre-check — two genuinely concurrent identical callers can both
    # observe "nothing exists yet" from a pre-check before either
    # write lands, which would make BOTH of them believe they created
    # the rule and both emit a CREATED audit event, even though the
    # database itself only ever holds one row. `create_rule_with_result`
    # resolves this the same way the Postgres implementation already
    # resolves the row itself: only the real DB-level winner (a fresh
    # insert, or the actual winner of a unique-constraint race) reports
    # `was_created=True` — see `RuleCreationResult`'s own docstring.
    creation_result = rule_repository.create_rule_with_result(
        sender_scope_type=sender_scope_type,
        sender_scope_value=sender_scope_value,
        subject_predicate_type=subject_predicate_type,
        subject_predicate_value=subject_predicate_value,
        document_type=document_type,
        source=source,
    )
    rule = creation_result.rule
    was_created = creation_result.was_created

    if was_created:
        # Sequential, non-atomic write-then-audit — see module docstring
        # ("Transaction/audit boundary") for why this is a known,
        # pre-existing PostgresAuditRepository limitation, not something
        # this WI introduces. The rule row itself is durable and
        # idempotent (producer/identity-keyed) even if the process
        # crashes before this audit event is written.
        audit_repository.record_audit_event(
            event_type="EVIDENCE_CLASSIFICATION_RULE_CREATED",
            actor_type=actor_type,
            actor_id=actor_id,
            subject_type="EvidenceClassificationRule",
            subject_id=rule.rule_id,
            correlation_id=rule.rule_id,
            causation_id=None,
            payload={
                "sender_scope_type": rule.sender_scope_type,
                "sender_scope_value": rule.sender_scope_value,
                "subject_predicate_type": rule.subject_predicate_type,
                "subject_predicate_value": rule.subject_predicate_value,
                "document_type": rule.document_type,
                "source": rule.source,
            },
        )

    return CreateRuleResult(rule=rule, match_count=match_count, was_created=was_created)


def retire_classification_rule(
    *,
    rule_id: str,
    actor_type: str,
    actor_id: str,
    rule_repository,
    audit_repository,
) -> RetireRuleResult:
    """Governed rule retirement (CD-6 Slice 5 WI-2 §6) — a strict
    one-way ``ACTIVE`` -> ``RETIRED`` transition (WI-1's own
    ``retire_rule``), made IDEMPOTENT-SAFE at this service layer:
    repeated retirement of the SAME rule is a safe no-op (returns the
    already-retired row, emits no second audit event) rather than
    letting WI-1's own ``InvalidStateTransitionError`` propagate for an
    ordinary double-submit.

    Raises:
        core.errors.NotFoundError: no such rule.
    """
    current = rule_repository.get_rule(rule_id)
    if current.status == RULE_STATUS_RETIRED:
        return RetireRuleResult(rule=current, was_retired_now=False)

    try:
        updated = rule_repository.retire_rule(rule_id)
    except InvalidStateTransitionError:
        # Lost a genuine race to a concurrent retire of the SAME rule
        # between the pre-check above and this call — the rule is now
        # RETIRED regardless of who won; treat exactly like the ordinary
        # already-retired pre-check above (idempotent-safe, no second
        # audit event).
        return RetireRuleResult(rule=rule_repository.get_rule(rule_id), was_retired_now=False)

    # Sequential, non-atomic write-then-audit — see module docstring
    # ("Transaction/audit boundary").
    audit_repository.record_audit_event(
        event_type="EVIDENCE_CLASSIFICATION_RULE_RETIRED",
        actor_type=actor_type,
        actor_id=actor_id,
        subject_type="EvidenceClassificationRule",
        subject_id=updated.rule_id,
        correlation_id=updated.rule_id,
        causation_id=None,
        payload={
            "sender_scope_type": updated.sender_scope_type,
            "sender_scope_value": updated.sender_scope_value,
            "subject_predicate_type": updated.subject_predicate_type,
            "subject_predicate_value": updated.subject_predicate_value,
            "document_type": updated.document_type,
        },
    )
    return RetireRuleResult(rule=updated, was_retired_now=True)
