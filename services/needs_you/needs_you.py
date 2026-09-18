"""Canonical ``NeedsYouItem`` domain model, state machine, and
repository (CD-6 Slice 1, PID §98.2/§98.5).

What this is, and why it is its own top-level component
----------------------------------------------------------
PID §98.2 requires "a universal Needs You queue" — one operator-facing
list, spanning every BAGMAN domain, of the small set of decisions only
a human can make. It is deliberately NOT modelled as a sub-concept of
``services.evidence.intake`` (the only domain with a real producer in
this slice) — PID §98.5 is explicit that the vocabulary must already
cover ``RULE_APPROVAL``/``CLASSIFICATION_REVIEW`` item types that have
no evidence-intake connection at all, and PID §98.6-98.7 name email
triage and invoice review as future producers into this SAME queue.
Structurally this mirrors ``services/evidence/intake/intake.py`` (own
domain object, own closed state machine, own repository ABC + in-memory
reference implementation, own durable PostgreSQL sibling) — the closest
existing precedent in this codebase for "a workflow-attempt object with
a small closed lifecycle, referencing canonical objects by id without
being owned by them".

Open vs. closed vocabulary (read before changing)
----------------------------------------------------
``item_type``, ``domain``, and ``allowed_action_type`` are deliberately
OPEN (validated only as ``^[A-Z][A-Z_]*$`` non-empty strings by
``contracts/needs_you/bagman.needs_you_item.v1.schema.json`` — never a
JSON Schema ``enum``/``const``) so a later CD-6 slice can raise a new
kind of question without a contract redesign — exactly the same
"open taxonomy field" doctrine
``tests/integration/test_architecture_boundaries.py`` already locks in
for ``GovernedEntity.entity_type``/``Source.source_type``/
``EvidenceItem.evidence_type``/``AuditEvent.event_type``. ``status`` and
``priority`` ARE closed (real, small, PID-given vocabularies with no
stated extensibility requirement — see the contract's own field
descriptions).

The non-exhaustive, DOCUMENTED (not enforced-as-a-closed-set)
``item_type`` vocabulary PID §98.5 names, as Python constants below:

* ``COMPANY_REQUIRED``     — an uploaded document needs a Company
  assigned before BAGMAN can code it (this slice's own real producer —
  see below).
* ``PURPOSE_REQUIRED``     — a document/spend needs a "why" before it
  can be treated as complete (folded into ``COMPANY_REQUIRED``'s own
  resolution payload for THIS slice — see below; kept here as a
  distinct named type for a future producer that needs to ask about
  purpose alone, independently of company).
* ``CLASSIFICATION_REVIEW`` — an AI-proposed classification needs
  operator confirmation (PID §98.6's email triage; no producer yet in
  this slice).
* ``RULE_APPROVAL``        — a proposed ``ProcessingRule`` (PID §98.6's
  "Adobe rule") needs operator sign-off (no producer yet in this
  slice).
* ``GENERIC_QUESTION``     — anything that does not fit the above (PID
  §98.5's own catch-all).

Slice 1's one real trigger (documented decision)
----------------------------------------------------
PID §98.3 gives exactly one operator interaction for a newly-uploaded
invoice/receipt/document: answer Company, What, and Why together, in
one review action. Rather than raising three separate NeedsYouItems
(``COMPANY_REQUIRED`` + ``PURPOSE_REQUIRED`` + a third for "what") that
would all have to be resolved in lockstep for one coherent operator
action, this slice raises exactly ONE item per accepted intake, typed
``COMPANY_REQUIRED`` (the blocking question — an unresolved company is
the harder gap; "what"/"why" are captured in the SAME resolution
action) with ``allowed_action_type="COMPANY_WHAT_WHY"`` — its
``resolution`` payload carries all three answers together
(``entity_id``/``what``/``why``). See
``app/api/routers/intake.py``'s own module docstring section on this
hook for exactly where it fires and why (right after ``ACCEPTED ->
REGISTERED`` succeeds, never before — an item that never reaches
canonical evidence has nothing to ask an operator about).

Idempotency design decision (mirrors
``services.evidence.intake.intake``'s own "Idempotency design
decision" section — read that one first)
-------------------------------------------------------------------------
A replayed intake request (PID §25/§53's own idempotent-replay
doctrine) must never raise a second ``COMPANY_REQUIRED`` item for the
same ``evidence_id`` — CD-6's own explicit acceptance requirement. This
module's ``create_needs_you_item`` therefore accepts an optional
``dedupe_key`` — by convention ``f"{item_type}:{source_object_reference}"``,
computed by the caller, never derived internally (this domain object
does not itself know whether a given ``(item_type,
source_object_reference)`` pair is meant to be unique; that is a
caller-level/producer-level decision, exactly as ``IntakeRecord``'s own
``idempotency_key`` is caller-supplied, never self-generated). Given a
``dedupe_key``, a repository resolves an existing row with that same
key instead of creating a second one — this is a plain
"resolve-or-create" idiom (unlike ``IntakeRecord``'s idempotency
design, there is no "identical vs. conflicting identifying tuple"
distinction to make here: by construction, the trigger call always
supplies the same ``item_type``/``source_object_reference`` pair for
the same evidence, so a dedupe hit is always a legitimate replay, never
a genuine conflict worth raising an error for). The durable, real
enforcement mechanism lives in
``persistence/postgres/needs_you_models.py``'s partial unique index —
the in-memory dict lookup below is a narrower, same-process-only
proxy for it (see that module's own docstring for the exact parity
this mirrors with ``IntakeRecordRow``'s ``uq_intake_records_idempotency_key``).
"""
from __future__ import annotations

import abc
import dataclasses
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Mapping, Optional

from core import identity
from core.contract_validation import validate_against_contract
from core.errors import InvalidStateTransitionError, NotFoundError, ValidationError
from core.timestamps import to_contract_string, utc_now

_SCHEMA = "needs_you/bagman.needs_you_item.v1.schema.json"

#: CD-6 Slice 1's only supported contract version for NeedsYouItem; the
#: contract itself pins `schema_version` to this exact value via a
#: JSON Schema `const`, so it is never a caller-supplied parameter.
SCHEMA_VERSION = "bagman.needs_you_item.v1"

# ---------------------------------------------------------------------
# documented (non-exhaustive), open item_type vocabulary — see module
# docstring. Importing these constants rather than typing the literal
# string is encouraged but NOT enforced by the contract (open field).
# ---------------------------------------------------------------------
ITEM_TYPE_COMPANY_REQUIRED = "COMPANY_REQUIRED"
ITEM_TYPE_PURPOSE_REQUIRED = "PURPOSE_REQUIRED"
ITEM_TYPE_CLASSIFICATION_REVIEW = "CLASSIFICATION_REVIEW"
ITEM_TYPE_RULE_APPROVAL = "RULE_APPROVAL"
ITEM_TYPE_GENERIC_QUESTION = "GENERIC_QUESTION"

#: CD-6 Slice 2 additions (PID §98.4, architect spec §7) — the open
#: vocabulary's own extensibility exercised for real: an AI-proposed
#: Xero account that fails `services.xero.ai_suggestion
#: .resolve_ai_suggested_account`'s candidate-set check, and a mapped
#: company whose reference data has gone stale (`services.xero.sync
#: .is_reference_data_stale`). Declared here now, WITHOUT a live
#: producer wired in this slice — a documented judgment call (see the
#: CD-6 Slice 2 delivery report): `XERO_ACCOUNT_REQUIRED`'s only
#: plausible trigger is an AI account-suggestion call this slice
#: deliberately does not build (see `services.xero.ai_suggestion`'s own
#: module docstring — no AI task/prompt proposes a Xero account yet);
#: `XERO_REFERENCE_DATA_STALE` has no natural "raise a question now"
#: moment in this slice either (no scheduled/cron job exists yet to
#: notice staleness between operator visits — `GET
#: /internal/xero/{entity_id}` already surfaces `reference_data_stale`
#: honestly for the GUI to display inline, which is a real, live signal,
#: just not one funnelled through this queue today). Both constants
#: exist now so the FIRST future producer for either one needs no
#: contract/vocabulary change — only a real trigger call site.
ITEM_TYPE_XERO_ACCOUNT_REQUIRED = "XERO_ACCOUNT_REQUIRED"
ITEM_TYPE_XERO_REFERENCE_DATA_STALE = "XERO_REFERENCE_DATA_STALE"

#: CD-6 Slice 4 addition (first real Microsoft Graph adapter + sweep
#: engine) — the one real Needs You producer this slice adds: an
#: ACTIVE Microsoft mailbox that needs a (re)connect action (see
#: `app/api/routers/mailboxes_microsoft.py`'s own module docstring for
#: exactly when this is created/auto-resolved). Mirrors
#: `ITEM_TYPE_COMPANY_REQUIRED`'s own "one item, one real action type"
#: shape.
ITEM_TYPE_MAILBOX_AUTH_REQUIRED = "MAILBOX_AUTH_REQUIRED"
ALLOWED_ACTION_CONNECT_MICROSOFT_MAILBOX = "CONNECT_MICROSOFT_MAILBOX"

#: CD-6 architect amendment (two-stage mail processing §4) — a mail
#: from a domain BAGMAN has never seen a MailboxDomainRule for, that
#: `services.mailbox.discovery_signals` judged a credible new
#: accounting-document candidate. Raised/resolved entirely by
#: `services/mailbox/sweep.py`/`app/api/routers/mailboxes_microsoft.py`
#: (kept there, not here — mirrors `ITEM_TYPE_MAILBOX_AUTH_REQUIRED`'s
#: own "producer lives next to its trigger" placement). Deduped
#: per-(mailbox_id, sender_domain) by the PRODUCER (a metadata scan,
#: not the generic `(item_type, source_object_reference)` mechanism —
#: `source_object_reference` here is the real, canonical
#: `mailbox_message_id` of the ONE message that first triggered this
#: item, since a domain string is not itself a valid canonical
#: identifier; see sweep.py's own module docstring for why that is
#: also exactly the correlation needed to reprocess that message
#: immediately on approval).
ITEM_TYPE_MAILBOX_DOMAIN_REVIEW = "MAILBOX_DOMAIN_REVIEW"
ALLOWED_ACTION_MAILBOX_DOMAIN_REVIEW = "MAILBOX_DOMAIN_REVIEW"

#: Slice 1's own single real allowed_action_type (see module docstring
#: "Slice 1's one real trigger"). Also open (not contract-enforced).
ALLOWED_ACTION_COMPANY_WHAT_WHY = "COMPANY_WHAT_WHY"

#: Closed status vocabulary (PID §98.5 — real, small, no stated
#: extensibility need). Matches the contract's own closed `status` enum.
STATUSES = frozenset({"OPEN", "RESOLVED", "DISMISSED"})
TERMINAL_STATUSES = frozenset({"RESOLVED", "DISMISSED"})

#: The single source of truth for valid NeedsYouItem state transitions
#: (mirrors `services.evidence.intake.intake.ALLOWED_TRANSITIONS`'s own
#: role for IntakeRecord) — an item is answered (RESOLVED) or
#: explicitly declined (DISMISSED); neither is ever reopened. A
#: producer that still has a live question after a DISMISS raises a
#: fresh item instead (same doctrine as IntakeRecord's own terminal
#: states never being re-entered).
ALLOWED_TRANSITIONS: dict[str, frozenset[str]] = {
    "OPEN": frozenset({"RESOLVED", "DISMISSED"}),
    "RESOLVED": frozenset(),
    "DISMISSED": frozenset(),
}

#: Closed priority vocabulary (PID §98.5's plain `priority` field).
PRIORITIES = frozenset({"LOW", "NORMAL", "HIGH"})
DEFAULT_PRIORITY = "NORMAL"


@dataclass(frozen=True)
class NeedsYouItem:
    """One entry in BAGMAN's universal Needs You queue (PID §98.5).

    Immutable once constructed: this is a frozen dataclass, and neither
    this type nor its repository ever mutates a field in place —
    :func:`transition` (below) produces a NEW snapshot on
    resolve/dismiss, exactly like ``IntakeRecord``'s own
    ``transition()``.
    """

    item_id: str
    item_type: str
    domain: str
    question: str
    allowed_action_type: str
    status: str
    created_at: datetime
    correlation_id: str
    source_object_reference: Optional[str] = None
    priority: str = DEFAULT_PRIORITY
    resolved_at: Optional[datetime] = None
    resolved_by_actor_type: Optional[str] = None
    resolved_by_actor_id: Optional[str] = None
    resolution: Optional[Mapping[str, Any]] = None
    metadata: Mapping[str, Any] = field(default_factory=dict)
    schema_version: str = SCHEMA_VERSION

    def to_dict(self) -> dict:
        """Render exactly the shape required by
        ``contracts/needs_you/bagman.needs_you_item.v1.schema.json``."""
        return {
            "item_id": self.item_id,
            "item_type": self.item_type,
            "domain": self.domain,
            "source_object_reference": self.source_object_reference,
            "question": self.question,
            "allowed_action_type": self.allowed_action_type,
            "priority": self.priority,
            "status": self.status,
            "created_at": to_contract_string(self.created_at),
            "resolved_at": to_contract_string(self.resolved_at) if self.resolved_at is not None else None,
            "resolved_by_actor_type": self.resolved_by_actor_type,
            "resolved_by_actor_id": self.resolved_by_actor_id,
            "resolution": dict(self.resolution) if self.resolution is not None else None,
            "correlation_id": self.correlation_id,
            "metadata": dict(self.metadata),
            "schema_version": self.schema_version,
        }


def _dedupe_key(item_type: str, source_object_reference: Optional[str]) -> Optional[str]:
    """The natural key used to detect a replayed producer call (see
    module docstring's "Idempotency design decision"). ``None`` when
    ``source_object_reference`` is absent — an item not anchored to one
    canonical object has no natural dedupe key, so every such call
    creates a genuinely new item (this matches PID §98.5's own
    'GENERIC_QUESTION'/general-operator-question use case, which is not
    expected to be idempotent-replay-safe in the same sense an
    evidence-anchored item is)."""
    if source_object_reference is None:
        return None
    return f"{item_type}:{source_object_reference}"


def transition(
    item: NeedsYouItem,
    new_status: str,
    *,
    resolution: Optional[Mapping[str, Any]],
    resolved_by_actor_type: str,
    resolved_by_actor_id: str,
) -> NeedsYouItem:
    """Move ``item`` to ``new_status`` (``RESOLVED``/``DISMISSED``),
    enforcing :data:`ALLOWED_TRANSITIONS`.

    Raises:
        core.errors.InvalidStateTransitionError: if ``new_status`` is
            not a valid transition from ``item.status`` — in practice
            this means the item is already terminal (already resolved
            or dismissed); see
            ``persistence.postgres.needs_you_repository`` and
            ``app/api/routers/needs_you.py`` for how a genuine
            double-submit is told apart from this genuine conflict at
            the HTTP boundary (idempotent-safe replay of the SAME
            resolution vs. a real attempt to change an already-decided
            item).
        core.errors.ValidationError: if the resulting record fails
            contract validation.
    """
    allowed = ALLOWED_TRANSITIONS.get(item.status, frozenset())
    if new_status not in allowed:
        raise InvalidStateTransitionError(
            f"NeedsYouItem '{item.item_id}' cannot transition from "
            f"'{item.status}' to '{new_status}'; allowed transitions from "
            f"'{item.status}' are {sorted(allowed) or '(none — terminal state)'}"
        )

    try:
        updated = dataclasses.replace(
            item,
            status=new_status,
            resolved_at=utc_now(),
            resolved_by_actor_type=resolved_by_actor_type,
            resolved_by_actor_id=resolved_by_actor_id,
            resolution=dict(resolution) if resolution is not None else None,
        )
        validate_against_contract(updated.to_dict(), _SCHEMA)
    except ValidationError:
        raise
    except Exception as exc:  # noqa: BLE001 - never leak a raw exception
        raise ValidationError(f"could not transition NeedsYouItem: {exc}") from exc

    return updated


class NeedsYouRepository(abc.ABC):
    """Repository abstraction for NeedsYouItem (PID §98.5)."""

    @abc.abstractmethod
    def create_needs_you_item(
        self,
        *,
        item_type: str,
        domain: str,
        question: str,
        allowed_action_type: str,
        correlation_id: Optional[str] = None,
        source_object_reference: Optional[str] = None,
        priority: str = DEFAULT_PRIORITY,
        metadata: Optional[Mapping[str, Any]] = None,
    ) -> NeedsYouItem:
        """Create a new `NeedsYouItem` in `OPEN` — or, if a prior item
        already exists for the same `(item_type, source_object_reference)`
        dedupe key (see :func:`_dedupe_key`), return THAT existing item
        unchanged instead (idempotent replay — see module docstring's
        "Idempotency design decision"; no error is raised for this
        case, unlike `IntakeRecord`'s stricter conflict semantics,
        because there is no independent "identifying content" to
        disagree on here)."""
        raise NotImplementedError

    @abc.abstractmethod
    def get_needs_you_item(self, item_id: str) -> NeedsYouItem:
        raise NotImplementedError

    @abc.abstractmethod
    def find_by_dedupe_key(self, item_type: str, source_object_reference: str) -> Optional[NeedsYouItem]:
        """Read-only lookup by the same `(item_type,
        source_object_reference)` pair :func:`_dedupe_key` combines.
        Returns `None` if no item exists — a query, never
        `NotFoundError`."""
        raise NotImplementedError

    @abc.abstractmethod
    def list_needs_you_items(
        self,
        *,
        status: Optional[str] = None,
        item_type: Optional[str] = None,
        domain: Optional[str] = None,
        limit: Optional[int] = None,
        offset: int = 0,
    ) -> list[NeedsYouItem]:
        """List NeedsYouItems, `OPEN` items first (by priority
        HIGH > NORMAL > LOW, then oldest-first within a priority — 'the
        operator should see the most pressing, longest-waiting item
        first'), then every resolved/dismissed item newest-first.
        `status`/`item_type`/`domain` are optional equality filters."""
        raise NotImplementedError

    @abc.abstractmethod
    def resolve_needs_you_item(
        self,
        item_id: str,
        *,
        new_status: str,
        resolution: Optional[Mapping[str, Any]],
        actor_type: str,
        actor_id: str,
    ) -> NeedsYouItem:
        """Look up `item_id`, apply :func:`transition`, persist and
        return the resulting `NeedsYouItem`."""
        raise NotImplementedError


_PRIORITY_SORT_RANK = {"HIGH": 0, "NORMAL": 1, "LOW": 2}


def _sort_key(item: NeedsYouItem) -> tuple:
    """OPEN items first (by priority, then oldest first); everything
    else after, newest-first. See `list_needs_you_items`'s own
    docstring for the operator-facing rationale."""
    if item.status == "OPEN":
        return (0, _PRIORITY_SORT_RANK.get(item.priority, 1), item.created_at, item.item_id)
    # `-created_at` is not directly negatable (datetime), so a
    # descending secondary sort is achieved via `reverse` at the
    # call site instead — see InMemoryNeedsYouRepository below.
    return (1, item.created_at, item.item_id)


class InMemoryNeedsYouRepository(NeedsYouRepository):
    """Narrow in-memory reference implementation (PID §21 Option A)."""

    def __init__(self) -> None:
        self._by_id: dict[str, NeedsYouItem] = {}
        self._by_dedupe_key: dict[str, str] = {}

    def create_needs_you_item(
        self,
        *,
        item_type: str,
        domain: str,
        question: str,
        allowed_action_type: str,
        correlation_id: Optional[str] = None,
        source_object_reference: Optional[str] = None,
        priority: str = DEFAULT_PRIORITY,
        metadata: Optional[Mapping[str, Any]] = None,
    ) -> NeedsYouItem:
        dedupe_key = _dedupe_key(item_type, source_object_reference)
        if dedupe_key is not None:
            existing_id = self._by_dedupe_key.get(dedupe_key)
            if existing_id is not None:
                return self._by_id[existing_id]

        try:
            candidate = NeedsYouItem(
                item_id=identity.generate_id(),
                item_type=item_type,
                domain=domain,
                source_object_reference=source_object_reference,
                question=question,
                allowed_action_type=allowed_action_type,
                priority=priority,
                status="OPEN",
                created_at=utc_now(),
                correlation_id=correlation_id if correlation_id is not None else identity.generate_id(),
                metadata=dict(metadata) if metadata is not None else {},
            )
            validate_against_contract(candidate.to_dict(), _SCHEMA)
        except ValidationError:
            raise
        except Exception as exc:  # noqa: BLE001 - never leak a raw exception
            raise ValidationError(f"could not create NeedsYouItem: {exc}") from exc

        self._by_id[candidate.item_id] = candidate
        if dedupe_key is not None:
            self._by_dedupe_key[dedupe_key] = candidate.item_id
        return candidate

    def get_needs_you_item(self, item_id: str) -> NeedsYouItem:
        try:
            return self._by_id[item_id]
        except KeyError:
            raise NotFoundError(f"no NeedsYouItem with item_id '{item_id}'") from None

    def find_by_dedupe_key(self, item_type: str, source_object_reference: str) -> Optional[NeedsYouItem]:
        existing_id = self._by_dedupe_key.get(_dedupe_key(item_type, source_object_reference))
        return self._by_id[existing_id] if existing_id is not None else None

    def list_needs_you_items(
        self,
        *,
        status: Optional[str] = None,
        item_type: Optional[str] = None,
        domain: Optional[str] = None,
        limit: Optional[int] = None,
        offset: int = 0,
    ) -> list[NeedsYouItem]:
        items = list(self._by_id.values())
        if status is not None:
            items = [i for i in items if i.status == status]
        if item_type is not None:
            items = [i for i in items if i.item_type == item_type]
        if domain is not None:
            items = [i for i in items if i.domain == domain]

        open_items = sorted(
            (i for i in items if i.status == "OPEN"),
            key=lambda i: (_PRIORITY_SORT_RANK.get(i.priority, 1), i.created_at, i.item_id),
        )
        closed_items = sorted(
            (i for i in items if i.status != "OPEN"),
            key=lambda i: (i.created_at, i.item_id),
            reverse=True,
        )
        ordered = open_items + closed_items

        if limit is None:
            return ordered[offset:]
        return ordered[offset : offset + limit]

    def resolve_needs_you_item(
        self,
        item_id: str,
        *,
        new_status: str,
        resolution: Optional[Mapping[str, Any]],
        actor_type: str,
        actor_id: str,
    ) -> NeedsYouItem:
        current = self.get_needs_you_item(item_id)
        updated = transition(
            current,
            new_status,
            resolution=resolution,
            resolved_by_actor_type=actor_type,
            resolved_by_actor_id=actor_id,
        )
        self._by_id[item_id] = updated
        return updated
