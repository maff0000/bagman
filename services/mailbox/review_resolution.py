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
  the triggering item); ``EXACT_ADDRESS`` validation via
  :func:`services.mailbox.domain_rule.validate_and_normalize_sender_address`;
  ``FIXED`` requiring a real entity via ``entity_repository.get_entity``;
  the exact audit-event shape/field names
  (``MAILBOX_DOMAIN_RULE_GRAYLIST``/``MAILBOX_DOMAIN_RULE_MUST_READ``/
  ``MAILBOX_DOMAIN_RULE_BLACKLIST``); and the candidate-only historical
  reprocessing call to ``reprocess_all_historical_candidates_for_domain``
  for a genuine ``ALLOW``/``MUST_READ`` resolution. ``IGNORE`` keeps its
  original ordering/idempotency shape byte-for-byte (upsert rule ->
  resolve item -> audit -> never a backfill call, and the plain
  already-``RESOLVED``-with-identical-resolution no-op vs.
  genuine-conflict ``ConflictError`` check unchanged). ``ALLOW`` was
  RESTRUCTURED (CD-6 GUI-operations-foundation follow-on WO — resumable
  MUST_READ backfill) so a genuine mid-backfill provider failure can be
  RETRIED to completion rather than stranding candidates forever behind
  an already-``RESOLVED`` item — see the function's own docstring for
  the full ordering/retry/self-heal contract this now implements.
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
    question has actually been answered). Unaffected by ``item.status``
    — evaluated first, unconditionally, before either the ``IGNORE`` or
    ``ALLOW`` shape below is even considered.

    ``decision="IGNORE"`` keeps its ORIGINAL, simple shape, byte-for-byte
    unchanged: a real ``item.status != "OPEN"`` check up front (an
    already-``RESOLVED`` item carrying this EXACT ``IGNORE`` resolution
    is a harmless idempotent no-op returning early with
    ``mailbox_domain_rule: None``; any other non-``OPEN`` status, or a
    ``RESOLVED`` item with a DIFFERENT resolution, is a genuine conflict
    -> ``ConflictError``), then upsert the ``BLACKLIST`` rule, resolve
    the item, emit ``MAILBOX_DOMAIN_RULE_BLACKLIST``, and return —
    ``reprocessed_messages`` is unconditionally ``[]`` and
    :func:`services.mailbox.sweep.reprocess_all_historical_candidates_for_domain`
    is never even called. There is nothing to make resumable here: an
    ``IGNORE`` never touches the backfill function at all, so it can
    never be interrupted mid-backfill.

    ``decision="ALLOW"`` — RESTRUCTURED (CD-6 GUI-operations-foundation
    follow-on WO) so the historical-candidate backfill
    (:func:`services.mailbox.sweep.reprocess_all_historical_candidates_for_domain`)
    is RESUMABLE across a genuine mid-batch provider/transport failure,
    never stranding already-discovered candidates behind an item that
    looks "done":

    * Destination-field validation (``destination_mode``/
      ``destination_entity_id``/a real ``entity_repository.get_entity``
      lookup) happens FIRST, unconditionally — a malformed ``ALLOW``
      request is rejected regardless of the item's current status,
      exactly as it always has been.
    * ``item.status == "RESOLVED"`` with this EXACT SAME ``ALLOW``
      resolution is the LEGACY SELF-HEAL path — a state this code can
      only encounter from an item resolved by the OLD ordering (rule
      created, item resolved, THEN backfill attempted — a genuine
      pre-this-fix stranding). The governing rule is looked up via
      ``mailbox_domain_rule_repository.find_exact`` (a data
      inconsistency — a ``RESOLVED``-ALLOW item with no matching rule at
      all — is a ``ConflictError``, never a fabricated new rule for an
      already-terminal item), and
      ``reprocess_all_historical_candidates_for_domain`` is run once
      more against it. This is naturally near-idempotent on its own once
      a prior run actually completed (the candidate query only returns
      genuinely-eligible ``CHECKED_NOT_CANDIDATE`` rows), so a
      fully-healthy legacy item costs zero provider calls here — but a
      genuinely STRANDED legacy candidate (never reached by the old
      code's own now-unreachable post-resolve backfill call) gets
      repaired. The item itself is NEVER re-resolved and NO audit event
      is emitted in this branch — an already-decided item's own decision
      record does not change.
    * ``item.status == "RESOLVED"`` with a DIFFERENT resolution is a
      genuine conflict -> ``ConflictError``, unchanged. Any OTHER
      non-``OPEN`` status is also a genuine conflict -> ``ConflictError``,
      unchanged.
    * ``item.status == "OPEN"`` is the genuine, real-time path — EITHER
      a true first-time approval, OR a RETRY of a decision whose earlier
      attempt raised mid-backfill (the item's own OPEN status is exactly
      what distinguishes this from the self-heal case above, since under
      this NEW ordering the item is only ever resolved AFTER the
      backfill call returns normally — never before). An exact rule is
      looked up via ``find_exact`` first:

      - No exact rule yet -> genuine first-time approval:
        ``upsert_rule`` creates it, and the
        ``MAILBOX_DOMAIN_RULE_MUST_READ`` audit event is emitted right
        here (correlated to the STILL-OPEN item's own ``correlation_id``
        — there is no ``updated_item`` yet, deliberately, since the item
        is not resolved until the backfill below actually succeeds).
      - An exact rule already exists and is semantically IDENTICAL to
        this request (same policy/match_mode/sender_address/
        destination_mode/destination_entity_id/processor_hint) -> this
        is a RETRY after an earlier interrupted backfill call for the
        SAME decision. The existing rule is reused AS-IS — ``upsert_rule``
        is deliberately NEVER called again here (it would silently
        refresh ``updated_at``/``approved_at``, which is exactly the
        unwanted "duplicate/refresh" side effect a pure resume must
        avoid) — and no second ``MAILBOX_DOMAIN_RULE_MUST_READ`` audit
        event is emitted (the first attempt's own audit event, from
        before the interruption, already stands as the true approval
        record).
      - An exact rule already exists with DIFFERENT semantics ->
        ``ConflictError`` — never silently overwritten; a deliberate
        policy change is a separate, different workflow from failure
        recovery.

      An existing rule at this identity whose policy is NOT
      ``MUST_READ`` (e.g. a prior ``KEEP_GRAY``/``GRAYLIST``, or a
      ``BLACKLIST``) can never be the result of an earlier ALLOW
      attempt — only a ``MUST_READ`` rule can be — so it is always
      treated as a genuine, legitimate policy TRANSITION rather than a
      retry ambiguity: the ordinary ``upsert_rule`` full-replace runs
      exactly as it always has (the architect's own "reversible"
      doctrine — there is no state a confirmed decision cannot later
      be changed away from), and the ``MAILBOX_DOMAIN_RULE_MUST_READ``
      audit event is emitted as usual.

      Either way, ``reprocess_all_historical_candidates_for_domain`` is
      then called (or re-called) against whichever rule was determined
      above, correlated to the item's OWN (still-open)
      ``correlation_id``. **If this raises, it is left to propagate
      completely uncaught** — the rule is already durably
      upserted/reused, the item is left exactly ``OPEN`` (never touched),
      and every candidate already processed before the raise keeps its
      own durable per-message outcome (each candidate commits its own
      row as it completes — there is no wrapping transaction across the
      whole backfill loop). This is the entire point of the reordering:
      an identical retry of the same decision resumes from wherever the
      previous attempt actually stopped, rather than silently doing
      nothing (the OLD ordering's already-``RESOLVED`` idempotent-no-op
      path would otherwise swallow the retry and never touch the
      stranded candidates again). Only once the backfill call returns
      NORMALLY is the item finally resolved
      (``needs_you_repository.resolve_needs_you_item(..., new_status="RESOLVED", ...)``)
      — never before.
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

    if decision == "IGNORE":
        # Original, simple shape — byte-for-byte unchanged. IGNORE never
        # calls the historical backfill at all, so there is nothing here
        # that can ever be interrupted mid-batch; the plain idempotent-
        # no-op-or-conflict check is exactly as correct as it always was.
        if item.status != "OPEN":
            if item.status == "RESOLVED" and (item.resolution or {}) == resolution:
                return {"needs_you_item": item.to_dict(), "mailbox_domain_rule": None, "reprocessed_messages": []}
            raise ConflictError(
                f"NeedsYouItem '{item_id}' is already '{item.status}' with a different resolution — "
                "refusing to silently change an already-decided item; this is a genuine conflict, not "
                "an idempotent retry"
            )

        policy = POLICY_BLACKLIST
        rule = mailbox_domain_rule_repository.upsert_rule(
            mailbox_id=mailbox_id,
            sender_domain=sender_domain,
            match_mode=match_mode,
            policy=policy,
            destination_entity_id=None,
            destination_mode=None,
            source=SOURCE_OPERATOR,
            processor_hint=processor_hint,
            approved_at=utc_now(),
            sender_address=normalized_sender_address,
        )
        updated_item = needs_you_repository.resolve_needs_you_item(
            item_id, new_status="RESOLVED", resolution=resolution, actor_type=actor_type, actor_id=actor_id
        )
        api.record_audit_event(
            event_type="MAILBOX_DOMAIN_RULE_BLACKLIST",
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
                "new_destination_entity_id": None,
                "previous_destination_mode": previous_destination_mode,
                "new_destination_mode": None,
            },
        )
        return {"needs_you_item": updated_item.to_dict(), "mailbox_domain_rule": rule.to_dict(), "reprocessed_messages": []}

    # decision == "ALLOW" — see this function's own docstring above for
    # the full ordering/retry/self-heal contract implemented below.
    policy = POLICY_MUST_READ
    if destination_mode not in (DESTINATION_MODE_FIXED, DESTINATION_MODE_REVIEW_REQUIRED):
        raise ValidationError("destination_mode must be 'FIXED' or 'REVIEW_REQUIRED' when decision is 'ALLOW'")
    if destination_mode == DESTINATION_MODE_FIXED and not destination_entity_id:
        raise ValidationError("destination_entity_id is required when destination_mode is 'FIXED'")
    if destination_entity_id:
        # Real existence check — never trust a caller-supplied
        # entity_id without proving it real first. Deliberately runs
        # regardless of item.status — a malformed ALLOW request is
        # rejected the same way no matter what state the item is in.
        entity_repository.get_entity(destination_entity_id)

    if item.status == "RESOLVED":
        if (item.resolution or {}) != resolution:
            raise ConflictError(
                f"NeedsYouItem '{item_id}' is already '{item.status}' with a different resolution — "
                "refusing to silently change an already-decided item; this is a genuine conflict, not "
                "an idempotent retry"
            )
        # Legacy self-heal — see this function's own docstring. Only
        # reachable for an item resolved under the OLD (pre-this-fix)
        # ordering; under the NEW ordering below, an OPEN item is never
        # resolved until its own backfill call has already returned
        # normally, so this state can never be freshly produced again.
        existing_rule = mailbox_domain_rule_repository.find_exact(
            mailbox_id=mailbox_id, sender_domain=sender_domain, match_mode=match_mode,
            sender_address=normalized_sender_address,
        )
        if existing_rule is None:
            raise ConflictError(
                f"NeedsYouItem '{item_id}' is RESOLVED with an ALLOW resolution but no matching "
                "MailboxDomainRule exists at this identity — a genuine data inconsistency; refusing "
                "to fabricate a new rule for an already-terminal item"
            )
        reprocessed = []
        if sender_domain:
            reprocessed = reprocess_all_historical_candidates_for_domain(
                mailbox=mailbox,
                mailbox_source_id=mailbox_source_id,
                sender_domain=sender_domain,
                rule=existing_rule,
                mailbox_domain_rule_repository=mailbox_domain_rule_repository,
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
        return {
            "needs_you_item": item.to_dict(),
            "mailbox_domain_rule": existing_rule.to_dict(),
            "reprocessed_messages": [m.to_dict() for m in reprocessed],
        }

    if item.status != "OPEN":
        raise ConflictError(
            f"NeedsYouItem '{item_id}' is already '{item.status}' with a different resolution — "
            "refusing to silently change an already-decided item; this is a genuine conflict, not "
            "an idempotent retry"
        )

    # item.status == "OPEN" — either a genuine first-time approval
    # (including a legitimate policy TRANSITION onto an existing non-
    # MUST_READ rule, e.g. a prior KEEP_GRAY/GRAYLIST or BLACKLIST at
    # this exact identity — the architect's own "reversible" doctrine:
    # there is no state a confirmed decision cannot later be changed
    # away from), or a retry of a decision whose earlier attempt raised
    # mid-backfill. Only an existing rule that is ALREADY `MUST_READ`
    # can possibly BE the result of an earlier (interrupted or
    # completed) ALLOW attempt — a rule at any other policy can never
    # be mistaken for one, so it is always a genuine transition,
    # handled by the plain `upsert_rule` replace below exactly as
    # before this WO.
    existing_rule = mailbox_domain_rule_repository.find_exact(
        mailbox_id=mailbox_id, sender_domain=sender_domain, match_mode=match_mode,
        sender_address=normalized_sender_address,
    )
    if existing_rule is not None and existing_rule.policy == POLICY_MUST_READ:
        identical = (
            existing_rule.match_mode == match_mode
            and existing_rule.sender_address == normalized_sender_address
            and existing_rule.destination_mode == destination_mode
            and existing_rule.destination_entity_id == destination_entity_id
            and existing_rule.processor_hint == processor_hint
        )
        if not identical:
            raise ConflictError(
                f"an exact MailboxDomainRule already exists for mailbox '{mailbox_id}' at this "
                f"identity (sender_domain={sender_domain!r}, match_mode={match_mode!r}, "
                f"sender_address={normalized_sender_address!r}) with different semantics than "
                f"requested (existing policy={existing_rule.policy!r}, "
                f"destination_mode={existing_rule.destination_mode!r}, "
                f"destination_entity_id={existing_rule.destination_entity_id!r}, "
                f"processor_hint={existing_rule.processor_hint!r}) — refusing to silently overwrite "
                "it; a deliberate policy change is a separate workflow, not failure recovery"
            )
        # Genuine reuse (a retry after an earlier interrupted backfill
        # call) — deliberately never call `upsert_rule` again here: it
        # would silently refresh `updated_at`/`approved_at`, exactly the
        # unwanted duplicate/refresh side effect a pure resume must
        # avoid. No second MAILBOX_DOMAIN_RULE_MUST_READ audit event
        # either — the first attempt's own event already stands.
        rule = existing_rule
    else:
        # Genuine first-time approval — either no rule existed yet at
        # this identity, or one exists at a DIFFERENT (non-MUST_READ)
        # policy and this ALLOW is a legitimate transition onto it (a
        # prior KEEP_GRAY/GRAYLIST or BLACKLIST being reversed) —
        # `upsert_rule` is a plain full replace either way, exactly as
        # before this WO.
        rule = mailbox_domain_rule_repository.upsert_rule(
            mailbox_id=mailbox_id,
            sender_domain=sender_domain,
            match_mode=match_mode,
            policy=policy,
            destination_entity_id=destination_entity_id,
            destination_mode=destination_mode,
            source=SOURCE_OPERATOR,
            processor_hint=processor_hint,
            approved_at=utc_now(),
            sender_address=normalized_sender_address,
        )
        api.record_audit_event(
            event_type="MAILBOX_DOMAIN_RULE_MUST_READ",
            actor_type=actor_type,
            actor_id=actor_id,
            subject_type="MailboxDomainRule",
            subject_id=rule.rule_id,
            # The item's own (still-OPEN) correlation_id — there is no
            # `updated_item` yet, deliberately: the item is not resolved
            # until the backfill below returns normally.
            correlation_id=item.correlation_id,
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
    #
    # Resumability (the whole point of this reordering): if this call
    # raises, it is left to propagate COMPLETELY UNCAUGHT — never
    # wrapped, never swallowed, and the item is NEVER resolved below.
    # The rule above is already durably upserted/reused, and every
    # candidate already processed before the raise keeps its own
    # durable per-message outcome. A retry of this exact same decision
    # re-enters this function with `item.status == "OPEN"` still, finds
    # the SAME rule via `find_exact` above (reused, not re-upserted),
    # and resumes the backfill from wherever
    # `list_candidate_messages_for_domain` naturally continues (already-
    # final candidates are no longer `CHECKED_NOT_CANDIDATE` and so are
    # no longer returned).
    reprocessed: list = []
    if sender_domain:
        reprocessed = reprocess_all_historical_candidates_for_domain(
            mailbox=mailbox,
            mailbox_source_id=mailbox_source_id,
            sender_domain=sender_domain,
            rule=rule,
            mailbox_domain_rule_repository=mailbox_domain_rule_repository,
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

    # Only reached once the backfill call above has returned NORMALLY —
    # never before. This is what makes the item's own OPEN status a
    # reliable signal, on retry, that at least one candidate may still
    # be stranded.
    updated_item = needs_you_repository.resolve_needs_you_item(
        item_id, new_status="RESOLVED", resolution=resolution, actor_type=actor_type, actor_id=actor_id
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
