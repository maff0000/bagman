"""``MailboxDomainRule`` — the durable, MAILBOX-SPECIFIC domain-policy
registry that drives Stage B of the two-stage mail-processing gate (CD-6
architect amendment, superseding Slice 4A's "ingest everything
unconditionally" sweep behaviour), extended by the CD-6
GUI-operations-foundation follow-on WO ("operator-learning" three-state
policy model — see below) to make a confirmed relevance decision
DURABLE: "Once Matt has explicitly confirmed a sender/source as
financially relevant, BAGMAN must remember that decision durably... Do
not raise another domain/source review merely because another message
arrives from the same confirmed source" (architect, verbatim).

Why mailbox-specific, never a global domain->entity table
------------------------------------------------------------
The same sender domain can legitimately mean something different in two
different mailboxes (architect spec §8) — this module never builds a
``sender_domain -> GovernedEntity`` table; every lookup/uniqueness rule
is scoped to ``(mailbox_id, sender_domain)`` (or, for an
``EXACT_ADDRESS`` rule, ``(mailbox_id, sender_address)`` — see "Rule
specificity" below). Only one real mailbox exists at the time of this
delivery (``matt@infosecurs.com``), so this matters architecturally, not
operationally, yet — but the schema must never assume otherwise
(architect spec, verbatim).

Three-state operator-learning policy model — the internal string values
chosen, and why (documented judgment call)
------------------------------------------------------------------------
The architect's own operator-facing vocabulary is ``MUST_READ`` /
``GRAYLIST`` / ``BLACKLIST``. Production carries ZERO
``MailboxDomainRule`` rows at the time of this delivery (nothing has
ever been approved), so there is no data-migration hazard either way —
this module deliberately RENAMES the wire/internal policy strings
themselves (``"ALLOWED"`` -> ``"MUST_READ"``, ``"IGNORED"`` ->
``"BLACKLIST"``, plus the genuinely new ``"GRAYLIST"``) rather than
keeping the old ``"ALLOWED"``/``"IGNORED"`` wire values and only
relabelling the GUI. This is the smaller, clearer diff: every caller in
this codebase already imports the named Python constants
(``POLICY_MUST_READ``/``POLICY_GRAYLIST``/``POLICY_BLACKLIST`` below),
never the raw string literal, so a real rename costs nothing at any call
site and leaves no confusing "the constant named ALLOWED now means
MUST_READ" indirection for a future reader. Doing the rename now, before
any row anywhere (production or otherwise) ever encodes the old name, is
strictly cheaper than doing it later.

* ``MUST_READ`` (was ``ALLOWED``) — Stage B fully trusts this source:
  full MIME fetch + evidence intake proceeds for every ordinary message
  (subject to the NEW per-message authentication check — see
  ``services/mailbox/sweep.py``'s own module docstring, "Authentication
  escalation" section), and — the whole point of this WO — a later
  message from the SAME confirmed source never re-raises a
  ``MAILBOX_DOMAIN_REVIEW`` item merely because it arrived.
* ``GRAYLIST`` (new) — a REAL, persistable, explicit "Matt looked at
  this and deliberately left it under review" state — see
  ``services/mailbox/sweep.py``'s own module docstring for why this
  behaves identically to "no rule at all" at the Stage-B gate (both
  keep running the bounded discovery heuristic and reusing one
  aggregated Needs You item), the ONLY difference being that a real row
  now exists so the GUI can show "GRAYLIST" instead of "not yet
  reviewed" for a domain Matt has actually looked at once already.
* ``BLACKLIST`` (was ``IGNORED``) — discovery-only, never a Needs You
  item, never a MIME fetch — identical behaviour to the old ``IGNORED``,
  renamed for operator-facing clarity/symmetry with ``MUST_READ``/
  ``GRAYLIST``.

Reversibility — every state can move to either other state
------------------------------------------------------------------------
The architect's own explicit "reversible" requirement for ``BLACKLIST``
generalises cleanly: there is no state a confirmed decision cannot later
be changed away from. See :data:`ALLOWED_POLICY_TRANSITIONS` below —
now a fully-connected graph across all three states (was a single
``ALLOWED<->IGNORED`` edge).

Uniqueness — one DOMAIN-LEVEL rule per (mailbox_id, sender_domain),
never a second row for a changed mind
------------------------------------------------------------------------
:meth:`MailboxDomainRuleRepository.upsert_rule` is a resolve-or-create-
or-update operation, mirroring
``services.mailbox.message.MailboxMessageRepository.record_observation``'s
own "never a blind insert" discipline: an operator changing their
decision about a domain (e.g. ``BLACKLIST`` -> ``MUST_READ``) updates
the SAME row. The real, authoritative enforcement is a database-level
PARTIAL unique index scoped to non-``EXACT_ADDRESS`` rows (see
``persistence/postgres/mailbox_domain_rule_models.py``); the in-memory
repository below mirrors it with a plain dict.

Rule specificity — a genuinely new ``EXACT_ADDRESS`` match mode, plus
most-specific-rule-wins resolution
------------------------------------------------------------------------
Alongside the existing ``EXACT`` (one domain only) and
``INCLUDE_SUBDOMAINS`` (a domain and every subdomain of it) match modes,
this WO adds :data:`MATCH_MODE_EXACT_ADDRESS` — a rule scoped to one
SPECIFIC sender email address rather than a whole domain (e.g. Matt
wants to always-trust ``ap@vendor.com`` specifically, while the rest of
``vendor.com`` stays ``GRAYLIST``). An ``EXACT_ADDRESS`` rule's identity
key is ``sender_address`` (normalised via :func:`normalize_address`),
never ``sender_domain`` alone — ``sender_domain`` is still always
populated on such a rule (derived from the address) purely for
domain-level reporting/grouping (bootstrap-floor computation, the
domain-review GUI's own per-domain worklist, ...), never for identity/
uniqueness.

:meth:`MailboxDomainRuleRepository.find_for_sender` resolves the SINGLE
governing rule for one observed message in this documented, most-
specific-first order:

1. An ``EXACT_ADDRESS`` rule matching the message's own exact sender
   email address (normalised).
2. A domain-level rule (``EXACT`` or ``INCLUDE_SUBDOMAINS`` — only one
   can exist per domain at a time, by construction) matching the
   message's exact sender domain.
3. An ``INCLUDE_SUBDOMAINS`` rule whose own domain is a PARENT of the
   message's sender domain.

No fuzzy matching anywhere — every tier above is an exact string
comparison after normalisation.

Policy lifecycle — a real, tested transition (architect spec §6)
------------------------------------------------------------------------
``policy`` is a small, closed, real state machine (see
:data:`ALLOWED_POLICY_TRANSITIONS`) — an operator must later be able to
flip a ``BLACKLIST`` domain back to ``MUST_READ``/``GRAYLIST`` (or any
other direction) as a plain lifecycle transition, never by
deleting/recreating the row.

Never a global domain->entity inference (architect spec §3)
------------------------------------------------------------------------
A ``MUST_READ`` rule relates to one of BAGMAN's canonical destinations
using a REAL ``destination_entity_id`` (never a string/name), and is
always either a confident, operator-approved routing decision
(``destination_mode="FIXED"``) or an honest "this domain is real
accounting evidence but its destination is not yet safely known"
placeholder (``destination_mode="REVIEW_REQUIRED"``, ``destination_entity_id=None``)
— this module never silently guesses a destination from the domain
alone.

``upsert_rule`` is a plain REPLACE, not routed through
:func:`transition_policy`'s guarded state machine
------------------------------------------------------------------------
Changing a rule's policy DIRECTLY via ``upsert_rule`` (e.g. an operator-
driven resolve/batch-resolve call) does NOT go through
:func:`transition_policy`'s own closed-graph legality check — this
remains a deliberate, unchanged design choice from before this WO (see
:meth:`MailboxDomainRuleRepository.upsert_rule`'s own docstring for the
full "same-policy upsert is a harmless no-op" reasoning this preserves):
a legitimate re-decision (of ANY shape, including a policy that stays
the same) must never be rejected by a guard designed for a different,
narrower caller.

Reprocessing doctrine on a policy change — a documented judgment call
------------------------------------------------------------------------
Changing a rule's policy DIRECTLY (e.g. via a bare ``upsert_rule`` call
outside the Needs You approval flow) does NOT, by itself, retroactively
reprocess every historically-``CHECKED_NOT_CANDIDATE``/blacklisted
``MailboxMessage`` row under the new policy — see
``services/mailbox/sweep.py``'s own module docstring for the full
reasoning (a documented, PL/architect-flagged judgment call). The one
real, explicit exception: resolving an OPEN ``MAILBOX_DOMAIN_REVIEW``
Needs You item with an ``ALLOW``/``MUST_READ`` decision back-processes
EVERY historical candidate for that domain, not merely the one message
that triggered the item (operational addendum, ahead of the first real
large historical sweep — architect spec §4's own explicit requirement,
corrected/broadened from its original "one message only" scope) — see
``services.mailbox.sweep.reprocess_all_historical_candidates_for_domain``.
"""
from __future__ import annotations

import abc
import dataclasses
import re
import unicodedata
from dataclasses import dataclass
from datetime import datetime
from typing import Optional

from core import identity
from core.contract_validation import validate_against_contract
from core.errors import ConflictError, InvalidStateTransitionError, NotFoundError, ValidationError
from core.timestamps import to_contract_string, utc_now

_SCHEMA = "mailbox/bagman.mailbox_domain_rule.v1.schema.json"
SCHEMA_VERSION = "bagman.mailbox_domain_rule.v1"

MATCH_MODE_EXACT = "EXACT"
MATCH_MODE_INCLUDE_SUBDOMAINS = "INCLUDE_SUBDOMAINS"
#: CD-6 GUI-operations-foundation follow-on WO — a rule scoped to one
#: SPECIFIC sender email address, never a whole domain. See module
#: docstring's "Rule specificity" section.
MATCH_MODE_EXACT_ADDRESS = "EXACT_ADDRESS"
#: Deterministic subject-aware mailbox domain policy (CD-6 GUI-
#: operations-foundation follow-on WO) — applies only to messages whose
#: sender domain EXACTLY equals `sender_domain` (never subdomains — a
#: subject predicate is deliberately never combined with
#: INCLUDE_SUBDOMAINS' own broader domain-scope semantics) AND whose
#: normalised subject satisfies the rule's own stored deterministic
#: predicate (`subject_predicate_type`/`subject_predicate_value` — see
#: `SUBJECT_PREDICATES`/`normalize_subject_for_policy` below). Its own
#: separate identity space (never the domain-level identity space
#: `EXACT`/`INCLUDE_SUBDOMAINS` share, and never `EXACT_ADDRESS`'s own
#: address-level identity space) is `(mailbox_id, sender_domain,
#: subject_predicate_type, subject_predicate_value)` — see
#: `persistence/postgres/mailbox_domain_rule_models.py`'s own module
#: docstring for the partial unique index this maps to.
MATCH_MODE_EXACT_DOMAIN_SUBJECT = "EXACT_DOMAIN_SUBJECT"
MATCH_MODES = frozenset(
    {MATCH_MODE_EXACT, MATCH_MODE_INCLUDE_SUBDOMAINS, MATCH_MODE_EXACT_ADDRESS, MATCH_MODE_EXACT_DOMAIN_SUBJECT}
)

#: Deliberately exactly two predicate types — no CONTAINS, no regex, no
#: glob, no NLP (CD-6 GUI-operations-foundation follow-on WO's own
#: explicit "deterministic" requirement: a subject rule must always be
#: mechanically explainable to Matt, never a fuzzy/probabilistic match).
SUBJECT_PREDICATE_EXACT = "EXACT"
SUBJECT_PREDICATE_STARTS_WITH = "STARTS_WITH"
SUBJECT_PREDICATES = frozenset({SUBJECT_PREDICATE_EXACT, SUBJECT_PREDICATE_STARTS_WITH})

#: Closed policy vocabulary (architect spec §2, CD-6 GUI-operations-
#: foundation follow-on WO's own three-state operator-learning model) —
#: matches the contract's own closed `policy` enum. See module
#: docstring's own "Three-state operator-learning policy model" section
#: for exactly what each value means and why these particular wire
#: strings were chosen (a real rename from the original two-state
#: `ALLOWED`/`IGNORED` vocabulary — production carries zero rows, so
#: there is no migration hazard).
POLICY_MUST_READ = "MUST_READ"
POLICY_GRAYLIST = "GRAYLIST"
POLICY_BLACKLIST = "BLACKLIST"
POLICIES = frozenset({POLICY_MUST_READ, POLICY_GRAYLIST, POLICY_BLACKLIST})

#: The single source of truth for valid MailboxDomainRule.policy
#: transitions (architect spec §6, generalised by the CD-6
#: GUI-operations-foundation follow-on WO's own explicit "reversible"
#: requirement: there is no state a confirmed decision cannot later be
#: changed away from — a fully-connected graph across all three states).
ALLOWED_POLICY_TRANSITIONS: dict[str, frozenset[str]] = {
    POLICY_MUST_READ: frozenset({POLICY_GRAYLIST, POLICY_BLACKLIST}),
    POLICY_GRAYLIST: frozenset({POLICY_MUST_READ, POLICY_BLACKLIST}),
    POLICY_BLACKLIST: frozenset({POLICY_MUST_READ, POLICY_GRAYLIST}),
}

DESTINATION_MODE_FIXED = "FIXED"
DESTINATION_MODE_REVIEW_REQUIRED = "REVIEW_REQUIRED"
DESTINATION_MODES = frozenset({DESTINATION_MODE_FIXED, DESTINATION_MODE_REVIEW_REQUIRED})

#: Documented (non-exhaustive), OPEN `source` vocabulary — see module
#: docstring/contract description for why this is never a closed set.
SOURCE_OPERATOR = "OPERATOR"
SOURCE_BAGMAN_PROPOSED = "BAGMAN_PROPOSED"


@dataclass(frozen=True)
class MailboxDomainRule:
    """One mailbox-specific domain-policy routing rule. Immutable once
    constructed — every change produces a NEW snapshot via
    :func:`transition_policy` or a repository upsert, never an in-place
    mutation."""

    rule_id: str
    mailbox_id: str
    sender_domain: str
    match_mode: str
    policy: str
    destination_entity_id: Optional[str]
    destination_mode: Optional[str]
    source: str
    processor_hint: Optional[str]
    approved_at: Optional[datetime]
    created_at: datetime
    updated_at: datetime
    last_seen_at: datetime
    #: CD-6 GUI-operations-foundation follow-on WO — populated ONLY for
    #: a `MATCH_MODE_EXACT_ADDRESS` rule (the real identity/uniqueness
    #: key for that match mode — see module docstring's "Rule
    #: specificity" section). `None` for a domain-level (`EXACT`/
    #: `INCLUDE_SUBDOMAINS`) rule, always normalised lowercase.
    sender_address: Optional[str] = None
    #: CD-6 GUI-operations-foundation follow-on WO — populated ONLY for
    #: a `MATCH_MODE_EXACT_DOMAIN_SUBJECT` rule (`SUBJECT_PREDICATES`
    #: member). `None` for every other match mode.
    subject_predicate_type: Optional[str] = None
    #: Always stored in canonical NORMALISED form (see
    #: `normalize_subject_for_policy`) — matching operates purely on
    #: normalised-vs-normalised comparison, never raw casing/spacing.
    #: Populated ONLY for a `MATCH_MODE_EXACT_DOMAIN_SUBJECT` rule.
    subject_predicate_value: Optional[str] = None
    schema_version: str = SCHEMA_VERSION

    def to_dict(self) -> dict:
        return {
            "rule_id": self.rule_id,
            "mailbox_id": self.mailbox_id,
            "sender_domain": self.sender_domain,
            "sender_address": self.sender_address,
            "match_mode": self.match_mode,
            "policy": self.policy,
            "destination_entity_id": self.destination_entity_id,
            "destination_mode": self.destination_mode,
            "source": self.source,
            "processor_hint": self.processor_hint,
            "approved_at": to_contract_string(self.approved_at) if self.approved_at is not None else None,
            "created_at": to_contract_string(self.created_at),
            "updated_at": to_contract_string(self.updated_at),
            "last_seen_at": to_contract_string(self.last_seen_at),
            "subject_predicate_type": self.subject_predicate_type,
            "subject_predicate_value": self.subject_predicate_value,
            "schema_version": self.schema_version,
        }


def normalize_domain(sender_domain: str) -> str:
    """The ONE place every caller normalises a sender domain before
    either a match/uniqueness comparison or storage — mirrors
    ``services.mailbox.mailbox.normalize_email``'s identical role."""
    return sender_domain.strip().lower()


def domain_in_scope(candidate_domain: str, target_domain: str, *, include_subdomains: bool) -> bool:
    """The ONE shared boundary check for "is this candidate's domain
    ``target_domain`` itself, or (only when ``include_subdomains`` is
    True) a genuine subdomain of it?" — both inputs are assumed ALREADY
    normalised (lowercase, stripped) by the caller via
    :func:`normalize_domain`, exactly like every other function in this
    module.

    This exists purely for
    ``services.mailbox.message.MailboxMessageRepository
    .list_candidate_messages_for_domain``'s own historical-candidate
    query (both the in-memory and Postgres implementations) — the
    historical-backfill query needs to widen to "this domain and its
    subdomains" for an ``INCLUDE_SUBDOMAINS`` rule the SAME way
    :meth:`MailboxDomainRuleRepository.find_for_sender`'s own Tier-3
    parent-domain check already does for live mail. This function
    reuses that EXACT SAME suffix-boundary semantics
    (``candidate_domain.endswith(f".{target_domain}")``) rather than
    re-deriving it independently — see ``find_for_sender``'s own Tier-3
    comment. It is deliberately a new, additional, narrow helper: it
    does NOT replace or touch either ``find_for_sender`` implementation,
    which keeps its own inline copy of this same check (duplicating the
    one-line boundary test is cheaper and safer here than introducing a
    shared-code dependency between the message-repository layer and the
    domain-rule-repository layer for a single ``.endswith`` call).

    Boundary rule: exact equality always matches, regardless of
    ``include_subdomains``. ``candidate_domain.endswith("." +
    target_domain)`` is checked ONLY when ``include_subdomains`` is
    True — this is a real dot-boundary suffix check, not a bare string
    suffix check, so e.g. ``"notexample.com"`` does NOT match
    ``"example.com"`` (no trailing-dot boundary), while
    ``"billing.example.com"`` and multi-level
    ``"receipts.eu.example.com"`` both correctly match."""
    if candidate_domain == target_domain:
        return True
    if not include_subdomains:
        return False
    return candidate_domain.endswith(f".{target_domain}")


def normalize_address(sender_address: str) -> str:
    """The ONE place every caller normalises a full sender email address
    before either a match/uniqueness comparison or storage (CD-6
    GUI-operations-foundation follow-on WO — `MATCH_MODE_EXACT_ADDRESS`).
    Mirrors :func:`normalize_domain`'s identical role one level up."""
    return sender_address.strip().lower()


def domain_from_address(sender_address: str) -> str:
    """The one place this module derives a domain from a full email
    address — mirrors ``services.mailbox.sweep._extract_sender_domain``'s
    own normalisation discipline, applied here to an already-known-valid
    address (an `EXACT_ADDRESS` rule's own `sender_address`)."""
    normalized = normalize_address(sender_address)
    if "@" not in normalized:
        raise ValidationError(f"'{sender_address}' is not a valid email address (missing '@')")
    domain = normalized.rsplit("@", 1)[-1]
    if not domain:
        raise ValidationError(f"'{sender_address}' is not a valid email address (empty domain)")
    return domain


#: Collapse consecutive internal whitespace to one ASCII space — applied
#: AFTER casefold/strip in `normalize_subject_for_policy` below.
_WHITESPACE_RUN = re.compile(r"\s+")


def normalize_subject_for_policy(subject: Optional[str]) -> Optional[str]:
    """The ONE place every caller normalises a message subject before
    either a `MATCH_MODE_EXACT_DOMAIN_SUBJECT` predicate comparison or
    storage (CD-6 GUI-operations-foundation follow-on WO — deterministic
    subject-aware mailbox domain policy). Applied in this EXACT order:

    1. Unicode NFKC normalisation (`unicodedata.normalize("NFKC", ...)`)
       — canonicalises visually-identical-but-differently-encoded
       characters (e.g. full-width vs. half-width forms) before any
       comparison.
    2. `.casefold()` — a real subject rule must never be defeated by
       case variation.
    3. `.strip()` — leading/trailing whitespace never carries meaning.
    4. Collapse consecutive INTERNAL whitespace to one ASCII space — a
       real, observed eBay defect (a literal double space inside a
       subject line) must not silently defeat an otherwise-correct
       predicate.

    Deliberately does NOT strip punctuation/digits, and does NOT strip
    `Re:`/`Fwd:` reply/forward prefixes — this is a NORMALISATION step,
    never a semantic rewrite; a predicate author who wants to match past
    a `Re:` prefix uses `STARTS_WITH` with the prefix included, or
    `EXACT` against the reply subject's own literal (normalised) text.

    `None` in -> `None` out (an `EXACT_DOMAIN_SUBJECT` rule never
    matches a message with no subject at all — see
    `MailboxDomainRuleRepository.find_for_sender`'s own Tier-2 gate)."""
    if subject is None:
        return None
    normalized = unicodedata.normalize("NFKC", subject).casefold().strip()
    return _WHITESPACE_RUN.sub(" ", normalized)


def subject_matches_predicate(subject: Optional[str], *, predicate_type: str, predicate_value: str) -> bool:
    """The ONE place a real message subject is tested against a stored
    `MATCH_MODE_EXACT_DOMAIN_SUBJECT` predicate — used by BOTH
    `MailboxDomainRuleRepository.find_for_sender`'s own Tier-2
    resolution (in-memory AND PostgreSQL implementations alike) and the
    creation-time "has this predicate ever/currently matched a real
    message" guards (`app/api/routers/mailboxes.py
    ::upsert_mailbox_policy_rule`'s `subject_predicate_observed` check,
    `services.mailbox.review_resolution.resolve_domain_review`'s own
    eligible-candidate check) — never reimplemented at any of those call
    sites.

    ``subject`` is the RAW (un-normalised) candidate subject —
    normalised HERE, internally, via `normalize_subject_for_policy`.
    ``predicate_value`` MUST already be in canonical normalised form
    (exactly what every rule stores — see
    `MailboxDomainRule.subject_predicate_value`'s own docstring); it is
    never re-normalised here, so a caller passing a raw, un-normalised
    predicate value would silently never match — every caller of this
    function is expected to already hold a normalised stored value."""
    normalized_subject = normalize_subject_for_policy(subject)
    if not normalized_subject:
        return False
    if predicate_type == SUBJECT_PREDICATE_EXACT:
        return normalized_subject == predicate_value
    if predicate_type == SUBJECT_PREDICATE_STARTS_WITH:
        return normalized_subject.startswith(predicate_value)
    return False


def validate_and_normalize_subject_predicate(
    *, match_mode: str, subject_predicate_type: Optional[str], subject_predicate_value: Optional[str]
) -> tuple[Optional[str], Optional[str]]:
    """The ONE shared STRUCTURAL validation+normalisation every caller
    that lets an operator create/update a `MATCH_MODE_EXACT_DOMAIN_SUBJECT`
    rule goes through (mirrors `validate_and_normalize_sender_address`'s
    identical role for `EXACT_ADDRESS`) — checked BEFORE any "has this
    predicate ever/currently matched a real message" observed-guard
    (that check is deliberately NOT here — it differs by caller, see
    `app/api/routers/mailboxes.py::upsert_mailbox_policy_rule` vs.
    `services.mailbox.review_resolution.resolve_domain_review`'s own
    docstrings for the two different observed-guards each applies):

    * ``match_mode != MATCH_MODE_EXACT_DOMAIN_SUBJECT`` -> both fields
      must be absent (`None`); returns ``(None, None)``.
    * ``match_mode == MATCH_MODE_EXACT_DOMAIN_SUBJECT`` -> both fields
      required: ``subject_predicate_type`` must be a real
      `SUBJECT_PREDICATES` member, and ``subject_predicate_value`` must
      normalise (via `normalize_subject_for_policy`) to a non-empty
      string. Returns ``(subject_predicate_type, normalized_value)``.
    """
    if match_mode != MATCH_MODE_EXACT_DOMAIN_SUBJECT:
        if subject_predicate_type is not None or subject_predicate_value is not None:
            raise ValidationError(
                f"subject_predicate_type/subject_predicate_value must not be supplied when match_mode is "
                f"'{match_mode}' — only '{MATCH_MODE_EXACT_DOMAIN_SUBJECT}' rules use a subject predicate"
            )
        return None, None

    if subject_predicate_type not in SUBJECT_PREDICATES:
        raise ValidationError(
            f"subject_predicate_type must be one of {sorted(SUBJECT_PREDICATES)} for "
            f"{MATCH_MODE_EXACT_DOMAIN_SUBJECT} (got {subject_predicate_type!r})"
        )
    normalized_value = normalize_subject_for_policy(subject_predicate_value)
    if not normalized_value:
        raise ValidationError(
            f"subject_predicate_value must normalize to a non-empty string for {MATCH_MODE_EXACT_DOMAIN_SUBJECT} "
            f"(got {subject_predicate_value!r})"
        )
    return subject_predicate_type, normalized_value


def validate_and_normalize_sender_address(
    *,
    message_repository,
    mailbox_id: str,
    sender_domain: str,
    match_mode: str,
    sender_address: Optional[str],
) -> Optional[str]:
    """The ONE shared implementation of the ``EXACT_ADDRESS`` validation
    contract every caller that lets an operator create/update a
    :class:`MailboxDomainRule` for one specific sender address must go
    through (CD-6 GUI-operations-foundation follow-on WO item A;
    relocated here — out of
    ``app/api/routers/mailboxes_microsoft.py``, where it was originally
    a private, router-local helper — by the CD-6 provider-neutral
    policy-rules-endpoint WO, so ``app/api/routers/mailboxes.py``'s
    ``POST /{mailbox_id}/policy-rules`` endpoint can reuse the EXACT
    SAME check rather than forking a parallel implementation;
    ``mailboxes_microsoft.py``'s own domain-review resolve/batch-resolve
    flow now imports this same function too — no behaviour change
    there, just the relocation):

    * ``match_mode != MATCH_MODE_EXACT_ADDRESS`` -> ``sender_address``
      must be absent/empty (a domain-level mode must never silently
      accept a stray address).
    * ``match_mode == MATCH_MODE_EXACT_ADDRESS`` -> ``sender_address``
      is REQUIRED, non-empty, normalised, its own domain must equal
      ``sender_domain`` (normalised), AND it must have been ACTUALLY
      OBSERVED for this mailbox (``message_repository
      .sender_address_observed(mailbox_id, normalized_address)``) — an
      operator must never be able to pre-authorize an address BAGMAN has
      never actually seen mail from.

    ``message_repository`` is duck-typed deliberately (never a
    ``services.mailbox.message.MailboxMessageRepository`` type import
    here) — that module already imports FROM this one
    (:func:`normalize_domain`), so importing its type back here would be
    circular.

    Returns the normalised address (``None`` for a domain-level rule).
    """
    if match_mode != MATCH_MODE_EXACT_ADDRESS:
        if sender_address:
            raise ValidationError(
                f"sender_address must not be supplied when match_mode is '{match_mode}' — only "
                f"'{MATCH_MODE_EXACT_ADDRESS}' rules are scoped to one specific address"
            )
        return None

    if not sender_address:
        raise ValidationError(f"sender_address is required when match_mode is '{MATCH_MODE_EXACT_ADDRESS}'")

    normalized_address = normalize_address(sender_address)
    address_domain = domain_from_address(normalized_address)
    if address_domain != normalize_domain(sender_domain):
        raise ValidationError(
            f"sender_address '{sender_address}' does not belong to sender_domain "
            f"'{sender_domain}' ('{address_domain}' != '{normalize_domain(sender_domain)}')"
        )
    if not message_repository.sender_address_observed(mailbox_id, normalized_address):
        raise ValidationError(
            f"sender_address '{normalized_address}' has never actually been observed for mailbox "
            f"'{mailbox_id}' — refusing to pre-authorize an address BAGMAN has never seen mail from"
        )
    return normalized_address


def validate_policy_fields_or_raise(
    *,
    policy: str,
    destination_entity_id: Optional[str],
    destination_mode: Optional[str],
    match_mode: Optional[str] = None,
) -> None:
    """Shared validation both repository implementations call before
    persisting a rule — see contract's own field descriptions for the
    exact rules enforced here.

    ``match_mode`` (CD-6 GUI-operations-foundation follow-on WO,
    optional/backward-compatible — every pre-existing caller that never
    passed it keeps its exact prior behaviour) additionally enforces
    that `MATCH_MODE_EXACT_DOMAIN_SUBJECT + POLICY_GRAYLIST` is
    explicitly REJECTED: subject-scoped GRAYLIST is not supported in
    this delivery (a subject predicate expresses a specific, deterministic
    "Matt already knows what this is" decision — MUST_READ or BLACKLIST
    — never an open-ended "keep checking with me" state, which stays a
    purely domain-level concept). Plain domain-level GRAYLIST/KEEP_GRAY
    (`EXACT`/`INCLUDE_SUBDOMAINS`) is completely unaffected."""
    if policy not in POLICIES:
        raise ValidationError(f"'{policy}' is not a governed MailboxDomainRule policy — must be one of {sorted(POLICIES)}")
    if match_mode == MATCH_MODE_EXACT_DOMAIN_SUBJECT and policy == POLICY_GRAYLIST:
        raise ValidationError(
            f"{MATCH_MODE_EXACT_DOMAIN_SUBJECT} rules do not support {POLICY_GRAYLIST} in this delivery — "
            f"use {POLICY_MUST_READ} or {POLICY_BLACKLIST} instead (subject-scoped graylisting is not supported)"
        )
    if policy in (POLICY_GRAYLIST, POLICY_BLACKLIST):
        if destination_entity_id is not None or destination_mode is not None:
            raise ValidationError(
                f"a {policy} MailboxDomainRule must never carry a destination_entity_id/destination_mode"
            )
        return
    # policy == POLICY_MUST_READ
    if destination_mode not in DESTINATION_MODES:
        raise ValidationError(
            f"a MUST_READ MailboxDomainRule requires destination_mode to be one of {sorted(DESTINATION_MODES)} "
            f"(got {destination_mode!r})"
        )
    if destination_mode == DESTINATION_MODE_FIXED and not destination_entity_id:
        raise ValidationError(
            "a MUST_READ MailboxDomainRule with destination_mode='FIXED' requires a real destination_entity_id "
            "— domain alone must never silently determine a destination (architect spec §3)"
        )


def validate_match_fields_or_raise(
    *,
    match_mode: str,
    sender_address: Optional[str],
    subject_predicate_type: Optional[str] = None,
    subject_predicate_value: Optional[str] = None,
) -> None:
    """CD-6 GUI-operations-foundation follow-on WO — the
    `MATCH_MODE_EXACT_ADDRESS` counterpart to
    :func:`validate_policy_fields_or_raise`: an `EXACT_ADDRESS` rule
    REQUIRES a real `sender_address`; a domain-level rule
    (`EXACT`/`INCLUDE_SUBDOMAINS`) must never carry one (mirrors
    `destination_entity_id`'s own "never present when it does not apply"
    discipline one field up).

    Extended (deterministic subject-aware mailbox domain policy) with
    the analogous, symmetric check for
    `subject_predicate_type`/`subject_predicate_value`: an
    `EXACT_DOMAIN_SUBJECT` rule REQUIRES `sender_domain` (always
    supplied by every real caller — enforced structurally, not here),
    must NEVER carry a `sender_address`, REQUIRES a real
    `subject_predicate_type` (`SUBJECT_PREDICATES` member) and a
    `subject_predicate_value` that normalises to a non-empty string
    (via `normalize_subject_for_policy`); every OTHER match mode must
    never carry either subject field — mirrors `sender_address`'s own
    "never present when it does not apply" discipline exactly."""
    if match_mode not in MATCH_MODES:
        raise ValidationError(f"'{match_mode}' is not a governed match_mode — must be one of {sorted(MATCH_MODES)}")

    if match_mode == MATCH_MODE_EXACT_ADDRESS:
        if not sender_address:
            raise ValidationError(
                "a MATCH_MODE_EXACT_ADDRESS MailboxDomainRule requires a real sender_address"
            )
        if subject_predicate_type is not None or subject_predicate_value is not None:
            raise ValidationError(
                "a MATCH_MODE_EXACT_ADDRESS MailboxDomainRule must never carry subject_predicate_type/"
                "subject_predicate_value (those fields only apply to MATCH_MODE_EXACT_DOMAIN_SUBJECT rules)"
            )
        return

    if match_mode == MATCH_MODE_EXACT_DOMAIN_SUBJECT:
        if sender_address is not None:
            raise ValidationError(
                "a MATCH_MODE_EXACT_DOMAIN_SUBJECT MailboxDomainRule must never carry a sender_address (that "
                "field only applies to MATCH_MODE_EXACT_ADDRESS rules)"
            )
        if subject_predicate_type not in SUBJECT_PREDICATES:
            raise ValidationError(
                f"subject_predicate_type must be one of {sorted(SUBJECT_PREDICATES)} for "
                f"{MATCH_MODE_EXACT_DOMAIN_SUBJECT} (got {subject_predicate_type!r})"
            )
        if not normalize_subject_for_policy(subject_predicate_value):
            raise ValidationError(
                f"subject_predicate_value must normalize to a non-empty string for "
                f"{MATCH_MODE_EXACT_DOMAIN_SUBJECT} (got {subject_predicate_value!r})"
            )
        return

    # EXACT / INCLUDE_SUBDOMAINS — neither sender_address nor a subject
    # predicate may ever be present.
    if sender_address is not None:
        raise ValidationError(
            f"a {match_mode} MailboxDomainRule must never carry a sender_address (that field only applies to "
            "MATCH_MODE_EXACT_ADDRESS rules)"
        )
    if subject_predicate_type is not None or subject_predicate_value is not None:
        raise ValidationError(
            f"a {match_mode} MailboxDomainRule must never carry subject_predicate_type/subject_predicate_value "
            "(those fields only apply to MATCH_MODE_EXACT_DOMAIN_SUBJECT rules)"
        )


def transition_policy(rule: MailboxDomainRule, new_policy: str, **field_updates) -> MailboxDomainRule:
    """Move ``rule.policy`` to ``new_policy``, enforcing
    :data:`ALLOWED_POLICY_TRANSITIONS`. Stamps ``updated_at``
    automatically."""
    allowed = ALLOWED_POLICY_TRANSITIONS.get(rule.policy, frozenset())
    if new_policy not in allowed:
        raise InvalidStateTransitionError(
            f"MailboxDomainRule '{rule.rule_id}' cannot transition policy from "
            f"'{rule.policy}' to '{new_policy}'; allowed transitions from "
            f"'{rule.policy}' are {sorted(allowed) or '(none)'}"
        )
    try:
        updated = dataclasses.replace(rule, policy=new_policy, updated_at=utc_now(), **field_updates)
        validate_policy_fields_or_raise(
            policy=updated.policy,
            destination_entity_id=updated.destination_entity_id,
            destination_mode=updated.destination_mode,
            match_mode=updated.match_mode,
        )
        validate_against_contract(updated.to_dict(), _SCHEMA)
    except ValidationError:
        raise
    except Exception as exc:  # noqa: BLE001 - never leak a raw exception
        raise ValidationError(f"could not transition MailboxDomainRule policy: {exc}") from exc
    return updated


class MailboxDomainRuleRepository(abc.ABC):
    """Repository abstraction for MailboxDomainRule."""

    @abc.abstractmethod
    def upsert_rule(
        self,
        *,
        mailbox_id: str,
        sender_domain: str,
        match_mode: str,
        policy: str,
        destination_entity_id: Optional[str],
        destination_mode: Optional[str],
        source: str,
        processor_hint: Optional[str] = None,
        approved_at: Optional[datetime] = None,
        sender_address: Optional[str] = None,
        subject_predicate_type: Optional[str] = None,
        subject_predicate_value: Optional[str] = None,
    ) -> MailboxDomainRule:
        """Resolve-or-create-or-update.

        For a domain-level rule (``match_mode`` is ``EXACT`` or
        ``INCLUDE_SUBDOMAINS``): keyed by ``(mailbox_id, sender_domain)``
        (``sender_domain`` normalised via :func:`normalize_domain`
        first) — see module docstring's "Uniqueness" section. A
        genuinely new domain creates a fresh row; an existing domain's
        rule is fully replaced (mirrors
        ``services.mailbox.mailbox.MailboxSourceRepository
        .update_mailbox``'s own "full replace, never a partial patch"
        documented choice), INCLUDING a policy change (e.g. operator-
        driven ``BLACKLIST`` -> ``MUST_READ``) — deliberately a plain
        replace here, NOT routed through :func:`transition_policy`'s
        own closed state machine (see module docstring's own dedicated
        section on this). ``sender_address`` must be omitted/``None``.

        For an ``EXACT_ADDRESS`` rule: keyed by ``(mailbox_id,
        sender_address)`` (normalised via :func:`normalize_address`) —
        an entirely separate identity space from the domain-level keying
        above (see module docstring's "Rule specificity" section).
        ``sender_domain`` is still required/stored (derived-and-
        cross-checked against ``sender_address``'s own domain when both
        are supplied), purely for reporting/grouping — never part of
        this match mode's own identity key.

        For an ``EXACT_DOMAIN_SUBJECT`` rule: keyed by ``(mailbox_id,
        sender_domain, subject_predicate_type, subject_predicate_value)``
        — a THIRD, separate identity space from both the domain-level
        keying above and the address-level keying below (see module
        docstring's "Rule specificity" section, and
        `persistence/postgres/mailbox_domain_rule_models.py`'s own
        module docstring for the partial unique index this maps to). A
        genuinely new predicate identity creates a fresh row; an
        existing exact predicate identity's rule is fully replaced —
        mirrors the domain-level/address-level upsert-or-replace
        behaviour exactly.

        A same-policy (or same-everything) upsert is a harmless no-op,
        rather than an error — mirrors this delivery's own established
        "a redundant same-state action must never fail" doctrine.
        """
        raise NotImplementedError

    @abc.abstractmethod
    def get_rule(self, rule_id: str) -> MailboxDomainRule:
        raise NotImplementedError

    @abc.abstractmethod
    def find_for_sender(
        self,
        *,
        mailbox_id: str,
        sender_domain: str,
        sender_address: Optional[str] = None,
        subject: Optional[str] = None,
    ) -> Optional[MailboxDomainRule]:
        """Resolve the SINGLE governing rule (if any) for a message from
        ``sender_domain``/``sender_address``/``subject`` observed in
        ``mailbox_id``, in most-specific-first order (see module
        docstring's "Rule specificity" section for the full four-tier
        resolution this implements — extended by the CD-6 GUI-operations-
        foundation follow-on WO's deterministic subject-aware policy):

        1. An ``EXACT_ADDRESS`` rule matching ``sender_address`` exactly
           (only attempted when ``sender_address`` is supplied) —
           subject-independent, always wins if present.
        2. An ``EXACT_DOMAIN_SUBJECT`` rule at the message's own EXACT
           sender domain (never via subdomain matching) whose stored
           predicate the message's own normalised ``subject`` satisfies
           (only attempted when ``subject`` is supplied and normalises
           non-empty — see `normalize_subject_for_policy`). When
           multiple rules match: an ``EXACT`` predicate always beats
           every ``STARTS_WITH`` predicate; among matching
           ``STARTS_WITH`` rules, the LONGEST ``subject_predicate_value``
           wins (explicit specificity ordering). Raises
           ``core.errors.ConflictError`` — never silently picks one,
           never falls through to a broader tier, never returns ``None``
           — if two-or-more matches genuinely cannot be ordered this way
           (only reachable via data corruption bypassing the normal,
           validated write path — the real write path's own uniqueness
           constraints make this structurally impossible in ordinary
           operation).
        3. A direct domain-level rule (``EXACT`` or
           ``INCLUDE_SUBDOMAINS`` — only one can exist per domain, by
           construction) matching the message's exact sender domain (an
           ``INCLUDE_SUBDOMAINS`` rule AT the message's own exact domain
           is still "direct" here, not "parent").
        4. An ``INCLUDE_SUBDOMAINS`` rule whose own domain is a PARENT of
           the message's sender domain.

        Returns ``None`` — never ``NotFoundError`` — when no rule
        governs this sender yet (the Stage-B 'unknown domain' path)."""
        raise NotImplementedError

    @abc.abstractmethod
    def find_exact(
        self,
        *,
        mailbox_id: str,
        sender_domain: str,
        match_mode: str,
        sender_address: Optional[str] = None,
        subject_predicate_type: Optional[str] = None,
        subject_predicate_value: Optional[str] = None,
    ) -> Optional[MailboxDomainRule]:
        """Look up the rule at this EXACT identity — never the
        most-specific-wins resolution :meth:`find_for_sender` performs
        (CD-6 policy-rules-endpoint WO). For ``match_mode`` ``EXACT``/
        ``INCLUDE_SUBDOMAINS`` (the domain-level identity space),
        resolves by ``(mailbox_id, sender_domain)`` among rows that are
        neither ``EXACT_ADDRESS`` nor ``EXACT_DOMAIN_SUBJECT``. For
        ``match_mode == MATCH_MODE_EXACT_ADDRESS`` (a SEPARATE identity
        space), resolves by ``(mailbox_id, sender_address)`` instead —
        ``sender_address`` is then REQUIRED (raises ``ValidationError``
        if omitted). For ``match_mode == MATCH_MODE_EXACT_DOMAIN_SUBJECT``
        (a THIRD, separate identity space), resolves ONLY that exact
        predicate identity — ``subject_predicate_type``/
        ``subject_predicate_value`` are then BOTH REQUIRED (raises
        ``ValidationError`` if either is omitted); never the domain
        fallback, never a DIFFERENT subject predicate, never an address
        rule.

        Returns ``None`` — never ``NotFoundError`` — when this specific
        identity has never had a rule. This is the correct "previous
        state" lookup for an operator-driven upsert at a specific
        identity: a brand-new address-level rule being created
        underneath an already-governed domain must get ``None`` here,
        never the broader domain rule's own state (``find_for_sender``
        would incorrectly return that broader rule via its own
        most-specific-first resolution when no address-specific rule
        exists yet — see
        ``app/api/routers/mailboxes.py::upsert_mailbox_policy_rule`` for
        the real caller this exists for)."""
        raise NotImplementedError

    @abc.abstractmethod
    def touch_last_seen(
        self,
        *,
        mailbox_id: str,
        sender_domain: str,
        seen_at: datetime,
        sender_address: Optional[str] = None,
        subject: Optional[str] = None,
    ) -> MailboxDomainRule:
        """Stamp ``last_seen_at`` on the rule matched for this sender
        (observability only — never a gate decision) — re-resolves via
        :meth:`find_for_sender` using the SAME ``sender_domain``/
        ``sender_address``/``subject`` the caller already resolved its
        governing rule from, so an ``EXACT_ADDRESS``- or
        ``EXACT_DOMAIN_SUBJECT``-governed message touches THAT rule,
        never a broader domain-level rule that happens to also exist for
        the same domain. Raises ``core.errors.NotFoundError`` if no rule
        exists."""
        raise NotImplementedError

    @abc.abstractmethod
    def list_rules(self, *, mailbox_id: str) -> list[MailboxDomainRule]:
        raise NotImplementedError


class InMemoryMailboxDomainRuleRepository(MailboxDomainRuleRepository):
    """Narrow in-memory reference implementation."""

    def __init__(self) -> None:
        self._by_id: dict[str, MailboxDomainRule] = {}
        #: Domain-level rule identity space (`EXACT`/`INCLUDE_SUBDOMAINS`).
        self._id_by_domain_key: dict[tuple[str, str], str] = {}
        #: CD-6 GUI-operations-foundation follow-on WO — a SEPARATE
        #: identity space for `EXACT_ADDRESS` rules, mirroring the two
        #: real, separate PARTIAL unique indexes the Postgres schema now
        #: carries (see `persistence/postgres/mailbox_domain_rule_models.py`).
        self._id_by_address_key: dict[tuple[str, str], str] = {}
        #: A THIRD, separate identity space for `EXACT_DOMAIN_SUBJECT`
        #: rules — an `EXACT_DOMAIN_SUBJECT` rule must NEVER be placed in
        #: `_id_by_domain_key` (it is not a domain-level rule for
        #: uniqueness purposes; several may coexist per domain).
        self._id_by_subject_key: dict[tuple[str, str, str, str], str] = {}

    def _domain_key(self, *, mailbox_id: str, sender_domain: str) -> tuple[str, str]:
        return (mailbox_id, normalize_domain(sender_domain))

    def _address_key(self, *, mailbox_id: str, sender_address: str) -> tuple[str, str]:
        return (mailbox_id, normalize_address(sender_address))

    def _subject_key(
        self, *, mailbox_id: str, sender_domain: str, subject_predicate_type: str, subject_predicate_value: str
    ) -> tuple[str, str, str, str]:
        return (
            mailbox_id,
            normalize_domain(sender_domain),
            subject_predicate_type,
            normalize_subject_for_policy(subject_predicate_value),
        )

    def upsert_rule(
        self,
        *,
        mailbox_id: str,
        sender_domain: str,
        match_mode: str,
        policy: str,
        destination_entity_id: Optional[str],
        destination_mode: Optional[str],
        source: str,
        processor_hint: Optional[str] = None,
        approved_at: Optional[datetime] = None,
        sender_address: Optional[str] = None,
        subject_predicate_type: Optional[str] = None,
        subject_predicate_value: Optional[str] = None,
    ) -> MailboxDomainRule:
        validate_match_fields_or_raise(
            match_mode=match_mode,
            sender_address=sender_address,
            subject_predicate_type=subject_predicate_type,
            subject_predicate_value=subject_predicate_value,
        )
        validate_policy_fields_or_raise(
            policy=policy, destination_entity_id=destination_entity_id, destination_mode=destination_mode,
            match_mode=match_mode,
        )

        is_address_rule = match_mode == MATCH_MODE_EXACT_ADDRESS
        is_subject_rule = match_mode == MATCH_MODE_EXACT_DOMAIN_SUBJECT
        if is_address_rule:
            normalized_address = normalize_address(sender_address)
            derived_domain = domain_from_address(normalized_address)
            if sender_domain and normalize_domain(sender_domain) != derived_domain:
                raise ValidationError(
                    f"sender_domain '{sender_domain}' does not match the domain of sender_address "
                    f"'{sender_address}' ('{derived_domain}')"
                )
            normalized_domain = derived_domain
            normalized_subject_type = None
            normalized_subject_value = None
            key = self._address_key(mailbox_id=mailbox_id, sender_address=normalized_address)
            existing_id = self._id_by_address_key.get(key)
        elif is_subject_rule:
            normalized_address = None
            normalized_domain = normalize_domain(sender_domain)
            normalized_subject_type = subject_predicate_type
            normalized_subject_value = normalize_subject_for_policy(subject_predicate_value)
            key = self._subject_key(
                mailbox_id=mailbox_id, sender_domain=normalized_domain,
                subject_predicate_type=normalized_subject_type, subject_predicate_value=normalized_subject_value,
            )
            existing_id = self._id_by_subject_key.get(key)
        else:
            normalized_address = None
            normalized_domain = normalize_domain(sender_domain)
            normalized_subject_type = None
            normalized_subject_value = None
            key = self._domain_key(mailbox_id=mailbox_id, sender_domain=normalized_domain)
            existing_id = self._id_by_domain_key.get(key)

        now = utc_now()

        if existing_id is None:
            try:
                candidate = MailboxDomainRule(
                    rule_id=identity.generate_id(),
                    mailbox_id=mailbox_id,
                    sender_domain=normalized_domain,
                    sender_address=normalized_address,
                    subject_predicate_type=normalized_subject_type,
                    subject_predicate_value=normalized_subject_value,
                    match_mode=match_mode,
                    policy=policy,
                    destination_entity_id=destination_entity_id,
                    destination_mode=destination_mode,
                    source=source,
                    processor_hint=processor_hint,
                    approved_at=approved_at,
                    created_at=now,
                    updated_at=now,
                    last_seen_at=now,
                )
                validate_against_contract(candidate.to_dict(), _SCHEMA)
            except ValidationError:
                raise
            except Exception as exc:  # noqa: BLE001 - never leak a raw exception
                raise ValidationError(f"could not create MailboxDomainRule: {exc}") from exc
            self._by_id[candidate.rule_id] = candidate
            if is_address_rule:
                self._id_by_address_key[key] = candidate.rule_id
            elif is_subject_rule:
                self._id_by_subject_key[key] = candidate.rule_id
            else:
                self._id_by_domain_key[key] = candidate.rule_id
            return candidate

        current = self._by_id[existing_id]
        try:
            updated = dataclasses.replace(
                current,
                sender_domain=normalized_domain,
                sender_address=normalized_address,
                subject_predicate_type=normalized_subject_type,
                subject_predicate_value=normalized_subject_value,
                match_mode=match_mode,
                policy=policy,
                destination_entity_id=destination_entity_id,
                destination_mode=destination_mode,
                source=source,
                processor_hint=processor_hint,
                approved_at=approved_at if approved_at is not None else current.approved_at,
                updated_at=now,
            )
            validate_against_contract(updated.to_dict(), _SCHEMA)
        except ValidationError:
            raise
        except Exception as exc:  # noqa: BLE001 - never leak a raw exception
            raise ValidationError(f"could not update MailboxDomainRule: {exc}") from exc
        self._by_id[existing_id] = updated
        return updated

    def get_rule(self, rule_id: str) -> MailboxDomainRule:
        try:
            return self._by_id[rule_id]
        except KeyError:
            raise NotFoundError(f"no MailboxDomainRule with rule_id '{rule_id}'") from None

    def find_for_sender(
        self,
        *,
        mailbox_id: str,
        sender_domain: str,
        sender_address: Optional[str] = None,
        subject: Optional[str] = None,
    ) -> Optional[MailboxDomainRule]:
        # Tier 1 — an EXACT_ADDRESS rule for this exact sender address
        # (the most specific possible match — see module docstring).
        # Subject-independent: always wins if present.
        if sender_address:
            address_id = self._id_by_address_key.get(
                self._address_key(mailbox_id=mailbox_id, sender_address=sender_address)
            )
            if address_id is not None:
                return self._by_id[address_id]

        normalized_domain = normalize_domain(sender_domain)
        normalized_subject = normalize_subject_for_policy(subject)

        # Tier 2 — an EXACT_DOMAIN_SUBJECT rule at this EXACT domain
        # whose predicate the message's own normalised subject
        # satisfies (never via subdomain matching — see module
        # docstring).
        if normalized_subject:
            matching_subject_rules = [
                rule
                for rule in self._by_id.values()
                if rule.mailbox_id == mailbox_id
                and rule.match_mode == MATCH_MODE_EXACT_DOMAIN_SUBJECT
                and rule.sender_domain == normalized_domain
                and subject_matches_predicate(
                    subject, predicate_type=rule.subject_predicate_type, predicate_value=rule.subject_predicate_value
                )
            ]
            if matching_subject_rules:
                exact_matches = [
                    r for r in matching_subject_rules if r.subject_predicate_type == SUBJECT_PREDICATE_EXACT
                ]
                if exact_matches:
                    # By construction (the write-path uniqueness index)
                    # two colliding EXACT identities are impossible in
                    # ordinary operation — a corruption test bypassing
                    # that path is the only way to reach the ambiguous
                    # branch below.
                    if len(exact_matches) > 1:
                        raise ConflictError(
                            f"ambiguous EXACT_DOMAIN_SUBJECT resolution for mailbox '{mailbox_id}' domain "
                            f"'{normalized_domain}': {len(exact_matches)} EXACT predicate rules all match this "
                            "subject — refusing to silently pick one (this indicates data corruption bypassing "
                            "the normal validated write path)"
                        )
                    return exact_matches[0]

                starts_with_matches = [
                    r for r in matching_subject_rules if r.subject_predicate_type == SUBJECT_PREDICATE_STARTS_WITH
                ]
                max_len = max(len(r.subject_predicate_value) for r in starts_with_matches)
                longest = [r for r in starts_with_matches if len(r.subject_predicate_value) == max_len]
                if len(longest) > 1:
                    raise ConflictError(
                        f"ambiguous EXACT_DOMAIN_SUBJECT resolution for mailbox '{mailbox_id}' domain "
                        f"'{normalized_domain}': {len(longest)} STARTS_WITH predicate rules of equal, "
                        f"longest-matching prefix length {max_len} all match this subject — refusing to "
                        "silently pick one (this indicates data corruption bypassing the normal validated "
                        "write path)"
                    )
                return longest[0]

        # Tier 3 — a direct domain-level rule (EXACT or INCLUDE_SUBDOMAINS
        # — only one can exist per domain, by construction) for this
        # exact domain.
        exact_id = self._id_by_domain_key.get((mailbox_id, normalized_domain))
        if exact_id is not None:
            return self._by_id[exact_id]

        # Tier 4 — an INCLUDE_SUBDOMAINS rule whose own domain is a
        # PARENT of the observed domain.
        for rule in self._by_id.values():
            if rule.mailbox_id != mailbox_id or rule.match_mode != MATCH_MODE_INCLUDE_SUBDOMAINS:
                continue
            if normalized_domain == rule.sender_domain or normalized_domain.endswith(f".{rule.sender_domain}"):
                return rule
        return None

    def find_exact(
        self,
        *,
        mailbox_id: str,
        sender_domain: str,
        match_mode: str,
        sender_address: Optional[str] = None,
        subject_predicate_type: Optional[str] = None,
        subject_predicate_value: Optional[str] = None,
    ) -> Optional[MailboxDomainRule]:
        if match_mode == MATCH_MODE_EXACT_ADDRESS:
            if not sender_address:
                raise ValidationError(f"sender_address is required when match_mode is '{MATCH_MODE_EXACT_ADDRESS}'")
            rule_id = self._id_by_address_key.get(
                self._address_key(mailbox_id=mailbox_id, sender_address=sender_address)
            )
        elif match_mode == MATCH_MODE_EXACT_DOMAIN_SUBJECT:
            if not subject_predicate_type or not subject_predicate_value:
                raise ValidationError(
                    "subject_predicate_type and subject_predicate_value are both required when match_mode is "
                    f"'{MATCH_MODE_EXACT_DOMAIN_SUBJECT}'"
                )
            rule_id = self._id_by_subject_key.get(
                self._subject_key(
                    mailbox_id=mailbox_id, sender_domain=sender_domain,
                    subject_predicate_type=subject_predicate_type, subject_predicate_value=subject_predicate_value,
                )
            )
        else:
            rule_id = self._id_by_domain_key.get(self._domain_key(mailbox_id=mailbox_id, sender_domain=sender_domain))
        return self._by_id[rule_id] if rule_id is not None else None

    def touch_last_seen(
        self,
        *,
        mailbox_id: str,
        sender_domain: str,
        seen_at: datetime,
        sender_address: Optional[str] = None,
        subject: Optional[str] = None,
    ) -> MailboxDomainRule:
        rule = self.find_for_sender(
            mailbox_id=mailbox_id, sender_domain=sender_domain, sender_address=sender_address, subject=subject
        )
        if rule is None:
            raise NotFoundError(
                f"no MailboxDomainRule governs mailbox_id={mailbox_id!r} sender_domain={sender_domain!r} — "
                "find_for_sender must resolve a rule before touch_last_seen"
            )
        updated = dataclasses.replace(rule, last_seen_at=seen_at)
        self._by_id[rule.rule_id] = updated
        return updated

    def list_rules(self, *, mailbox_id: str) -> list[MailboxDomainRule]:
        return sorted(
            (r for r in self._by_id.values() if r.mailbox_id == mailbox_id),
            key=lambda r: (r.sender_domain, r.rule_id),
        )
