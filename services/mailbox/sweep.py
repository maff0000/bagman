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
     :func:`reprocess_all_historical_candidates_for_domain` — operational
     addendum ahead of the first real large historical sweep — for how
     EVERY historical candidate for that domain, not merely the ONE
     triggering message, gets its MIME fetched immediately once an
     operator approves). A non-candidate is marked
     ``CHECKED_NOT_CANDIDATE`` and nothing further happens.

A message already in any of ``services.mailbox.message
.FINAL_INGESTION_STATUSES`` (which now includes
``CHECKED_NOT_CANDIDATE``) is never re-decided by a later ORDINARY
sweep, even if the governing ``MailboxDomainRule``'s policy has since
changed — a **documented judgment call**: a rule-policy change does NOT,
by itself, automatically reprocess previously-seen messages under the
OLD policy (e.g. a domain rule updated directly, bypassing the Needs
You approval flow entirely, still reprocesses nothing retroactively).

**Operational addendum (ahead of the first real large historical
sweep) — corrects/narrows the ORIGINAL, narrower design this paragraph
used to describe:** resolving an OPEN ``MAILBOX_DOMAIN_REVIEW`` Needs
You item with an ``ALLOW`` decision is the one real, explicit operator
action this module treats differently — see
:func:`reprocess_all_historical_candidates_for_domain`. It no longer
reprocesses only the ONE specific message that triggered the item;
it back-processes EVERY historical ``CHECKED_NOT_CANDIDATE`` candidate
BAGMAN has already discovered for that ``(mailbox_id, sender_domain)``
pair (the architect's own explicit correction: "the domain-learning
mechanism is not useful if it only affects future mail" — a domain
with 40 real historical candidate invoices must have all 40 reprocessed
on approval, not just the one that happened to raise the item). This
remains a narrow, EXPLICIT, operator-triggered action, never an
automatic side effect of an ordinary sweep or of a bare
``MailboxDomainRule.upsert_rule`` call outside this one approval flow —
flagged here prominently for PL/architect review, not silently chosen.

Historical bootstrap boundary — governed AND mailbox-scoped, never a
global magic date (CD-6 architect amendment, second correction —
supersedes both Slice 4A's flat 7-day boundary AND this module's own
first amendment's "GLOBAL MINIMUM across every entity, unconditionally"
design)
------------------------------------------------------------------------
:func:`compute_bootstrap_floor` replaces the old
``DEFAULT_BOOTSTRAP_DAYS`` constant entirely, and no longer computes a
single global minimum across every governed entity in the system
regardless of mailbox — the architect explicitly rejected that as "a
global magic date". Instead, a mailbox's FIRST-EVER sweep of a folder
bootstraps from a boundary DERIVED specifically for THAT mailbox:

1. Determine the mailbox's in-scope destination entities — the union
   of its own optional ``default_entity_id`` hint (when set) and every
   ``destination_entity_id`` named by one of its own ``ALLOWED``
   ``services.mailbox.domain_rule.MailboxDomainRule`` rows.
2. For each in-scope entity, derive its own real historical-bootstrap
   requirement via
   ``services.mailbox.bootstrap_policy.compute_entity_historical_bootstrap``
   — the previous-completed-accounting-period-start of that entity's
   ``fiscal_year_start_month_day``, clamped by its optional
   ``historical_floor_override_at`` (see that field's own docstring on
   ``core.entity.GovernedEntity`` for the full "clamp, not the answer"
   doctrine this corrects).
3. The mailbox's bootstrap floor is the MINIMUM across those in-scope
   entities' derived values.

**The fallback rule (real, load-bearing — not a placeholder):** if a
mailbox's in-scope set is EMPTY (the real, current state of
``matt@infosecurs.com``: ``default_entity_id`` is ``None`` and zero
``MailboxDomainRule`` rows exist yet — a genuine bootstrapping case),
:func:`compute_bootstrap_floor` falls back to EVERY governed entity
currently seeded — the previous, conservative "any mailbox could in
principle discover REVIEW_REQUIRED-routed evidence for any entity"
behaviour — and the floor is the minimum across all of them. This is
still real per-entity derivation, never a stored literal read as-is;
it is simply applied to a wider entity set when no narrower scope is
yet known.

If ANY in-scope entity (or, under the fallback, any seeded entity) is
missing ``fiscal_year_start_month_day``, :func:`compute_bootstrap_floor`
raises ``core.errors.ConflictError`` rather than inventing a fallback
date or silently excluding that entity (architect spec: "surface the
missing configuration before historical sweep") — checked on EVERY
sweep call, not merely a mailbox's first, which remains the simpler and
more conservative of two reasonable designs (see that function's own
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

Folder discovery — a whole-sweep precondition (CD-6 architect amendment,
supersedes Slice 4/CD-6's own original hardcoded ``[Inbox, Junk]`` sweep)
------------------------------------------------------------------------
Before iterating any folder, :func:`run_sweep` calls
``adapter.discover_monitored_folders(mailbox_id=...)`` EXACTLY ONCE —
this module stays genuinely provider-neutral: it never resolves a
well-known folder name, never applies the monitored/excluded-folder
doctrine itself, and never assumes Inbox/Junk are the only two folders
— see ``services/mailbox/microsoft/adapter.py``'s own module docstring
for the real Microsoft-Graph-specific recursive enumeration + well-
known-id classification + monitored-set computation this delegates to.
The returned monitored folder list now includes Inbox, Junk Email,
Deleted Items, and every custom/nested/hidden folder the mailbox's own
folder tree contains (Sent Items/Drafts/Outbox are excluded from
monitoring entirely) — **Deleted Items is explicitly in scope**: a
message discovered there runs through the EXACT SAME Stage-A/Stage-B
gate, MIME-fetch, evidence-ingest pipeline as any other monitored
folder; its deleted location is recorded as provenance
(`observed_folder`/`observed_folder_display_name`), never an instruction
to skip or discard it.

A non-``OK`` folder-discovery outcome is a WHOLE-SWEEP precondition
failure, not an ordinary per-folder transient failure — without a real
folder list, nothing in this sweep can be safely attempted at all, so
:func:`run_sweep` raises :class:`_SweepStopped` for every non-``OK``
discovery outcome except ``RATE_LIMITED`` (given the SAME bounded
single-retry-then-fail treatment as an ordinary delta page — see
``_MAX_RATE_LIMIT_BACKOFF_SECONDS`` below). ``AUTH_ERROR``/
``CONFIG_ERROR``/``PERMISSION_ERROR`` map to their own existing
``SweepFailureReason`` codes (identical to the per-folder handling
below); any other non-``OK`` status uses the new, dedicated
``SweepFailureReason.FOLDER_DISCOVERY_FAILED`` — flagged here
prominently as a documented judgment call, not a silent choice.

Folder-ID-keyed cursors — the Part D migration judgment call (read
before changing anything about cursor keys)
------------------------------------------------------------------------
``services.mailbox.cursor.MailboxFolderCursor`` was ALREADY keyed by a
generic ``folder: str`` — never a Postgres enum, never constrained to
``"INBOX"``/``"JUNK"`` at the schema layer (see that module's own
docstring). This amendment changes ONLY what VALUE the sweep engine now
passes as ``folder``: the real, resolved Microsoft Graph folder id
(``MonitoredFolder.folder_id``) instead of the old closed-set literal
strings ``"INBOX"``/``"JUNK"``.

**The judgment call**: the real, live Infosecurs mailbox already has
durable cursor rows keyed by the OLD literal strings ``"INBOX"``/
``"JUNK"`` from Slice 4A's acceptance run (128 real messages already
ingested). This delivery does **not** attempt to remap those old rows
to their real Graph folder ids — doing so would require a LIVE Graph
call to resolve ``"INBOX"``/``"JUNK"`` to this specific mailbox's real
folder ids, which is not something a data migration can do offline, and
is unnecessary. Instead: the very first sweep after this change simply
finds NO cursor row for Inbox/Junk Email's own real folder ids (a
brand-new key), so ``get_or_bootstrap`` creates a FRESH cursor and Inbox/
Junk Email re-run a full BOOTSTRAP-floor-bounded delta round, exactly
like a folder BAGMAN is discovering for the very first time. This is
safe — never a source of duplicate evidence — because
``services.mailbox.message.MailboxMessageRepository.record_observation``'s
own (mailbox_id, immutable_provider_message_id) idempotency (see module
docstring's "Idempotency" section above) means every one of the 128
already-known messages simply resolves to its EXISTING
``MailboxMessage``/``EvidenceItem`` row again — counted as a
``duplicates`` re-observation, never a second evidence object. The OLD
``"INBOX"``/``"JUNK"``-keyed cursor rows are deliberately left in place
in ``mailbox_folder_cursors`` (never deleted) as a historical audit
trail of exactly where the old, pre-amendment cursor scheme left off —
see the accompanying Alembic migration's own comment for the identical
reasoning at the schema-change layer. This was the simpler and safer of
two reasonable designs (the alternative — an offline remap script
cross-referencing old labels against real Graph folder ids — is not
even correctly possible without a live Graph call).

``MailboxSweepRun.folders_attempted`` — now a structured list, never a
raw folder id alone
------------------------------------------------------------------------
``folders_attempted`` now reflects the REAL discovered/monitored folder
set for this mailbox, not a hardcoded ``["INBOX", "JUNK"]``. Each entry
is a small ``{"folder_id": ..., "display_name": ...}`` mapping — never
a bare folder id string (a raw Graph folder id is an opaque, non-human-
readable value that must never be presented to an operator as if it
were a friendly folder name) and never a bare display name alone
(display names are not a safe identity key — see the "well-known
folder identity" doctrine in
``services/mailbox/microsoft/graph_client.py``'s own module docstring).
A GUI surface rendering this list uses ``display_name``; anything
treating folder identity/equality uses ``folder_id``.
"""
from __future__ import annotations

import time
from datetime import datetime
from typing import Any, Mapping, Optional, Protocol, Sequence

from core.entity import EntityRepository
from core.errors import ConflictError
from core.timestamps import to_contract_string, utc_now
from services.evidence.intake.scanner import EvidenceSafetyScanner
from services.mailbox.bootstrap_policy import compute_entity_historical_bootstrap
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
    INGESTION_STATUS_CHECKED_NOT_CANDIDATE,
    INGESTION_STATUS_FAILED,
    INGESTION_STATUS_INGESTED,
    INGESTION_STATUS_QUARANTINED,
    INGESTION_STATUS_VANISHED,
    MailboxMessage,
    MailboxMessageRepository,
)
from services.mailbox.microsoft.adapter import FolderDiscoveryResult
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


def compute_bootstrap_floor(
    *,
    mailbox: MailboxSource,
    entity_repository: EntityRepository,
    domain_rule_repository: MailboxDomainRuleRepository,
    now: Optional[datetime] = None,
) -> datetime:
    """``mailbox``'s own real historical-sweep bootstrap floor — never a
    global magic date — see module docstring's "Historical bootstrap
    boundary" section for the full reasoning this implements:

    1. Resolve ``mailbox``'s in-scope destination entity ids: the union
       of its own optional ``default_entity_id`` hint (if set) and
       every ``destination_entity_id`` named by one of its own
       ``ALLOWED`` ``MailboxDomainRule`` rows.
    2. If that set is EMPTY (the real, current ``matt@infosecurs.com``
       state — a genuine bootstrapping case, not a placeholder), fall
       back to EVERY governed entity currently seeded (the previous,
       conservative "any mailbox could discover REVIEW_REQUIRED
       evidence for any entity" behaviour).
    3. Derive each in-scope (or fallback) entity's own real historical-
       bootstrap requirement via
       ``services.mailbox.bootstrap_policy.compute_entity_historical_bootstrap``.
    4. Return the MINIMUM across those derived values.

    Raises:
        core.errors.ConflictError: no governed entities are seeded at
            all (only reachable via the fallback path — an in-scope
            set can never be "resolved but empty of real entities",
            since every id in it came from a real hint/rule), or at
            least one IN-SCOPE entity (or, under the fallback, at
            least one seeded entity) is missing
            ``fiscal_year_start_month_day`` (architect spec: "surface
            the missing configuration before historical sweep" — this
            never invents a fallback date and never silently excludes
            the incomplete entity from the minimum).
    """
    resolved_now = now if now is not None else utc_now()

    in_scope_entity_ids: set[str] = set()
    if mailbox.default_entity_id is not None:
        in_scope_entity_ids.add(mailbox.default_entity_id)
    for rule in domain_rule_repository.list_rules(mailbox_id=mailbox.mailbox_id):
        if rule.policy == POLICY_ALLOWED and rule.destination_entity_id is not None:
            in_scope_entity_ids.add(rule.destination_entity_id)

    if in_scope_entity_ids:
        entities = [entity_repository.get_entity(entity_id) for entity_id in in_scope_entity_ids]
        scope_description = "in-scope"
    else:
        # Fallback: a real, load-bearing rule (see module docstring),
        # never a placeholder for "we haven't built scoping yet".
        entities = entity_repository.list_entities()
        scope_description = "fallback (no in-scope destination entities yet resolved)"
        if not entities:
            raise ConflictError(
                f"cannot compute the historical-sweep bootstrap floor for mailbox '{mailbox.mailbox_id}': "
                "no governed entities are seeded yet — seed the canonical entity registry before "
                "running any mailbox sweep"
            )

    missing = sorted(e.canonical_name for e in entities if e.fiscal_year_start_month_day is None)
    if missing:
        raise ConflictError(
            f"cannot compute the historical-sweep bootstrap floor for mailbox '{mailbox.mailbox_id}': "
            f"the following {scope_description} governed entities are missing "
            f"fiscal_year_start_month_day configuration: {missing} — surface and resolve this "
            "configuration before any historical sweep runs (CD-6 architect amendment §1); BAGMAN "
            "never invents a fallback bootstrap date or silently excludes an entity"
        )

    return min(compute_entity_historical_bootstrap(entity, now=resolved_now) for entity in entities)


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
    """Create a fresh ``MAILBOX_DOMAIN_REVIEW`` item for the first
    candidate seen from ``(mailbox_id, sender_domain)``, or — operational
    addendum, ahead of the first real large historical sweep — accumulate
    live aggregate stats on the SAME still-``OPEN`` item when a second,
    third, ... candidate from the same still-unresolved domain is seen
    (previously this reuse path returned the existing item completely
    unchanged, so a domain with 40 candidate messages looked identical,
    in the item's own metadata, to one with exactly 1 — the architect's
    own operational addendum requires the item to say honestly how many
    candidates are actually waiting behind it).

    Accumulated fields (see ``services.needs_you.needs_you.NeedsYouItem
    .metadata``'s own now-open-ended shape):

    * ``candidate_message_count`` — how many candidate messages from
      this domain have been seen so far, this and prior sweeps.
    * ``first_seen_at`` — the EARLIEST candidate's ``received_at``, set
      once at creation, never changed on reuse.
    * ``last_seen_at`` — the LATEST candidate's ``received_at``, updated
      on every reuse.
    * ``attachment_bearing_count`` — how many of the candidates so far
      had an attachment.

    Populated once, at creation only, never changed on reuse (a REUSE
    call never re-derives these — they describe the domain/mailbox
    itself, not the individual candidate that triggered a reuse):

    * ``proposed_destination_entity_id`` — the mailbox's own
      ``default_entity_id`` HINT, if set, else ``None``. Deliberately
      labelled here as a non-authoritative HINT ONLY — mirrors
      ``services.mailbox.mailbox``'s own established "``default_entity_id``
      is only ever an optional DISPLAY hint... never ownership
      assertion" doctrine (see that module's own docstring): this is
      never a recommendation or a pre-filled answer BAGMAN is confident
      in, purely "this mailbox happens to have this hint set, for
      whatever it is worth to the operator reviewing this item".
    * ``proposed_processor_hint`` — always ``None``. Nothing in this
      slice informs a real processor hint for an unresolved domain; this
      key exists so a future producer that DOES know one has a place to
      put it without a contract/metadata-shape change, but this
      delivery must never invent one.
    * ``confidence_reason`` — a short, plain string reusing
      ``services.mailbox.discovery_signals.evaluate_discovery_candidate``'s
      own ``DiscoverySignalResult.reason`` (the bounded, non-AI,
      keyword-level heuristic's own real, honest explanation of what it
      matched — e.g. "subject contains keyword 'invoice'") — never a
      fabricated or more specific claim than that bounded heuristic
      actually determined.
    """
    existing = _find_open_domain_review_item(needs_you_repository, mailbox_id=mailbox.mailbox_id, sender_domain=sender_domain)
    received_at_str = to_contract_string(message.received_at)

    if existing is not None:
        current_count = existing.metadata.get("candidate_message_count") or 1
        current_attachment_count = existing.metadata.get("attachment_bearing_count") or 0
        return needs_you_repository.update_item_metadata(
            existing.item_id,
            metadata_updates={
                "candidate_message_count": current_count + 1,
                "last_seen_at": received_at_str,
                "attachment_bearing_count": current_attachment_count + (1 if message.has_attachments else 0),
            },
        )

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
            "candidate_message_count": 1,
            "first_seen_at": received_at_str,
            "last_seen_at": received_at_str,
            "attachment_bearing_count": 1 if message.has_attachments else 0,
            # Non-authoritative HINT only — see this function's own
            # docstring's "Populated once" section above.
            "proposed_destination_entity_id": mailbox.default_entity_id,
            "proposed_processor_hint": None,
            "confidence_reason": reason,
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

    def discover_monitored_folders(self, *, mailbox_id: str) -> FolderDiscoveryResult: ...


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
    bootstrap_timestamp = compute_bootstrap_floor(
        mailbox=mailbox,
        entity_repository=entity_repository,
        domain_rule_repository=domain_rule_repository,
        now=resolved_now,
    )

    with sweep_lock.held(mailbox.mailbox_id):
        run = sweep_run_repository.create_run(mailbox_id=mailbox.mailbox_id, trigger=trigger)

        folders_attempted: list[dict] = []
        messages_seen = 0
        messages_new = 0
        evidence_created = 0
        duplicates = 0
        quarantined = 0
        failures = 0
        # Operational-addendum aggregate reporting counters (ahead of
        # the first real large historical sweep) — see
        # `services/mailbox/sweep_run.py`'s own `MailboxSweepRun` field
        # docstrings for what each one means.
        sender_domains_seen: set[str] = set()
        allowed_domain_messages = 0
        ignored_domain_messages = 0
        unknown_domain_messages = 0
        likely_financial_candidates = 0
        messages_with_attachments = 0
        graph_throttle_retries = 0
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
            # -- Folder discovery (CD-6 architect amendment) — a
            # whole-sweep precondition, called exactly once, BEFORE any
            # folder is iterated. See module docstring's own "Folder
            # discovery" section for the full reasoning below.
            discovery = adapter.discover_monitored_folders(mailbox_id=mailbox.mailbox_id)
            if discovery.status == GraphOutcomeStatus.RATE_LIMITED:
                graph_throttle_retries += 1
                backoff = min(discovery.retry_after_seconds or 5.0, _MAX_RATE_LIMIT_BACKOFF_SECONDS)
                sleep_fn(backoff)
                discovery = adapter.discover_monitored_folders(mailbox_id=mailbox.mailbox_id)

            if discovery.status == GraphOutcomeStatus.AUTH_ERROR:
                raise _SweepStopped(
                    error_code=SweepFailureReason.TOKEN_REFRESH_FAILED,
                    error_detail=discovery.error_detail or "Microsoft OAuth token invalid/expired; reconnect required",
                )
            if discovery.status == GraphOutcomeStatus.CONFIG_ERROR:
                raise _SweepStopped(
                    error_code=SweepFailureReason.CONFIG_ERROR,
                    error_detail=discovery.error_detail or "Microsoft mail is not configured",
                )
            if discovery.status == GraphOutcomeStatus.PERMISSION_ERROR:
                adapter.report_connection_error(
                    mailbox.mailbox_id,
                    error_code=SweepFailureReason.PERMISSION_ERROR,
                    error_detail=discovery.error_detail or "permission error during folder discovery",
                )
                raise _SweepStopped(
                    error_code=SweepFailureReason.PERMISSION_ERROR,
                    error_detail=discovery.error_detail or "permission error during folder discovery",
                )
            if discovery.status != GraphOutcomeStatus.OK:
                # Folder discovery itself could not be completed this
                # round — without a real folder list, nothing can be
                # safely attempted (see module docstring; never blended
                # into a per-folder transient-failure code).
                raise _SweepStopped(
                    error_code=SweepFailureReason.FOLDER_DISCOVERY_FAILED,
                    error_detail=discovery.error_detail or f"folder discovery failed ({discovery.status.value})",
                )

            for monitored_folder in discovery.folders:
                folder = monitored_folder.folder_id
                folder_display_name = monitored_folder.display_name
                # Operational addendum — per-folder operational counts
                # (see `services/mailbox/sweep_run.py`'s own
                # `folders_attempted` entry docstring/contract
                # description for exactly what each sub-field means).
                # This dict is appended NOW, up front, and mutated IN
                # PLACE as this folder is processed below — so even an
                # abrupt whole-sweep stop (`_SweepStopped`) mid-folder
                # still leaves this entry carrying accurate PARTIAL
                # counts for whatever was processed before the stop,
                # rather than nothing at all.
                folder_entry = {
                    "folder_id": folder,
                    "display_name": folder_display_name,
                    "completed": False,
                    "messages_seen": 0,
                    "new_discovery_records": 0,
                    "deep_processing_count": 0,
                    "cursor_established": False,
                }
                folders_attempted.append(folder_entry)
                cursor = cursor_repository.get_or_bootstrap(
                    mailbox_id=mailbox.mailbox_id,
                    provider_kind=mailbox.provider_kind,
                    folder=folder,
                    bootstrap_timestamp=bootstrap_timestamp,
                )
                # `get_or_bootstrap` is itself durable/synchronous — a
                # `MailboxFolderCursor` row now exists for this folder
                # regardless of whether it was pre-existing or just
                # bootstrapped by this very call (distinct from
                # `completed`, which additionally requires the round to
                # finish AND `delta_link` to advance — see the schema's
                # own field description).
                folder_entry["cursor_established"] = True
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
                        graph_throttle_retries += 1
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
                        folder_entry["messages_seen"] += 1
                        # Stage-A metadata is captured for EVERY observed
                        # message this run, regardless of dedup outcome
                        # (operational addendum — see
                        # `unique_sender_domains`/`messages_with_attachments`'s
                        # own contract field descriptions).
                        sender_domain = _extract_sender_domain(msg.sender_address)
                        if sender_domain:
                            sender_domains_seen.add(sender_domain)
                        if msg.has_attachments:
                            messages_with_attachments += 1

                        existing = message_repository.find_by_provider_id(mailbox.mailbox_id, msg.immutable_id)
                        if existing is not None and existing.ingestion_status in FINAL_INGESTION_STATUSES:
                            duplicates += 1
                            message_repository.record_observation(
                                mailbox_id=mailbox.mailbox_id,
                                provider_kind=mailbox.provider_kind,
                                immutable_provider_message_id=msg.immutable_id,
                                internet_message_id=msg.internet_message_id,
                                observed_folder=folder,
                                observed_folder_display_name=folder_display_name,
                                subject=msg.subject,
                                sender_address=msg.sender_address,
                                sender_display_name=msg.sender_display_name,
                                received_at=msg.received_at or resolved_now,
                                has_attachments=msg.has_attachments,
                                ingestion_status=existing.ingestion_status,
                                evidence_id=existing.evidence_id,
                                sender_domain=sender_domain,
                                attachment_metadata=_attachment_metadata_dicts(msg.attachment_metadata),
                                auth_signals=dict(msg.auth_signals),
                            )
                            continue

                        # -- Stage A (always) + Stage B (the domain gate) --
                        attachment_metadata = _attachment_metadata_dicts(msg.attachment_metadata)
                        auth_signals = dict(msg.auth_signals)

                        def _record_discovery_only(status: str) -> MailboxMessage:
                            message, _ = message_repository.record_observation(
                                mailbox_id=mailbox.mailbox_id,
                                provider_kind=mailbox.provider_kind,
                                immutable_provider_message_id=msg.immutable_id,
                                internet_message_id=msg.internet_message_id,
                                observed_folder=folder,
                                observed_folder_display_name=folder_display_name,
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
                            ignored_domain_messages += 1
                            domain_rule_repository.touch_last_seen(
                                mailbox_id=mailbox.mailbox_id, sender_domain=rule.sender_domain, seen_at=resolved_now
                            )
                            messages_new += 1
                            folder_entry["new_discovery_records"] += 1
                            _record_discovery_only(INGESTION_STATUS_CHECKED_NOT_CANDIDATE)
                            continue

                        if rule is None or rule.policy != POLICY_ALLOWED:
                            # Unknown domain — the bounded, non-AI
                            # discovery-candidate heuristic (architect
                            # spec §4). Never a MIME fetch either way.
                            unknown_domain_messages += 1
                            signal = evaluate_discovery_candidate(
                                subject=msg.subject, attachment_metadata=attachment_metadata
                            )
                            if signal.is_candidate:
                                likely_financial_candidates += 1
                            messages_new += 1
                            folder_entry["new_discovery_records"] += 1
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
                        allowed_domain_messages += 1
                        domain_rule_repository.touch_last_seen(
                            mailbox_id=mailbox.mailbox_id, sender_domain=rule.sender_domain, seen_at=resolved_now
                        )

                        content_result = adapter.fetch_message_content(
                            mailbox_id=mailbox.mailbox_id, immutable_message_id=msg.immutable_id
                        )

                        if content_result.status == GraphOutcomeStatus.NOT_FOUND:
                            messages_new += 1
                            folder_entry["new_discovery_records"] += 1
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
                        folder_entry["new_discovery_records"] += 1
                        # Operational addendum — "deep processing": a
                        # full MIME fetch (`content_result.status == OK`,
                        # already checked above) + the evidence-create
                        # pipeline (`ingest_email_evidence` below) was
                        # attempted this run for this message, whatever
                        # its eventual outcome (ingested/quarantined/
                        # governed-oversize-failed) — the ALLOWED-domain
                        # path's own real cost centre (see the schema's
                        # own field description).
                        folder_entry["deep_processing_count"] += 1
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
                                observed_folder_display_name=folder_display_name,
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
                                observed_folder_display_name=folder_display_name,
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
                                observed_folder_display_name=folder_display_name,
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
                    folder_entry["completed"] = True

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
                unique_sender_domains=len(sender_domains_seen),
                allowed_domain_messages=allowed_domain_messages,
                ignored_domain_messages=ignored_domain_messages,
                unknown_domain_messages=unknown_domain_messages,
                likely_financial_candidates=likely_financial_candidates,
                messages_with_attachments=messages_with_attachments,
                graph_throttle_retries=graph_throttle_retries,
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
                unique_sender_domains=len(sender_domains_seen),
                allowed_domain_messages=allowed_domain_messages,
                ignored_domain_messages=ignored_domain_messages,
                unknown_domain_messages=unknown_domain_messages,
                likely_financial_candidates=likely_financial_candidates,
                messages_with_attachments=messages_with_attachments,
                graph_throttle_retries=graph_throttle_retries,
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
            unique_sender_domains=len(sender_domains_seen),
            allowed_domain_messages=allowed_domain_messages,
            ignored_domain_messages=ignored_domain_messages,
            unknown_domain_messages=unknown_domain_messages,
            likely_financial_candidates=likely_financial_candidates,
            messages_with_attachments=messages_with_attachments,
            graph_throttle_retries=graph_throttle_retries,
        )
        mailbox_repository.record_microsoft_sweep_success(mailbox.mailbox_id, swept_at=resolved_now)
        return completed


def _reprocess_one_message(
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
    """Private single-message reprocessing helper (was, until the
    operational addendum ahead of the first real large historical
    sweep, this module's own PUBLIC
    ``reprocess_message_after_domain_rule_approval`` — renamed/narrowed
    to private when :func:`reprocess_all_historical_candidates_for_domain`
    became the real public entrypoint; every other caller/test has been
    updated to call the plural function instead — see that function's
    own docstring for why "only the one triggering message" was never
    good enough).

    Architect spec §4's own explicit requirement: "After approval, the
    SPECIFIC triggering candidate email must become eligible for full
    processing immediately — do not wait for a future unrelated message
    to trigger re-processing." `message_id` is one specific
    `MailboxMessage.mailbox_message_id` — either the item's own
    `source_object_reference` (the ONE triggering message) or one of the
    OTHER historical `CHECKED_NOT_CANDIDATE` candidates
    :func:`reprocess_all_historical_candidates_for_domain` discovered
    for the same domain; this helper treats every candidate identically
    (see module docstring's "Two-stage mail processing" section for why
    that correlation is real, not a guess).

    Idempotent-safe: a message already in
    `services.mailbox.message.FINAL_INGESTION_STATUSES` (a double-
    submit of the same approval, or a message this sweep already fully
    processed via a later ordinary sweep round in the meantime) is
    returned UNCHANGED — never re-fetched, never re-ingested a second
    time. Deliberately does NOT itself check `mailbox.status`/
    `connection_state` — the caller (`reprocess_all_historical_candidates_for_domain`)
    owns that precondition once, up front, rather than repeating a
    per-message check across a bounded sequential loop.
    """
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
            observed_folder_display_name=current.observed_folder_display_name,
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
            observed_folder_display_name=current.observed_folder_display_name,
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
            observed_folder_display_name=current.observed_folder_display_name,
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
        observed_folder_display_name=current.observed_folder_display_name,
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


def reprocess_all_historical_candidates_for_domain(
    *,
    mailbox: MailboxSource,
    mailbox_source_id: str,
    sender_domain: str,
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
) -> list[MailboxMessage]:
    """Operational addendum (ahead of the first real large historical
    sweep) — the architect's own most important new requirement here:
    "The domain-learning mechanism is not useful if it only affects
    future mail." Approving a domain must back-process EVERY
    previously-discovered historical candidate for that domain, not
    only the one message that happened to trigger the
    ``MAILBOX_DOMAIN_REVIEW`` Needs You item.

    Supersedes the old, narrower ``reprocess_message_after_domain_rule_approval``
    (renamed :func:`_reprocess_one_message`, now private) as this
    module's real public reprocessing entrypoint —
    ``app/api/routers/mailboxes_microsoft.py::resolve_mailbox_domain_review``
    calls this on ``ALLOW``, never the private single-message helper
    directly. Uses ``message_repository.list_candidate_messages_for_domain``
    to discover every eligible historical candidate for
    ``(mailbox.mailbox_id, sender_domain)`` — which, by construction
    (see that method's own docstring), ALWAYS includes the one item-
    triggering message too, since it is also
    ``CHECKED_NOT_CANDIDATE`` — so there is no need to special-case it
    separately from the rest.

    Bounded execution (architect §11 — "no retry storm, no unbounded
    parallel Graph requests"): candidates are reprocessed SEQUENTIALLY,
    one message at a time, reusing the exact same
    ``_MAX_RATE_LIMIT_BACKOFF_SECONDS`` rate-limit backoff discipline
    every other Graph call in this module already uses (via
    ``adapter.fetch_message_content`` inside
    :func:`_reprocess_one_message`) — this function deliberately never
    introduces concurrency here, however many historical candidates a
    domain turns out to have.

    Idempotent-safe as a whole, not merely per-message: invoking this
    function TWICE for the same domain (e.g. a genuine double-submit of
    the same ALLOW decision) never re-fetches/re-ingests anything a
    second time — every individual message's own existing
    `_reprocess_one_message` idempotency backstop still applies, AND a
    message already reprocessed by the first call is no longer
    ``CHECKED_NOT_CANDIDATE`` (it is now ``INGESTED``/``QUARANTINED``/
    ``FAILED``/``VANISHED``), so `list_candidate_messages_for_domain`
    itself naturally no longer returns it on a second call.

    Returns the list of resulting `MailboxMessage` objects, oldest-
    received-first (mirrors `list_candidate_messages_for_domain`'s own
    ordering) — an EMPTY list, never `None`, when no eligible historical
    candidate exists for this domain.

    Raises:
        core.errors.ConflictError: `mailbox` is not ACTIVE+CONNECTED
            (same defensive backstop as `run_sweep`'s own precondition;
            checked ONCE here, up front, never per-message).
    """
    if mailbox.status != "ACTIVE" or mailbox.connection_state != CONNECTION_STATE_CONNECTED:
        raise ConflictError(
            f"mailbox '{mailbox.mailbox_id}' is not ACTIVE+CONNECTED — cannot reprocess historical "
            "candidates (reconnect the mailbox before approving this domain review item)"
        )

    candidates = message_repository.list_candidate_messages_for_domain(
        mailbox_id=mailbox.mailbox_id, sender_domain=sender_domain
    )

    results: list[MailboxMessage] = []
    for candidate in candidates:
        results.append(
            _reprocess_one_message(
                mailbox=mailbox,
                mailbox_source_id=mailbox_source_id,
                message_id=candidate.mailbox_message_id,
                rule=rule,
                adapter=adapter,
                message_repository=message_repository,
                api=api,
                object_store=object_store,
                scanner=scanner,
                actor_type=actor_type,
                actor_id=actor_id,
                correlation_id=correlation_id,
                now=now,
            )
        )
    return results
