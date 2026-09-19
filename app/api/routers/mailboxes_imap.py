"""``/internal/mailboxes/*/imap/*`` — the IMAP mailbox connection
lifecycle + sweep-engine HTTP surface for `matt@noust.ai` (CD-6
GUI-operations-foundation follow-on WO — second mailbox provider).

Mirrors ``app/api/routers/mailboxes_microsoft.py``'s own shape/
conventions exactly (thin router, no business logic — every handler
here validates/parses the HTTP request, calls into
``services.mailbox.*``/``services.mailbox.imap.*``, and renders the
returned domain object's own ``to_dict()``), with ONE structural
difference: "connect" here is a direct username/password login attempt
against an already-file-mounted credential, never an interactive OAuth
redirect flow — there is no ``/imap/connect`` "here's a URL, go sign
in" response, no callback endpoint, no state token. See
:func:`connect_imap` below for exactly what "connect" means here.

Endpoints
---------
* ``POST /internal/mailboxes/{mailbox_id}/imap/connect`` — read the
  stored `matt@noust.ai` credential and attempt a real IMAP login now;
  on success marks the mailbox `CONNECTED`.
* ``POST /internal/mailboxes/{mailbox_id}/imap/disconnect``
* ``POST /internal/mailboxes/{mailbox_id}/imap/sweep`` — "Sweep now".
* ``GET /internal/mailboxes/{mailbox_id}/imap/sweeps``
* ``GET /internal/mailboxes/{mailbox_id}/imap/messages``
* ``GET /internal/mailboxes/{mailbox_id}/imap/domain-rules`` — a plain
  read; WRITING a rule goes through the EXISTING provider-neutral
  ``POST /internal/mailboxes/{mailbox_id}/policy-rules`` endpoint
  (``app/api/routers/mailboxes.py``) — this router never forks a second
  rule-write surface.
* ``GET /internal/mailboxes/{mailbox_id}/imap/domain-review`` /
  ``POST .../domain-review/{item_id}/resolve`` /
  ``POST .../domain-review/batch-resolve`` — mirrors
  ``mailboxes_microsoft.py``'s own identical three endpoints, adapted to
  call ``composition.imap_mailbox_adapter`` instead of
  ``composition.microsoft_mailbox_adapter``. No Xero-correlation
  endpoint exists here (that WO addition was Microsoft-mailbox-scoped
  triage tooling, out of this delivery's scope — nothing prevents a
  future WO adding it).
* ``GET /internal/mailboxes/{mailbox_id}/imap/security-review`` /
  ``POST .../security-review/{item_id}/resolve`` — mirrors
  ``mailboxes_microsoft.py``'s own identical two endpoints.

Connection-state method reuse — a documented judgment call
------------------------------------------------------------------------
``services.mailbox.mailbox.MailboxSourceRepository``'s own
``connection_state`` transition methods (``begin_microsoft_connect``/
``mark_microsoft_connected``/``mark_microsoft_auth_required``/
``mark_microsoft_connection_error``/``disconnect_microsoft``) carry a
"microsoft"-prefixed name from that repository's own Slice-4 origin, but
contain ZERO Microsoft-specific logic — they only drive the generic,
provider-neutral ``connection_state`` state machine
(``ALLOWED_CONNECTION_TRANSITIONS`` — see
``services/mailbox/mailbox.py``'s own module docstring). This router
reuses them AS-IS for the IMAP provider too, rather than adding five new,
behaviourally-identical, differently-named methods to that repository —
the smaller, more honest diff (a real rename would be a larger, separate
refactor touching the Microsoft router too, out of this WO's own
"minimal and additive" scope). Flagged here prominently for PL/architect
review, not a silent choice.

Server-side-only credential handling
------------------------------------------------------------------------
No endpoint here ever accepts a username/password/IMAP host as a REQUEST
parameter — the stored file-mounted credential
(``services.mailbox.imap.secrets.read_noustai_imap_credentials``) is the
ONLY source, read entirely server-side. The browser never receives IMAP
credential material of any kind.
"""
from __future__ import annotations

from typing import Any, Optional

from fastapi import APIRouter
from pydantic import BaseModel

from app.api.composition import ensure_seed_entities, get_composition, get_mailbox_source_id
from core.errors import ConflictError, NotFoundError, ValidationError
from services.mailbox.domain_rule import MATCH_MODE_EXACT
from services.mailbox.imap.imap_client import ImapOutcomeStatus
from services.mailbox.lock import MailboxSweepLockError
from services.mailbox.mailbox import PROVIDER_IMAP
from services.mailbox.review_resolution import resolve_domain_review, resolve_security_review
from services.mailbox.sweep import run_sweep
from services.mailbox.sweep_run import TRIGGER_MANUAL
from services.needs_you.needs_you import (
    ITEM_TYPE_MAILBOX_AUTHENTICATION_ESCALATION,
    ITEM_TYPE_MAILBOX_DOMAIN_REVIEW,
)

router = APIRouter(prefix="/internal/mailboxes")


def _require_imap_mailbox(composition, mailbox_id: str):
    mailbox = composition.mailbox_source_repository.get_mailbox(mailbox_id)
    if mailbox.provider_kind != PROVIDER_IMAP:
        raise ValidationError(
            f"mailbox '{mailbox_id}' has provider_kind '{mailbox.provider_kind}', not '{PROVIDER_IMAP}' — "
            "this endpoint is IMAP-adapter-only"
        )
    return mailbox


#: Maps a non-OK `ImapOutcomeStatus` from a login attempt to the
#: (`mark_*` method name, `SweepFailureReason`-style error_code) shape
#: this router's own `connect`/`disconnect` handlers need — mirrors
#: `MicrosoftGraphMailboxAdapter`'s own AUTH_REQUIRED-vs-ERROR
#: distinction (module docstring's own "Connection-state method reuse"
#: section): an AUTH failure is remediable by re-entering credentials
#: (AUTH_REQUIRED); everything else (TLS failure, transport failure,
#: malformed response) is a genuine fault reconnecting alone would not
#: fix (ERROR).
def _apply_login_outcome(composition, mailbox_id: str, outcome) -> Any:
    if outcome.status == ImapOutcomeStatus.OK:
        return composition.mailbox_source_repository.mark_microsoft_connected(mailbox_id)
    if outcome.status == ImapOutcomeStatus.AUTH_ERROR:
        return composition.mailbox_source_repository.mark_microsoft_auth_required(
            mailbox_id, error_detail=outcome.error_detail
        )
    return composition.mailbox_source_repository.mark_microsoft_connection_error(
        mailbox_id, error_code=outcome.status.value, error_detail=outcome.error_detail or outcome.status.value
    )


class ConnectImapRequest(BaseModel):
    actor_type: str
    actor_id: str


class DisconnectImapRequest(BaseModel):
    actor_type: str
    actor_id: str


class SweepImapRequest(BaseModel):
    actor_type: str
    actor_id: str


@router.post("/{mailbox_id}/imap/connect", status_code=201)
async def connect_imap(mailbox_id: str, payload: ConnectImapRequest) -> dict[str, Any]:
    """"Connect" here means: read the stored `matt@noust.ai` credential
    and attempt a real IMAP login NOW — no interactive OAuth flow, no
    redirect, no callback (see module docstring). A `NOT_CONFIGURED`
    outcome (no credential files on disk yet) is an honest
    `ValidationError` -> 400, never a crash and never a connection_state
    mutation — mirrors
    `mailboxes_microsoft.py::connect_microsoft`'s own
    `is_configured()` precondition gate exactly.
    """
    composition = get_composition()
    _require_imap_mailbox(composition, mailbox_id)

    if not composition.imap_mailbox_adapter.is_configured():
        raise ValidationError(
            "matt@noust.ai IMAP is not configured yet — no username/password found at "
            "/opt/bagman/secrets/mail/noustai/ (the operator has not placed credentials there yet). "
            "This is expected until the PL provisions real credentials."
        )

    # NOT_CONFIGURED -> AUTH_REQUIRED first (idempotent no-op if already
    # AUTH_REQUIRED) — the connection_state machine has no direct
    # NOT_CONFIGURED -> CONNECTED edge (see
    # services/mailbox/mailbox.py::ALLOWED_CONNECTION_TRANSITIONS); this
    # mirrors the Microsoft flow's own two-step shape, just performed
    # synchronously within this one request instead of across an OAuth
    # redirect round trip.
    composition.mailbox_source_repository.begin_microsoft_connect(mailbox_id)

    outcome = composition.imap_mailbox_adapter.attempt_login(mailbox_id)
    updated = _apply_login_outcome(composition, mailbox_id, outcome)

    composition.api.record_audit_event(
        event_type="MAILBOX_CONNECTED" if outcome.status == ImapOutcomeStatus.OK else "MAILBOX_AUTH_FAILED",
        actor_type=payload.actor_type,
        actor_id=payload.actor_id,
        subject_type="MailboxSource",
        subject_id=mailbox_id,
        correlation_id=mailbox_id,
        causation_id=None,
        payload={"mailbox_id": mailbox_id, "status": outcome.status.value},
    )
    return updated.to_dict()


@router.post("/{mailbox_id}/imap/disconnect")
async def disconnect_imap(mailbox_id: str, payload: DisconnectImapRequest) -> dict[str, Any]:
    composition = get_composition()
    _require_imap_mailbox(composition, mailbox_id)

    updated = composition.mailbox_source_repository.disconnect_microsoft(mailbox_id)
    composition.api.record_audit_event(
        event_type="MAILBOX_DISCONNECTED",
        actor_type=payload.actor_type,
        actor_id=payload.actor_id,
        subject_type="MailboxSource",
        subject_id=mailbox_id,
        correlation_id=mailbox_id,
        causation_id=None,
        payload={"mailbox_id": mailbox_id},
    )
    return updated.to_dict()


@router.post("/{mailbox_id}/imap/sweep")
async def sweep_imap(mailbox_id: str, payload: SweepImapRequest) -> dict[str, Any]:
    """Mirrors `mailboxes_microsoft.py::sweep_microsoft`'s own doctrine
    exactly (a provider/auth/malformed-response failure is data, never
    an HTTP error)."""
    composition = get_composition()
    mailbox = _require_imap_mailbox(composition, mailbox_id)
    mailbox_source_id = get_mailbox_source_id(composition, mailbox)
    ensure_seed_entities(composition)

    composition.api.record_audit_event(
        event_type="MAILBOX_SWEEP_STARTED",
        actor_type=payload.actor_type,
        actor_id=payload.actor_id,
        subject_type="MailboxSource",
        subject_id=mailbox_id,
        correlation_id=mailbox_id,
        causation_id=None,
        payload={"mailbox_id": mailbox_id, "trigger": TRIGGER_MANUAL},
    )

    try:
        run = run_sweep(
            mailbox=mailbox,
            mailbox_source_id=mailbox_source_id,
            trigger=TRIGGER_MANUAL,
            adapter=composition.imap_mailbox_adapter,
            message_repository=composition.mailbox_message_repository,
            sweep_run_repository=composition.mailbox_sweep_run_repository,
            cursor_repository=composition.mailbox_folder_cursor_repository,
            sweep_lock=composition.mailbox_sweep_lock,
            mailbox_repository=composition.mailbox_source_repository,
            domain_rule_repository=composition.mailbox_domain_rule_repository,
            needs_you_repository=composition.needs_you_repository,
            entity_repository=composition.api.entity_repository,
            api=composition.api,
            object_store=composition.object_store,
            scanner=composition.scanner,
            actor_type=payload.actor_type,
            actor_id=payload.actor_id,
        )
    except MailboxSweepLockError as exc:
        raise ConflictError(str(exc)) from exc

    event_type = {
        "SUCCEEDED": "MAILBOX_SWEEP_SUCCEEDED",
        "PARTIAL": "MAILBOX_SWEEP_PARTIAL",
        "FAILED": "MAILBOX_SWEEP_FAILED",
    }.get(run.status, "MAILBOX_SWEEP_FAILED")
    composition.api.record_audit_event(
        event_type=event_type,
        actor_type=payload.actor_type,
        actor_id=payload.actor_id,
        subject_type="MailboxSweepRun",
        subject_id=run.sweep_run_id,
        correlation_id=mailbox_id,
        causation_id=None,
        payload={
            "mailbox_id": mailbox_id,
            "status": run.status,
            "messages_seen": run.messages_seen,
            "messages_new": run.messages_new,
            "evidence_created": run.evidence_created,
            "error_code": run.error_code,
        },
    )
    return run.to_dict()


@router.get("/{mailbox_id}/imap/sweeps")
async def list_imap_sweeps(mailbox_id: str, limit: int = 20) -> dict[str, Any]:
    composition = get_composition()
    _require_imap_mailbox(composition, mailbox_id)
    if limit <= 0 or limit > 200:
        raise ValidationError(f"limit must be between 1 and 200 (got {limit})")
    runs = composition.mailbox_sweep_run_repository.list_runs(mailbox_id=mailbox_id, limit=limit)
    return {"mailbox_id": mailbox_id, "items": [r.to_dict() for r in runs], "count": len(runs)}


@router.get("/{mailbox_id}/imap/messages")
async def list_imap_messages(mailbox_id: str, limit: int = 50) -> dict[str, Any]:
    composition = get_composition()
    _require_imap_mailbox(composition, mailbox_id)
    if limit <= 0 or limit > 200:
        raise ValidationError(f"limit must be between 1 and 200 (got {limit})")
    messages = composition.mailbox_message_repository.list_messages(mailbox_id=mailbox_id, limit=limit)
    return {"mailbox_id": mailbox_id, "items": [m.to_dict() for m in messages], "count": len(messages)}


@router.get("/{mailbox_id}/imap/domain-rules")
async def list_imap_domain_rules(mailbox_id: str) -> dict[str, Any]:
    """A plain read — see module docstring: writing a rule goes through
    the existing provider-neutral `POST /{mailbox_id}/policy-rules`
    endpoint, never a second write surface here."""
    composition = get_composition()
    _require_imap_mailbox(composition, mailbox_id)
    rules = composition.mailbox_domain_rule_repository.list_rules(mailbox_id=mailbox_id)
    return {"mailbox_id": mailbox_id, "items": [r.to_dict() for r in rules], "count": len(rules)}


@router.get("/{mailbox_id}/imap/domain-review")
async def list_imap_domain_review_items(mailbox_id: str, status: str = "OPEN") -> dict[str, Any]:
    composition = get_composition()
    _require_imap_mailbox(composition, mailbox_id)
    items = [
        item
        for item in composition.needs_you_repository.list_needs_you_items(
            item_type=ITEM_TYPE_MAILBOX_DOMAIN_REVIEW, domain="MAILBOX", status=status
        )
        if item.metadata.get("mailbox_id") == mailbox_id
    ]
    return {"mailbox_id": mailbox_id, "items": [i.to_dict() for i in items], "count": len(items)}


class ResolveImapDomainReviewRequest(BaseModel):
    """Mirrors `mailboxes_microsoft.py::ResolveMailboxDomainReviewRequest`
    field-for-field — see that model's own docstring for what each field
    means."""

    actor_type: str
    actor_id: str
    decision: str  # "ALLOW" | "IGNORE" | "KEEP_GRAY"
    destination_entity_id: Optional[str] = None
    destination_mode: Optional[str] = None
    match_mode: str = MATCH_MODE_EXACT
    processor_hint: Optional[str] = None
    sender_address: Optional[str] = None


def _resolve_imap_domain_review_core(
    composition, *, mailbox_id: str, mailbox, item_id: str, payload: ResolveImapDomainReviewRequest
) -> dict[str, Any]:
    """Thin IMAP-specific wrapper around
    ``services.mailbox.review_resolution.resolve_domain_review`` — the
    ONE real, provider-neutral implementation of this workflow, shared
    with ``app/api/routers/mailboxes_microsoft.py``'s own
    ``_resolve_mailbox_domain_review_core`` (see that function's own
    docstring, and ``services.mailbox.review_resolution``'s own module
    docstring, for the full behavioural contract). This function's only
    remaining job is IMAP-specific HTTP-boundary plumbing: resolving
    `mailbox_source_id` and supplying `composition.imap_mailbox_adapter`
    as the one genuinely provider-specific input."""
    mailbox_source_id = get_mailbox_source_id(composition, mailbox)
    return resolve_domain_review(
        needs_you_repository=composition.needs_you_repository,
        mailbox_message_repository=composition.mailbox_message_repository,
        mailbox_domain_rule_repository=composition.mailbox_domain_rule_repository,
        entity_repository=composition.api.entity_repository,
        api=composition.api,
        object_store=composition.object_store,
        scanner=composition.scanner,
        adapter=composition.imap_mailbox_adapter,
        mailbox=mailbox,
        mailbox_id=mailbox_id,
        mailbox_source_id=mailbox_source_id,
        item_id=item_id,
        actor_type=payload.actor_type,
        actor_id=payload.actor_id,
        decision=payload.decision,
        destination_entity_id=payload.destination_entity_id,
        destination_mode=payload.destination_mode,
        match_mode=payload.match_mode,
        processor_hint=payload.processor_hint,
        sender_address=payload.sender_address,
    )


@router.post("/{mailbox_id}/imap/domain-review/{item_id}/resolve")
async def resolve_imap_domain_review(
    mailbox_id: str, item_id: str, payload: ResolveImapDomainReviewRequest
) -> dict[str, Any]:
    composition = get_composition()
    mailbox = _require_imap_mailbox(composition, mailbox_id)
    return _resolve_imap_domain_review_core(composition, mailbox_id=mailbox_id, mailbox=mailbox, item_id=item_id, payload=payload)


class BatchResolveImapDomainReviewItem(BaseModel):
    item_id: str
    decision: str
    destination_entity_id: Optional[str] = None
    destination_mode: Optional[str] = None
    match_mode: str = MATCH_MODE_EXACT
    processor_hint: Optional[str] = None
    sender_address: Optional[str] = None


class BatchResolveImapDomainReviewRequest(BaseModel):
    actor_type: str
    actor_id: str
    items: list[BatchResolveImapDomainReviewItem]


@router.post("/{mailbox_id}/imap/domain-review/batch-resolve")
async def batch_resolve_imap_domain_review(
    mailbox_id: str, payload: BatchResolveImapDomainReviewRequest
) -> dict[str, Any]:
    composition = get_composition()
    mailbox = _require_imap_mailbox(composition, mailbox_id)

    results = []
    for entry in payload.items:
        try:
            outcome = _resolve_imap_domain_review_core(
                composition,
                mailbox_id=mailbox_id,
                mailbox=mailbox,
                item_id=entry.item_id,
                payload=ResolveImapDomainReviewRequest(
                    actor_type=payload.actor_type,
                    actor_id=payload.actor_id,
                    decision=entry.decision,
                    destination_entity_id=entry.destination_entity_id,
                    destination_mode=entry.destination_mode,
                    match_mode=entry.match_mode,
                    processor_hint=entry.processor_hint,
                    sender_address=entry.sender_address,
                ),
            )
            results.append({"item_id": entry.item_id, "ok": True, **outcome})
        except (ValidationError, ConflictError, NotFoundError) as exc:
            results.append({"item_id": entry.item_id, "ok": False, "error": str(exc)})

    return {"mailbox_id": mailbox_id, "results": results}


@router.get("/{mailbox_id}/imap/security-review")
async def list_imap_security_review_items(mailbox_id: str, status: str = "OPEN") -> dict[str, Any]:
    composition = get_composition()
    _require_imap_mailbox(composition, mailbox_id)
    items = [
        item
        for item in composition.needs_you_repository.list_needs_you_items(
            item_type=ITEM_TYPE_MAILBOX_AUTHENTICATION_ESCALATION, domain="MAILBOX", status=status
        )
        if item.metadata.get("mailbox_id") == mailbox_id
    ]
    return {"mailbox_id": mailbox_id, "items": [i.to_dict() for i in items], "count": len(items)}


class ResolveImapSecurityReviewRequest(BaseModel):
    actor_type: str
    actor_id: str
    decision: str  # "PROCESS_THIS_MESSAGE_ONCE" | "DO_NOT_PROCESS_THIS_MESSAGE"


@router.post("/{mailbox_id}/imap/security-review/{item_id}/resolve")
async def resolve_imap_security_review(
    mailbox_id: str, item_id: str, payload: ResolveImapSecurityReviewRequest
) -> dict[str, Any]:
    """Thin IMAP-specific wrapper around
    ``services.mailbox.review_resolution.resolve_security_review`` — the
    ONE real, provider-neutral implementation of this workflow, shared
    with ``app/api/routers/mailboxes_microsoft.py::resolve_microsoft_security_review``
    (see that function's own docstring for the full behavioural
    contract). This function's only remaining job is IMAP-specific HTTP-
    boundary plumbing: resolving `mailbox_source_id` and supplying
    `composition.imap_mailbox_adapter` as the one genuinely provider-
    specific input."""
    composition = get_composition()
    mailbox = _require_imap_mailbox(composition, mailbox_id)
    mailbox_source_id = get_mailbox_source_id(composition, mailbox)
    return resolve_security_review(
        needs_you_repository=composition.needs_you_repository,
        mailbox_message_repository=composition.mailbox_message_repository,
        mailbox_domain_rule_repository=composition.mailbox_domain_rule_repository,
        api=composition.api,
        object_store=composition.object_store,
        scanner=composition.scanner,
        adapter=composition.imap_mailbox_adapter,
        mailbox=mailbox,
        mailbox_id=mailbox_id,
        mailbox_source_id=mailbox_source_id,
        item_id=item_id,
        actor_type=payload.actor_type,
        actor_id=payload.actor_id,
        decision=payload.decision,
    )
