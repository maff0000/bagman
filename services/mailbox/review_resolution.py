"""``resolve_domain_review``/``resolve_security_review`` — the ONE
shared, provider-neutral implementation of the two operator-decision
workflows every mailbox provider router exposes (CD-6 GUI-operations-
foundation follow-on WO, "second mailbox provider" delivery).

Why this module exists
------------------------------------------------------------------------
Before this module existed, ``app/api/routers/mailboxes_microsoft.py``
and ``app/api/routers/mailboxes_imap.py`` each carried their OWN,
wholesale-duplicated copy of both workflows
(``_resolve_mailbox_domain_review_core``/``_resolve_imap_domain_review_core``
and ``resolve_microsoft_security_review``/``resolve_imap_security_review``)
— near-line-for-line copies whose only real difference was which
provider-specific adapter got threaded through to
``reprocess_all_historical_candidates_for_domain``/
``process_security_reviewed_message_once``. That duplication was
architecturally wrong: this module's two service-layer dependents
(:func:`services.mailbox.sweep.reprocess_all_historical_candidates_for_domain`,
:func:`services.mailbox.sweep.process_security_reviewed_message_once`,
:func:`services.mailbox.sweep.decline_security_reviewed_message`) are
ALREADY correctly provider-neutral — they take ``adapter`` as an
explicit parameter. The router-level orchestration around them now
follows the exact same pattern.

Bounded, NOT a new framework
------------------------------------------------------------------------
This is a deliberate, bounded shared-orchestration module for these two
specific workflows ONLY. It is NOT a general command bus, NOT a generic
dependency-injection container, and NOT a new abstraction layer for any
other mailbox workflow. Every dependency is a plain, explicit keyword
parameter — mirroring the exact discipline
``services/mailbox/sweep.py``'s own public functions
(``run_sweep``/``reprocess_all_historical_candidates_for_domain``/
``process_security_reviewed_message_once``) already established:
service-layer code here never accepts an app-layer ``composition``
object, only the specific repositories/collaborators it actually uses.

``adapter`` is the ONLY provider-specific input
------------------------------------------------------------------------
Every other parameter (repositories, ``EntityRepository``, audit
recording, ``EvidenceSafetyScanner``, ``EvidenceObjectStore``, Needs You
semantics, domain-rule/destination semantics) is IDENTICAL across
providers by construction — the Microsoft Graph mailbox and the IMAP
mailbox share the exact same
``services.mailbox.domain_rule.MailboxDomainRuleRepository``,
``services.needs_you.needs_you.NeedsYouRepository``, and
``services.mailbox.message.MailboxMessageRepository`` implementations,
governed by the exact same policy/destination/audit-event doctrine. The
adapter (``MicrosoftGraphMailboxAdapter``/``ImapMailboxAdapter``) is the
one genuinely provider-specific object — it alone knows how to fetch a
message's headers/MIME content from that specific provider — and both
functions below accept it as a single explicit ``adapter`` keyword
parameter, exactly matching the shape
``reprocess_all_historical_candidates_for_domain``/
``process_security_reviewed_message_once`` themselves already require.

Caller contract — a pure extraction, no behaviour change
------------------------------------------------------------------------
Both functions below are a faithful, byte-for-byte-equivalent merge of
the two routers' own former private implementations. Every nuance of
the original behaviour is preserved exactly:

* :func:`resolve_domain_review` — ``KEEP_GRAY``'s own deliberately
  different shape (upserts a real ``GRAYLIST`` rule but never resolves
  the triggering item); the already-``RESOLVED``-with-identical-
  resolution idempotent no-op path vs. a genuine-conflict
  ``ConflictError``; ``EXACT_ADDRESS`` validation via
  :func:`services.mailbox.domain_rule.validate_and_normalize_sender_address`;
  ``FIXED`` requiring a real entity via ``entity_repository.get_entity``;
  the exact audit-event shape/field names
  (``MAILBOX_DOMAIN_RULE_GRAYLIST``/``MAILBOX_DOMAIN_RULE_MUST_READ``/
  ``MAILBOX_DOMAIN_RULE_BLACKLIST``); and the candidate-only historical
  reprocessing call to ``reprocess_all_historical_candidates_for_domain``
  for a genuine ``ALLOW``/``MUST_READ`` resolution.
* :func:`resolve_security_review` — ``PROCESS_THIS_MESSAGE_ONCE`` vs.
  ``DO_NOT_PROCESS_THIS_MESSAGE``; same-outcome idempotency; conflict on
  a changed decision; "no governing ``MUST_READ`` rule found" as a
  ``ConflictError``; the exact audit-event shapes for both
  ``MAILBOX_SECURITY_REVIEW_DECLINED``/
  ``MAILBOX_SECURITY_REVIEW_PROCESSED_ONCE``; and never modifying the
  governing ``MailboxDomainRule`` — a one-message override, never a
  relevance re-decision.

Callers — thin router wrappers only
------------------------------------------------------------------------
``app/api/routers/mailboxes_microsoft.py::_resolve_mailbox_domain_review_core``/
``resolve_microsoft_security_review`` and
``app/api/routers/mailboxes_imap.py::_resolve_imap_domain_review_core``/
``resolve_imap_security_review`` are now thin wrappers that do ONLY
provider-specific HTTP-boundary work (mailbox/provider validation,
request-body parsing) and then call straight through to the two
functions below, passing ``adapter=composition.microsoft_mailbox_adapter``
or ``adapter=composition.imap_mailbox_adapter`` respectively. Neither
router's own public HTTP contract changed.
"""
from __future__ import annotations

from typing import Any, Optional, Protocol

from core.entity import EntityRepository
from core.errors import ConflictError, ValidationError
from core.timestamps import utc_now
from services.evidence.intake.scanner import EvidenceSafetyScanner
from services.mailbox.domain_rule import (
    DESTINATION_MODE_FIXED,
    DESTINATION_MODE_REVIEW_REQUIRED,
    MATCH_MODE_EXACT,
    MATCH_MODE_EXACT_ADDRESS,
    MATCH_MODE_INCLUDE_SUBDOMAINS,
    POLICY_BLACKLIST,
    POLICY_GRAYLIST,
    POLICY_MUST_READ,
    SOURCE_OPERATOR,
    MailboxDomainRuleRepository,
    validate_and_normalize_sender_address,
)
from services.mailbox.mailbox import MailboxSource
from services.mailbox.message import MailboxMessageRepository
from services.mailbox.sweep import (
    decline_security_reviewed_message,
    process_security_reviewed_message_once,
    reprocess_all_historical_candidates_for_domain,
)
from services.needs_you.needs_you import (
    ITEM_TYPE_MAILBOX_AUTHENTICATION_ESCALATION,
    ITEM_TYPE_MAILBOX_DOMAIN_REVIEW,
    NeedsYouRepository,
)


class _AdapterProtocol(Protocol):
    """The narrow surface either function below needs from a
    provider adapter — mirrors
    ``services.mailbox.sweep._AdapterProtocol`` exactly (that module's
    own private protocol is not imported directly here, to keep this
    module's own dependency surface self-contained), since both
    functions here only ever pass ``adapter`` straight through to
    ``services.mailbox.sweep``'s own public functions, never call a
    method on it directly themselves."""

    def fetch_folder_delta(self, **kwargs: Any) -> Any: ...

    def fetch_message_content(self, **kwargs: Any) -> Any: ...

    def fetch_message_headers(self, **kwargs: Any) -> Any: ...

    def report_connection_error(self, mailbox_id: str, *, error_code: str, error_detail: str) -> None: ...


class _EvidenceAPIProtocol(Protocol):
    def register_evidence(self, **kwargs: Any) -> Any: ...

    def record_provenance(self, **kwargs: Any) -> Any: ...

    def record_audit_event(self, **kwargs: Any) -> Any: ...


class _ObjectStoreProtocol(Protocol):
    def put(self, object_id: Any, content_hash: Any, data: Any) -> Any: ...

    def put_prefixed(self, prefix: Any, object_id: Any, content_hash: Any, data: Any) -> Any: ...


_SECURITY_REVIEW_DECISIONS = ("PROCESS_THIS_MESSAGE_ONCE", "DO_NOT_PROCESS_THIS_MESSAGE")


def resolve_domain_review(
    *,
    needs_you_repository: NeedsYouRepository,
    mailbox_message_repository: MailboxMessageRepository,
    mailbox_domain_rule_repository: MailboxDomainRuleRepository,
    entity_repository: EntityRepository,
    api: _EvidenceAPIProtocol,
    object_store: _ObjectStoreProtocol,
    scanner: EvidenceSafetyScanner,
    adapter: _AdapterProtocol,
    mailbox: MailboxSource,
    mailbox_id: str,
    mailbox_source_id: str,
    item_id: str,
    actor_type: str,
    actor_id: str,
    decision: str,  # "ALLOW" | "IGNORE" | "KEEP_GRAY"
    destination_entity_id: Optional[str],
    destination_mode: Optional[str],  # "FIXED" | "REVIEW_REQUIRED" — required when decision == "ALLOW"
    match_mode: str,  # "EXACT" | "INCLUDE_SUBDOMAINS" | "EXACT_ADDRESS"
    processor_hint: Optional[str],
    sender_address: Optional[str],
) -> dict[str, Any]:
    """Resolve one ``ITEM_TYPE_MAILBOX_DOMAIN_REVIEW`` Needs You item —
    see this module's own docstring for the full preserved behavioural
    contract (originally
    ``app/api/routers/mailboxes_microsoft.py::_resolve_mailbox_domain_review_core``'s
    own docstring — read that history in the extraction's own commit for
    the complete narrative).

    ``decision="KEEP_GRAY"`` ("Keep checking with me") is a deliberately
    DIFFERENT shape from ``ALLOW``/``IGNORE``: it upserts a real
    ``GRAYLIST`` ``MailboxDomainRule`` for this
    ``(mailbox_id, sender_domain)`` but does NOT resolve the triggering
    item — it stays ``OPEN``. This is a documented, intentional choice:
    "Matt looked at this and deliberately left it under review" is a
    real, useful, distinct state from "never looked at at all", but no
    final relevance/destination call has been made yet, so the item
    legitimately stays open for a later look (mirrors the architect's
    own "GRAYLIST behaves like no rule at Stage B" doctrine — the Needs
    You side of that same doctrine: nothing about this domain's OPEN
    question has actually been answered).
    """
    item = needs_you_repository.get_needs_you_item(item_id)

    if item.item_type != ITEM_TYPE_MAILBOX_DOMAIN_REVIEW:
        raise ValidationError(f"NeedsYouItem '{item_id}' is not a {ITEM_TYPE_MAILBOX_DOMAIN_REVIEW} item")
    if item.metadata.get("mailbox_id") != mailbox_id:
        raise ValidationError(f"NeedsYouItem '{item_id}' does not belong to mailbox '{mailbox_id}'")

    if decision not in ("ALLOW", "IGNORE", "KEEP_GRAY"):
        raise ValidationError(f"decision must be 'ALLOW', 'IGNORE' or 'KEEP_GRAY' (got {decision!r})")
    if match_mode not in (MATCH_MODE_EXACT, MATCH_MODE_INCLUDE_SUBDOMAINS, MATCH_MODE_EXACT_ADDRESS):
        raise ValidationError(
            f"match_mode must be 'EXACT', 'INCLUDE_SUBDOMAINS' or 'EXACT_ADDRESS' (got {match_mode!r})"
        )
    if decision == "KEEP_GRAY" and match_mode == MATCH_MODE_EXACT_ADDRESS:
        # Documented, deliberate out-of-scope judgment call — graylisting
        # is inherently domain-level triage ("Matt looked at this domain
        # once and deliberately left it under review"); an address-scoped
        # graylist has no real operator use case this delivery builds
        # for. Use 'EXACT'/'INCLUDE_SUBDOMAINS' instead.
        raise ValidationError(
            "match_mode 'EXACT_ADDRESS' is not supported for decision 'KEEP_GRAY' — graylisting is "
            "inherently domain-level triage; use 'EXACT' or 'INCLUDE_SUBDOMAINS' instead"
        )

    sender_domain = item.metadata.get("sender_domain")
    normalized_sender_address = validate_and_normalize_sender_address(
        message_repository=mailbox_message_repository,
        mailbox_id=mailbox_id,
        sender_domain=sender_domain,
        match_mode=match_mode,
        sender_address=sender_address,
    )
    resolution = {
        "decision": decision,
        "destination_entity_id": destination_entity_id,
        "destination_mode": destination_mode,
        "match_mode": match_mode,
        "sender_address": normalized_sender_address,
    }

    # Audit trail — snapshot whatever governs this domain BEFORE this
    # action, so the audit event below can carry a real
    # previous_policy/previous_destination_* alongside the new values.
    # `find_for_sender` (never a raw domain-only dict lookup) is reused
    # deliberately — this IS the exact resolution a real message from
    # this domain would hit right now.
    previous_rule = mailbox_domain_rule_repository.find_for_sender(
        mailbox_id=mailbox_id, sender_domain=sender_domain, sender_address=normalized_sender_address
    )
    previous_policy = previous_rule.policy if previous_rule is not None else None
    previous_destination_entity_id = previous_rule.destination_entity_id if previous_rule is not None else None
    previous_destination_mode = previous_rule.destination_mode if previous_rule is not None else None

    if decision == "KEEP_GRAY":
        # A real rule row, but never a resolution of the item itself —
        # see this function's own docstring above.
        rule = mailbox_domain_rule_repository.upsert_rule(
            mailbox_id=mailbox_id,
            sender_domain=sender_domain,
            match_mode=match_mode,
            policy=POLICY_GRAYLIST,
            destination_entity_id=None,
            destination_mode=None,
            source=SOURCE_OPERATOR,
            processor_hint=processor_hint,
            approved_at=utc_now(),
        )
        api.record_audit_event(
            event_type="MAILBOX_DOMAIN_RULE_GRAYLIST",
            actor_type=actor_type,
            actor_id=actor_id,
            subject_type="MailboxDomainRule",
            subject_id=rule.rule_id,
            correlation_id=item.correlation_id,
            causation_id=None,
            payload={
                "mailbox_id": mailbox_id,
                "sender_domain": sender_domain,
                "needs_you_item_id": item_id,
                "previous_policy": previous_policy,
                "new_policy": POLICY_GRAYLIST,
                "previous_destination_entity_id": previous_destination_entity_id,
                "new_destination_entity_id": None,
                "previous_destination_mode": previous_destination_mode,
                "new_destination_mode": None,
            },
        )
        return {"needs_you_item": item.to_dict(), "mailbox_domain_rule": rule.to_dict(), "reprocessed_messages": []}

    if item.status != "OPEN":
        if item.status == "RESOLVED" and (item.resolution or {}) == resolution:
            return {"needs_you_item": item.to_dict(), "mailbox_domain_rule": None, "reprocessed_messages": []}
        raise ConflictError(
            f"NeedsYouItem '{item_id}' is already '{item.status}' with a different resolution — "
            "refusing to silently change an already-decided item; this is a genuine conflict, not "
            "an idempotent retry"
        )

    if decision == "ALLOW":
        policy = POLICY_MUST_READ
        if destination_mode not in (DESTINATION_MODE_FIXED, DESTINATION_MODE_REVIEW_REQUIRED):
            raise ValidationError("destination_mode must be 'FIXED' or 'REVIEW_REQUIRED' when decision is 'ALLOW'")
        if destination_mode == DESTINATION_MODE_FIXED and not destination_entity_id:
            raise ValidationError("destination_entity_id is required when destination_mode is 'FIXED'")
        if destination_entity_id:
            # Real existence check — never trust a caller-supplied
            # entity_id without proving it real first.
            entity_repository.get_entity(destination_entity_id)
    else:
        policy = POLICY_BLACKLIST

    rule = mailbox_domain_rule_repository.upsert_rule(
        mailbox_id=mailbox_id,
        sender_domain=sender_domain,
        match_mode=match_mode,
        policy=policy,
        destination_entity_id=destination_entity_id if policy == POLICY_MUST_READ else None,
        destination_mode=destination_mode if policy == POLICY_MUST_READ else None,
        source=SOURCE_OPERATOR,
        processor_hint=processor_hint,
        approved_at=utc_now(),
        sender_address=normalized_sender_address,
    )

    updated_item = needs_you_repository.resolve_needs_you_item(
        item_id, new_status="RESOLVED", resolution=resolution, actor_type=actor_type, actor_id=actor_id
    )
    api.record_audit_event(
        event_type="MAILBOX_DOMAIN_RULE_MUST_READ" if policy == POLICY_MUST_READ else "MAILBOX_DOMAIN_RULE_BLACKLIST",
        actor_type=actor_type,
        actor_id=actor_id,
        subject_type="MailboxDomainRule",
        subject_id=rule.rule_id,
        correlation_id=updated_item.correlation_id,
        causation_id=None,
        payload={
            "mailbox_id": mailbox_id,
            "sender_domain": sender_domain,
            "needs_you_item_id": item_id,
            "previous_policy": previous_policy,
            "new_policy": policy,
            "previous_destination_entity_id": previous_destination_entity_id,
            "new_destination_entity_id": rule.destination_entity_id,
            "previous_destination_mode": previous_destination_mode,
            "new_destination_mode": rule.destination_mode,
        },
    )

    # Operational addendum (ahead of the first real large historical
    # sweep) — back-process EVERY historical candidate BAGMAN has
    # already discovered for this domain, not merely the one message
    # that happened to trigger this item. `sender_domain` is always
    # present on a real `MAILBOX_DOMAIN_REVIEW` item's own metadata; the
    # guard below is defensive, never expected to be exercised for a
    # well-formed item.
    reprocessed: list = []
    if policy == POLICY_MUST_READ and sender_domain:
        reprocessed = reprocess_all_historical_candidates_for_domain(
            mailbox=mailbox,
            mailbox_source_id=mailbox_source_id,
            sender_domain=sender_domain,
            rule=rule,
            adapter=adapter,
            message_repository=mailbox_message_repository,
            needs_you_repository=needs_you_repository,
            api=api,
            object_store=object_store,
            scanner=scanner,
            actor_type=actor_type,
            actor_id=actor_id,
            correlation_id=updated_item.correlation_id,
        )

    return {
        "needs_you_item": updated_item.to_dict(),
        "mailbox_domain_rule": rule.to_dict(),
        "reprocessed_messages": [m.to_dict() for m in reprocessed],
    }


def resolve_security_review(
    *,
    needs_you_repository: NeedsYouRepository,
    mailbox_message_repository: MailboxMessageRepository,
    mailbox_domain_rule_repository: MailboxDomainRuleRepository,
    api: _EvidenceAPIProtocol,
    object_store: _ObjectStoreProtocol,
    scanner: EvidenceSafetyScanner,
    adapter: _AdapterProtocol,
    mailbox: MailboxSource,
    mailbox_id: str,
    mailbox_source_id: str,
    item_id: str,
    actor_type: str,
    actor_id: str,
    decision: str,  # "PROCESS_THIS_MESSAGE_ONCE" | "DO_NOT_PROCESS_THIS_MESSAGE"
) -> dict[str, Any]:
    """Resolve one ``ITEM_TYPE_MAILBOX_AUTHENTICATION_ESCALATION`` Needs
    You item — the real resolution workflow for a
    ``SECURITY_REVIEW``-held message. See this module's own docstring
    for the full preserved behavioural contract (originally
    ``app/api/routers/mailboxes_microsoft.py::resolve_microsoft_security_review``'s
    own docstring).

    * ``decision="PROCESS_THIS_MESSAGE_ONCE"`` — fetches this ONE
      message's raw MIME and applies the GOVERNING
      ``MailboxDomainRule``'s own destination semantics exactly as the
      normal MUST_READ path would. Does NOT modify the governing rule's
      own policy at all — a one-message override, never a relevance
      re-decision. Idempotent under a double-submit.
    * ``decision="DO_NOT_PROCESS_THIS_MESSAGE"`` — records the explicit
      decision (audited), performs no MIME fetch/evidence ingestion, and
      does NOT blacklist the underlying source/domain — the governing
      ``MailboxDomainRule`` stays untouched.

    Both decisions resolve the triggering item as part of the same call.
    A real attempt to change an already-decided item to a DIFFERENT
    outcome raises ``ConflictError``.
    """
    item = needs_you_repository.get_needs_you_item(item_id)
    if item.item_type != ITEM_TYPE_MAILBOX_AUTHENTICATION_ESCALATION:
        raise ValidationError(
            f"NeedsYouItem '{item_id}' is not a {ITEM_TYPE_MAILBOX_AUTHENTICATION_ESCALATION} item"
        )
    if item.metadata.get("mailbox_id") != mailbox_id:
        raise ValidationError(f"NeedsYouItem '{item_id}' does not belong to mailbox '{mailbox_id}'")
    if decision not in _SECURITY_REVIEW_DECISIONS:
        raise ValidationError(f"decision must be one of {_SECURITY_REVIEW_DECISIONS} (got {decision!r})")

    resolution = {"decision": decision}
    message_id = item.metadata.get("mailbox_message_id")

    if item.status != "OPEN":
        if item.status == "RESOLVED" and (item.resolution or {}) == resolution:
            message = mailbox_message_repository.get_message(message_id)
            return {"needs_you_item": item.to_dict(), "mailbox_message": message.to_dict()}
        raise ConflictError(
            f"NeedsYouItem '{item_id}' is already '{item.status}' with a different resolution — "
            "refusing to silently change an already-decided item; this is a genuine conflict, not "
            "an idempotent retry"
        )

    if decision == "DO_NOT_PROCESS_THIS_MESSAGE":
        updated_message = decline_security_reviewed_message(
            message_repository=mailbox_message_repository, message_id=message_id
        )
        updated_item = needs_you_repository.resolve_needs_you_item(
            item_id, new_status="RESOLVED", resolution=resolution, actor_type=actor_type, actor_id=actor_id
        )
        api.record_audit_event(
            event_type="MAILBOX_SECURITY_REVIEW_DECLINED",
            actor_type=actor_type,
            actor_id=actor_id,
            subject_type="MailboxMessage",
            subject_id=updated_message.mailbox_message_id,
            correlation_id=updated_item.correlation_id,
            causation_id=None,
            payload={"mailbox_id": mailbox_id, "needs_you_item_id": item_id},
        )
        return {"needs_you_item": updated_item.to_dict(), "mailbox_message": updated_message.to_dict()}

    # PROCESS_THIS_MESSAGE_ONCE
    message = mailbox_message_repository.get_message(message_id)
    rule = mailbox_domain_rule_repository.find_for_sender(
        mailbox_id=mailbox_id, sender_domain=message.sender_domain, sender_address=message.sender_address
    )
    if rule is None or rule.policy != POLICY_MUST_READ:
        raise ConflictError(
            f"no governing MUST_READ MailboxDomainRule found for message '{message_id}' — cannot "
            "process it (this should never happen for a genuine MAILBOX_AUTHENTICATION_ESCALATION "
            "item, which is only ever raised for a MUST_READ source)"
        )

    updated_message = process_security_reviewed_message_once(
        mailbox=mailbox,
        mailbox_source_id=mailbox_source_id,
        message_id=message_id,
        rule=rule,
        adapter=adapter,
        message_repository=mailbox_message_repository,
        needs_you_repository=needs_you_repository,
        api=api,
        object_store=object_store,
        scanner=scanner,
        actor_type=actor_type,
        actor_id=actor_id,
        correlation_id=item.correlation_id,
    )
    updated_item = needs_you_repository.resolve_needs_you_item(
        item_id, new_status="RESOLVED", resolution=resolution, actor_type=actor_type, actor_id=actor_id
    )
    api.record_audit_event(
        event_type="MAILBOX_SECURITY_REVIEW_PROCESSED_ONCE",
        actor_type=actor_type,
        actor_id=actor_id,
        subject_type="MailboxMessage",
        subject_id=updated_message.mailbox_message_id,
        correlation_id=updated_item.correlation_id,
        causation_id=None,
        payload={
            "mailbox_id": mailbox_id,
            "needs_you_item_id": item_id,
            "ingestion_status": updated_message.ingestion_status,
            "evidence_id": updated_message.evidence_id,
        },
    )
    return {"needs_you_item": updated_item.to_dict(), "mailbox_message": updated_message.to_dict()}
