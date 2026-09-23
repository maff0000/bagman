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
  genuine-conflict ``ConflictError`` check unchanged) for every
  match_mode OTHER than ``EXACT_DOMAIN_SUBJECT`` — a later CD-6
  hardening delta (item 3) adds a subject-BLACKLIST-specific idempotent-
  retry-vs-genuine-change check ahead of the upsert for that one
  match_mode ONLY (see :func:`resolve_domain_review`'s own docstring),
  because the residual lifecycle means an OPEN item no longer implies
  "no prior successful IGNORE" once a subject predicate is involved.
  ``ALLOW`` was
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

import time
from typing import Any, Callable, Optional, Protocol

from core.entity import EntityRepository
from core.errors import ConflictError, ValidationError
from core.timestamps import to_contract_string, utc_now
from services.evidence.intake.scanner import EvidenceSafetyScanner
from services.mailbox.domain_rule import (
    DESTINATION_MODE_FIXED,
    DESTINATION_MODE_REVIEW_REQUIRED,
    MATCH_MODE_EXACT,
    MATCH_MODE_EXACT_ADDRESS,
    MATCH_MODE_EXACT_DOMAIN_SUBJECT,
    MATCH_MODE_INCLUDE_SUBDOMAINS,
    POLICY_BLACKLIST,
    POLICY_GRAYLIST,
    POLICY_MUST_READ,
    SOURCE_OPERATOR,
    MailboxDomainRuleRepository,
    normalize_domain,
    subject_matches_predicate,
    validate_and_normalize_sender_address,
    validate_and_normalize_subject_predicate,
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

#: Domain-review `resolution` dicts recorded BEFORE this delivery (real
#: pre-existing stored rows, and every pre-existing test's own literal
#: "legacy" fixture — see
#: `tests/integration/test_mailbox_sweep.py::test_resolve_domain_review_allow_legacy_stranded_state_self_heals_without_reresolving_or_reauditing`)
#: never carried `subject_predicate_type`/`subject_predicate_value` at
#: all — those keys did not exist yet. A stored resolution missing them
#: entirely is semantically IDENTICAL to one that carries them as
#: explicit `None` (no subject predicate was ever part of that older
#: decision), so every "is this the same resolution" comparison below
#: pads a stored resolution with those two keys defaulting to `None`
#: before comparing, rather than a raw dict `==` that would otherwise
#: treat missing-vs-`None` as a spurious mismatch.
_LEGACY_RESOLUTION_DEFAULTS = {"subject_predicate_type": None, "subject_predicate_value": None}


def _resolution_matches(stored: Optional[dict], resolution: dict) -> bool:
    if not stored:
        return False
    return {**_LEGACY_RESOLUTION_DEFAULTS, **stored} == resolution


def _resolve_or_keep_open_for_residual(
    *,
    needs_you_repository: NeedsYouRepository,
    mailbox_message_repository: MailboxMessageRepository,
    mailbox_domain_rule_repository: MailboxDomainRuleRepository,
    mailbox_id: str,
    sender_domain: str,
    item_id: str,
    resolution: dict,
    actor_type: str,
    actor_id: str,
):
    """Deterministic subject-aware mailbox domain policy — the corrected
    residual-based domain-review lifecycle rule for a SUBJECT-SCOPED
    (``match_mode == MATCH_MODE_EXACT_DOMAIN_SUBJECT``) ALLOW/IGNORE
    decision ONLY (never called for any other match_mode — see
    :func:`resolve_domain_review`'s own docstring for why: a plain
    domain-level/address-level decision must retain its EXACT prior
    "always fully resolves the item" behaviour, unmodified by this
    function's own existence).

    Recomputes, from canonical persisted state, the domain's RESIDUAL
    review-eligible candidates: every remaining
    ``discovery_candidate=True``/``CHECKED_NOT_CANDIDATE`` message at
    this domain (via :meth:`MailboxMessageRepository
    .list_candidate_messages_for_domain`) whose CURRENT effective rule —
    resolved via the SAME authoritative
    :meth:`MailboxDomainRuleRepository.find_for_sender` every other
    caller in this codebase uses (mailbox/sender_address/sender_domain/
    subject) — is EITHER ``None`` OR still ``POLICY_GRAYLIST``. A
    candidate already governed by ``MUST_READ`` or ``BLACKLIST``
    (whether via the just-created subject rule or any OTHER existing
    rule) is NOT residual, even if its own ``ingestion_status`` still
    happens to read ``CHECKED_NOT_CANDIDATE`` (a ``BLACKLIST``-governed
    candidate is never processed/never changes status, by design).

    * residual > 0 -> the item stays ``OPEN``; its ``metadata`` is
      recomputed from the RESIDUAL set only (never the original full
      set) — ``candidate_message_count``/``first_seen_at``/
      ``last_seen_at``/``attachment_bearing_count`` (mirrors
      ``services.mailbox.sweep._synchronize_domain_review_aggregate``'s
      own recomputed-fields semantics exactly, applied here to the
      residual subset), PLUS two new bounded fields:
      ``governed_subject_rule_count`` and
      ``remaining_candidate_message_count`` (identical value to
      ``candidate_message_count``, exposed under this explicit name
      too).

      ``governed_subject_rule_count`` is DELIBERATELY **not** derived
      from the residual/candidate set at all — a candidate-derived count
      would be structurally near-useless: the very definition of
      "residual" (line ~296 below) excludes every candidate the subject
      rule just successfully governed, so a count taken only over
      still-eligible candidates would read 0 immediately after a subject
      rule finishes successfully processing its own candidates (the
      common case), silently misrepresenting "no subject rule governs
      anything here" right when one just did. Instead this counts real,
      persisted ``EXACT_DOMAIN_SUBJECT`` rows at this exact
      ``(mailbox_id, sender_domain)`` via
      :meth:`MailboxDomainRuleRepository.list_rules` — a stable "how
      many subject-specific carve-outs currently exist for this domain"
      signal, independent of any single candidate's own processing
      state (an interrupted/partial subject MUST_READ backfill, and a
      subject BLACKLIST rule — which never processes anything at all —
      both still correctly count here too).
    * residual == 0 -> the item resolves NORMALLY (status ->
      ``RESOLVED``), exactly like today's unconditional full-domain
      ALLOW/IGNORE resolution — this is the corrected behaviour: subject
      approval does NOT always keep the item open, only when genuine
      residual work remains.

    Never creates a new ``MAILBOX_DOMAIN_REVIEW`` item — subsequent
    narrowing (another subject ALLOW, a subject BLACKLIST, a later
    broad domain ALLOW/IGNORE, or KEEP_GRAY) always lands on this SAME
    item via the caller re-supplying the same ``item_id``.
    """
    candidates = mailbox_message_repository.list_candidate_messages_for_domain(
        mailbox_id=mailbox_id, sender_domain=sender_domain
    )
    residual = []
    for candidate in candidates:
        effective_rule = mailbox_domain_rule_repository.find_for_sender(
            mailbox_id=candidate.mailbox_id,
            sender_domain=candidate.sender_domain,
            sender_address=candidate.sender_address,
            subject=candidate.subject,
        )
        if effective_rule is None or effective_rule.policy == POLICY_GRAYLIST:
            residual.append(candidate)

    normalized_domain = normalize_domain(sender_domain)
    governed_subject_rule_count = sum(
        1
        for existing_rule in mailbox_domain_rule_repository.list_rules(mailbox_id=mailbox_id)
        if existing_rule.match_mode == MATCH_MODE_EXACT_DOMAIN_SUBJECT
        and existing_rule.sender_domain == normalized_domain
    )

    if not residual:
        return needs_you_repository.resolve_needs_you_item(
            item_id, new_status="RESOLVED", resolution=resolution, actor_type=actor_type, actor_id=actor_id
        )

    first_seen_at = min(m.received_at for m in residual)
    last_seen_at = max(m.received_at for m in residual)
    attachment_bearing_count = sum(1 for m in residual if m.has_attachments)
    return needs_you_repository.update_item_metadata(
        item_id,
        metadata_updates={
            "candidate_message_count": len(residual),
            "first_seen_at": to_contract_string(first_seen_at),
            "last_seen_at": to_contract_string(last_seen_at),
            "attachment_bearing_count": attachment_bearing_count,
            "governed_subject_rule_count": governed_subject_rule_count,
            "remaining_candidate_message_count": len(residual),
        },
    )


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
    match_mode: str,  # "EXACT" | "INCLUDE_SUBDOMAINS" | "EXACT_ADDRESS" | "EXACT_DOMAIN_SUBJECT"
    processor_hint: Optional[str],
    sender_address: Optional[str],
    subject_predicate_type: Optional[str] = None,
    subject_predicate_value: Optional[str] = None,
    sleep_fn: Callable[[float], None] = time.sleep,
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
    unchanged for every match_mode OTHER than ``EXACT_DOMAIN_SUBJECT``
    (see below): a real ``item.status != "OPEN"`` check up front (an
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

    ``decision="IGNORE"`` with ``match_mode == "EXACT_DOMAIN_SUBJECT"``
    (CD-6 GUI-operations-foundation follow-on hardening delta, item 3) —
    the ONE genuinely different shape within ``IGNORE``: the residual
    lifecycle means the triggering item can legitimately stay ``OPEN``
    after a subject BLACKLIST already fully, successfully applied (other
    residual candidates at the same domain remain ungoverned), so
    ``item.status == OPEN`` no longer implies "no prior successful
    IGNORE occurred" for this specific subject identity the way it still
    does for every other match_mode. Before mutating, the exact identity
    is looked up via ``find_exact`` (the SAME call shape the ``ALLOW``
    path's own retry/reuse check above already uses). If a rule already
    exists there AND is semantically identical to this request (same
    ``policy=BLACKLIST``/``processor_hint`` — a BLACKLIST rule carries no
    destination fields, so nothing else to compare), this is a pure
    idempotent retry: the SAME rule is reused as-is, ``upsert_rule`` is
    never called again (it would silently refresh
    ``created_at``/``updated_at``/``approved_at``), and no second
    ``MAILBOX_DOMAIN_RULE_BLACKLIST`` audit event is emitted. Either way
    (idempotent reuse OR a genuine new/changed decision, the latter
    going through the ordinary ``upsert_rule`` + audit-event path exactly
    as before), residual state is ALWAYS recomputed via
    :func:`_resolve_or_keep_open_for_residual` afterwards — residual can
    change between calls if other candidates were separately governed in
    the meantime. ``EXACT``/``INCLUDE_SUBDOMAINS``/``EXACT_ADDRESS``
    BLACKLIST are completely unaffected by this branch — for those match
    modes a successful IGNORE always fully resolves the item, so this
    residual-partial scenario is structurally impossible, and the
    already-``RESOLVED``-item idempotent-vs-conflict check above already
    correctly handles their own retry case.

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
      ``correlation_id``, with ``sleep_fn`` threaded straight through
      (see this function's own ``sleep_fn`` parameter, defaulting to
      ``time.sleep`` — CD-6 follow-on quota-scaling fix: a genuine
      ``RATE_LIMITED`` result during that backfill is now bounded-
      retried exactly once per call, per
      ``services.mailbox.sweep._reprocess_one_message``'s own docstring
      — this never changes what propagates out of THIS function; only a
      RATE_LIMITED that survives its own one retry still raises
      ``ConflictError`` here, exactly as any other genuine provider
      failure always has). **If this raises, it is left to propagate
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
    if match_mode not in (
        MATCH_MODE_EXACT, MATCH_MODE_INCLUDE_SUBDOMAINS, MATCH_MODE_EXACT_ADDRESS, MATCH_MODE_EXACT_DOMAIN_SUBJECT,
    ):
        raise ValidationError(
            "match_mode must be 'EXACT', 'INCLUDE_SUBDOMAINS', 'EXACT_ADDRESS' or 'EXACT_DOMAIN_SUBJECT' "
            f"(got {match_mode!r})"
        )
    if decision == "KEEP_GRAY" and match_mode in (MATCH_MODE_EXACT_ADDRESS, MATCH_MODE_EXACT_DOMAIN_SUBJECT):
        # Documented, deliberate out-of-scope judgment call — graylisting
        # is inherently domain-level triage ("Matt looked at this domain
        # once and deliberately left it under review"); neither an
        # address-scoped NOR a subject-scoped graylist has a real
        # operator use case this delivery builds for (mirrors
        # `validate_policy_fields_or_raise`'s own EXACT_DOMAIN_SUBJECT +
        # GRAYLIST rejection one layer down). Use 'EXACT'/
        # 'INCLUDE_SUBDOMAINS' instead.
        raise ValidationError(
            f"match_mode '{match_mode}' is not supported for decision 'KEEP_GRAY' — graylisting is "
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
    normalized_subject_predicate_type, normalized_subject_predicate_value = validate_and_normalize_subject_predicate(
        match_mode=match_mode,
        subject_predicate_type=subject_predicate_type,
        subject_predicate_value=subject_predicate_value,
    )
    resolution = {
        "decision": decision,
        "destination_entity_id": destination_entity_id,
        "destination_mode": destination_mode,
        "match_mode": match_mode,
        "sender_address": normalized_sender_address,
        "subject_predicate_type": normalized_subject_predicate_type,
        "subject_predicate_value": normalized_subject_predicate_value,
    }

    # Audit trail — snapshot whatever governs THIS EXACT identity BEFORE
    # this action, so the audit event below can carry a real
    # previous_policy/previous_destination_* alongside the new values.
    #
    # Corrected (CD-6 GUI-operations-foundation follow-on hardening
    # delta, item 2): this USED to call `find_for_sender` — the
    # most-specific-wins resolution a real INCOMING MESSAGE would hit —
    # which is the wrong question to ask here. This decision is about
    # ONE SPECIFIC identity (`match_mode`/`sender_domain`/
    # `sender_address`/`subject_predicate_type`/`subject_predicate_value`),
    # never "whatever happens to govern this domain most specifically
    # right now" — a brand-new EXACT_DOMAIN_SUBJECT rule created
    # underneath an already-governed EXACT domain, for example, must
    # report `previous_policy=None` (nothing has ever governed THIS
    # subject identity before), never the broader domain rule's own
    # policy. `find_exact` (never `find_for_sender`) is the correct
    # "what was THIS identity's own prior state" lookup — the exact same
    # discipline `find_exact`'s own docstring documents, and the exact
    # same call shape already used a few lines below for the ALLOW
    # path's own retry/reuse-vs-conflict check. For the three
    # pre-existing match modes (`EXACT`/`INCLUDE_SUBDOMAINS`/
    # `EXACT_ADDRESS`) this returns the IDENTICAL value `find_for_sender`
    # always returned in the ordinary case (this decision's own identity
    # has no more-specific override to fall behind, since it IS the
    # identity being decided) — except where `find_for_sender` was
    # ALREADY wrong for the same reason (e.g. a brand-new EXACT_ADDRESS
    # rule created underneath an existing broader domain rule), which
    # `find_exact` now also correctly fixes.
    previous_rule = mailbox_domain_rule_repository.find_exact(
        mailbox_id=mailbox_id, sender_domain=sender_domain, match_mode=match_mode,
        sender_address=normalized_sender_address,
        subject_predicate_type=normalized_subject_predicate_type,
        subject_predicate_value=normalized_subject_predicate_value,
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
                "match_mode": match_mode,
                "subject_predicate_type": normalized_subject_predicate_type,
                "subject_predicate_value": normalized_subject_predicate_value,
            },
        )
        return {"needs_you_item": item.to_dict(), "mailbox_domain_rule": rule.to_dict(), "reprocessed_messages": []}

    if decision == "IGNORE":
        # Original, simple shape — byte-for-byte unchanged. IGNORE never
        # calls the historical backfill at all, so there is nothing here
        # that can ever be interrupted mid-batch; the plain idempotent-
        # no-op-or-conflict check is exactly as correct as it always was.
        if item.status != "OPEN":
            if item.status == "RESOLVED" and _resolution_matches(item.resolution, resolution):
                return {"needs_you_item": item.to_dict(), "mailbox_domain_rule": None, "reprocessed_messages": []}
            raise ConflictError(
                f"NeedsYouItem '{item_id}' is already '{item.status}' with a different resolution — "
                "refusing to silently change an already-decided item; this is a genuine conflict, not "
                "an idempotent retry"
            )

        policy = POLICY_BLACKLIST

        # Subject-BLACKLIST partial-residual idempotent retry (CD-6
        # GUI-operations-foundation follow-on hardening delta, item 3).
        #
        # With the residual lifecycle above, an EXACT_DOMAIN_SUBJECT
        # IGNORE can succeed while the item stays OPEN (other residual
        # candidates at the same domain remain ungoverned) — so, unlike
        # every OTHER match_mode (where a successful IGNORE always fully
        # resolves the item, making `item.status == OPEN` a reliable
        # "no prior successful IGNORE occurred yet" signal), OPEN no
        # longer implies that here. Mirrors the ALLOW path's own
        # `find_exact`-based reuse-vs-conflict discipline above (see
        # that branch's own comments) rather than inventing a new shape:
        # look up the exact identity FIRST, and only actually mutate the
        # rule (`upsert_rule`, which refreshes `updated_at`/`approved_at`
        # and would wrongly look like a fresh decision) when this is
        # genuinely a NEW or CHANGED decision, never a byte-identical
        # repeat of one already durably applied.
        if match_mode == MATCH_MODE_EXACT_DOMAIN_SUBJECT:
            existing_rule = mailbox_domain_rule_repository.find_exact(
                mailbox_id=mailbox_id, sender_domain=sender_domain, match_mode=match_mode,
                sender_address=normalized_sender_address,
                subject_predicate_type=normalized_subject_predicate_type,
                subject_predicate_value=normalized_subject_predicate_value,
            )
            # BLACKLIST rules never carry destination fields (enforced by
            # `validate_policy_fields_or_raise`), so there is nothing
            # destination-shaped to compare here — `policy` +
            # `processor_hint` is the complete semantic identity of a
            # BLACKLIST decision. `existing_rule`'s own match_mode/
            # sender_address/subject_predicate_type/subject_predicate_value
            # are guaranteed equal to this request's already, by
            # construction of the `find_exact` lookup key above — no
            # need to re-compare them.
            is_idempotent_repeat = (
                existing_rule is not None
                and existing_rule.policy == POLICY_BLACKLIST
                and existing_rule.processor_hint == processor_hint
            )
            if is_idempotent_repeat:
                # A pure retry of an already-applied decision — reuse
                # the SAME rule AS-IS. Deliberately never call
                # `upsert_rule` again here (it would silently refresh
                # `created_at`/`updated_at`/`approved_at`, exactly the
                # unwanted refresh side effect a pure resume must
                # avoid), and never emit a second
                # `MAILBOX_DOMAIN_RULE_BLACKLIST` audit event (the first
                # attempt's own event already stands as the true
                # decision record).
                rule = existing_rule
                emit_blacklist_audit_event = False
            else:
                # A genuine new decision, or a genuine CHANGE of an
                # existing decision's semantics (e.g. an existing
                # MUST_READ transitioning to BLACKLIST, or a repeat
                # BLACKLIST with a different `processor_hint`) — a real
                # mutation, never silently treated as idempotent.
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
                    subject_predicate_type=normalized_subject_predicate_type,
                    subject_predicate_value=normalized_subject_predicate_value,
                )
                emit_blacklist_audit_event = True
        else:
            # EXACT / INCLUDE_SUBDOMAINS / EXACT_ADDRESS — completely
            # untouched: for these match modes a successful IGNORE
            # ALWAYS fully resolves the item (this residual-partial
            # scenario is structurally impossible), so the existing
            # idempotent-vs-conflict check further up (the
            # `item.status != "OPEN"` guard) already correctly handles
            # their own retry case; every OPEN submit here is still a
            # genuine, unconditional fresh mutation exactly as before.
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
                subject_predicate_type=normalized_subject_predicate_type,
                subject_predicate_value=normalized_subject_predicate_value,
            )
            emit_blacklist_audit_event = True

        # Deterministic subject-aware mailbox domain policy — a subject-
        # scoped BLACKLIST requires NO historical provider fetch
        # (mirrors plain domain BLACKLIST's own discovery-only
        # behaviour), but DOES trigger the same residual recomputation
        # an EXACT_DOMAIN_SUBJECT ALLOW does (item 19-23/24): the item
        # resolves only once every currently-eligible candidate at this
        # domain is governed by MUST_READ/BLACKLIST (this new subject
        # rule, or any other existing rule) — never for any OTHER
        # match_mode, which keeps its exact original unconditional-
        # resolve behaviour. This recomputation always runs, even on the
        # idempotent-retry path above — residual state can change
        # between calls if OTHER candidates were separately governed in
        # between.
        if match_mode == MATCH_MODE_EXACT_DOMAIN_SUBJECT:
            updated_item = _resolve_or_keep_open_for_residual(
                needs_you_repository=needs_you_repository,
                mailbox_message_repository=mailbox_message_repository,
                mailbox_domain_rule_repository=mailbox_domain_rule_repository,
                mailbox_id=mailbox_id,
                sender_domain=sender_domain,
                item_id=item_id,
                resolution=resolution,
                actor_type=actor_type,
                actor_id=actor_id,
            )
        else:
            updated_item = needs_you_repository.resolve_needs_you_item(
                item_id, new_status="RESOLVED", resolution=resolution, actor_type=actor_type, actor_id=actor_id
            )
        if emit_blacklist_audit_event:
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
                    "match_mode": match_mode,
                    "subject_predicate_type": normalized_subject_predicate_type,
                    "subject_predicate_value": normalized_subject_predicate_value,
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

    if match_mode == MATCH_MODE_EXACT_DOMAIN_SUBJECT:
        # Deterministic subject-aware mailbox domain policy — a
        # domain-review ALLOW answer must not create a subject rule that
        # resolves NONE of the question's actual (currently eligible)
        # candidates. Deliberately runs BEFORE any mutation, regardless
        # of item.status — mirrors the destination-field validation
        # above. This is a STRICTER guard than the generic policy-rules
        # endpoint's own `subject_predicate_observed` check (any
        # ingestion status, ever observed) — here it must be a message
        # still `discovery_candidate=True`/`CHECKED_NOT_CANDIDATE` right
        # now (see `services.mailbox.message.MailboxMessageRepository
        # .list_candidate_messages_for_domain`'s own docstring).
        eligible_candidates = mailbox_message_repository.list_candidate_messages_for_domain(
            mailbox_id=mailbox_id, sender_domain=sender_domain
        )
        if not any(
            subject_matches_predicate(
                candidate.subject,
                predicate_type=normalized_subject_predicate_type,
                predicate_value=normalized_subject_predicate_value,
            )
            for candidate in eligible_candidates
        ):
            raise ValidationError(
                f"no currently eligible candidate message at domain '{sender_domain}' matches the subject "
                f"predicate {normalized_subject_predicate_type}={normalized_subject_predicate_value!r} — a "
                "domain-review ALLOW answer must not create a subject rule that resolves none of the "
                "question's actual candidates"
            )

    if item.status == "RESOLVED":
        if not _resolution_matches(item.resolution, resolution):
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
            subject_predicate_type=normalized_subject_predicate_type,
            subject_predicate_value=normalized_subject_predicate_value,
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
                sleep_fn=sleep_fn,
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
        subject_predicate_type=normalized_subject_predicate_type,
        subject_predicate_value=normalized_subject_predicate_value,
    )
    if existing_rule is not None and existing_rule.policy == POLICY_MUST_READ:
        identical = (
            existing_rule.match_mode == match_mode
            and existing_rule.sender_address == normalized_sender_address
            and existing_rule.subject_predicate_type == normalized_subject_predicate_type
            and existing_rule.subject_predicate_value == normalized_subject_predicate_value
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
            subject_predicate_type=normalized_subject_predicate_type,
            subject_predicate_value=normalized_subject_predicate_value,
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
                "match_mode": match_mode,
                "subject_predicate_type": normalized_subject_predicate_type,
                "subject_predicate_value": normalized_subject_predicate_value,
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
            sleep_fn=sleep_fn,
        )

    # Only reached once the backfill call above has returned NORMALLY —
    # never before. This is what makes the item's own OPEN status a
    # reliable signal, on retry, that at least one candidate may still
    # be stranded.
    #
    # Deterministic subject-aware mailbox domain policy — a subject-
    # scoped ALLOW (match_mode == EXACT_DOMAIN_SUBJECT) resolves the
    # item ONLY once every currently-eligible candidate at this domain
    # is governed by MUST_READ/BLACKLIST (this new subject rule, or any
    # other existing rule); otherwise it stays OPEN with recomputed
    # residual metadata (see `_resolve_or_keep_open_for_residual`'s own
    # docstring — items 19-23/37). Every OTHER match_mode retains its
    # EXACT original unconditional-resolve behaviour, unmodified.
    if match_mode == MATCH_MODE_EXACT_DOMAIN_SUBJECT:
        updated_item = _resolve_or_keep_open_for_residual(
            needs_you_repository=needs_you_repository,
            mailbox_message_repository=mailbox_message_repository,
            mailbox_domain_rule_repository=mailbox_domain_rule_repository,
            mailbox_id=mailbox_id,
            sender_domain=sender_domain,
            item_id=item_id,
            resolution=resolution,
            actor_type=actor_type,
            actor_id=actor_id,
        )
    else:
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
        mailbox_id=mailbox_id, sender_domain=message.sender_domain, sender_address=message.sender_address,
        subject=message.subject,
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
