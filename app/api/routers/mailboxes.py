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
