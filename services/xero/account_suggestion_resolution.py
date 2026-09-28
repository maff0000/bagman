"""The human-control loop for the Xero Account Suggestion Producer
(BAGMAN accounting platform, `xero/account-suggestion-producer` WO):
``XeroAccountSuggestion`` (AI proposal) -> ``XERO_ACCOUNT_REQUIRED``
Needs You item -> operator accepts/corrects ->
``XeroAccountAssignment`` (the authoritative, write-once coding
decision).

This module owns exactly one operation, :func:`resolve_account_suggestion`
— the resolver. Mirrors
``services.evidence.classification_review.resolve_classification_review``'s
own division of labour: this function does NOT itself transition the
``NeedsYouItem`` to ``RESOLVED`` — the caller
(``app/api/routers/needs_you.py``) does that AFTER this function
succeeds, exactly like the existing ``COMPANY_REQUIRED``/
``CLASSIFICATION_REVIEW`` branches: real business-logic mutation
(the governed assignment write) FIRST, generic Needs You state
transition + its own audit event SECOND — so a failed assignment can
never leave the operator's question falsely marked resolved.

Fresh re-fetch, never the suggestion-time snapshot
------------------------------------------------------------------------
The eligible-account set used to validate the operator's chosen
`account_id` is re-fetched from `xero_account_repository`/
`services.xero.eligibility.list_eligible_accounts` AT RESOLUTION TIME,
never reused from whatever set the suggestion was originally produced
against — Xero's chart of accounts may have changed (a new sync, an
account archived) in between. A chosen `account_id` outside the FRESH
set fails closed with `core.errors.ValidationError` — no assignment is
created, and the Needs You item stays OPEN (the caller only marks it
RESOLVED after this function returns successfully).

AI_ACCEPTED vs OPERATOR_SELECTED
------------------------------------------------------------------------
The decision source is derived from comparing the operator's chosen
`account_id` against the ORIGINAL `XeroAccountSuggestion.suggested_account_id`
(looked up via the Needs You item's own `metadata['suggestion_id']`) —
`AI_ACCEPTED` when they match exactly, `OPERATOR_SELECTED` otherwise
(including the defensive case where no live suggestion record can be
found at all, e.g. a future caller that raises an `XERO_ACCOUNT_REQUIRED`
item with no suggestion attached — treated as an operator's own free
choice, never silently mislabelled AI_ACCEPTED without a real
suggestion to compare against).
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping, Optional

from core.errors import ValidationError
from services.xero.account_assignment import (
    SOURCE_AI_ACCEPTED,
    SOURCE_OPERATOR_SELECTED,
    XeroAccountAssignment,
)
from services.xero.ai_suggestion import resolve_ai_suggested_account
from services.xero.eligibility import list_eligible_accounts

#: The closed set of top-level `resolution` keys this resolver accepts
#: (mirrors `classification_review._RESOLUTION_ALLOWED_KEYS`'s own
#: "durable operator-action provenance, never a free-form bag"
#: discipline) — a caller supplies exactly `{"account_id": "..."}`.
_RESOLUTION_ALLOWED_KEYS = frozenset({"account_id"})

EVENT_ACCOUNT_ASSIGNED = "XERO_ACCOUNT_ASSIGNED"


@dataclass(frozen=True)
class AccountSuggestionResolutionResult:
    """Result of :func:`resolve_account_suggestion`."""

    assignment: XeroAccountAssignment
    #: True only if THIS call genuinely created the assignment row
    #: (repository-authoritative — see
    #: `services.xero.account_assignment.InMemoryXeroAccountAssignmentRepository
    #: .create_assignment`'s own idempotent-replay contract). False for
    #: a plain retry of the exact same decision.
    assignment_was_created: bool


def _validate_resolution_or_raise(resolution: Optional[Mapping[str, Any]]) -> str:
    if not resolution:
        raise ValidationError(
            "an XERO_ACCOUNT_REQUIRED resolution requires a non-empty `resolution` object carrying "
            "a real `account_id`"
        )
    extra_keys = set(resolution.keys()) - _RESOLUTION_ALLOWED_KEYS
    if extra_keys:
        raise ValidationError(
            f"resolution contains unsupported key(s) {sorted(extra_keys)} — only "
            f"{sorted(_RESOLUTION_ALLOWED_KEYS)} are permitted for an XERO_ACCOUNT_REQUIRED resolution"
        )
    account_id = resolution.get("account_id")
    if not account_id:
        raise ValidationError("resolution.account_id must be present and non-empty")
    return account_id


def resolve_account_suggestion(
    *,
    needs_you_item,
    resolution: Optional[Mapping[str, Any]],
    actor_type: str,
    actor_id: str,
    evidence_repository,
    xero_connection_repository,
    xero_account_repository,
    suggestion_repository,
    assignment_repository,
    record_audit_event,
) -> AccountSuggestionResolutionResult:
    """Resolve one `XERO_ACCOUNT_REQUIRED` Needs You item. Does NOT
    transition `needs_you_item` itself — the caller
    (`app/api/routers/needs_you.py`) does that AFTER this call succeeds.

    Sequence:

    1. Validate `resolution` shape.
    2. Prove `needs_you_item.source_object_reference` is a real
       `EvidenceItem` with a resolved `entity_id`
       (`evidence_repository.get_evidence` — its own `NotFoundError`
       propagates honestly).
    3. Prove `entity_id`'s `XeroConnection` is still `CONNECTED`.
    4. Re-fetch the FRESH eligible-account set and validate the chosen
       `account_id` against it (never the suggestion-time snapshot).
    5. Create/reuse the `XeroAccountAssignment` (write-once — see
       `services.xero.account_assignment`'s own module docstring for
       the idempotent-same-value / `ConflictError`-on-different-value
       contract, propagated uncaught here).

    Raises:
        core.errors.ValidationError: malformed resolution shape, no
            evidence anchor, an unresolved entity, a disconnected Xero
            connection, or a chosen `account_id` outside the fresh
            eligible set.
        core.errors.NotFoundError: `source_object_reference` does not
            resolve to a real `EvidenceItem`.
        core.errors.ConflictError: an assignment already exists for
            this evidence with a DIFFERENT `account_id` (propagated
            uncaught from `assignment_repository.create_assignment`).
    """
    chosen_account_id = _validate_resolution_or_raise(resolution)

    evidence_id = needs_you_item.source_object_reference
    if not evidence_id:
        raise ValidationError(
            f"NeedsYouItem '{needs_you_item.item_id}' has no source_object_reference — there is no "
            "evidence for this XERO_ACCOUNT_REQUIRED item to resolve against"
        )
    # Prove the anchor is real BEFORE anything else — a stale/bogus
    # source_object_reference must fail honestly (NotFoundError) rather
    # than surface confusingly from deep inside the assignment write.
    evidence = evidence_repository.get_evidence(evidence_id)
    entity_id = evidence.entity_id
    if not entity_id:
        raise ValidationError(
            f"EvidenceItem '{evidence_id}' referenced by NeedsYouItem '{needs_you_item.item_id}' has no "
            "resolved entity_id — an XERO_ACCOUNT_REQUIRED item can never be resolved for evidence "
            "whose company assignment is itself unresolved"
        )

    connection = xero_connection_repository.get_by_entity(entity_id)
    if connection is None or connection.status != "CONNECTED":
        raise ValidationError(
            f"entity '{entity_id}' has no CONNECTED XeroConnection — refusing to resolve an "
            "XERO_ACCOUNT_REQUIRED item against an account set that can no longer be trusted"
        )

    # FRESH re-fetch — never the suggestion-time snapshot (see module
    # docstring).
    fresh_eligible = list_eligible_accounts(xero_account_repository.list_accounts(entity_id=entity_id))
    account_resolution = resolve_ai_suggested_account(chosen_account_id, eligible_candidates=fresh_eligible)
    if not account_resolution.resolved:
        raise ValidationError(
            f"resolution.account_id {chosen_account_id!r} is not one of entity '{entity_id}''s CURRENT "
            f"eligible Xero accounts ({account_resolution.reason}) — refusing to resolve this item "
            "against a stale or invalid account"
        )

    suggestion_id = needs_you_item.metadata.get("suggestion_id") if needs_you_item.metadata else None
    suggestion = None
    if suggestion_id:
        try:
            suggestion = suggestion_repository.get_suggestion(suggestion_id)
        except Exception:  # noqa: BLE001 - a missing/unreadable suggestion never blocks resolution
            suggestion = None

    source = (
        SOURCE_AI_ACCEPTED
        if suggestion is not None and suggestion.suggested_account_id == account_resolution.account_id
        else SOURCE_OPERATOR_SELECTED
    )

    before = assignment_repository.get_by_evidence(evidence_id)
    assignment = assignment_repository.create_assignment(
        evidence_id=evidence_id,
        entity_id=entity_id,
        tenant_id=connection.tenant_id,
        account_id=account_resolution.account_id,
        source=source,
        suggestion_id=suggestion_id,
        assigned_by_actor_type=actor_type,
        assigned_by_actor_id=actor_id,
    )
    assignment_was_created = before is None

    if assignment_was_created:
        record_audit_event(
            event_type=EVENT_ACCOUNT_ASSIGNED,
            actor_type=actor_type,
            actor_id=actor_id,
            subject_type="XeroAccountAssignment",
            subject_id=assignment.assignment_id,
            correlation_id=needs_you_item.correlation_id,
            causation_id=None,
            payload={
                "evidence_id": evidence_id,
                "entity_id": entity_id,
                "tenant_id": connection.tenant_id,
                "account_id": assignment.account_id,
                "source": source,
                "needs_you_item_id": needs_you_item.item_id,
                "suggestion_id": suggestion_id,
            },
        )

    return AccountSuggestionResolutionResult(assignment=assignment, assignment_was_created=assignment_was_created)
