"""``run_sweep`` — the provider-neutral sweep orchestration engine (CD-6
Slice 4).

Provider-neutral by construction
------------------------------------
This module never imports anything from ``services/mailbox/microsoft/``
except the ``MicrosoftGraphMailboxAdapter`` TYPE it is handed (and even
that only for a type hint — every call site uses the adapter through
its own three narrow methods:
:meth:`~services.mailbox.microsoft.adapter.MicrosoftGraphMailboxAdapter.fetch_folder_delta`/
:meth:`~....fetch_message_content`/:meth:`~....report_connection_error`).
A future IMAP/Gmail adapter implementing the same narrow surface could
be swapped in without touching a single line here — this is the
"provider-neutral sweep orchestration" half of the architect's own
required separation (see that adapter module's own docstring for the
other half).

Hard scope boundary — read before extending
--------------------------------------------------
This module (and everything it calls) NEVER performs real AI/ML
classification, proposes an account/entity with any confidence beyond
an operator-approved rule, or makes any accounting decision. The
CD-6 architect amendment's own Stage-B "domain gate" and Stage-A-only
"is this worth one Needs You question" heuristic
(``services.mailbox.discovery_signals``) are both explicitly bounded,
non-AI, keyword-level checks — never a classifier, never a confidence
score, never a model call. No "What/Why"/coding field exists anywhere
in this call chain, and none may be added here — real accounting
classification remains Slice 5's entire scope. If a future change to
this file introduces anything resembling that, it does not belong here.

Two-stage mail processing (CD-6 architect amendment, supersedes Slice
4A's "ingest everything unconditionally" sweep)
------------------------------------------------------------------------
Every observed message is CHECKED; not every message is fully
ingested. For each message in a delta round:

1. Stage A (always) — bounded discovery metadata is captured: sender
   domain, attachment filename/content-type/size list, and best-effort
   SPF/DKIM/DMARC signals — all from the SAME delta request Graph
   already returns (see ``services/mailbox/microsoft/graph_client.py``'s
   own ``$select``/``$expand`` additions) — never a body/`$value` fetch.
2. Stage B (the domain gate) — the sender's domain is looked up in this
   mailbox's own ``services.mailbox.domain_rule.MailboxDomainRuleRepository``:

   * ``ALLOWED`` — proceeds with the existing Slice 4A full-MIME-fetch-
     and-evidence-ingest path, attaching the rule's
     ``destination_entity_id``/``destination_mode``/``processor_hint``
     to the resulting ``MailboxMessage.metadata["routing"]`` as
     NON-AUTHORITATIVE routing metadata (never accounting truth).
   * ``IGNORED`` — marked ``CHECKED_NOT_CANDIDATE``; no MIME fetch;
     the rule's ``last_seen_at`` is touched; never a Needs You item.
   * no rule yet — ``services.mailbox.discovery_signals
     .evaluate_discovery_candidate`` runs (Stage-A metadata only). A
     credible candidate raises (or reuses) exactly one
     ``services.needs_you.needs_you.ITEM_TYPE_MAILBOX_DOMAIN_REVIEW``
     item per ``(mailbox_id, sender_domain)`` — still no MIME fetch (see
     :func:`reprocess_message_after_domain_rule_approval` for how the
     ONE triggering message gets its MIME fetched immediately once an
     operator approves). A non-candidate is marked
     ``CHECKED_NOT_CANDIDATE`` and nothing further happens.

A message already in any of ``services.mailbox.message
.FINAL_INGESTION_STATUSES`` (which now includes
``CHECKED_NOT_CANDIDATE``) is never re-decided by a later sweep, even
if the governing ``MailboxDomainRule``'s policy has since changed — a
**documented judgment call**: a rule-policy change does NOT
automatically reprocess previously-seen messages under the OLD policy.
Only the ONE specific message that triggered an open
``MAILBOX_DOMAIN_REVIEW`` Needs You item is guaranteed immediate
reprocessing, as part of resolving THAT item (architect spec §4's own
explicit requirement) — a broader "reprocess every historically-ignored
message under the new rule" would be a large, potentially expensive
backfill BAGMAN does not perform as a side effect of one operator
decision; that would need to be an explicit, separate, later operator
action. Flagged here prominently for PL/architect review, not silently
chosen.

Historical bootstrap boundary — governed, not hardcoded (CD-6 architect
amendment, supersedes Slice 4A's flat 7-day boundary)
------------------------------------------------------------------------
:func:`compute_bootstrap_floor` replaces the old
``DEFAULT_BOOTSTRAP_DAYS`` constant entirely. A mailbox's FIRST-EVER
sweep of a folder now bootstraps from the GLOBAL MINIMUM (earliest)
``core.entity.GovernedEntity.email_bootstrap_floor_at`` across every
governed entity currently seeded — never something narrower tied to a
mailbox's own optional ``default_entity_id`` hint (which remains just a
hint, never a restriction): any mailbox could in principle discover
REVIEW_REQUIRED-routed evidence for ANY entity via the operator-
approval path above, so the only safe, conservative, spec-compliant
choice is the global minimum. If ANY governed entity is missing this
configuration, :func:`compute_bootstrap_floor` raises
``core.errors.ConflictError`` rather than inventing a fallback or
silently excluding that entity (architect spec: "surface the missing
configuration before historical sweep") — checked on EVERY sweep call,
not merely a mailbox's first, which is the simpler and more
conservative of two reasonable designs (see that function's own
docstring for why).

Idempotency & the cursor-advance rule — read before changing anything
about failure handling
--------------------------------------------------------------------------
Idempotency is enforced by ``services.mailbox.message
.MailboxMessageRepository.record_observation``'s own (mailbox_id,
immutable_provider_message_id) resolve-or-create semantics and by
``services.evidence.evidence.EvidenceRepository.register_evidence``'s
own ``external_reference`` replay detection — NEVER by the cursor
itself (architect spec, verbatim: "idempotency, not the cursor,
prevents duplicate evidence — correctness over minimizing re-reads"). A
folder's durable ``delta_link`` cursor is therefore only ever advanced
via :meth:`~services.mailbox.cursor.MailboxFolderCursorRepository
.advance_cursor` AFTER that folder's ENTIRE delta round (every page,
every message) has been safely, durably handled — see
:data:`_DURABLY_HANDLED_OUTCOMES` below for the precise, documented
distinction between a durably-handled terminal outcome (quarantine,
oversize, vanished — all of which advance the cursor past that
message) and a genuinely transient failure (network/5xx/429-exhausted/
malformed-response — none of which advance the folder's cursor at all
this round, so the exact same message is safely re-seen and re-
attempted on the NEXT sweep).

Concurrency — one sweep per mailbox at a time
--------------------------------------------------
:func:`run_sweep` acquires ``services.mailbox.lock.MailboxSweepLock``
for the WHOLE duration of the sweep (see that module's own docstring
for why a dedicated lease, not a database row lock). A second
concurrent sweep attempt for the SAME mailbox raises
:class:`services.mailbox.lock.MailboxSweepLockError` immediately,
before any `MailboxSweepRun` row is even created — there is nothing
for THIS declined attempt to durably record; the mailbox is simply
busy (the caller — the HTTP router — maps this to an honest 409).
"""
from __future__ import annotations

import time
from datetime import datetime
from typing import Any, Mapping, Optional, Protocol, Sequence

from core.entity import EntityRepository
from core.errors import ConflictError
from core.timestamps import utc_now
from services.evidence.intake.scanner import EvidenceSafetyScanner
from services.mailbox.cursor import MailboxFolderCursorRepository
from services.mailbox.discovery_signals import evaluate_discovery_candidate
from services.mailbox.domain_rule import (
    POLICY_ALLOWED,
    POLICY_IGNORED,
    MailboxDomainRule,
    MailboxDomainRuleRepository,
)
from services.mailbox.lock import MailboxSweepLock
from services.mailbox.mailbox import (
    CONNECTION_STATE_CONNECTED,
    MailboxSource,
    MailboxSourceRepository,
)
from services.mailbox.message import (
    FINAL_INGESTION_STATUSES,
    FOLDER_INBOX,
    FOLDER_JUNK,
    INGESTION_STATUS_CHECKED_NOT_CANDIDATE,
    INGESTION_STATUS_FAILED,
    INGESTION_STATUS_INGESTED,
    INGESTION_STATUS_QUARANTINED,
    INGESTION_STATUS_VANISHED,
    MailboxMessage,
    MailboxMessageRepository,
)
from services.mailbox.microsoft.evidence_ingest import (
    INGEST_STATUS_FAILED,
    INGEST_STATUS_INGESTED,
    INGEST_STATUS_QUARANTINED,
    ingest_email_evidence,
)
from services.mailbox.microsoft.graph_client import GraphOutcomeStatus
from services.mailbox.sweep_run import MailboxSweepRunRepository, SweepFailureReason
from services.needs_you.needs_you import (
    ALLOWED_ACTION_MAILBOX_DOMAIN_REVIEW,
    ITEM_TYPE_MAILBOX_DOMAIN_REVIEW,
    NeedsYouRepository,
)

#: The two, and only two, folders this slice ever sweeps (architect
#: spec — never Sent Items/Deleted Items/Archive/custom folders).
SWEPT_FOLDERS = (FOLDER_INBOX, FOLDER_JUNK)


def compute_bootstrap_floor(entity_repository: EntityRepository) -> datetime:
    """The GLOBAL MINIMUM ``email_bootstrap_floor_at`` across every
    governed entity currently seeded — see module docstring's
    "Historical bootstrap boundary" section for the full reasoning.

    Raises:
        core.errors.ConflictError: no governed entities are seeded yet,
            or at least one is missing ``email_bootstrap_floor_at``
            (architect spec: "surface the missing configuration before
            historical sweep" — this never invents a fallback date and
            never silently excludes the incomplete entity from the
            minimum).
    """
    entities = entity_repository.list_entities()
    if not entities:
        raise ConflictError(
            "cannot compute the mailbox historical-sweep bootstrap floor: no governed entities "
            "are seeded yet — seed the canonical entity registry before running any mailbox sweep"
        )
    missing = sorted(e.canonical_name for e in entities if e.email_bootstrap_floor_at is None)
    if missing:
        raise ConflictError(
            "cannot compute the mailbox historical-sweep bootstrap floor: the following governed "
            f"entities are missing email_bootstrap_floor_at configuration: {missing} — surface and "
            "resolve this configuration before any historical sweep runs (CD-6 architect amendment "
            "§1); BAGMAN never invents a fallback bootstrap date or silently excludes an entity"
        )
    return min(e.email_bootstrap_floor_at for e in entities)


def _extract_sender_domain(sender_address: Optional[str]) -> Optional[str]:
    """The one place this module derives a domain from an email
    address — mirrors ``services.mailbox.mailbox.normalize_email``'s
    own normalisation discipline. Returns ``None`` for an absent/
    malformed address rather than guessing."""
    if not sender_address or "@" not in sender_address:
        return None
    domain = sender_address.rsplit("@", 1)[-1].strip().lower()
    return domain or None


def _attachment_metadata_dicts(attachment_metadata: Sequence[Mapping[str, Any]]) -> tuple:
    return tuple(dict(a) for a in attachment_metadata)


def _routing_metadata(rule: MailboxDomainRule) -> dict:
    """Non-authoritative ROUTING metadata attached to an ALLOWED-rule
    message's own ``MailboxMessage.metadata["routing"]`` — architect
    spec §5: "never as accounting truth". Deliberately folded into the
    existing free-form ``metadata`` field rather than three new typed
    ``MailboxMessage`` columns — a documented, in-scope judgment call
    (see this delivery's own final report)."""
    return {
        "routing": {
            "destination_entity_id": rule.destination_entity_id,
            "destination_mode": rule.destination_mode,
            "processor_hint": rule.processor_hint,
            "mailbox_domain_rule_id": rule.rule_id,
        }
    }

#: Bounded worst-case backoff for one rate-limited delta page — mirrors
#: `services.xero.sync._MAX_RATE_LIMIT_BACKOFF_SECONDS`'s own reasoning.
_MAX_RATE_LIMIT_BACKOFF_SECONDS = 30.0

#: A message outcome this durably records (a genuine, non-lost terminal
#: record) — counts as PROCESSED for cursor-advance purposes even
#: though it is not a plain success (architect spec's own explicit,
#: subtle distinction — see module docstring).
_DURABLY_HANDLED_INGEST_OUTCOMES = frozenset(
    {INGEST_STATUS_QUARANTINED, INGEST_STATUS_FAILED}  # FAILED here == oversize, a governed terminal outcome
)


def _find_open_domain_review_item(
    needs_you_repository: NeedsYouRepository, *, mailbox_id: str, sender_domain: str
):
    """Application-level duplicate-prevention for
    ``ITEM_TYPE_MAILBOX_DOMAIN_REVIEW`` — NOT the generic
    ``(item_type, source_object_reference)`` dedupe mechanism, because
    ``source_object_reference`` here is the real, canonical
    ``mailbox_message_id`` of the ONE triggering message (a genuine
    identifier — needed for immediate reprocessing on approval, see
    module docstring), not a ``(mailbox_id, sender_domain)`` composite
    (which is not itself a valid canonical identifier — see the
    contract's own closed UUID-shaped ``source_object_reference``
    field). A second, different message from the SAME still-unresolved
    domain must reuse the SAME open item rather than raising a new one
    per message (architect spec §4) — this scan (bounded to currently-
    OPEN items of this one item_type) is how that is enforced."""
    for item in needs_you_repository.list_needs_you_items(
        item_type=ITEM_TYPE_MAILBOX_DOMAIN_REVIEW, domain="MAILBOX", status="OPEN"
    ):
        if item.metadata.get("mailbox_id") == mailbox_id and item.metadata.get("sender_domain") == sender_domain:
            return item
    return None


def _create_or_reuse_domain_review_item(
    needs_you_repository: NeedsYouRepository,
    *,
    mailbox: MailboxSource,
    sender_domain: str,
    message: MailboxMessage,
    reason: str,
):
    existing = _find_open_domain_review_item(needs_you_repository, mailbox_id=mailbox.mailbox_id, sender_domain=sender_domain)
    if existing is not None:
        return existing
    return needs_you_repository.create_needs_you_item(
        item_type=ITEM_TYPE_MAILBOX_DOMAIN_REVIEW,
        domain="MAILBOX",
        source_object_reference=message.mailbox_message_id,
        question=(
            f"New invoice/accounting-document source detected — {mailbox.display_name} "
            f"({mailbox.email_address}), domain '{sender_domain}'"
        ),
        allowed_action_type=ALLOWED_ACTION_MAILBOX_DOMAIN_REVIEW,
        priority="NORMAL",
        metadata={
            "mailbox_id": mailbox.mailbox_id,
            "email_address": mailbox.email_address,
            "sender_domain": sender_domain,
            "triggering_mailbox_message_id": message.mailbox_message_id,
            "reason": reason,
        },
    )


class _AdapterProtocol(Protocol):
    def fetch_folder_delta(
        self,
        *,
        mailbox_id: str,
        folder: str,
        delta_link: Optional[str] = None,
        next_link: Optional[str] = None,
        bootstrap_timestamp: Optional[datetime] = None,
    ): ...

    def fetch_message_content(self, *, mailbox_id: str, immutable_message_id: str): ...

    def report_connection_error(self, mailbox_id: str, *, error_code: str, error_detail: str) -> None: ...


class _EvidenceAPIProtocol(Protocol):
    def register_evidence(self, **kwargs): ...

    def record_provenance(self, **kwargs): ...

    def record_audit_event(self, **kwargs): ...


class _ObjectStoreProtocol(Protocol):
    def put(self, object_id, content_hash, data): ...

    def put_prefixed(self, prefix, object_id, content_hash, data): ...


class _SweepStopped(Exception):
    """Internal-only signal: a whole-sweep-stopping condition (a
    reconnect-required auth failure, or BAGMAN's own configuration
    being broken) was hit — raised, caught once at the top of
    :func:`run_sweep`, never escapes this module."""

    def __init__(self, *, error_code: str, error_detail: str) -> None:
        super().__init__(error_detail)
        self.error_code = error_code
        self.error_detail = error_detail


def run_sweep(
    *,
    mailbox: MailboxSource,
    mailbox_source_id: str,
    trigger: str,
    adapter: _AdapterProtocol,
    message_repository: MailboxMessageRepository,
    sweep_run_repository: MailboxSweepRunRepository,
    cursor_repository: MailboxFolderCursorRepository,
    sweep_lock: MailboxSweepLock,
    mailbox_repository: MailboxSourceRepository,
    domain_rule_repository: MailboxDomainRuleRepository,
    needs_you_repository: NeedsYouRepository,
    entity_repository: EntityRepository,
    api: _EvidenceAPIProtocol,
    object_store: _ObjectStoreProtocol,
    scanner: EvidenceSafetyScanner,
    actor_type: str,
    actor_id: str,
    now: Optional[datetime] = None,
    sleep_fn=time.sleep,
):
    """Run exactly one sweep attempt for `mailbox`. Always returns a
    terminal `MailboxSweepRun` — never raises for an ordinary provider/
    auth/rate-limit/malformed-response failure (all of those are
    PARTIAL/FAILED runs). Raises:

    * `core.errors.ConflictError` — `mailbox` is not
      ACTIVE+CONNECTED (a defensive backstop; the caller/GUI is
      expected to never offer 'Sweep now' otherwise — mirrors
      `services.xero.sync.run_sync`'s identical precondition-raise
      shape); OR the governed-entity bootstrap-floor configuration is
      incomplete (see `compute_bootstrap_floor`).
    * `services.mailbox.lock.MailboxSweepLockError` — a concurrent
      sweep of the SAME mailbox is already in progress (see module
      docstring's "Concurrency" section).
    """
    if mailbox.status != "ACTIVE" or mailbox.connection_state != CONNECTION_STATE_CONNECTED:
        raise ConflictError(
            f"mailbox '{mailbox.mailbox_id}' is not ACTIVE+CONNECTED — cannot sweep "
            "(the caller should not offer 'Sweep now' in this state)"
        )

    resolved_now = now if now is not None else utc_now()
    # Validated/computed on EVERY sweep call, not merely a mailbox's
    # first — see module docstring's "Historical bootstrap boundary"
    # section for why this is the simpler, more conservative choice.
    bootstrap_timestamp = compute_bootstrap_floor(entity_repository)

    with sweep_lock.held(mailbox.mailbox_id):
        run = sweep_run_repository.create_run(mailbox_id=mailbox.mailbox_id, trigger=trigger)

        folders_attempted: list[str] = []
        messages_seen = 0
        messages_new = 0
        evidence_created = 0
        duplicates = 0
        quarantined = 0
        failures = 0
        any_folder_had_transient_failure = False
        any_folder_fully_succeeded = False
        # PL-review finding: RESYNC_REQUIRED (Graph invalidated this
        # folder's delta token, e.g. HTTP 410 Gone) is NOT an ordinary
        # transient failure — a plain retry on the next sweep will hit
        # the exact same RESYNC_REQUIRED status forever, since the
        # stale delta_link never becomes valid again on its own. The
        # architect's own spec requires this to "surface a governed
        # resync-required condition" distinctly, never blend into a
        # generic transient-failure bucket an operator would read as
        # "will probably self-heal by waiting" — tracked separately so
        # the completed run's error_code can say exactly that.
        any_folder_needs_resync = False

        try:
            for folder in SWEPT_FOLDERS:
                folders_attempted.append(folder)
                cursor = cursor_repository.get_or_bootstrap(
                    mailbox_id=mailbox.mailbox_id,
                    provider_kind=mailbox.provider_kind,
                    folder=folder,
                    bootstrap_timestamp=bootstrap_timestamp,
                )
                is_bootstrap_round = cursor.delta_link is None

                folder_had_transient_failure = False
                final_delta_link: Optional[str] = None
                next_link: Optional[str] = None
                first_page = True

                while True:
                    page = adapter.fetch_folder_delta(
                        mailbox_id=mailbox.mailbox_id,
                        folder=folder,
                        delta_link=cursor.delta_link if (first_page and not is_bootstrap_round) else None,
                        next_link=next_link if not first_page else None,
                        bootstrap_timestamp=cursor.bootstrap_timestamp if (first_page and is_bootstrap_round) else None,
                    )
                    first_page = False

                    if page.status == GraphOutcomeStatus.RATE_LIMITED:
                        backoff = min(page.retry_after_seconds or 5.0, _MAX_RATE_LIMIT_BACKOFF_SECONDS)
                        sleep_fn(backoff)
                        page = adapter.fetch_folder_delta(
                            mailbox_id=mailbox.mailbox_id,
                            folder=folder,
                            delta_link=None,
                            next_link=next_link,
                            bootstrap_timestamp=None,
                        )
                        if page.status == GraphOutcomeStatus.RATE_LIMITED:
                            folder_had_transient_failure = True
                            failures += 1
                            break

                    if page.status in (GraphOutcomeStatus.AUTH_ERROR,):
                        raise _SweepStopped(
                            error_code=SweepFailureReason.TOKEN_REFRESH_FAILED,
                            error_detail=page.error_detail or "Microsoft OAuth token invalid/expired; reconnect required",
                        )
                    if page.status == GraphOutcomeStatus.CONFIG_ERROR:
                        raise _SweepStopped(
                            error_code=SweepFailureReason.CONFIG_ERROR,
                            error_detail=page.error_detail or "Microsoft mail is not configured",
                        )
                    if page.status == GraphOutcomeStatus.PERMISSION_ERROR:
                        adapter.report_connection_error(
                            mailbox.mailbox_id,
                            error_code=SweepFailureReason.PERMISSION_ERROR,
                            error_detail=page.error_detail or "permission error",
                        )
                        folder_had_transient_failure = True
                        failures += 1
                        break
                    if page.status == GraphOutcomeStatus.RESYNC_REQUIRED:
                        folder_had_transient_failure = True
                        any_folder_needs_resync = True
                        failures += 1
                        break
                    if page.status in (
                        GraphOutcomeStatus.TRANSPORT_ERROR,
                        GraphOutcomeStatus.TIMEOUT,
                        GraphOutcomeStatus.MALFORMED_RESPONSE,
                        GraphOutcomeStatus.PROVIDER_ERROR,
                    ):
                        folder_had_transient_failure = True
                        failures += 1
                        break

                    # OK — process this page's messages.
                    for msg in page.messages:
                        if msg.removed:
                            continue
                        messages_seen += 1
                        existing = message_repository.find_by_provider_id(mailbox.mailbox_id, msg.immutable_id)
                        if existing is not None and existing.ingestion_status in FINAL_INGESTION_STATUSES:
                            duplicates += 1
                            message_repository.record_observation(
                                mailbox_id=mailbox.mailbox_id,
                                provider_kind=mailbox.provider_kind,
                                immutable_provider_message_id=msg.immutable_id,
                                internet_message_id=msg.internet_message_id,
                                observed_folder=folder,
                                subject=msg.subject,
                                sender_address=msg.sender_address,
                                sender_display_name=msg.sender_display_name,
                                received_at=msg.received_at or resolved_now,
                                has_attachments=msg.has_attachments,
                                ingestion_status=existing.ingestion_status,
                                evidence_id=existing.evidence_id,
                                sender_domain=_extract_sender_domain(msg.sender_address),
                                attachment_metadata=_attachment_metadata_dicts(msg.attachment_metadata),
                                auth_signals=dict(msg.auth_signals),
                            )
                            continue

                        # -- Stage A (always) + Stage B (the domain gate) --
                        sender_domain = _extract_sender_domain(msg.sender_address)
                        attachment_metadata = _attachment_metadata_dicts(msg.attachment_metadata)
                        auth_signals = dict(msg.auth_signals)

                        def _record_discovery_only(status: str) -> MailboxMessage:
                            message, _ = message_repository.record_observation(
                                mailbox_id=mailbox.mailbox_id,
                                provider_kind=mailbox.provider_kind,
                                immutable_provider_message_id=msg.immutable_id,
                                internet_message_id=msg.internet_message_id,
                                observed_folder=folder,
                                subject=msg.subject,
                                sender_address=msg.sender_address,
                                sender_display_name=msg.sender_display_name,
                                received_at=msg.received_at or resolved_now,
                                has_attachments=msg.has_attachments,
                                ingestion_status=status,
                                sender_domain=sender_domain,
                                attachment_metadata=attachment_metadata,
                                auth_signals=auth_signals,
                            )
                            return message

                        rule = (
                            domain_rule_repository.find_for_sender(mailbox_id=mailbox.mailbox_id, sender_domain=sender_domain)
                            if sender_domain
                            else None
                        )

                        if rule is not None and rule.policy == POLICY_IGNORED:
                            domain_rule_repository.touch_last_seen(
                                mailbox_id=mailbox.mailbox_id, sender_domain=rule.sender_domain, seen_at=resolved_now
                            )
                            messages_new += 1
                            _record_discovery_only(INGESTION_STATUS_CHECKED_NOT_CANDIDATE)
                            continue

                        if rule is None or rule.policy != POLICY_ALLOWED:
                            # Unknown domain — the bounded, non-AI
                            # discovery-candidate heuristic (architect
                            # spec §4). Never a MIME fetch either way.
                            signal = evaluate_discovery_candidate(
                                subject=msg.subject, attachment_metadata=attachment_metadata
                            )
                            messages_new += 1
                            message = _record_discovery_only(INGESTION_STATUS_CHECKED_NOT_CANDIDATE)
                            if signal.is_candidate and sender_domain:
                                _create_or_reuse_domain_review_item(
                                    needs_you_repository,
                                    mailbox=mailbox,
                                    sender_domain=sender_domain,
                                    message=message,
                                    reason=signal.reason,
                                )
                            continue

                        # rule.policy == POLICY_ALLOWED — the existing
                        # Slice 4A full-MIME-fetch-and-evidence-ingest
                        # path, now gated behind an approved rule.
                        domain_rule_repository.touch_last_seen(
                            mailbox_id=mailbox.mailbox_id, sender_domain=rule.sender_domain, seen_at=resolved_now
                        )

                        content_result = adapter.fetch_message_content(
                            mailbox_id=mailbox.mailbox_id, immutable_message_id=msg.immutable_id
                        )

                        if content_result.status == GraphOutcomeStatus.NOT_FOUND:
                            messages_new += 1
                            _record_discovery_only(INGESTION_STATUS_VANISHED)
                            continue

                        if content_result.status == GraphOutcomeStatus.AUTH_ERROR:
                            raise _SweepStopped(
                                error_code=SweepFailureReason.TOKEN_REFRESH_FAILED,
                                error_detail=content_result.error_detail or "Microsoft OAuth token invalid/expired",
                            )

                        if content_result.status != GraphOutcomeStatus.OK:
                            # Transient content-fetch failure: leave NO
                            # message row at all (see module docstring)
                            # so the next sweep re-sees this message as
                            # NEW and retries. Deliberately NOT counted
                            # in `messages_new` (that field means "a new
                            # MailboxMessage row was created this run" —
                            # see the contract's own field description)
                            # — only `failures` reflects this attempt.
                            failures += 1
                            folder_had_transient_failure = True
                            continue

                        messages_new += 1
                        outcome = ingest_email_evidence(
                            raw_mime_bytes=content_result.content or b"",
                            mailbox_id=mailbox.mailbox_id,
                            mailbox_source_id=mailbox_source_id,
                            immutable_provider_message_id=msg.immutable_id,
                            observed_at=msg.received_at or resolved_now,
                            received_at=msg.received_at or resolved_now,
                            sender_address=msg.sender_address,
                            subject=msg.subject,
                            api=api,
                            object_store=object_store,
                            scanner=scanner,
                            actor_type=actor_type,
                            actor_id=actor_id,
                            correlation_id=run.sweep_run_id,
                        )

                        routing_metadata = _routing_metadata(rule)

                        if outcome.status == INGEST_STATUS_INGESTED:
                            evidence_created += 1
                            message, _ = message_repository.record_observation(
                                mailbox_id=mailbox.mailbox_id,
                                provider_kind=mailbox.provider_kind,
                                immutable_provider_message_id=msg.immutable_id,
                                internet_message_id=msg.internet_message_id,
                                observed_folder=folder,
                                subject=msg.subject,
                                sender_address=msg.sender_address,
                                sender_display_name=msg.sender_display_name,
                                received_at=msg.received_at or resolved_now,
                                has_attachments=msg.has_attachments,
                                ingestion_status=INGESTION_STATUS_INGESTED,
                                evidence_id=outcome.evidence.evidence_id if outcome.evidence is not None else None,
                                sender_domain=sender_domain,
                                attachment_metadata=attachment_metadata,
                                auth_signals=auth_signals,
                                metadata=routing_metadata,
                            )
                            if outcome.evidence is not None:
                                api.record_provenance(
                                    subject_type="MailboxMessage",
                                    subject_id=message.mailbox_message_id,
                                    evidence_id=outcome.evidence.evidence_id,
                                    # Closed vocabulary (`core.provenance`'s own contract enum) —
                                    # this message projection was directly OBSERVED FROM its
                                    # evidence (the raw MIME), not extracted/derived/generated.
                                    relationship="OBSERVED_FROM",
                                    actor_type=actor_type,
                                    actor_id=actor_id,
                                    correlation_id=run.sweep_run_id,
                                )
                                # Architect spec's exact audit event name — canonical
                                # IDs only in the payload (never subject/sender text).
                                api.record_audit_event(
                                    event_type="EMAIL_EVIDENCE_INGESTED",
                                    actor_type=actor_type,
                                    actor_id=actor_id,
                                    subject_type="EvidenceItem",
                                    subject_id=outcome.evidence.evidence_id,
                                    correlation_id=run.sweep_run_id,
                                    causation_id=None,
                                    payload={
                                        "mailbox_id": mailbox.mailbox_id,
                                        "mailbox_message_id": message.mailbox_message_id,
                                        "sweep_run_id": run.sweep_run_id,
                                    },
                                )
                        elif outcome.status == INGEST_STATUS_QUARANTINED:
                            quarantined += 1
                            quarantined_message, _ = message_repository.record_observation(
                                mailbox_id=mailbox.mailbox_id,
                                provider_kind=mailbox.provider_kind,
                                immutable_provider_message_id=msg.immutable_id,
                                internet_message_id=msg.internet_message_id,
                                observed_folder=folder,
                                subject=msg.subject,
                                sender_address=msg.sender_address,
                                sender_display_name=msg.sender_display_name,
                                received_at=msg.received_at or resolved_now,
                                has_attachments=msg.has_attachments,
                                ingestion_status=INGESTION_STATUS_QUARANTINED,
                                sender_domain=sender_domain,
                                attachment_metadata=attachment_metadata,
                                auth_signals=auth_signals,
                                metadata=routing_metadata,
                            )
                            api.record_audit_event(
                                event_type="EMAIL_EVIDENCE_QUARANTINED",
                                actor_type=actor_type,
                                actor_id=actor_id,
                                subject_type="MailboxMessage",
                                subject_id=quarantined_message.mailbox_message_id,
                                correlation_id=run.sweep_run_id,
                                causation_id=None,
                                payload={"mailbox_id": mailbox.mailbox_id, "sweep_run_id": run.sweep_run_id},
                            )
                        else:  # INGEST_STATUS_FAILED — oversized message, a governed terminal outcome
                            message_repository.record_observation(
                                mailbox_id=mailbox.mailbox_id,
                                provider_kind=mailbox.provider_kind,
                                immutable_provider_message_id=msg.immutable_id,
                                internet_message_id=msg.internet_message_id,
                                observed_folder=folder,
                                subject=msg.subject,
                                sender_address=msg.sender_address,
                                sender_display_name=msg.sender_display_name,
                                received_at=msg.received_at or resolved_now,
                                has_attachments=msg.has_attachments,
                                ingestion_status=INGESTION_STATUS_FAILED,
                                sender_domain=sender_domain,
                                attachment_metadata=attachment_metadata,
                                auth_signals=auth_signals,
                                metadata=routing_metadata,
                            )

                    if page.next_link:
                        next_link = page.next_link
                        continue

                    final_delta_link = page.delta_link
                    break

                any_folder_had_transient_failure = any_folder_had_transient_failure or folder_had_transient_failure
                if not folder_had_transient_failure and final_delta_link:
                    cursor_repository.advance_cursor(
                        mailbox_id=mailbox.mailbox_id,
                        provider_kind=mailbox.provider_kind,
                        folder=folder,
                        delta_link=final_delta_link,
                    )
                    any_folder_fully_succeeded = True

        except _SweepStopped as stopped:
            completed = sweep_run_repository.complete_run(
                run.sweep_run_id,
                new_status="FAILED",
                folders_attempted=folders_attempted,
                messages_seen=messages_seen,
                messages_new=messages_new,
                evidence_created=evidence_created,
                duplicates=duplicates,
                quarantined=quarantined,
                failures=failures,
                error_code=stopped.error_code,
                error_detail=stopped.error_detail,
            )
            return completed

        if any_folder_had_transient_failure:
            # PARTIAL when SOMETHING durable was accomplished this run
            # (a whole folder's round completed and its cursor
            # advanced, or at least one message was durably ingested/
            # quarantined) — FAILED only when nothing durable happened
            # at all (architect spec: "PARTIAL — at least one folder/
            # message hit a transient failure but at least one other
            # folder/message succeeded").
            new_status = (
                "PARTIAL"
                if (any_folder_fully_succeeded or messages_new > 0 or evidence_created > 0 or quarantined > 0)
                else "FAILED"
            )
            # RESYNC_REQUIRED takes priority over the generic transient
            # codes below whenever it occurred this run — it is the
            # single most actionable signal an operator can see here
            # (everything else may plausibly self-heal by simply
            # sweeping again; this one will not).
            if any_folder_needs_resync:
                error_code = SweepFailureReason.RESYNC_REQUIRED
                error_detail = (
                    "Microsoft Graph invalidated this folder's delta cursor (resync required) — a plain retry "
                    "will not self-heal; an explicit operator-driven resync/backfill is required "
                    "(architect spec: never an automatic destructive fallback)"
                )
            else:
                error_code = SweepFailureReason.PARTIAL_FAILURES if new_status == "PARTIAL" else SweepFailureReason.PROVIDER_ERROR
                error_detail = f"{failures} message(s)/folder page(s) hit a transient failure this run"
            completed = sweep_run_repository.complete_run(
                run.sweep_run_id,
                new_status=new_status,
                folders_attempted=folders_attempted,
                messages_seen=messages_seen,
                messages_new=messages_new,
                evidence_created=evidence_created,
                duplicates=duplicates,
                quarantined=quarantined,
                failures=failures,
                error_code=error_code,
                error_detail=error_detail,
            )
            return completed

        completed = sweep_run_repository.complete_run(
            run.sweep_run_id,
            new_status="SUCCEEDED",
            folders_attempted=folders_attempted,
            messages_seen=messages_seen,
            messages_new=messages_new,
            evidence_created=evidence_created,
            duplicates=duplicates,
            quarantined=quarantined,
            failures=failures,
        )
        mailbox_repository.record_microsoft_sweep_success(mailbox.mailbox_id, swept_at=resolved_now)
        return completed


def reprocess_message_after_domain_rule_approval(
    *,
    mailbox: MailboxSource,
    mailbox_source_id: str,
    message_id: str,
    rule: MailboxDomainRule,
    adapter: _AdapterProtocol,
    message_repository: MailboxMessageRepository,
    api: _EvidenceAPIProtocol,
    object_store: _ObjectStoreProtocol,
    scanner: EvidenceSafetyScanner,
    actor_type: str,
    actor_id: str,
    correlation_id: Optional[str] = None,
    now: Optional[datetime] = None,
) -> MailboxMessage:
    """Architect spec §4's own explicit requirement: "After approval,
    the SPECIFIC triggering candidate email must become eligible for
    full processing immediately — do not wait for a future unrelated
    message to trigger re-processing." Called by
    ``app/api/routers/mailboxes_microsoft.py`` as part of resolving an
    ``ITEM_TYPE_MAILBOX_DOMAIN_REVIEW`` Needs You item with an ALLOW
    decision — `message_id` is that item's own `source_object_reference`
    (the real, canonical `mailbox_message_id` of the ONE triggering
    message — see module docstring's "Two-stage mail processing"
    section for why that correlation is real, not a guess).

    Idempotent-safe: a message already in
    `services.mailbox.message.FINAL_INGESTION_STATUSES` (a double-
    submit of the same approval, or a message this sweep already fully
    processed via a later ordinary sweep round in the meantime) is
    returned UNCHANGED — never re-fetched, never re-ingested a second
    time.

    Raises:
        core.errors.ConflictError: `mailbox` is not ACTIVE+CONNECTED
            (same defensive backstop as `run_sweep`'s own precondition).
    """
    if mailbox.status != "ACTIVE" or mailbox.connection_state != CONNECTION_STATE_CONNECTED:
        raise ConflictError(
            f"mailbox '{mailbox.mailbox_id}' is not ACTIVE+CONNECTED — cannot reprocess a message "
            "(reconnect the mailbox before approving this domain review item)"
        )

    current = message_repository.get_message(message_id)
    if current.ingestion_status in FINAL_INGESTION_STATUSES - {INGESTION_STATUS_CHECKED_NOT_CANDIDATE}:
        # Already fully, durably decided by something else (e.g. a
        # genuine double-submit of this same approval) — a safe no-op.
        return current

    content_result = adapter.fetch_message_content(
        mailbox_id=mailbox.mailbox_id, immutable_message_id=current.immutable_provider_message_id
    )

    routing_metadata = _routing_metadata(rule)

    if content_result.status == GraphOutcomeStatus.NOT_FOUND:
        message, _ = message_repository.record_observation(
            mailbox_id=mailbox.mailbox_id,
            provider_kind=mailbox.provider_kind,
            immutable_provider_message_id=current.immutable_provider_message_id,
            internet_message_id=current.internet_message_id,
            observed_folder=current.observed_folder,
            subject=current.subject,
            sender_address=current.sender_address,
            sender_display_name=current.sender_display_name,
            received_at=current.received_at,
            has_attachments=current.has_attachments,
            ingestion_status=INGESTION_STATUS_VANISHED,
            sender_domain=current.sender_domain,
            attachment_metadata=current.attachment_metadata,
            auth_signals=current.auth_signals,
        )
        return message

    if content_result.status != GraphOutcomeStatus.OK:
        raise ConflictError(
            f"could not fetch content for message '{message_id}' during domain-rule-approval "
            f"reprocessing: {content_result.status.value} — {content_result.error_detail or ''}"
        )

    outcome = ingest_email_evidence(
        raw_mime_bytes=content_result.content or b"",
        mailbox_id=mailbox.mailbox_id,
        mailbox_source_id=mailbox_source_id,
        immutable_provider_message_id=current.immutable_provider_message_id,
        observed_at=current.received_at,
        received_at=current.received_at,
        sender_address=current.sender_address,
        subject=current.subject,
        api=api,
        object_store=object_store,
        scanner=scanner,
        actor_type=actor_type,
        actor_id=actor_id,
        correlation_id=correlation_id,
    )

    if outcome.status == INGEST_STATUS_INGESTED:
        message, _ = message_repository.record_observation(
            mailbox_id=mailbox.mailbox_id,
            provider_kind=mailbox.provider_kind,
            immutable_provider_message_id=current.immutable_provider_message_id,
            internet_message_id=current.internet_message_id,
            observed_folder=current.observed_folder,
            subject=current.subject,
            sender_address=current.sender_address,
            sender_display_name=current.sender_display_name,
            received_at=current.received_at,
            has_attachments=current.has_attachments,
            ingestion_status=INGESTION_STATUS_INGESTED,
            evidence_id=outcome.evidence.evidence_id if outcome.evidence is not None else None,
            sender_domain=current.sender_domain,
            attachment_metadata=current.attachment_metadata,
            auth_signals=current.auth_signals,
            metadata=routing_metadata,
        )
        if outcome.evidence is not None:
            api.record_provenance(
                subject_type="MailboxMessage",
                subject_id=message.mailbox_message_id,
                evidence_id=outcome.evidence.evidence_id,
                relationship="OBSERVED_FROM",
                actor_type=actor_type,
                actor_id=actor_id,
                correlation_id=correlation_id,
            )
            api.record_audit_event(
                event_type="EMAIL_EVIDENCE_INGESTED",
                actor_type=actor_type,
                actor_id=actor_id,
                subject_type="EvidenceItem",
                subject_id=outcome.evidence.evidence_id,
                correlation_id=correlation_id,
                causation_id=None,
                payload={
                    "mailbox_id": mailbox.mailbox_id,
                    "mailbox_message_id": message.mailbox_message_id,
                    "reprocessed_after_domain_rule_approval": True,
                },
            )
        return message

    if outcome.status == INGEST_STATUS_QUARANTINED:
        message, _ = message_repository.record_observation(
            mailbox_id=mailbox.mailbox_id,
            provider_kind=mailbox.provider_kind,
            immutable_provider_message_id=current.immutable_provider_message_id,
            internet_message_id=current.internet_message_id,
            observed_folder=current.observed_folder,
            subject=current.subject,
            sender_address=current.sender_address,
            sender_display_name=current.sender_display_name,
            received_at=current.received_at,
            has_attachments=current.has_attachments,
            ingestion_status=INGESTION_STATUS_QUARANTINED,
            sender_domain=current.sender_domain,
            attachment_metadata=current.attachment_metadata,
            auth_signals=current.auth_signals,
            metadata=routing_metadata,
        )
        api.record_audit_event(
            event_type="EMAIL_EVIDENCE_QUARANTINED",
            actor_type=actor_type,
            actor_id=actor_id,
            subject_type="MailboxMessage",
            subject_id=message.mailbox_message_id,
            correlation_id=correlation_id,
            causation_id=None,
            payload={"mailbox_id": mailbox.mailbox_id, "reprocessed_after_domain_rule_approval": True},
        )
        return message

    # INGEST_STATUS_FAILED — oversized message, a governed terminal outcome.
    message, _ = message_repository.record_observation(
        mailbox_id=mailbox.mailbox_id,
        provider_kind=mailbox.provider_kind,
        immutable_provider_message_id=current.immutable_provider_message_id,
        internet_message_id=current.internet_message_id,
        observed_folder=current.observed_folder,
        subject=current.subject,
        sender_address=current.sender_address,
        sender_display_name=current.sender_display_name,
        received_at=current.received_at,
        has_attachments=current.has_attachments,
        ingestion_status=INGESTION_STATUS_FAILED,
        sender_domain=current.sender_domain,
        attachment_metadata=current.attachment_metadata,
        auth_signals=current.auth_signals,
        metadata=routing_metadata,
    )
    return message
