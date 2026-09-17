"""``MailboxSource`` — the operator-facing mailbox DEFINITION registry
(CD-6 Slice 3: "Mailbox Management", TAB 1 / Email).

What this is, and — critically — what it is NOT
----------------------------------------------------
This component owns a *registry of mailbox definitions* BAGMAN will
later monitor for evidence (a future slice). It is a foundation only.
Read this list of things this slice deliberately does NOT implement
before changing anything here — every one of these was explicitly
named out-of-scope by the delivery brief, and none of it may be stubbed
"for completeness":

    * Microsoft Graph OAuth or mail fetch
    * IMAP login/OAuth
    * Gmail OAuth
    * a mailbox sweep scheduler/poller of any kind
    * email body ingestion or attachment extraction
    * invoice detection / AI email classification / "importance" rules
    * duplicate-email detection
    * evidence creation from email
    * provider webhooks
    * mailbox background workers

No code path anywhere in this module (or its repository
implementations, or the HTTP router built on top of it) ever calls out
to Microsoft/Google/an IMAP server/anything else. ``provider_kind``
exists purely so an operator can declare which FUTURE adapter a
mailbox definition is destined for.

Critical identity doctrine — a mailbox is NOT a company
------------------------------------------------------------
A ``MailboxSource`` is never a BAGMAN company/accounting entity/Xero
organisation/ledger/invoice owner. ``mailbox == company`` must never be
encoded anywhere as a canonical invariant. ``default_entity_id`` is
only ever an optional DISPLAY hint (see the contract's own field
description, and ``core.source.Source.governed_entity_hint``'s
identical "hint, never ownership assertion" doctrine, which this
mirrors deliberately) — validated, when present, against the real
canonical entity registry by the HTTP layer
(``app/api/routers/mailboxes.py``), never here: this module does not
import ``core.entity``/``EntityRepository`` at all, exactly the same
layering choice ``app/api/routers/xero.py::_require_entity`` already
makes for its own entity-existence check (a thin-router-layer
responsibility, not a domain-layer one).

Secret doctrine — read this twice
--------------------------------------
NO mailbox password, OAuth token, refresh token, client secret, app
secret, private key, or equivalent may EVER be stored on this domain
object, returned by ``to_dict()``, placed in an audit-event payload, or
logged. No such field exists anywhere on
``contracts/mailbox/bagman.mailbox_source.v1.schema.json``, and none
may ever be added to it. This slice needs zero real credentials at
all — ``connection_state`` is honest, non-secret STATUS vocabulary
only (see below), never a place a credential could hide.

Lifecycle state machine (see :data:`ALLOWED_TRANSITIONS`)
----------------------------------------------------------------
::

    ACTIVE    -> {DISABLED, RETIRED}
    DISABLED  -> {ACTIVE, RETIRED}
    RETIRED   -> {}            (terminal — see judgment call below)

* ``ACTIVE``/``DISABLED`` are both non-destructive, freely-reversible
  sub-states of "still a live definition" — an operator can flip
  between them at will (the GUI's Enable/Disable action).
* ``RETIRED`` is what the GUI's "Delete"/"Remove" action actually
  does — the row is PRESERVED, never physically deleted, so a future
  slice's evidence provenance can never be left pointing at a mailbox
  definition that silently vanished.
* **Judgment call (flagged for PL review):** ``RETIRED`` is terminal in
  this slice — there is no transition back out of it, so a retired
  mailbox cannot be directly re-enabled. Unlike
  ``services.xero.connection.XeroConnection`` (where every non-PENDING
  state can re-enter ``PENDING`` because reconnecting always re-proves
  a fresh, real OAuth grant), retiring a mailbox is a deliberate,
  considered "stop treating this address as a source" decision with no
  equivalent fresh-proof step to re-run — silently reviving a retired
  row's history by flipping a field back is judged the wrong shape of
  "undo" for this slice. If Matt genuinely wants to resume monitoring
  the same address later, the correct action is registering a FRESH
  mailbox definition for it (uniqueness is enforced globally and does
  NOT free up on retirement — see :class:`MailboxSourceRepository`'s
  own docstring — so this is intentionally a decision the PL should
  confirm, not a silent default).
* ``enabled`` (a plain bool, kept in lock-step with ``status`` by
  :func:`transition`) exists purely as a convenience so a GUI/consumer
  can render enabled/disabled without decoding ``status`` itself —
  ``enabled is True`` if and only if ``status == "ACTIVE"``.

``connection_state`` is a SEPARATE field from lifecycle ``status``
------------------------------------------------------------------------
Every mailbox created in this slice is minted ``NOT_CONFIGURED`` and
NOTHING in this module (or its repositories, or the HTTP router) ever
changes it to anything else — ``CONNECTED`` above all: no code path
exists anywhere in this slice that could ever set it, because nothing
in this slice actually connects to anything (see module docstring's
out-of-scope list). ``AUTH_REQUIRED``/``READY_FOR_CONNECTION`` are
forward-declared contract vocabulary for a FUTURE slice's real
adapter/OAuth work — the exact same "declared now, no producer yet"
pattern ``services.needs_you.needs_you.ITEM_TYPE_XERO_ACCOUNT_REQUIRED``
already establishes.

Provider closed-set doctrine (mirrors ``services.xero.connection``)
------------------------------------------------------------------------
``provider_kind`` is an OPEN string at the contract layer (see that
field's own description for why — the same
``test_no_schema_file_hardcodes_a_provider_name_inside_a_structural_constraint``
rule ``services/xero/connection.py`` documents) and a CLOSED,
Python-level set here: :data:`PROVIDER_KINDS`. No provider connection
code exists anywhere behind any of these three values in this slice —
they exist purely so a mailbox definition can declare its FUTURE
adapter.

Uniqueness — a documented choice
--------------------------------------
A real mailbox address is globally unique in practice (one email
address cannot simultaneously belong to two different real mailboxes),
so uniqueness here is GLOBAL across every ``MailboxSource`` row
regardless of ``provider_kind`` — never merely provider-qualified.
Email addresses are normalised to lowercase before both the uniqueness
comparison and storage (case-insensitive-safe, per RFC-practice mailbox
identity). **Judgment call:** unlike
``services.xero.connection.XeroConnection.tenant_id`` (deliberately
FREED on disconnect, because a fresh OAuth consent can always re-prove
a new binding), a mailbox's email address uniqueness is NEVER freed by
retirement — a retired row still represents "this real address,
historically", and creating a second row for the same address would
be confusing regardless of the first row's lifecycle state. The real,
authoritative enforcement is the database-level unique constraint (see
``persistence/postgres/mailbox_models.py``); this in-memory repository
mirrors it with a plain dict, the same "application check first,
database constraint is the real proof under a race" discipline every
other repository in this codebase already follows.
"""
from __future__ import annotations

import abc
import dataclasses
import re
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Mapping, Optional

from core import identity
from core.contract_validation import validate_against_contract
from core.errors import ConflictError, InvalidStateTransitionError, NotFoundError, ValidationError
from core.timestamps import to_contract_string, utc_now

_SCHEMA = "mailbox/bagman.mailbox_source.v1.schema.json"

SCHEMA_VERSION = "bagman.mailbox_source.v1"

#: CD-6 Slice 3's own closed, Python-level provider set — see the
#: module docstring's "Provider closed-set doctrine" section for why
#: this is enforced here rather than in the JSON contract.
PROVIDER_MICROSOFT_GRAPH = "MICROSOFT_GRAPH"
PROVIDER_IMAP = "IMAP"
PROVIDER_GOOGLE_GMAIL = "GOOGLE_GMAIL"
PROVIDER_KINDS = frozenset({PROVIDER_MICROSOFT_GRAPH, PROVIDER_IMAP, PROVIDER_GOOGLE_GMAIL})

#: Closed lifecycle vocabulary (see module docstring's "Lifecycle state
#: machine" section). Matches the contract's own closed `status` enum.
STATUSES = frozenset({"ACTIVE", "DISABLED", "RETIRED"})
TERMINAL_STATUSES = frozenset({"RETIRED"})

#: The single source of truth for valid MailboxSource state transitions
#: — see module docstring for the full rationale, including the
#: documented judgment call that RETIRED is terminal in this slice.
ALLOWED_TRANSITIONS: dict[str, frozenset[str]] = {
    "ACTIVE": frozenset({"DISABLED", "RETIRED"}),
    "DISABLED": frozenset({"ACTIVE", "RETIRED"}),
    "RETIRED": frozenset(),
}

#: Honest, non-secret connection/readiness vocabulary (see module
#: docstring's "connection_state is a SEPARATE field" section). Every
#: mailbox in THIS slice is minted here and stays here for its entire
#: lifetime — no code path anywhere changes it.
CONNECTION_STATE_NOT_CONFIGURED = "NOT_CONFIGURED"
CONNECTION_STATE_AUTH_REQUIRED = "AUTH_REQUIRED"
CONNECTION_STATE_READY_FOR_CONNECTION = "READY_FOR_CONNECTION"
CONNECTION_STATE_CONNECTED = "CONNECTED"
CONNECTION_STATES = frozenset(
    {
        CONNECTION_STATE_NOT_CONFIGURED,
        CONNECTION_STATE_AUTH_REQUIRED,
        CONNECTION_STATE_READY_FOR_CONNECTION,
        CONNECTION_STATE_CONNECTED,
    }
)

#: A deliberately simple, non-RFC-5322-exhaustive structural check —
#: this is an operator-curated governance registry, not a mail
#: transport validator; it only needs to reject obviously-malformed
#: input. Matches the contract's own `pattern`.
_EMAIL_PATTERN = re.compile(r"^[^\s@]+@[^\s@]+\.[^\s@]+$")


def normalize_email(email_address: str) -> str:
    """Lowercase + strip — the ONE place every caller (both repository
    implementations, and any future caller) normalises a mailbox email
    address before either a uniqueness comparison or storage (see
    module docstring's "Uniqueness" section)."""
    return email_address.strip().lower()


def validate_email_or_raise(email_address: str) -> None:
    """Public (not module-private) since both this module and
    ``persistence.postgres.mailbox_repository`` need the identical
    check — mirrors this codebase's convention of the DOMAIN module
    owning validation that a sibling persistence implementation also
    calls (rather than duplicating the regex there)."""
    if not _EMAIL_PATTERN.match(email_address):
        raise ValidationError(f"'{email_address}' is not a structurally valid email address")


def validate_provider_kind_or_raise(provider_kind: str) -> None:
    """Public for the same reason as :func:`validate_email_or_raise`
    above."""
    if provider_kind not in PROVIDER_KINDS:
        raise ValidationError(
            f"'{provider_kind}' is not a governed provider_kind — must be one of {sorted(PROVIDER_KINDS)}"
        )


@dataclass(frozen=True)
class MailboxSource:
    """One operator-registered mailbox definition. Immutable once
    constructed — every transition below produces a NEW snapshot via
    :func:`transition`, never an in-place mutation."""

    mailbox_id: str
    display_name: str
    email_address: str
    provider_kind: str
    default_entity_id: Optional[str]
    enabled: bool
    status: str
    connection_state: str
    last_connection_check_at: Optional[datetime]
    last_successful_sweep_at: Optional[datetime]
    last_error_code: Optional[str]
    last_error_detail: Optional[str]
    created_at: datetime
    updated_at: datetime
    metadata: Mapping[str, Any] = field(default_factory=dict)
    schema_version: str = SCHEMA_VERSION

    def to_dict(self) -> dict:
        """Render exactly the shape required by
        ``contracts/mailbox/bagman.mailbox_source.v1.schema.json``.
        Never includes a secret field of any kind — see module
        docstring's "Secret doctrine"."""
        return {
            "mailbox_id": self.mailbox_id,
            "display_name": self.display_name,
            "email_address": self.email_address,
            "provider_kind": self.provider_kind,
            "default_entity_id": self.default_entity_id,
            "enabled": self.enabled,
            "status": self.status,
            "connection_state": self.connection_state,
            "last_connection_check_at": (
                to_contract_string(self.last_connection_check_at)
                if self.last_connection_check_at is not None
                else None
            ),
            "last_successful_sweep_at": (
                to_contract_string(self.last_successful_sweep_at)
                if self.last_successful_sweep_at is not None
                else None
            ),
            "last_error_code": self.last_error_code,
            "last_error_detail": self.last_error_detail,
            "created_at": to_contract_string(self.created_at),
            "updated_at": to_contract_string(self.updated_at),
            "metadata": dict(self.metadata),
            "schema_version": self.schema_version,
        }


def transition(mailbox: MailboxSource, new_status: str) -> MailboxSource:
    """Move ``mailbox`` to ``new_status``, enforcing
    :data:`ALLOWED_TRANSITIONS`. Stamps ``updated_at`` automatically and
    keeps ``enabled`` in lock-step (``True`` iff ``new_status ==
    "ACTIVE"``) — see module docstring.

    Raises:
        core.errors.InvalidStateTransitionError: if ``new_status`` is
            not a valid transition from ``mailbox.status``.
        core.errors.ValidationError: if the resulting mailbox fails
            contract validation.
    """
    allowed = ALLOWED_TRANSITIONS.get(mailbox.status, frozenset())
    if new_status not in allowed:
        raise InvalidStateTransitionError(
            f"MailboxSource '{mailbox.mailbox_id}' cannot transition from "
            f"'{mailbox.status}' to '{new_status}'; allowed transitions from "
            f"'{mailbox.status}' are {sorted(allowed) or '(none — terminal state)'}"
        )

    try:
        updated = dataclasses.replace(
            mailbox,
            status=new_status,
            enabled=(new_status == "ACTIVE"),
            updated_at=utc_now(),
        )
        validate_against_contract(updated.to_dict(), _SCHEMA)
    except ValidationError:
        raise
    except Exception as exc:  # noqa: BLE001 - never leak a raw exception
        raise ValidationError(f"could not transition MailboxSource: {exc}") from exc

    return updated


class MailboxSourceRepository(abc.ABC):
    """Repository abstraction for MailboxSource. See module docstring's
    "Uniqueness" section for the exact, documented global/
    case-insensitive email-uniqueness rule every implementation must
    enforce."""

    @abc.abstractmethod
    def create_mailbox(
        self,
        *,
        display_name: str,
        email_address: str,
        provider_kind: str,
        default_entity_id: Optional[str] = None,
        metadata: Optional[Mapping[str, Any]] = None,
    ) -> MailboxSource:
        """Create a new ``MailboxSource`` in ``ACTIVE``/
        ``NOT_CONFIGURED``.

        Raises:
            core.errors.ValidationError: malformed email or an
                ungoverned ``provider_kind``.
            core.errors.ConflictError: ``email_address`` (normalised)
                already belongs to another mailbox, of ANY lifecycle
                status (see module docstring — uniqueness is never
                freed by retirement).
        """
        raise NotImplementedError

    @abc.abstractmethod
    def update_mailbox(
        self,
        mailbox_id: str,
        *,
        display_name: str,
        email_address: str,
        provider_kind: str,
        default_entity_id: Optional[str],
    ) -> MailboxSource:
        """Full metadata replace (a documented judgment call — this
        slice's 'Edit' action always submits the complete current form,
        never a partial patch; see ``app/api/routers/mailboxes.py``'s
        own docstring). Never changes ``status``/``enabled``/
        ``connection_state`` — those have their own dedicated methods
        below."""
        raise NotImplementedError

    @abc.abstractmethod
    def enable_mailbox(self, mailbox_id: str) -> MailboxSource:
        """PENDING/ACTIVE-style idempotency: a mailbox already `ACTIVE`
        is returned unchanged, never `InvalidStateTransitionError` — a
        second stale browser tab (or a genuine double-click race)
        calling this on an already-enabled mailbox is a redundant
        confirmation of the status quo, not a state-machine violation.
        `DISABLED -> ACTIVE` is a real, audited transition; `ACTIVE ->
        ACTIVE` is a no-op."""
        raise NotImplementedError

    @abc.abstractmethod
    def disable_mailbox(self, mailbox_id: str) -> MailboxSource:
        """Same idempotency as :meth:`enable_mailbox`, mirrored for
        `DISABLED`."""
        raise NotImplementedError

    @abc.abstractmethod
    def retire_mailbox(self, mailbox_id: str) -> MailboxSource:
        """Same idempotency, mirrored for `RETIRED` — re-retiring an
        already-retired mailbox is a safe no-op, not a "transition back
        out of a terminal state" (which remains genuinely forbidden;
        see :data:`ALLOWED_TRANSITIONS`)."""
        raise NotImplementedError

    @abc.abstractmethod
    def get_mailbox(self, mailbox_id: str) -> MailboxSource:
        raise NotImplementedError

    @abc.abstractmethod
    def get_by_email(self, email_address: str) -> Optional[MailboxSource]:
        """Read-only lookup by normalised email — never
        ``NotFoundError``, mirrors
        ``services.xero.connection.XeroConnectionRepository.get_by_entity``'s
        own "absence is an ordinary, expected state" doctrine."""
        raise NotImplementedError

    @abc.abstractmethod
    def list_mailboxes(self) -> list[MailboxSource]:
        raise NotImplementedError


class InMemoryMailboxSourceRepository(MailboxSourceRepository):
    """Narrow in-memory reference implementation (PID §21 Option A)."""

    def __init__(self) -> None:
        self._by_id: dict[str, MailboxSource] = {}
        self._id_by_email: dict[str, str] = {}

    def _new_row(
        self,
        *,
        display_name: str,
        email_address: str,
        provider_kind: str,
        default_entity_id: Optional[str],
        metadata: Optional[Mapping[str, Any]],
    ) -> MailboxSource:
        validate_email_or_raise(email_address)
        validate_provider_kind_or_raise(provider_kind)
        now = utc_now()
        try:
            candidate = MailboxSource(
                mailbox_id=identity.generate_id(),
                display_name=display_name,
                email_address=normalize_email(email_address),
                provider_kind=provider_kind,
                default_entity_id=default_entity_id,
                enabled=True,
                status="ACTIVE",
                connection_state=CONNECTION_STATE_NOT_CONFIGURED,
                last_connection_check_at=None,
                last_successful_sweep_at=None,
                last_error_code=None,
                last_error_detail=None,
                created_at=now,
                updated_at=now,
                metadata=dict(metadata) if metadata is not None else {},
            )
            validate_against_contract(candidate.to_dict(), _SCHEMA)
        except ValidationError:
            raise
        except Exception as exc:  # noqa: BLE001 - never leak a raw exception
            raise ValidationError(f"could not create MailboxSource: {exc}") from exc
        return candidate

    def create_mailbox(
        self,
        *,
        display_name: str,
        email_address: str,
        provider_kind: str,
        default_entity_id: Optional[str] = None,
        metadata: Optional[Mapping[str, Any]] = None,
    ) -> MailboxSource:
        normalized = normalize_email(email_address)
        if normalized in self._id_by_email:
            raise ConflictError(
                f"a MailboxSource for '{normalized}' already exists — email uniqueness is global "
                "and never freed by retirement (see services/mailbox/mailbox.py's own docstring)"
            )
        candidate = self._new_row(
            display_name=display_name,
            email_address=email_address,
            provider_kind=provider_kind,
            default_entity_id=default_entity_id,
            metadata=metadata,
        )
        self._by_id[candidate.mailbox_id] = candidate
        self._id_by_email[candidate.email_address] = candidate.mailbox_id
        return candidate

    def update_mailbox(
        self,
        mailbox_id: str,
        *,
        display_name: str,
        email_address: str,
        provider_kind: str,
        default_entity_id: Optional[str],
    ) -> MailboxSource:
        current = self.get_mailbox(mailbox_id)
        validate_email_or_raise(email_address)
        validate_provider_kind_or_raise(provider_kind)
        normalized = normalize_email(email_address)
        if normalized != current.email_address:
            existing_id = self._id_by_email.get(normalized)
            if existing_id is not None and existing_id != mailbox_id:
                raise ConflictError(
                    f"a MailboxSource for '{normalized}' already exists — cannot reassign this "
                    "email to a different mailbox_id"
                )

        try:
            updated = dataclasses.replace(
                current,
                display_name=display_name,
                email_address=normalized,
                provider_kind=provider_kind,
                default_entity_id=default_entity_id,
                updated_at=utc_now(),
            )
            validate_against_contract(updated.to_dict(), _SCHEMA)
        except ValidationError:
            raise
        except Exception as exc:  # noqa: BLE001 - never leak a raw exception
            raise ValidationError(f"could not update MailboxSource: {exc}") from exc

        if normalized != current.email_address:
            del self._id_by_email[current.email_address]
            self._id_by_email[normalized] = mailbox_id
        self._by_id[mailbox_id] = updated
        return updated

    def _idempotent_transition(self, mailbox_id: str, new_status: str) -> MailboxSource:
        """Idempotent no-op when already in `new_status` — see
        `persistence.postgres.mailbox_repository
        .PostgresMailboxSourceRepository._simple_transition`'s own
        docstring for the full "a second stale browser tab must not
        500" reasoning this mirrors exactly."""
        current = self.get_mailbox(mailbox_id)
        if current.status == new_status:
            return current
        updated = transition(current, new_status)
        self._by_id[mailbox_id] = updated
        return updated

    def enable_mailbox(self, mailbox_id: str) -> MailboxSource:
        return self._idempotent_transition(mailbox_id, "ACTIVE")

    def disable_mailbox(self, mailbox_id: str) -> MailboxSource:
        return self._idempotent_transition(mailbox_id, "DISABLED")

    def retire_mailbox(self, mailbox_id: str) -> MailboxSource:
        return self._idempotent_transition(mailbox_id, "RETIRED")

    def get_mailbox(self, mailbox_id: str) -> MailboxSource:
        try:
            return self._by_id[mailbox_id]
        except KeyError:
            raise NotFoundError(f"no MailboxSource with mailbox_id '{mailbox_id}'") from None

    def get_by_email(self, email_address: str) -> Optional[MailboxSource]:
        existing_id = self._id_by_email.get(normalize_email(email_address))
        return self._by_id[existing_id] if existing_id is not None else None

    def list_mailboxes(self) -> list[MailboxSource]:
        return sorted(self._by_id.values(), key=lambda m: (m.display_name, m.mailbox_id))
