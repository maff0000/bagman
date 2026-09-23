"""``/internal/mailboxes/*`` — the mailbox-definition registry HTTP
surface (CD-6 Slice 3: Mailbox Management, TAB 1 / Email).

Thin router, no business logic (the same "thin router, all domain
logic elsewhere" convention ``app/api/routers/xero.py``'s own module
docstring establishes, itself following ``app/api/routers/needs_you.py``):
every handler here validates/parses the HTTP request, calls into
``services.mailbox.mailbox.MailboxSourceRepository``/
``core.api.BagmanCanonicalAPI``, and renders the returned domain
object's own ``to_dict()``. No canonical invariant (state-machine
legality, email uniqueness) is enforced here — it all lives in
``services/mailbox/mailbox.py`` and its repository implementations.

Browser-supplied-field discipline (critical)
------------------------------------------------
The browser can NEVER supply, on any endpoint here: ``mailbox_id``
(always server-generated via ``core.identity.generate_id()``, inside
the repository), any timestamp, a secret/credential value of any kind
(no such field exists on the request models below, and none may ever
be added — see ``services/mailbox/mailbox.py``'s own "Secret doctrine"),
or a ``connection_state`` of ``CONNECTED`` (no request model below even
HAS a ``connection_state`` field — every mailbox is minted
``NOT_CONFIGURED`` by the repository layer alone, and nothing here can
override that).

``default_entity_id`` hint validation
------------------------------------------
When a caller supplies ``default_entity_id`` (create or update), this
router — never ``services/mailbox/mailbox.py`` itself — confirms it
names a real canonical ``GovernedEntity`` via
``composition.api.entity_repository.get_entity(...)`` (raising
``NotFoundError`` -> HTTP 404 for an unknown id), mirroring
``app/api/routers/xero.py::_require_entity``'s identical layering
choice exactly. This is a HINT validation only (the id must be real),
never an ownership assertion — see the domain module's own "Critical
identity doctrine" section.

Endpoints
---------
* ``POST /internal/mailboxes`` — create a mailbox definition (starts
  ``ACTIVE``/``NOT_CONFIGURED``).
* ``GET /internal/mailboxes`` — list every mailbox definition.
* ``GET /internal/mailboxes/{mailbox_id}`` — single mailbox detail.
* ``PUT /internal/mailboxes/{mailbox_id}`` — edit metadata. A
  documented judgment call: full replace, not a partial patch — the
  GUI's Edit drawer always submits the complete current form (mirrors
  how ``needs-you.js``'s own resolution form always submits every
  field together) — never changes ``status``/``enabled``/
  ``connection_state``, which have their own dedicated endpoints below.
* ``POST /internal/mailboxes/{mailbox_id}/enable``
* ``POST /internal/mailboxes/{mailbox_id}/disable``
* ``POST /internal/mailboxes/{mailbox_id}/retire`` — the GUI's
  "Delete"/"Remove" action's real effect; the row is preserved, never
  physically deleted (see ``services/mailbox/mailbox.py``'s own module
  docstring).
* ``POST /internal/mailboxes/{mailbox_id}/policy-rules`` — the
  provider-neutral ``MailboxDomainRule`` policy-management surface (CD-6
  policy-rules-endpoint WO). Unlike every other endpoint in this file,
  this one does NOT require ``provider_kind == MICROSOFT_GRAPH`` (or any
  other particular provider) — see
  ``upsert_mailbox_policy_rule``'s own docstring below for the full
  behaviour, including why it deliberately never touches a Needs You
  item and never triggers historical back-processing.

Every mutation calls ``composition.api.record_audit_event(...)`` —
mirrors the exact call shape already used throughout
``app/api/routers/xero.py``/``app/api/routers/needs_you.py``. Payloads
carry only non-secret metadata (mailbox_id/display_name/email_address/
provider_kind/default_entity_id) — never anything credential-shaped,
because no such field exists anywhere on this domain.
"""
from __future__ import annotations

from typing import Any, Optional

from fastapi import APIRouter
from pydantic import BaseModel

from app.api.composition import get_composition
from core.errors import ValidationError
from core.timestamps import utc_now
from services.mailbox.domain_rule import (
    DESTINATION_MODE_FIXED,
    DESTINATION_MODE_REVIEW_REQUIRED,
    DESTINATION_MODES,
    MATCH_MODE_EXACT_DOMAIN_SUBJECT,
    MATCH_MODES,
    POLICIES,
    POLICY_MUST_READ,
    SOURCE_OPERATOR,
    validate_and_normalize_sender_address,
    validate_and_normalize_subject_predicate,
)

router = APIRouter(prefix="/internal/mailboxes")


def _require_entity_hint(composition, default_entity_id: Optional[str]) -> None:
    """Confirm `default_entity_id` (when supplied) is a real canonical
    `GovernedEntity` before doing anything else — mirrors
    `app/api/routers/xero.py::_require_entity` exactly. `None` is
    always valid (no hint at all — the ordinary case)."""
    if default_entity_id is not None:
        composition.api.entity_repository.get_entity(default_entity_id)  # raises NotFoundError if unknown


class CreateMailboxRequest(BaseModel):
    display_name: str
    email_address: str
    provider_kind: str
    default_entity_id: Optional[str] = None
    actor_type: str
    actor_id: str


class UpdateMailboxRequest(BaseModel):
    """Full metadata replace — see this module's own docstring."""

    display_name: str
    email_address: str
    provider_kind: str
    default_entity_id: Optional[str] = None
    actor_type: str
    actor_id: str


class MailboxActionRequest(BaseModel):
    """Body for enable/disable/retire — no other field is accepted."""

    actor_type: str
    actor_id: str


def _audit_payload(mailbox) -> dict[str, Any]:
    """Non-secret metadata only — see this module's own docstring."""
    return {
        "mailbox_id": mailbox.mailbox_id,
        "display_name": mailbox.display_name,
        "email_address": mailbox.email_address,
        "provider_kind": mailbox.provider_kind,
        "default_entity_id": mailbox.default_entity_id,
        "status": mailbox.status,
    }


@router.post("", status_code=201)
async def create_mailbox(payload: CreateMailboxRequest) -> dict[str, Any]:
    composition = get_composition()
    _require_entity_hint(composition, payload.default_entity_id)

    mailbox = composition.mailbox_source_repository.create_mailbox(
        display_name=payload.display_name,
        email_address=payload.email_address,
        provider_kind=payload.provider_kind,
        default_entity_id=payload.default_entity_id,
    )

    composition.api.record_audit_event(
        event_type="MAILBOX_CREATED",
        actor_type=payload.actor_type,
        actor_id=payload.actor_id,
        subject_type="MailboxSource",
        subject_id=mailbox.mailbox_id,
        correlation_id=mailbox.mailbox_id,
        causation_id=None,
        payload=_audit_payload(mailbox),
    )
    return mailbox.to_dict()


@router.get("")
async def list_mailboxes() -> dict[str, Any]:
    composition = get_composition()
    items = composition.mailbox_source_repository.list_mailboxes()
    return {"items": [m.to_dict() for m in items], "count": len(items)}


@router.get("/{mailbox_id}")
async def get_mailbox(mailbox_id: str) -> dict[str, Any]:
    composition = get_composition()
    mailbox = composition.mailbox_source_repository.get_mailbox(mailbox_id)
    return mailbox.to_dict()


@router.put("/{mailbox_id}")
async def update_mailbox(mailbox_id: str, payload: UpdateMailboxRequest) -> dict[str, Any]:
    composition = get_composition()
    _require_entity_hint(composition, payload.default_entity_id)

    mailbox = composition.mailbox_source_repository.update_mailbox(
        mailbox_id,
        display_name=payload.display_name,
        email_address=payload.email_address,
        provider_kind=payload.provider_kind,
        default_entity_id=payload.default_entity_id,
    )

    composition.api.record_audit_event(
        event_type="MAILBOX_UPDATED",
        actor_type=payload.actor_type,
        actor_id=payload.actor_id,
        subject_type="MailboxSource",
        subject_id=mailbox.mailbox_id,
        correlation_id=mailbox.mailbox_id,
        causation_id=None,
        payload=_audit_payload(mailbox),
    )
    return mailbox.to_dict()


@router.post("/{mailbox_id}/enable")
async def enable_mailbox(mailbox_id: str, payload: MailboxActionRequest) -> dict[str, Any]:
    composition = get_composition()
    mailbox = composition.mailbox_source_repository.enable_mailbox(mailbox_id)
    composition.api.record_audit_event(
        event_type="MAILBOX_ENABLED",
        actor_type=payload.actor_type,
        actor_id=payload.actor_id,
        subject_type="MailboxSource",
        subject_id=mailbox.mailbox_id,
        correlation_id=mailbox.mailbox_id,
        causation_id=None,
        payload=_audit_payload(mailbox),
    )
    return mailbox.to_dict()


@router.post("/{mailbox_id}/disable")
async def disable_mailbox(mailbox_id: str, payload: MailboxActionRequest) -> dict[str, Any]:
    composition = get_composition()
    mailbox = composition.mailbox_source_repository.disable_mailbox(mailbox_id)
    composition.api.record_audit_event(
        event_type="MAILBOX_DISABLED",
        actor_type=payload.actor_type,
        actor_id=payload.actor_id,
        subject_type="MailboxSource",
        subject_id=mailbox.mailbox_id,
        correlation_id=mailbox.mailbox_id,
        causation_id=None,
        payload=_audit_payload(mailbox),
    )
    return mailbox.to_dict()


@router.post("/{mailbox_id}/retire")
async def retire_mailbox(mailbox_id: str, payload: MailboxActionRequest) -> dict[str, Any]:
    composition = get_composition()
    mailbox = composition.mailbox_source_repository.retire_mailbox(mailbox_id)
    composition.api.record_audit_event(
        event_type="MAILBOX_RETIRED",
        actor_type=payload.actor_type,
        actor_id=payload.actor_id,
        subject_type="MailboxSource",
        subject_id=mailbox.mailbox_id,
        correlation_id=mailbox.mailbox_id,
        causation_id=None,
        payload=_audit_payload(mailbox),
    )
    return mailbox.to_dict()


class UpsertMailboxPolicyRuleRequest(BaseModel):
    """Request body for
    ``POST /internal/mailboxes/{mailbox_id}/policy-rules`` (CD-6
    policy-rules-endpoint WO) — see ``upsert_mailbox_policy_rule``'s own
    docstring for the full behaviour this drives.

    ``reason`` is the operator's free-text rationale for this specific
    policy decision — required, non-empty, and goes ONLY into the
    resulting audit event's payload. ``MailboxDomainRule`` itself has no
    such field (see ``services/mailbox/domain_rule.py``'s own schema) —
    this is deliberately never persisted onto the rule row.
    """

    actor_type: str
    actor_id: str
    match_mode: str  # "EXACT" | "INCLUDE_SUBDOMAINS" | "EXACT_ADDRESS" | "EXACT_DOMAIN_SUBJECT"
    sender_domain: str
    sender_address: Optional[str] = None
    #: Deterministic subject-aware mailbox domain policy — required, and
    #: ONLY accepted, when `match_mode == "EXACT_DOMAIN_SUBJECT"` (see
    #: `services.mailbox.domain_rule.validate_and_normalize_subject_predicate`'s
    #: own docstring for the structural validation this goes through,
    #: plus this endpoint's own `subject_predicate_observed` creation-
    #: time guard below).
    subject_predicate_type: Optional[str] = None  # "EXACT" | "STARTS_WITH"
    subject_predicate_value: Optional[str] = None
    policy: str  # "MUST_READ" | "GRAYLIST" | "BLACKLIST"
    destination_mode: Optional[str] = None  # "FIXED" | "REVIEW_REQUIRED" — only meaningful for MUST_READ
    destination_entity_id: Optional[str] = None
    processor_hint: Optional[str] = None
    reason: str


@router.post("/{mailbox_id}/policy-rules")
async def upsert_mailbox_policy_rule(mailbox_id: str, payload: UpsertMailboxPolicyRuleRequest) -> dict[str, Any]:
    """Create or update ONE ``MailboxDomainRule`` directly, by its own
    identity — provider-neutral, independent of any Needs You item (CD-6
    policy-rules-endpoint WO).

    Why this exists — the gap this closes
    ------------------------------------------------------------------
    Before this endpoint, the ONLY way to create/update a
    ``MailboxDomainRule`` over HTTP was
    ``POST /internal/mailboxes/{mailbox_id}/microsoft/domain-review/{item_id}/resolve``
    (and its batch sibling) in
    ``app/api/routers/mailboxes_microsoft.py`` — both require an OPEN
    ``MAILBOX_DOMAIN_REVIEW`` Needs You item, and both correctly REFUSE
    to touch an item that is already ``RESOLVED`` with a different
    resolution (a resolved domain-review item is permanent historical
    record of what question was answered when — that guard is untouched
    by this endpoint). Once a domain has been learned (e.g.
    ``send.xero.com -> MUST_READ/REVIEW_REQUIRED``, already resolved),
    there was previously no way to later add a more-specific
    address-level override (e.g. ``noreply@send.xero.com -> BLACKLIST``)
    underneath it — there is no fresh Needs You item to resolve for a
    sub-address, and creating one artificially isn't how discovery
    works. This endpoint operates directly on ``MailboxDomainRule``
    identity instead, entirely independent of the Needs You surface.

    Provider-neutral, deliberately NOT Microsoft-adapter-specific
    ------------------------------------------------------------------
    Lives in THIS router (not ``mailboxes_microsoft.py``) and never
    requires any particular ``provider_kind`` — unlike
    ``mailboxes_microsoft.py::_require_microsoft_mailbox``, only the
    mailbox's own existence is checked
    (``composition.mailbox_source_repository.get_mailbox`` — raises
    ``NotFoundError`` -> 404 for an unknown mailbox_id). A
    ``MailboxDomainRule`` is already mailbox-scoped, never
    provider-scoped (see ``services/mailbox/domain_rule.py``'s own
    module docstring) — everything except the Graph adapter itself must
    stay provider-neutral.

    Validation reused, never forked
    ------------------------------------------------------------------
    * ``match_mode``/``policy`` are checked against the same closed
      vocabularies (``MATCH_MODES``/``POLICIES``) every other caller in
      this codebase already imports from
      ``services.mailbox.domain_rule``.
    * The exact-address validation (real observation + domain match) is
      the SAME shared
      ``services.mailbox.domain_rule.validate_and_normalize_sender_address``
      ``mailboxes_microsoft.py``'s own domain-review resolve flow now
      also imports — one implementation, not two.
    * The ``MUST_READ`` destination-mode/entity validation mirrors
      ``mailboxes_microsoft.py::_resolve_mailbox_domain_review_core``'s
      own identical checks exactly (destination_mode required, FIXED
      requires a real destination_entity_id that resolves via
      ``composition.api.entity_repository.get_entity``). NEW here (this
      endpoint accepts ``policy`` directly from the caller, unlike the
      resolve endpoint's own ALLOW/IGNORE decision plumbing which can
      never produce this shape): a ``GRAYLIST``/``BLACKLIST`` request
      that ALSO supplies a destination is rejected outright — those two
      policies must never carry one.

    Previous state — exact identity, never the broader effective rule
    ------------------------------------------------------------------
    The audit event's ``previous_policy``/``previous_destination_mode``/
    ``previous_destination_entity_id`` come from
    ``MailboxDomainRuleRepository.find_exact`` — the rule (if any) at
    the EXACT identity this request targets — never
    ``find_for_sender``'s most-specific-wins resolution, which would
    incorrectly surface a broader domain rule's own state as the
    "previous" state of a brand-new address-level rule being created
    underneath it. A fresh address rule's ``previous_policy`` is always
    ``null``, never the domain's own current policy.

    Never touches Needs You, never back-processes history
    ------------------------------------------------------------------
    This endpoint performs NO lookup, resolve, or creation of any Needs
    You item, and NEVER calls
    ``services.mailbox.sweep.reprocess_all_historical_candidates_for_domain``
    — a deliberate difference from the domain-review resolve flow (Matt's
    explicit instruction: a rule edit here must not cause surprise
    historical ingestion).

    Idempotency — a true no-op performs NO repository write at all
    ------------------------------------------------------------------
    ``was_no_op`` is computed BEFORE any repository call, by comparing
    EVERY operator-controlled semantic field of the identity's previous
    state (from ``find_exact``) to the resolved new state:
    ``match_mode``, ``policy``, ``destination_mode``,
    ``destination_entity_id``, AND ``processor_hint``. When all of them
    are identical, this handler does NOT call
    ``MailboxDomainRuleRepository.upsert_rule`` at all (never touching
    ``approved_at``/``updated_at`` on the persisted row — an unconditional
    ``upsert_rule`` call would otherwise silently refresh those
    timestamps even though nothing semantically changed) and does NOT
    emit a ``MAILBOX_POLICY_RULE_UPSERTED`` audit event; the response
    returns the EXISTING ``previous_rule`` unchanged, with
    ``was_no_op: true``.

    A request that changes ONLY ``processor_hint`` is a genuine
    mutation (``was_no_op: false``) — it updates the SAME rule identity
    (same ``rule_id``) via ``upsert_rule`` and emits one audit event
    whose payload carries ``previous_processor_hint``/
    ``new_processor_hint``.

    A domain-level rule's ``match_mode`` may legitimately change in
    place between ``EXACT`` and ``INCLUDE_SUBDOMAINS`` (same rule
    identity, via ``find_exact``'s own domain-identity behaviour); the
    audit payload carries both ``previous_match_mode`` and
    ``new_match_mode`` (``previous_match_mode`` is ``null`` when
    creating a brand-new rule).
    """
    composition = get_composition()
    # Existence check only — provider-neutral (no `provider_kind`
    # constraint, unlike `mailboxes_microsoft.py::_require_microsoft_mailbox`);
    # raises `NotFoundError` -> 404 for an unknown mailbox_id.
    composition.mailbox_source_repository.get_mailbox(mailbox_id)

    if not payload.reason or not payload.reason.strip():
        raise ValidationError("reason is required and must be non-empty")

    if payload.match_mode not in MATCH_MODES:
        raise ValidationError(f"match_mode must be one of {sorted(MATCH_MODES)} (got {payload.match_mode!r})")
    if payload.policy not in POLICIES:
        raise ValidationError(f"policy must be one of {sorted(POLICIES)} (got {payload.policy!r})")

    normalized_sender_address = validate_and_normalize_sender_address(
        message_repository=composition.mailbox_message_repository,
        mailbox_id=mailbox_id,
        sender_domain=payload.sender_domain,
        match_mode=payload.match_mode,
        sender_address=payload.sender_address,
    )
    normalized_subject_predicate_type, normalized_subject_predicate_value = validate_and_normalize_subject_predicate(
        match_mode=payload.match_mode,
        subject_predicate_type=payload.subject_predicate_type,
        subject_predicate_value=payload.subject_predicate_value,
    )
    if payload.match_mode == MATCH_MODE_EXACT_DOMAIN_SUBJECT:
        # Creation-time observed guard (item 12, first paragraph) — a
        # NEW EXACT_DOMAIN_SUBJECT rule created via this generic,
        # provider-neutral endpoint requires proof the predicate matches
        # AT LEAST ONE persisted message (ANY ingestion status — this is
        # "has this predicate EVER matched a real message here", not
        # about current eligibility; contrast with
        # `services.mailbox.review_resolution.resolve_domain_review`'s
        # OWN, stricter "currently eligible candidate" guard for the
        # domain-review ALLOW workflow specifically) for
        # `(mailbox_id, sender_domain)` before allowing creation.
        if not composition.mailbox_message_repository.subject_predicate_observed(
            mailbox_id=mailbox_id, sender_domain=payload.sender_domain,
            predicate_type=normalized_subject_predicate_type, predicate_value=normalized_subject_predicate_value,
        ):
            raise ValidationError(
                f"subject predicate {normalized_subject_predicate_type}={normalized_subject_predicate_value!r} "
                f"has never matched a persisted message at domain '{payload.sender_domain}' for mailbox "
                f"'{mailbox_id}' — refusing to pre-authorize a predicate BAGMAN has never seen matching mail for"
            )

    if payload.policy == POLICY_MUST_READ:
        if payload.destination_mode not in DESTINATION_MODES:
            raise ValidationError(f"destination_mode must be one of {sorted(DESTINATION_MODES)} when policy is 'MUST_READ'")
        if payload.destination_mode == DESTINATION_MODE_FIXED and not payload.destination_entity_id:
            raise ValidationError("destination_entity_id is required when destination_mode is 'FIXED'")
        if payload.destination_mode == DESTINATION_MODE_REVIEW_REQUIRED and payload.destination_entity_id:
            raise ValidationError("destination_entity_id must not be supplied when destination_mode is 'REVIEW_REQUIRED'")
        if payload.destination_entity_id:
            # Real existence check — mirrors `app/api/routers/xero.py
            # ::_require_entity`'s own pattern (never trust a
            # caller-supplied entity_id without proving it real).
            composition.api.entity_repository.get_entity(payload.destination_entity_id)
        resolved_destination_entity_id = payload.destination_entity_id
        resolved_destination_mode = payload.destination_mode
    else:
        if payload.destination_entity_id is not None or payload.destination_mode is not None:
            raise ValidationError(
                f"a {payload.policy} policy rule must never carry a destination_entity_id/destination_mode"
            )
        resolved_destination_entity_id = None
        resolved_destination_mode = None

    previous_rule = composition.mailbox_domain_rule_repository.find_exact(
        mailbox_id=mailbox_id,
        sender_domain=payload.sender_domain,
        match_mode=payload.match_mode,
        sender_address=normalized_sender_address,
        subject_predicate_type=normalized_subject_predicate_type,
        subject_predicate_value=normalized_subject_predicate_value,
    )
    previous_match_mode = previous_rule.match_mode if previous_rule is not None else None
    previous_policy = previous_rule.policy if previous_rule is not None else None
    previous_destination_mode = previous_rule.destination_mode if previous_rule is not None else None
    previous_destination_entity_id = previous_rule.destination_entity_id if previous_rule is not None else None
    previous_processor_hint = previous_rule.processor_hint if previous_rule is not None else None
    previous_subject_predicate_type = previous_rule.subject_predicate_type if previous_rule is not None else None
    previous_subject_predicate_value = previous_rule.subject_predicate_value if previous_rule is not None else None

    was_no_op = (
        previous_rule is not None
        and previous_match_mode == payload.match_mode
        and previous_policy == payload.policy
        and previous_destination_mode == resolved_destination_mode
        and previous_destination_entity_id == resolved_destination_entity_id
        and previous_processor_hint == payload.processor_hint
        and previous_subject_predicate_type == normalized_subject_predicate_type
        and previous_subject_predicate_value == normalized_subject_predicate_value
    )

    if was_no_op:
        # A true no-op performs NO repository write at all — never
        # touches `approved_at`/`updated_at` on the persisted row, and
        # never emits an audit event. Return the existing rule as-is.
        rule = previous_rule
    else:
        rule = composition.mailbox_domain_rule_repository.upsert_rule(
            mailbox_id=mailbox_id,
            sender_domain=payload.sender_domain,
            match_mode=payload.match_mode,
            policy=payload.policy,
            destination_entity_id=resolved_destination_entity_id,
            destination_mode=resolved_destination_mode,
            source=SOURCE_OPERATOR,
            processor_hint=payload.processor_hint,
            approved_at=utc_now(),
            sender_address=normalized_sender_address,
            subject_predicate_type=normalized_subject_predicate_type,
            subject_predicate_value=normalized_subject_predicate_value,
        )

        composition.api.record_audit_event(
            event_type="MAILBOX_POLICY_RULE_UPSERTED",
            actor_type=payload.actor_type,
            actor_id=payload.actor_id,
            subject_type="MailboxDomainRule",
            subject_id=rule.rule_id,
            correlation_id=rule.rule_id,
            causation_id=None,
            payload={
                "mailbox_id": mailbox_id,
                "rule_id": rule.rule_id,
                "previous_match_mode": previous_match_mode,
                "new_match_mode": rule.match_mode,
                "sender_domain": rule.sender_domain,
                "sender_address": normalized_sender_address,
                "previous_policy": previous_policy,
                "new_policy": rule.policy,
                "previous_destination_mode": previous_destination_mode,
                "new_destination_mode": rule.destination_mode,
                "previous_destination_entity_id": previous_destination_entity_id,
                "new_destination_entity_id": rule.destination_entity_id,
                "previous_processor_hint": previous_processor_hint,
                "new_processor_hint": rule.processor_hint,
                "previous_subject_predicate_type": previous_subject_predicate_type,
                "new_subject_predicate_type": rule.subject_predicate_type,
                "previous_subject_predicate_value": previous_subject_predicate_value,
                "new_subject_predicate_value": rule.subject_predicate_value,
                "reason": payload.reason,
            },
        )

    return {
        "mailbox_domain_rule": rule.to_dict(),
        "previous_policy": previous_policy,
        "previous_destination_mode": previous_destination_mode,
        "previous_destination_entity_id": previous_destination_entity_id,
        "was_no_op": was_no_op,
    }
