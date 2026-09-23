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
2. Stage B (the domain/sender gate) — CD-6 GUI-operations-foundation
   follow-on WO: a THREE-state operator-learning policy model (was a
   two-state ``ALLOWED``/``IGNORED`` model) — the sender is looked up in
   this mailbox's own
   ``services.mailbox.domain_rule.MailboxDomainRuleRepository`` (most-
   specific-rule-wins across an ``EXACT_ADDRESS``/``EXACT``/
   ``INCLUDE_SUBDOMAINS`` match — see that module's own docstring):

   * ``MUST_READ`` (was ``ALLOWED``) — this specific MESSAGE's own
     authentication signals are checked FIRST (see "Authentication
     escalation" below); on a genuine PASS, proceeds with the existing
     Slice 4A full-MIME-fetch-and-evidence-ingest path, attaching the
     rule's ``destination_entity_id``/``destination_mode``/
     ``processor_hint`` to the resulting
     ``MailboxMessage.metadata["routing"]`` as NON-AUTHORITATIVE routing
     metadata (never accounting truth) — and, when
     ``destination_mode == "FIXED"``, the resulting ``EvidenceItem`` now
     gets a REAL ``entity_id`` at registration time (the CD-6
     GUI-operations-foundation follow-on WO's own fix for a confirmed
     pre-existing gap — see ``services/mailbox/microsoft/evidence_ingest.py``).
     A later, ordinary message from this SAME confirmed source NEVER
     re-raises a ``MAILBOX_DOMAIN_REVIEW`` item merely because it
     arrived — the whole point of this durable-memory model (architect,
     verbatim: "Once Matt has explicitly confirmed a sender/source as
     financially relevant, BAGMAN must remember that decision durably").
   * ``GRAYLIST`` (new) — behaves IDENTICALLY to "no rule yet" below
     (Matt looked at this domain once and deliberately left it under
     review; a real row exists purely for GUI display) — never a MIME
     fetch, same aggregated ``MAILBOX_DOMAIN_REVIEW`` item reuse.
   * ``BLACKLIST`` (was ``IGNORED``) — marked ``CHECKED_NOT_CANDIDATE``;
     no MIME fetch; the rule's ``last_seen_at`` is touched; never a
     Needs You item.
   * no rule yet (or ``GRAYLIST``) — ``services.mailbox.discovery_signals
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

Authentication escalation — a new, per-message security gate on top of
a MUST_READ source (CD-6 GUI-operations-foundation follow-on WO, later
hardened by that WO's own follow-on "five confirmed integration gaps"
fix — see items B/C of that fix's own PID for the full history)
------------------------------------------------------------------------
Trusting a SOURCE (a ``MUST_READ`` rule) is not the same as trusting
every individual MESSAGE claiming to be from it — a compromised/spoofed
sender is a real, different risk. :func:`evaluate_message_authentication`
now delegates to a real, bounded, deterministic (never AI/ML)
provider-neutral + Microsoft-selector pipeline
(``services.mailbox.authentication_assessment``/
``services.mailbox.microsoft.authentication`` — see those modules' own
docstrings for the complete PASS/FAIL/UNKNOWN trust-boundary reasoning,
built against real, live, redacted diagnostic header data) driven by the
message's own RAW headers, never the old flat, best-effort
``auth_signals`` dict alone (a confirmed real bug: that dict's own
parser silently kept only the FIRST token for a mechanism, even within
one legitimate header, and the old gate could not distinguish a
genuinely Microsoft-trusted header from an attacker-forgeable one at
all). Only a genuine PASS proceeds; both FAIL and UNKNOWN escalate —
an inconclusive verdict is never silently treated as trusted.

On a FAIL/UNKNOWN, the message is marked ``services.mailbox.message
.INGESTION_STATUS_SECURITY_REVIEW`` — a distinct, honest outcome (this
IS a real candidate that failed a security check, never merely "not a
candidate") — and exactly one
``services.needs_you.needs_you.ITEM_TYPE_MAILBOX_AUTHENTICATION_ESCALATION``
item is raised, deduped on ``(item_type, source_object_reference)`` with
``source_object_reference`` set to this message's own canonical
``mailbox_message_id`` (the generic dedupe mechanism
``services.needs_you.needs_you.NeedsYouRepository.create_needs_you_item``
already provides — equivalent to per-``(mailbox_id,
immutable_provider_message_id)`` deduplication, since
``record_observation`` itself already resolves that tuple to one stable
``mailbox_message_id``). This is architecturally a DIFFERENT question
from ``MAILBOX_DOMAIN_REVIEW`` — never the same item type, never the
same dedupe scope — architect, verbatim: "this is not BAGMAN re-asking
Matt's relevance decision — it is a new security event". The governing
``MailboxDomainRule`` itself is never touched by this — it stays
``MUST_READ``; only this ONE message's own outcome is affected. A
``SECURITY_REVIEW`` message is never MIME-fetched/evidence-created via
the normal trusted path, and is deliberately EXCLUDED from
``MailboxMessageRepository.list_candidate_messages_for_domain``'s own
eligibility filter (that filter already requires
``ingestion_status == CHECKED_NOT_CANDIDATE``, which ``SECURITY_REVIEW``
never is) — a security-escalated message must never be silently swept
into a later domain-approval back-process.

Document-level destination review for MUST_READ + REVIEW_REQUIRED (CD-6
GUI-operations-foundation follow-on WO)
------------------------------------------------------------------------
A ``MUST_READ``-policy, ``REVIEW_REQUIRED``-destination message that
passes its authentication check gets fully, deeply ingested exactly like
today (``entity_id`` stays ``None`` — domain alone never determines a
destination, architect §3) — but now ALSO raises (idempotently, keyed on
``(item_type, source_object_reference=evidence_id)``, reusing
``app/api/routers/intake.py``'s own established
``COMPANY_REQUIRED``/``COMPANY_WHAT_WHY`` vocabulary verbatim, so the
EXISTING Needs You review-drawer form renders it with zero GUI change)
one document-scoped Needs You item per evidence item created this way —
see :func:`_raise_document_destination_review_item`. This is the real
per-document resolution mechanism a domain-level ``REVIEW_REQUIRED``
rule defers to: resolving ONE such item never mutates the governing
``MailboxDomainRule`` itself (that item is resolved through the EXISTING
generic ``POST /internal/needs-you/{id}/resolve`` endpoint, scoped
purely to its own ``evidence_id`` — see that router's own docstring,
untouched by this WO), so a second, different ambiguous message from the
SAME domain always raises its OWN separate item, still
``REVIEW_REQUIRED`` at the rule level (architect's own explicit
acceptance test: one ambiguous document's destination choice never
silently converts every future document from that source).

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
   ``destination_entity_id`` named by one of its own ``MUST_READ``
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
from typing import Any, Callable, Mapping, Optional, Protocol, Sequence

from core.entity import EntityRepository
from core.errors import ConflictError, PersistenceError
from core.timestamps import to_contract_string, utc_now
from services.evidence.intake.scanner import EvidenceSafetyScanner
from services.mailbox.authentication_assessment import AUTH_ASSESSMENT_PASS, AuthenticationAssessment
from services.mailbox.bootstrap_policy import compute_entity_historical_bootstrap
from services.mailbox.cursor import MailboxFolderCursorRepository
from services.mailbox.discovery_signals import evaluate_discovery_candidate
from services.mailbox.domain_rule import (
    DESTINATION_MODE_FIXED,
    DESTINATION_MODE_REVIEW_REQUIRED,
    MATCH_MODE_INCLUDE_SUBDOMAINS,
    POLICY_BLACKLIST,
    POLICY_MUST_READ,
    MailboxDomainRule,
    MailboxDomainRuleRepository,
)
from services.mailbox.gmail.authentication import assess_gmail_authentication
from services.mailbox.imap.authentication import assess_imap_authentication
from services.mailbox.lock import MailboxSweepLock
from services.mailbox.mailbox import (
    CONNECTION_STATE_CONNECTED,
    PROVIDER_GOOGLE_GMAIL,
    PROVIDER_IMAP,
    PROVIDER_MICROSOFT_GRAPH,
    MailboxSource,
    MailboxSourceRepository,
)
from services.mailbox.message import (
    FINAL_INGESTION_STATUSES,
    INGESTION_STATUS_CHECKED_NOT_CANDIDATE,
    INGESTION_STATUS_FAILED,
    INGESTION_STATUS_INGESTED,
    INGESTION_STATUS_QUARANTINED,
    INGESTION_STATUS_SECURITY_REVIEW,
    INGESTION_STATUS_VANISHED,
    MailboxMessage,
    MailboxMessageRepository,
)
from services.mailbox.microsoft.adapter import FolderDiscoveryResult
from services.mailbox.microsoft.authentication import assess_microsoft_authentication
from services.mailbox.microsoft.evidence_ingest import (
    INGEST_STATUS_FAILED,
    INGEST_STATUS_INGESTED,
    INGEST_STATUS_QUARANTINED,
    ingest_email_evidence,
)
from services.mailbox.microsoft.graph_client import GraphOutcomeStatus
from services.mailbox.sweep_run import MailboxSweepRunRepository, SweepFailureReason
from services.needs_you.needs_you import (
    ALLOWED_ACTION_COMPANY_WHAT_WHY,
    ALLOWED_ACTION_MAILBOX_AUTHENTICATION_ESCALATION,
    ALLOWED_ACTION_MAILBOX_DOMAIN_REVIEW,
    ITEM_TYPE_COMPANY_REQUIRED,
    ITEM_TYPE_MAILBOX_AUTHENTICATION_ESCALATION,
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
       ``MUST_READ`` ``MailboxDomainRule`` rows.
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
        if rule.policy == POLICY_MUST_READ and rule.destination_entity_id is not None:
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
    """Non-authoritative ROUTING metadata attached to a MUST_READ-rule
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


def evaluate_message_authentication(
    raw_headers: Optional[Sequence[Mapping[str, Optional[str]]]],
    *,
    provider_kind: str = PROVIDER_MICROSOFT_GRAPH,
) -> AuthenticationAssessment:
    """CD-6 GUI-operations-foundation follow-on WO — the bounded,
    deterministic (never AI/ML) per-message authentication check a
    ``MUST_READ`` rule's own trusted-source status is checked against
    before this SPECIFIC message is allowed down the normal MIME-fetch-
    and-evidence path. See module docstring's own "Authentication
    escalation" section for the full reasoning.

    **Superseded design note**: this function used to gate directly on
    the flat, best-effort ``auth_signals`` dict (a bare ``spf``/``dkim``/
    ``dmarc`` == ``"fail"`` check) — a confirmed, real defect the
    architect found on direct code inspection: that flat dict came from
    ``services.mailbox.microsoft.graph_client._parse_auth_signals``'s own
    ``setdefault``-based parser, which silently kept only the FIRST
    token for a mechanism even within one legitimate trusted header (two
    real, live Infosecurs samples carried two separate ``dkim=`` tokens
    in a single header), and never distinguished a genuinely trusted
    Microsoft-stamped header from an attacker-forgeable one at all. This
    function now delegates entirely to the real provider-neutral +
    provider-specific selector pipeline, driven by the message's own RAW
    headers rather than that flat dict.

    **Second-provider addition (IMAP delivery)** — ``provider_kind`` is
    the ONE, minimal, additive provider-neutral abstraction point this
    delivery added to this file: a plain dispatch on the mailbox's own
    governed ``provider_kind`` to the correct provider-specific
    selector. Every call site in this module passes
    ``provider_kind=mailbox.provider_kind`` explicitly; the default
    (``PROVIDER_MICROSOFT_GRAPH``) exists purely so this remains
    backward-compatible with any caller that predates this parameter —
    Microsoft's own behaviour is BYTE-IDENTICAL before and after this
    change (the exact same
    :func:`services.mailbox.microsoft.authentication.assess_microsoft_authentication`
    call, for the exact same inputs). See
    :func:`services.mailbox.imap.authentication.assess_imap_authentication`'s
    own module docstring for the IMAP selector's own (deliberately
    PROVISIONAL) trust-boundary reasoning.

    **Third-provider addition (Gmail delivery)** — one further additive
    branch, dispatching ``PROVIDER_GOOGLE_GMAIL`` to
    :func:`services.mailbox.gmail.authentication.assess_gmail_authentication`
    (also deliberately PROVISIONAL — see that module's own docstring).
    The Microsoft and IMAP branches above are UNCHANGED, byte-identical,
    by this addition.

    Never raises; never returns anything other than a bounded
    :class:`~services.mailbox.authentication_assessment.AuthenticationAssessment`.
    """
    if provider_kind == PROVIDER_IMAP:
        return assess_imap_authentication(raw_headers)
    if provider_kind == PROVIDER_GOOGLE_GMAIL:
        return assess_gmail_authentication(raw_headers)
    return assess_microsoft_authentication(raw_headers)


def _auth_assessment_metadata(assessment: AuthenticationAssessment) -> dict:
    """CD-6 GUI-operations-foundation follow-on WO (item C) — the
    ``MailboxMessage.metadata`` shape a persisted authentication
    assessment is stored under, so a later operator/reader can see WHY a
    message passed or was held (never just the bare terminal status).
    Folded into the existing, already-established open ``metadata``
    field — a documented, no-migration judgment call (see
    ``services.mailbox.authentication_assessment.AuthenticationAssessment
    .to_metadata``'s own docstring)."""
    return {"auth_assessment": assessment.to_metadata()}


def _raise_authentication_escalation_item(
    needs_you_repository: NeedsYouRepository, *, mailbox: MailboxSource, message: MailboxMessage, reason: str
):
    """Raise (or, on a benign replay, resolve to the EXISTING) one
    ``ITEM_TYPE_MAILBOX_AUTHENTICATION_ESCALATION`` item for this ONE
    message — see module docstring's "Authentication escalation"
    section for why this is deduped per-message (via the generic
    ``(item_type, source_object_reference=mailbox_message_id)``
    mechanism ``create_needs_you_item`` already provides), never per-
    domain, and never conflated with ``MAILBOX_DOMAIN_REVIEW``."""
    return needs_you_repository.create_needs_you_item(
        item_type=ITEM_TYPE_MAILBOX_AUTHENTICATION_ESCALATION,
        domain="MAILBOX",
        source_object_reference=message.mailbox_message_id,
        question=(
            f"A message from a trusted (MUST_READ) source failed its authentication check — "
            f"{mailbox.display_name} ({mailbox.email_address}), sender '{message.sender_address or 'unknown'}'"
        ),
        allowed_action_type=ALLOWED_ACTION_MAILBOX_AUTHENTICATION_ESCALATION,
        priority="HIGH",
        metadata={
            "mailbox_id": mailbox.mailbox_id,
            "email_address": mailbox.email_address,
            "sender_address": message.sender_address,
            "sender_domain": message.sender_domain,
            "mailbox_message_id": message.mailbox_message_id,
            "subject": message.subject,
            "reason": reason,
        },
    )


def _raise_document_destination_review_item(
    needs_you_repository: NeedsYouRepository, *, mailbox: MailboxSource, message: MailboxMessage, evidence
):
    """CD-6 GUI-operations-foundation follow-on WO — see module
    docstring's own "Document-level destination review" section. Reuses
    ``app/api/routers/intake.py::_create_needs_you_item_for_accepted_evidence``'s
    exact ``COMPANY_REQUIRED``/``COMPANY_WHAT_WHY`` vocabulary verbatim
    (never a new item type) so the EXISTING Needs You review-drawer form
    renders this with zero GUI change, deduped idempotently via the
    generic ``(item_type, source_object_reference=evidence_id)``
    mechanism — one item per evidence item, never per domain."""
    original_label = message.subject or evidence.evidence_id
    return needs_you_repository.create_needs_you_item(
        item_type=ITEM_TYPE_COMPANY_REQUIRED,
        domain="MAILBOX",
        source_object_reference=evidence.evidence_id,
        question=f"Which company is this document for, and what/why? ({original_label})",
        allowed_action_type=ALLOWED_ACTION_COMPANY_WHAT_WHY,
        metadata={
            "evidence_id": evidence.evidence_id,
            "mailbox_id": mailbox.mailbox_id,
            "email_address": mailbox.email_address,
            "mailbox_message_id": message.mailbox_message_id,
            "sender_address": message.sender_address,
            "sender_domain": message.sender_domain,
            "subject": message.subject,
        },
    )


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


def _synchronize_domain_review_aggregate(
    needs_you_repository: NeedsYouRepository,
    message_repository: MailboxMessageRepository,
    *,
    mailbox: MailboxSource,
    sender_domain: str,
    triggering_message: MailboxMessage,
    reason: str,
):
    """Deterministic projection of canonical, currently-eligible
    candidate ``MailboxMessage`` rows for ``(mailbox.mailbox_id,
    sender_domain)`` — via ``MailboxMessageRepository
    .list_candidate_messages_for_domain``, the SAME canonical query
    :func:`reprocess_all_historical_candidates_for_domain` already uses
    — onto that domain's OPEN ``MAILBOX_DOMAIN_REVIEW`` item's aggregate
    metadata (or a freshly-created item, if none is currently OPEN — see
    "Missing-item self-heal" below). Never increments a stored counter
    and never assumes processing/presentation order reflects
    chronological order — every call recomputes the FULL aggregate from
    canonical persisted state, so calling this function twice against
    UNCHANGED canonical rows produces byte-identical metadata both
    times.

    **Supersedes the old incremental-accumulation design** (formerly
    ``_create_or_reuse_domain_review_item``), which updated a REUSED
    item by incrementing a stored counter and blindly overwriting
    ``last_seen_at`` with whatever candidate happened to be PROCESSED
    on this particular call — silently wrong whenever a provider's own
    historical enumeration order is not itself chronological. This was
    a real, confirmed production defect: a real Gmail historical sweep
    produced a domain-review item with ``last_seen_at`` chronologically
    EARLIER than ``first_seen_at``, because Gmail's historical
    enumeration commonly presents newer messages before older ones
    within a single sweep round — the old code's ``last_seen_at`` held
    whatever was processed most recently in PROCESSING order, never the
    latest in TIME order.

    Recomputed fields — unambiguous semantics (the architect's own
    explicit correction: these are the candidate MESSAGES' own
    timestamps, nothing else):

    * ``candidate_message_count`` — ``len(candidates)``, the count of
      every currently-eligible candidate on record for this domain.
    * ``first_seen_at`` — the MINIMUM ``received_at`` across every
      candidate ``MailboxMessage`` currently on record for this domain
      — the earliest candidate MESSAGE's own receipt time. NOT BAGMAN's
      own discovery/observation time, NOT this Needs You item's own
      creation/update time, and NOT provider enumeration order.
    * ``last_seen_at`` — the MAXIMUM ``received_at`` across the same
      set — identical semantics, latest instead of earliest.
    * ``attachment_bearing_count`` — how many of those candidates have
      ``has_attachments``.

    Populated only when creating a fresh item, never re-derived on a
    later synchronize call (they describe the domain/mailbox itself,
    not any one candidate):

    * ``proposed_destination_entity_id`` — the mailbox's own
      ``default_entity_id`` HINT, if set, else ``None``. Deliberately
      labelled here as a non-authoritative HINT ONLY — mirrors
      ``services.mailbox.mailbox``'s own established "``default_entity_id``
      is only ever an optional DISPLAY hint... never ownership
      assertion" doctrine (see that module's own docstring).
    * ``proposed_processor_hint`` — always ``None`` (see the old
      docstring's identical note — unchanged by this correction).
    * ``confidence_reason`` — ``reason``, as supplied by the caller —
      for the ordinary discovery call site, this is
      ``services.mailbox.discovery_signals.evaluate_discovery_candidate``'s
      own real, honest explanation; for the duplicate/self-heal call
      site (see :func:`run_sweep`'s own duplicate-branch handling), this
      is the ALREADY-PERSISTED ``discovery_reason`` off the existing
      ``MailboxMessage`` row — never a freshly-reclassified value.

    Missing-item self-heal: if no OPEN item currently exists for this
    domain — either a genuinely new domain, or a prior sweep that
    durably persisted a candidate ``MailboxMessage`` row but crashed
    before ever creating/updating its Needs You item (the CD-6
    lifecycle-hardening ``except Exception`` in :func:`run_sweep`
    catches exactly this) — a fresh item is created here, using
    ``triggering_message`` as its source object. Safe specifically
    because this is a pure recomputation from canonical rows, never an
    increment: creating the item late (on a replay) still yields the
    exact same aggregate a timely creation would have.
    """
    candidates = message_repository.list_candidate_messages_for_domain(
        mailbox_id=mailbox.mailbox_id, sender_domain=sender_domain
    )
    # `triggering_message` is itself always a member of `candidates` by
    # construction — it was just persisted (or re-observed) as
    # `discovery_candidate is True` / `CHECKED_NOT_CANDIDATE` before
    # this function is ever called — so `candidates` should never
    # genuinely be empty here. Defended anyway: never a bare
    # `min()`/`max()` crash on a surprising empty sequence.
    if not candidates:
        candidates = [triggering_message]

    candidate_count = len(candidates)
    first_seen_at = min(m.received_at for m in candidates)
    last_seen_at = max(m.received_at for m in candidates)
    attachment_bearing_count = sum(1 for m in candidates if m.has_attachments)

    existing = _find_open_domain_review_item(needs_you_repository, mailbox_id=mailbox.mailbox_id, sender_domain=sender_domain)
    if existing is not None:
        return needs_you_repository.update_item_metadata(
            existing.item_id,
            metadata_updates={
                "candidate_message_count": candidate_count,
                "first_seen_at": to_contract_string(first_seen_at),
                "last_seen_at": to_contract_string(last_seen_at),
                "attachment_bearing_count": attachment_bearing_count,
            },
        )

    return needs_you_repository.create_needs_you_item(
        item_type=ITEM_TYPE_MAILBOX_DOMAIN_REVIEW,
        domain="MAILBOX",
        source_object_reference=triggering_message.mailbox_message_id,
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
            "triggering_mailbox_message_id": triggering_message.mailbox_message_id,
            "reason": reason,
            "candidate_message_count": candidate_count,
            "first_seen_at": to_contract_string(first_seen_at),
            "last_seen_at": to_contract_string(last_seen_at),
            "attachment_bearing_count": attachment_bearing_count,
            # Non-authoritative HINT only — see this function's own
            # docstring's "Populated only when creating" section above.
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

    def fetch_message_headers(self, *, mailbox_id: str, immutable_message_id: str): ...

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
                    # Resolve this page's request position ONCE, into
                    # local variables, before the first attempt. A
                    # rate-limited retry below reuses these exact same
                    # values — it must retry the identical logical page
                    # request, never recompute or lose the bootstrap
                    # boundary / delta cursor / continuation token.
                    request_delta_link = cursor.delta_link if (first_page and not is_bootstrap_round) else None
                    request_next_link = next_link if not first_page else None
                    request_bootstrap_timestamp = (
                        cursor.bootstrap_timestamp if (first_page and is_bootstrap_round) else None
                    )
                    first_page = False

                    page = adapter.fetch_folder_delta(
                        mailbox_id=mailbox.mailbox_id,
                        folder=folder,
                        delta_link=request_delta_link,
                        next_link=request_next_link,
                        bootstrap_timestamp=request_bootstrap_timestamp,
                    )

                    if page.status == GraphOutcomeStatus.RATE_LIMITED:
                        graph_throttle_retries += 1
                        backoff = min(page.retry_after_seconds or 5.0, _MAX_RATE_LIMIT_BACKOFF_SECONDS)
                        sleep_fn(backoff)
                        page = adapter.fetch_folder_delta(
                            mailbox_id=mailbox.mailbox_id,
                            folder=folder,
                            delta_link=request_delta_link,
                            next_link=request_next_link,
                            bootstrap_timestamp=request_bootstrap_timestamp,
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
                            reobserved_message, _ = message_repository.record_observation(
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
                            # Missing-review-item self-heal (architect
                            # ruling, section 7): a replayed candidate
                            # message short-circuits here on every
                            # ordinary re-observation (`CHECKED_NOT_
                            # CANDIDATE` is itself a member of
                            # `FINAL_INGESTION_STATUSES`), so if the
                            # ORIGINAL sweep persisted this candidate row
                            # successfully but then failed to create/
                            # update its `MAILBOX_DOMAIN_REVIEW` item
                            # (e.g. a crash between those two steps, now
                            # correctly caught by the CD-6 lifecycle-
                            # hardening `except Exception` in this
                            # function), a replay must not leave that
                            # aggregate permanently missing/stale. Safe
                            # to call unconditionally on every duplicate
                            # of an eligible candidate — never re-runs
                            # `evaluate_discovery_candidate`, never
                            # reclassifies (`existing.discovery_reason`
                            # is the ALREADY-PERSISTED reason), and is a
                            # no-op in effect on a genuine duplicate
                            # (the synchronize function recomputes from
                            # canonical rows, which already included this
                            # message, not from a "seen this sweep round"
                            # counter).
                            if (
                                existing.ingestion_status == INGESTION_STATUS_CHECKED_NOT_CANDIDATE
                                and existing.discovery_candidate is True
                                and sender_domain
                            ):
                                _synchronize_domain_review_aggregate(
                                    needs_you_repository,
                                    message_repository,
                                    mailbox=mailbox,
                                    sender_domain=sender_domain,
                                    triggering_message=reobserved_message,
                                    reason=existing.discovery_reason,
                                )
                            continue

                        # -- Stage A (always) + Stage B (the domain gate) --
                        attachment_metadata = _attachment_metadata_dicts(msg.attachment_metadata)
                        auth_signals = dict(msg.auth_signals)

                        def _record_discovery_only(
                            status: str,
                            *,
                            discovery_candidate: Optional[bool] = None,
                            discovery_reason: Optional[str] = None,
                            discovery_checked_at: Optional[datetime] = None,
                            metadata: Optional[Mapping[str, Any]] = None,
                        ) -> MailboxMessage:
                            """Second CD-6 architect amendment (persisted
                            discovery decision) — the three new optional
                            params default to the "not applicable" `None`
                            state so the VANISHED call site (unrelated to
                            domain-gate discovery) doesn't need to pass
                            anything. The IGNORED-domain branch passes
                            only `discovery_checked_at` (the heuristic
                            never ran there); the UNKNOWN-domain branch
                            passes all three."""
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
                                discovery_candidate=discovery_candidate,
                                discovery_reason=discovery_reason,
                                discovery_checked_at=discovery_checked_at,
                                metadata=metadata,
                            )
                            return message

                        rule = (
                            domain_rule_repository.find_for_sender(
                                mailbox_id=mailbox.mailbox_id,
                                sender_domain=sender_domain,
                                sender_address=msg.sender_address,
                            )
                            if sender_domain
                            else None
                        )

                        if rule is not None and rule.policy == POLICY_BLACKLIST:
                            ignored_domain_messages += 1
                            domain_rule_repository.touch_last_seen(
                                mailbox_id=mailbox.mailbox_id,
                                sender_domain=rule.sender_domain,
                                seen_at=resolved_now,
                                sender_address=msg.sender_address,
                            )
                            messages_new += 1
                            folder_entry["new_discovery_records"] += 1
                            # `discovery_candidate`/`discovery_reason`
                            # stay `None` — the domain gate short-
                            # circuits BEFORE `evaluate_discovery_candidate`
                            # is ever called for a BLACKLIST-policy
                            # message (see module docstring). Only
                            # `discovery_checked_at` is honestly set —
                            # this message WAS checked, just not by the
                            # Stage-A heuristic.
                            _record_discovery_only(
                                INGESTION_STATUS_CHECKED_NOT_CANDIDATE, discovery_checked_at=resolved_now
                            )
                            continue

                        if rule is None or rule.policy != POLICY_MUST_READ:
                            # No rule, or GRAYLIST — both behave
                            # identically here: the bounded, non-AI
                            # discovery-candidate heuristic (architect
                            # spec §4). Never a MIME fetch either way.
                            unknown_domain_messages += 1
                            signal = evaluate_discovery_candidate(
                                subject=msg.subject,
                                attachment_metadata=attachment_metadata,
                                has_attachments=msg.has_attachments,
                            )
                            if signal.is_candidate:
                                likely_financial_candidates += 1
                            messages_new += 1
                            folder_entry["new_discovery_records"] += 1
                            message = _record_discovery_only(
                                INGESTION_STATUS_CHECKED_NOT_CANDIDATE,
                                discovery_candidate=signal.is_candidate,
                                discovery_reason=signal.reason if signal.is_candidate else None,
                                discovery_checked_at=resolved_now,
                            )
                            if signal.is_candidate and sender_domain:
                                _synchronize_domain_review_aggregate(
                                    needs_you_repository,
                                    message_repository,
                                    mailbox=mailbox,
                                    sender_domain=sender_domain,
                                    triggering_message=message,
                                    reason=signal.reason,
                                )
                            continue

                        # rule.policy == POLICY_MUST_READ — a confirmed,
                        # durably-trusted source. This specific MESSAGE's
                        # own authentication signals are checked FIRST
                        # (see module docstring's "Authentication
                        # escalation" section) before any MIME fetch.
                        allowed_domain_messages += 1
                        domain_rule_repository.touch_last_seen(
                            mailbox_id=mailbox.mailbox_id,
                            sender_domain=rule.sender_domain,
                            seen_at=resolved_now,
                            sender_address=msg.sender_address,
                        )

                        assessment = evaluate_message_authentication(
                            msg.raw_headers, provider_kind=mailbox.provider_kind
                        )
                        if assessment.verdict != AUTH_ASSESSMENT_PASS:
                            messages_new += 1
                            folder_entry["new_discovery_records"] += 1
                            escalated_message = _record_discovery_only(
                                INGESTION_STATUS_SECURITY_REVIEW,
                                discovery_checked_at=resolved_now,
                                metadata=_auth_assessment_metadata(assessment),
                            )
                            _raise_authentication_escalation_item(
                                needs_you_repository, mailbox=mailbox, message=escalated_message, reason=assessment.reason
                            )
                            continue

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
                        # governed-oversize-failed) — the MUST_READ-
                        # domain path's own real cost centre (see the
                        # schema's own field description).
                        folder_entry["deep_processing_count"] += 1
                        # CD-6 GUI-operations-foundation follow-on WO —
                        # the real entity-assignment fix: a FIXED
                        # destination now threads its real
                        # destination_entity_id straight through to
                        # `EvidenceRepository.register_evidence` at
                        # registration time (never `assign_entity` — see
                        # this delivery's own final report for why that
                        # method remains unwired). A REVIEW_REQUIRED
                        # destination stays unresolved (`entity_id=None`)
                        # exactly as before — domain alone never
                        # determines a destination (architect §3).
                        entity_id_for_evidence = (
                            rule.destination_entity_id if rule.destination_mode == DESTINATION_MODE_FIXED else None
                        )
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
                            entity_id=entity_id_for_evidence,
                            external_reference_namespace=mailbox.provider_kind,
                        )

                        # CD-6 GUI-operations-foundation follow-on WO
                        # (item C) — persist the PASSING assessment too
                        # (never only the FAIL/UNKNOWN case above), so a
                        # later operator/reader can see WHY a message
                        # passed, not only why one was held.
                        routing_metadata = {**_routing_metadata(rule), **_auth_assessment_metadata(assessment)}

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
                                # CD-6 GUI-operations-foundation follow-on
                                # WO — REVIEW_REQUIRED-destination
                                # evidence raises its OWN document-scoped
                                # COMPANY_REQUIRED item (see module
                                # docstring's own "Document-level
                                # destination review" section); a FIXED
                                # destination already has a real
                                # entity_id (above) and never needs one.
                                if rule.destination_mode == DESTINATION_MODE_REVIEW_REQUIRED:
                                    _raise_document_destination_review_item(
                                        needs_you_repository, mailbox=mailbox, message=message, evidence=outcome.evidence
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

        except Exception as exc:
            # An unexpected exception NOT already turned into a
            # `_SweepStopped` by a known-failure path above (e.g. this
            # delivery's own now-proven non-UTC `Date`-header defect, or
            # a genuine database/persistence failure) must still
            # terminalize this run's `MailboxSweepRun` row rather than
            # leave it stuck `RUNNING` forever behind an unhandled HTTP
            # 500 — see module/PID discussion of this defect. Never
            # `except BaseException` here: `KeyboardInterrupt`/
            # `SystemExit` must keep propagating uncaught.
            error_code = (
                SweepFailureReason.PERSISTENCE_ERROR
                if isinstance(exc, PersistenceError)
                else SweepFailureReason.UNEXPECTED_ERROR
            )
            # Sanitized, bounded detail ONLY — never `str(exc)` or any
            # raw exception message/provider response/sender data/SQL
            # value/credential; see this delivery's own hard constraint.
            error_detail = f"unexpected internal exception during mailbox sweep ({type(exc).__name__})"
            failures += 1
            # Deliberately NOT wrapped in its own try/except — if
            # `complete_run` itself raises (the sweep-run repository/
            # persistence layer being unavailable), that exception must
            # propagate naturally (architect's own explicit ruling: no
            # recursive recovery, no retry). The existing top-level
            # unhandled-exception handling in `app/api/main.py` already
            # sanitizes anything that does escape this far.
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
                error_code=error_code,
                error_detail=error_detail,
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
    needs_you_repository: NeedsYouRepository,
    api: _EvidenceAPIProtocol,
    object_store: _ObjectStoreProtocol,
    scanner: EvidenceSafetyScanner,
    actor_type: str,
    actor_id: str,
    correlation_id: Optional[str] = None,
    now: Optional[datetime] = None,
    sleep_fn: Callable[[float], None] = time.sleep,
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

    CD-6 GUI-operations-foundation follow-on WO (item C) — this helper
    now ALSO runs the real authentication gate, via a fresh, bounded,
    METADATA-ONLY headers refresh (`adapter.fetch_message_headers`),
    before ever fetching full MIME — see this function's own inline
    comment, above the `fetch_message_headers` call, for the full
    reasoning (this supersedes this docstring's own former "never runs
    evaluate_message_authentication" description, which was itself a
    confirmed, real, blocking integration gap the architect found on
    direct code inspection). The other two additions from the earlier
    revision of this WO remain: threading `rule.destination_entity_id`
    through to `ingest_email_evidence` when `destination_mode == "FIXED"`
    (the same entity-assignment fix `run_sweep` itself applies), and
    raising a document-scoped `COMPANY_REQUIRED` item when
    `destination_mode == "REVIEW_REQUIRED"` and ingestion succeeds
    (mirrors `run_sweep`'s own identical call — see that function's own
    module docstring, "Document-level destination review" section).

    CD-6 follow-on quota-scaling fix — bounded RATE_LIMITED retry (this
    delivery): this function's own module-level docstring / the public
    :func:`reprocess_all_historical_candidates_for_domain`'s own
    docstring used to CLAIM this reused "the existing bounded rate-limit
    backoff discipline" every other Graph/Gmail call in this module
    already uses — that claim was false for `RATE_LIMITED` specifically:
    both the `adapter.fetch_message_headers` call and the
    `adapter.fetch_message_content` call below used to treat ANY non-OK/
    non-NOT_FOUND status (RATE_LIMITED included) identically — an
    immediate, un-retried `raise ConflictError(...)`. Fixed here: for
    EITHER call, a `RATE_LIMITED` result is retried EXACTLY ONCE — the
    identical bounded shape `run_sweep`'s own page-fetch retry already
    uses (`backoff = min(retry_after_seconds or 5.0,
    _MAX_RATE_LIMIT_BACKOFF_SECONDS)`, `sleep_fn(backoff)`, then one
    single retried call with the SAME arguments) — before falling
    through to the existing NOT_FOUND/OK/else-raise handling, which
    itself needed no changes at all: a second consecutive `RATE_LIMITED`
    is simply one more non-OK/non-NOT_FOUND status that same existing
    `raise ConflictError(...)` branch already covers, so it is never
    special-cased separately. Neither retry mutates this message's own
    ingestion status, calls `adapter.report_connection_error`, or
    touches `mailbox.connection_state` — only the FINAL outcome of each
    call (first attempt if not RATE_LIMITED, otherwise the one retry) is
    ever recorded, exactly mirroring `run_sweep`'s own "the surrounding
    failure path handles a still-RATE_LIMITED retry" doctrine.
    `adapter.fetch_message_headers`/`fetch_message_content` (the Gmail
    adapter's own implementations) already re-consult their own shared
    per-mailbox `messages.get` pacing governor internally on every call
    they make, retries included — this function stays fully adapter-
    agnostic and never touches that governor (or any other adapter-
    internal detail) directly.
    """
    current = message_repository.get_message(message_id)
    if current.ingestion_status in FINAL_INGESTION_STATUSES - {INGESTION_STATUS_CHECKED_NOT_CANDIDATE}:
        # Already fully, durably decided by something else (e.g. a
        # genuine double-submit of this same approval) — a safe no-op.
        return current

    # CD-6 GUI-operations-foundation follow-on WO (item C) — historical
    # back-processing must now pass the SAME security gate an ordinary
    # live sweep applies (this helper's own docstring USED TO admit it
    # "never runs evaluate_message_authentication" — a confirmed,
    # blocking integration gap the architect found; fixed here). The 959
    # real historical candidate `MailboxMessage` rows only carry the OLD,
    # buggy parser's flat `auth_signals` — never trusted for this new
    # assessment. A bounded, METADATA-ONLY headers refresh (never a full
    # MIME fetch merely to perform this check) is fetched fresh, by this
    # message's own immutable provider id, and assessed via the SAME
    # provider-neutral + Microsoft-selector pipeline the live-sweep path
    # uses. PASS -> proceed to the existing MIME-fetch/evidence path
    # exactly as before. FAIL/UNKNOWN -> mark SECURITY_REVIEW and raise
    # the SAME escalation item type the live-sweep path raises — never
    # silently skip/drop the message, never silently proceed as if it
    # passed.
    headers_result = adapter.fetch_message_headers(
        mailbox_id=mailbox.mailbox_id, immutable_message_id=current.immutable_provider_message_id
    )
    if headers_result.status == GraphOutcomeStatus.RATE_LIMITED:
        # Bounded single retry — identical shape to `run_sweep`'s own
        # page-fetch retry (see this function's own docstring, "CD-6
        # follow-on quota-scaling fix" section). A second RATE_LIMITED
        # is never special-cased: it falls straight into the existing
        # NOT_FOUND/OK/else-raise handling below like any other non-OK
        # status would.
        backoff = min(headers_result.retry_after_seconds or 5.0, _MAX_RATE_LIMIT_BACKOFF_SECONDS)
        sleep_fn(backoff)
        headers_result = adapter.fetch_message_headers(
            mailbox_id=mailbox.mailbox_id, immutable_message_id=current.immutable_provider_message_id
        )

    if headers_result.status == GraphOutcomeStatus.NOT_FOUND:
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

    if headers_result.status != GraphOutcomeStatus.OK:
        raise ConflictError(
            f"could not fetch headers for message '{message_id}' during domain-rule-approval "
            f"reprocessing (required before the authentication gate can run): "
            f"{headers_result.status.value} — {headers_result.error_detail or ''}"
        )

    assessment = evaluate_message_authentication(headers_result.raw_headers, provider_kind=mailbox.provider_kind)
    if assessment.verdict != AUTH_ASSESSMENT_PASS:
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
            ingestion_status=INGESTION_STATUS_SECURITY_REVIEW,
            sender_domain=current.sender_domain,
            attachment_metadata=current.attachment_metadata,
            auth_signals=current.auth_signals,
            metadata=_auth_assessment_metadata(assessment),
        )
        _raise_authentication_escalation_item(
            needs_you_repository, mailbox=mailbox, message=message, reason=assessment.reason
        )
        return message

    content_result = adapter.fetch_message_content(
        mailbox_id=mailbox.mailbox_id, immutable_message_id=current.immutable_provider_message_id
    )
    if content_result.status == GraphOutcomeStatus.RATE_LIMITED:
        # Bounded single retry — identical shape to the headers-fetch
        # retry above (see this function's own docstring). A second
        # RATE_LIMITED falls straight into the existing NOT_FOUND/OK/
        # else-raise handling below unchanged.
        backoff = min(content_result.retry_after_seconds or 5.0, _MAX_RATE_LIMIT_BACKOFF_SECONDS)
        sleep_fn(backoff)
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

    entity_id_for_evidence = rule.destination_entity_id if rule.destination_mode == DESTINATION_MODE_FIXED else None
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
        entity_id=entity_id_for_evidence,
        external_reference_namespace=mailbox.provider_kind,
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
            if rule.destination_mode == DESTINATION_MODE_REVIEW_REQUIRED:
                _raise_document_destination_review_item(
                    needs_you_repository, mailbox=mailbox, message=message, evidence=outcome.evidence
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
    mailbox_domain_rule_repository: MailboxDomainRuleRepository,
    adapter: _AdapterProtocol,
    message_repository: MailboxMessageRepository,
    needs_you_repository: NeedsYouRepository,
    api: _EvidenceAPIProtocol,
    object_store: _ObjectStoreProtocol,
    scanner: EvidenceSafetyScanner,
    actor_type: str,
    actor_id: str,
    correlation_id: Optional[str] = None,
    now: Optional[datetime] = None,
    sleep_fn: Callable[[float], None] = time.sleep,
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
    separately from the rest. When ``rule.match_mode`` is
    ``MATCH_MODE_INCLUDE_SUBDOMAINS`` this candidate query is ALSO
    widened (``include_subdomains=True``) to reach candidates from real
    subdomains of ``sender_domain`` (e.g. approving ``example.com``
    INCLUDE_SUBDOMAINS must reach ``billing.example.com`` candidates
    too) — see :func:`services.mailbox.domain_rule.domain_in_scope`.

    Effective-rule re-resolution — the real defect this function used to
    have, and the fix (PL-escalated architect defect, three parts):
    candidates matching the broadened domain query are NOT simply
    handed the caller's own ``rule`` unconditionally. Historically this
    function did exactly that, which was wrong in three distinct ways:
    (1) approving one specific ``EXACT_ADDRESS`` (e.g. ``ap@vendor.com``)
    would wrongly sweep in every OTHER address at ``vendor.com`` too
    (``marketing@vendor.com``, ...) since the old query only ever
    matched on domain; (2) approving a domain with
    ``INCLUDE_SUBDOMAINS`` never reached subdomain candidates at all
    (fixed by the widened query above); (3) most importantly, a
    candidate the widened query surfaces might actually be governed by
    a DIFFERENT, MORE SPECIFIC existing rule (e.g. a pre-existing
    ``BLACKLIST``/``EXACT_ADDRESS`` override on one address, or a
    ``BLACKLIST``/``GRAYLIST`` override on a child domain) — blindly
    applying the newly-approved ``rule`` to it would silently overrule
    that more-specific decision. The fix: for EACH candidate, re-resolve
    its own TRUE effective governing rule via
    ``mailbox_domain_rule_repository.find_for_sender`` (the SAME
    authoritative most-specific-first resolution ``run_sweep`` already
    uses for live mail — see that function's own domain-gate section),
    keyed by the candidate's OWN ``sender_domain``/``sender_address``
    (never the caller's ``sender_domain`` parameter). A candidate is
    reprocessed ONLY when that effective rule's ``rule_id`` is EXACTLY
    the just-approved ``rule.rule_id`` — never merely "same policy" or
    "same domain". Any candidate whose effective rule resolves to
    something else (a different, more specific rule) — or, in the
    defensive/unreachable case, to no rule at all — is skipped entirely:
    zero header fetch, zero MIME fetch, zero scanner call, zero status
    mutation, and it does NOT appear in the returned list. This never
    duplicates ``find_for_sender``'s own three-tier precedence logic —
    it is called, not reimplemented.

    Bounded execution (architect §11 — "no retry storm, no unbounded
    parallel Graph requests"): candidates are reprocessed SEQUENTIALLY,
    one message at a time — this function deliberately never introduces
    concurrency here, however many historical candidates a domain turns
    out to have. A genuine `RATE_LIMITED` result from either
    ``adapter.fetch_message_headers`` or ``adapter.fetch_message_content``
    inside :func:`_reprocess_one_message` is now (CD-6 follow-on quota-
    scaling fix) actually retried once, reusing the exact same
    ``_MAX_RATE_LIMIT_BACKOFF_SECONDS``-bounded backoff shape
    ``run_sweep``'s own page-fetch retry uses — see
    :func:`_reprocess_one_message`'s own docstring, "CD-6 follow-on
    quota-scaling fix" section, for the full contract (this docstring
    used to claim that retry discipline already existed here; it did
    not, for `RATE_LIMITED` specifically, until this fix). ``sleep_fn``
    (defaulting to ``time.sleep``, mirroring ``run_sweep``'s own
    identical parameter) is threaded straight through to each
    :func:`_reprocess_one_message` call so tests can drive this
    deterministically with zero real sleeping.

    Idempotent-safe as a whole, not merely per-message: invoking this
    function TWICE for the same domain (e.g. a genuine double-submit of
    the same ALLOW decision) never re-fetches/re-ingests anything a
    second time — every individual message's own existing
    `_reprocess_one_message` idempotency backstop still applies, AND a
    message already reprocessed by the first call is no longer
    ``CHECKED_NOT_CANDIDATE`` (it is now ``INGESTED``/``QUARANTINED``/
    ``FAILED``/``VANISHED``), so `list_candidate_messages_for_domain`
    itself naturally no longer returns it on a second call. A candidate
    SKIPPED by effective-rule filtering stays ``CHECKED_NOT_CANDIDATE``
    — untouched, still eligible for a later, explicit decision about it
    specifically (e.g. its own domain/address being separately
    approved) — never stuck in some new intermediate state.

    Returns the list of resulting `MailboxMessage` objects for
    candidates that were ACTUALLY processed, oldest-received-first
    (mirrors `list_candidate_messages_for_domain`'s own ordering, since
    skipped candidates are simply omitted rather than reordering
    anything) — an EMPTY list, never `None`, when no eligible historical
    candidate exists for this domain, or none of the discovered
    candidates are actually governed by this rule.

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

    include_subdomains = rule.match_mode == MATCH_MODE_INCLUDE_SUBDOMAINS
    candidates = message_repository.list_candidate_messages_for_domain(
        mailbox_id=mailbox.mailbox_id, sender_domain=sender_domain, include_subdomains=include_subdomains
    )

    results: list[MailboxMessage] = []
    for candidate in candidates:
        effective_rule = mailbox_domain_rule_repository.find_for_sender(
            mailbox_id=candidate.mailbox_id,
            sender_domain=candidate.sender_domain,
            sender_address=candidate.sender_address,
        )
        if effective_rule is None or effective_rule.rule_id != rule.rule_id:
            # A different, more-specific rule actually governs this
            # candidate (or, defensively, none does) — skip entirely,
            # no side effects, and never appear in `results`.
            continue
        results.append(
            _reprocess_one_message(
                mailbox=mailbox,
                mailbox_source_id=mailbox_source_id,
                message_id=candidate.mailbox_message_id,
                rule=effective_rule,
                adapter=adapter,
                message_repository=message_repository,
                needs_you_repository=needs_you_repository,
                api=api,
                object_store=object_store,
                scanner=scanner,
                actor_type=actor_type,
                actor_id=actor_id,
                correlation_id=correlation_id,
                now=now,
                sleep_fn=sleep_fn,
            )
        )
    return results


def process_security_reviewed_message_once(
    *,
    mailbox: MailboxSource,
    mailbox_source_id: str,
    message_id: str,
    rule: MailboxDomainRule,
    adapter: _AdapterProtocol,
    message_repository: MailboxMessageRepository,
    needs_you_repository: NeedsYouRepository,
    api: _EvidenceAPIProtocol,
    object_store: _ObjectStoreProtocol,
    scanner: EvidenceSafetyScanner,
    actor_type: str,
    actor_id: str,
    correlation_id: Optional[str] = None,
) -> MailboxMessage:
    """CD-6 GUI-operations-foundation follow-on WO (item D) — the real
    resolution workflow for a ``SECURITY_REVIEW``-held message: an
    operator's explicit ``PROCESS_THIS_MESSAGE_ONCE`` decision (see
    ``app/api/routers/mailboxes_microsoft.py``'s own dedicated
    ``POST .../security-review/{item_id}/resolve`` endpoint).

    Deliberately does NOT run :func:`evaluate_message_authentication`
    again — the whole point of this one-message override is that an
    operator has already looked at WHY this message failed/was
    inconclusive and decided, for THIS one message only, to proceed
    anyway; re-running the identical check would just fail it again for
    the identical reason. The governing ``MailboxDomainRule`` (``rule``)
    is never modified by this call — it stays exactly ``MUST_READ`` (a
    one-message override, never a relevance/policy re-decision).

    Otherwise mirrors :func:`_reprocess_one_message`'s own MIME-fetch +
    ``ingest_email_evidence`` + FIXED/REVIEW_REQUIRED destination
    handling exactly (including the identical NOT_FOUND -> VANISHED and
    other-non-OK -> raised ``ConflictError`` outcomes).

    Idempotent-safe: a message no longer ``SECURITY_REVIEW`` (already
    processed by an earlier call — a genuine double-submit of the same
    ``PROCESS_THIS_MESSAGE_ONCE`` decision) is returned UNCHANGED — never
    re-fetched, never re-ingested, never a second evidence item.
    """
    current = message_repository.get_message(message_id)
    if current.ingestion_status != INGESTION_STATUS_SECURITY_REVIEW:
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
            f"could not fetch content for message '{message_id}' during security-review "
            f"PROCESS_THIS_MESSAGE_ONCE: {content_result.status.value} — {content_result.error_detail or ''}"
        )

    entity_id_for_evidence = rule.destination_entity_id if rule.destination_mode == DESTINATION_MODE_FIXED else None
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
        entity_id=entity_id_for_evidence,
        external_reference_namespace=mailbox.provider_kind,
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
                    "processed_after_security_review_override": True,
                },
            )
            if rule.destination_mode == DESTINATION_MODE_REVIEW_REQUIRED:
                _raise_document_destination_review_item(
                    needs_you_repository, mailbox=mailbox, message=message, evidence=outcome.evidence
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
            payload={"mailbox_id": mailbox.mailbox_id, "processed_after_security_review_override": True},
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


def decline_security_reviewed_message(
    *, message_repository: MailboxMessageRepository, message_id: str
) -> MailboxMessage:
    """CD-6 GUI-operations-foundation follow-on WO (item D) — an
    operator's explicit ``DO_NOT_PROCESS_THIS_MESSAGE`` decision: records
    the decision (as an honest metadata marker), performs NO MIME fetch/
    evidence ingestion, and leaves ``ingestion_status`` at the real,
    honest terminal value ``SECURITY_REVIEW`` — never silently reused as
    ``CHECKED_NOT_CANDIDATE`` (that would be dishonest here: this message
    WAS a real candidate that WAS security-reviewed, not one Stage-A
    heuristic quietly decided was irrelevant). Never touches the
    governing ``MailboxDomainRule`` — this is a per-message security
    decision, never a relevance decision; the source/domain is never
    blacklisted by this call. Idempotent by construction — a repeated
    call simply re-records the same metadata marker."""
    current = message_repository.get_message(message_id)
    message, _ = message_repository.record_observation(
        mailbox_id=current.mailbox_id,
        provider_kind=current.provider_kind,
        immutable_provider_message_id=current.immutable_provider_message_id,
        internet_message_id=current.internet_message_id,
        observed_folder=current.observed_folder,
        observed_folder_display_name=current.observed_folder_display_name,
        subject=current.subject,
        sender_address=current.sender_address,
        sender_display_name=current.sender_display_name,
        received_at=current.received_at,
        has_attachments=current.has_attachments,
        ingestion_status=INGESTION_STATUS_SECURITY_REVIEW,
        sender_domain=current.sender_domain,
        attachment_metadata=current.attachment_metadata,
        auth_signals=current.auth_signals,
        metadata={"security_review_decision": "DO_NOT_PROCESS_THIS_MESSAGE"},
    )
    return message
