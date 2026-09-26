"""``/internal/mailboxes/*/gmail/*`` — the THIRD real mailbox connection
lifecycle + sweep-engine HTTP surface (CD-6 GUI-operations-foundation
follow-on WO — Gmail mailbox provider, two independent accounts:
``mgs241171@gmail.com`` and ``matt.george.scott@gmail.com``).

Mirrors ``app/api/routers/mailboxes_microsoft.py``'s own shape for the
interactive-OAuth connection lifecycle (authorize-redirect -> callback
-> token-exchange, unlike IMAP's direct-credential "connect") and
``app/api/routers/mailboxes_imap.py``'s own thin-wrapper shape for the
domain-review/security-review workflows (the freshest, cleanest
precedent for delegating straight to
``services.mailbox.review_resolution`` — see that module's own
docstring: `adapter` is the only genuinely provider-specific input).
Thin router, no business logic — every handler here validates/parses the
HTTP request, calls into ``services.mailbox.*``/``services.mailbox.gmail.*``,
and renders the returned domain object's own ``to_dict()``.

Server-side-only OAuth exchange (mirrors Microsoft's own doctrine)
------------------------------------------------------------------------
The browser reaches exactly three of these endpoints directly:
``POST /{mailbox_id}/gmail/connect`` (returns a URL to navigate to —
never receives a `client_secret` or a token), the Google-redirected
``GET /gmail/oauth/callback`` (receives `code`/`state` only — the actual
token EXCHANGE, which needs `client_secret`, happens entirely inside
this server process), and ``POST /{mailbox_id}/gmail/disconnect``. No
endpoint anywhere here ever accepts a raw access/refresh token, a Gmail
identity, a page token, or an evidence object path as a REQUEST
parameter — every one of those is either resolved entirely server-side
or never leaves the server at all.

Two independent Gmail mailboxes — routing the callback to the RIGHT one
------------------------------------------------------------------------
Unlike Microsoft's own single-production-mailbox precedent, TWO Gmail
mailboxes (`mgs241171@gmail.com` and `matt.george.scott@gmail.com`) can
each independently be mid-connect-flow at once. `GET /gmail/oauth/callback`
never trusts a browser-supplied mailbox identity — the ONE thing it
trusts to know which mailbox a given callback is for is the consumed,
server-minted `GmailOAuthState.mailbox_id` (via
`services.mailbox.gmail.oauth_state.consume_state` — see that module's
own docstring for why this is bound to `mailbox_id`, exactly like
Microsoft's own `MailboxOAuthState`). Two flows in flight simultaneously
therefore route correctly because each carries its OWN, independently-
minted `state` value bound to its OWN `mailbox_id` — this router's own
callback handler is written generically (it never special-cases "the
one Gmail mailbox"), mirroring `mailboxes_microsoft.py::microsoft_oauth_callback`'s
own already-generic implementation (that handler was ALREADY written
this way — see its own docstring's "mailbox is resolved from the
consumed `state`" note — this router simply inherits the same
correctness by construction, not a new mechanism).

``default_entity_id`` — never set anywhere in this router
------------------------------------------------------------------------
No code path in this router (or the composition wiring it calls
through) ever assigns a `default_entity_id` to a Gmail mailbox — a
mailbox's `default_entity_id` is set ONLY via
`app/api/routers/mailboxes.py`'s own create/update endpoints, at the
CALLER's (operator's) explicit discretion, exactly like every other
provider (see `services/mailbox/mailbox.py`'s own "a mailbox is NOT a
company" doctrine). This router never reads or writes that field at all.

Endpoints
---------
* ``POST /internal/mailboxes/{mailbox_id}/gmail/connect`` — begin (or
  re-begin) an OAuth connect flow for one Gmail mailbox.
* ``GET /internal/mailboxes/gmail/oauth/callback`` — the exact
  Google-redirected path (not mailbox-id-scoped in the URL — the
  mailbox is resolved from the consumed `state`). Always 303-redirects
  to `/gmail/oauth/result` (mirrors Microsoft's own hardening finding —
  OAuth credential material must never remain in the browser URL at
  rest).
* ``GET /internal/mailboxes/gmail/oauth/result`` — the clean,
  code/state-free landing page.
* ``POST /internal/mailboxes/{mailbox_id}/gmail/disconnect``
* ``POST /internal/mailboxes/{mailbox_id}/gmail/sweep`` — "Sweep now",
  reusing `services.mailbox.sweep.run_sweep` unchanged.
* ``GET /internal/mailboxes/{mailbox_id}/gmail/sweeps``
* ``GET /internal/mailboxes/{mailbox_id}/gmail/messages``
* ``GET /internal/mailboxes/{mailbox_id}/gmail/domain-rules`` — a plain
  read; WRITING a rule directly (independent of a Needs You item) goes
  through the EXISTING provider-neutral
  ``POST /internal/mailboxes/{mailbox_id}/policy-rules`` endpoint — this
  router never forks a second rule-write surface (mirrors
  `mailboxes_imap.py`'s own identical choice).
* ``GET /internal/mailboxes/{mailbox_id}/gmail/domain-review`` /
  ``POST .../domain-review/{item_id}/resolve`` /
  ``POST .../domain-review/batch-resolve`` — thin wrappers over
  ``services.mailbox.review_resolution.resolve_domain_review``, exactly
  mirroring `mailboxes_imap.py`'s own three endpoints (no Xero-
  correlation endpoint here either — that was Microsoft-mailbox-scoped
  triage tooling, out of this delivery's scope).
* ``GET /internal/mailboxes/{mailbox_id}/gmail/security-review`` /
  ``POST .../security-review/{item_id}/resolve`` — thin wrapper over
  ``services.mailbox.review_resolution.resolve_security_review``.
"""
from __future__ import annotations

import os
import urllib.parse
from typing import Any, Optional

from fastapi import APIRouter
from fastapi.responses import HTMLResponse, RedirectResponse
from pydantic import BaseModel

from app.api.composition import ensure_seed_entities, get_composition, get_mailbox_source_id
from core.errors import BagmanError, ConflictError, NotFoundError, OAuthStateError, ValidationError
from services.mailbox.domain_rule import MATCH_MODE_EXACT
from services.mailbox.gmail.oauth_state import consume_state
from services.mailbox.lock import MailboxSweepLockError
from services.mailbox.mailbox import PROVIDER_GOOGLE_GMAIL
from services.mailbox.review_resolution import resolve_domain_review, resolve_security_review
from services.mailbox.sweep import run_sweep
from services.mailbox.sweep_run import TRIGGER_MANUAL
from services.needs_you.needs_you import ITEM_TYPE_MAILBOX_AUTHENTICATION_ESCALATION, ITEM_TYPE_MAILBOX_DOMAIN_REVIEW

router = APIRouter(prefix="/internal/mailboxes")

#: Mirrors `app/api/routers/mailboxes_microsoft.py::_DEFAULT_REDIRECT_URI`
#: exactly — the SAME loopback TLS terminator, a DIFFERENT path (this
#: delivery's own callback endpoint). Overridable purely for tests/a
#: disposable dev instance; production never sets the override.
_DEFAULT_REDIRECT_URI = "https://localhost:8543/internal/mailboxes/gmail/oauth/callback"


def _gmail_redirect_uri() -> str:
    return os.environ.get("BAGMAN_GMAIL_MAIL_REDIRECT_URI", _DEFAULT_REDIRECT_URI)


def _require_gmail_mailbox(composition, mailbox_id: str):
    mailbox = composition.mailbox_source_repository.get_mailbox(mailbox_id)
    if mailbox.provider_kind != PROVIDER_GOOGLE_GMAIL:
        raise ValidationError(
            f"mailbox '{mailbox_id}' has provider_kind '{mailbox.provider_kind}', not "
            f"'{PROVIDER_GOOGLE_GMAIL}' — this endpoint is Gmail-adapter-only"
        )
    return mailbox


class ConnectGmailRequest(BaseModel):
    actor_type: str
    actor_id: str


class DisconnectGmailRequest(BaseModel):
    actor_type: str
    actor_id: str


class SweepGmailRequest(BaseModel):
    actor_type: str
    actor_id: str


@router.post("/{mailbox_id}/gmail/connect", status_code=201)
async def connect_gmail(mailbox_id: str, payload: ConnectGmailRequest) -> dict[str, Any]:
    composition = get_composition()
    mailbox = _require_gmail_mailbox(composition, mailbox_id)

    if not composition.gmail_mailbox_adapter.is_configured():
        raise ValidationError(
            "Gmail is not configured yet — no client_id/client_secret found at "
            "/opt/bagman/secrets/mail/gmail/ (no Google Cloud OAuth app has been registered). "
            "This is expected until the PL provisions real credentials."
        )

    mailbox = composition.mailbox_source_repository.begin_microsoft_connect(mailbox_id)
    oauth_state = composition.mailbox_gmail_oauth_state_repository.create_state(mailbox_id=mailbox_id)
    authorize_url = composition.gmail_mailbox_adapter.build_authorize_url(state=oauth_state.state, redirect_uri=_gmail_redirect_uri())

    composition.api.record_audit_event(
        event_type="MAILBOX_AUTH_INITIATED",
        actor_type=payload.actor_type,
        actor_id=payload.actor_id,
        subject_type="MailboxSource",
        subject_id=mailbox.mailbox_id,
        correlation_id=mailbox.mailbox_id,
        causation_id=None,
        payload={"mailbox_id": mailbox_id},
    )
    return {"mailbox_id": mailbox_id, "authorize_url": authorize_url, "state_expires_at": oauth_state.expires_at.isoformat()}


#: Closed set of landing-page reason keys — mirrors
#: `mailboxes_microsoft.py::_RESULT_REASONS`'s own XSS-closing doctrine
#: exactly (`/oauth/result` is a public GET endpoint reachable directly,
#: so only a recognised key ever selects rendered text).
_RESULT_REASONS: dict[str, str] = {
    "missing_state": "Google did not return a state value — rejected.",
    "invalid_state": "This connection link is invalid, expired, or already used.",
    "no_pending_connection": "No pending connection was found for this flow.",
    "missing_code": "Google did not return an authorization code.",
    "token_exchange_failed": "Could not complete the Gmail token exchange. Please try connecting again.",
    "identity_lookup_failed": "Could not verify your Gmail identity. Please try connecting again.",
    "wrong_account": "The Google account you signed in with does not match this mailbox's own address.",
    "connected": "BAGMAN is now connected to this mailbox.",
}
_UNKNOWN_REASON_MESSAGE = "Something went wrong with this connection attempt."


def _result_page(*, ok: bool, reason: str) -> HTMLResponse:
    colour = "#1a7f37" if ok else "#b42318"
    message = _RESULT_REASONS.get(reason, _UNKNOWN_REASON_MESSAGE)
    return HTMLResponse(
        f"<!doctype html><html><body style='font-family: system-ui; padding: 2rem;'>"
        f"<h1 style='color:{colour}'>{'Connected' if ok else 'Connection failed'}</h1>"
        f"<p>{message}</p><p>You can close this tab and return to BAGMAN.</p>"
        f"</body></html>"
    )


def _redirect_to_result(*, ok: bool, reason: str) -> RedirectResponse:
    query = {"ok": "true" if ok else "false", "reason": reason}
    return RedirectResponse(url=f"/internal/mailboxes/gmail/oauth/result?{urllib.parse.urlencode(query)}", status_code=303)


@router.get("/gmail/oauth/result")
async def gmail_oauth_result(ok: bool = False, reason: str = "") -> HTMLResponse:
    return _result_page(ok=ok, reason=reason)


@router.get("/gmail/oauth/callback")
async def gmail_oauth_callback(code: Optional[str] = None, state: Optional[str] = None) -> RedirectResponse:
    composition = get_composition()

    if not state:
        return _redirect_to_result(ok=False, reason="missing_state")

    try:
        consumed_state = consume_state(composition.mailbox_gmail_oauth_state_repository, state)
    except OAuthStateError as exc:
        from core import identity

        synthetic_id = identity.generate_id()
        composition.api.record_audit_event(
            event_type="MAILBOX_AUTH_FAILED",
            actor_type="EXTERNAL_SYSTEM",
            actor_id="gmail-oauth-callback",
            subject_type="GmailOAuthState",
            subject_id=synthetic_id,
            correlation_id=synthetic_id,
            causation_id=None,
            payload={"reason": "invalid_state", "detail": str(exc)},
        )
        return _redirect_to_result(ok=False, reason="invalid_state")

    # The consumed state's own `mailbox_id` is the ONLY thing this
    # callback trusts to know which mailbox this flow is for — see
    # module docstring's "Two independent Gmail mailboxes" section.
    mailbox_id = consumed_state.mailbox_id
    try:
        mailbox = composition.mailbox_source_repository.get_mailbox(mailbox_id)
    except NotFoundError:
        return _redirect_to_result(ok=False, reason="no_pending_connection")

    def _fail(
        reason_key: str, detail: str, *, provider_operation: Optional[str] = None, provider_status: Optional[str] = None
    ) -> RedirectResponse:
        # Bounded, closed-vocabulary audit diagnostics ONLY — `detail`
        # (free text; may embed a raw upstream Google error body) is
        # used for the operator-facing HTML result page's own wording
        # (via `_redirect_to_result`/`_RESULT_REASONS`) and NEVER passed
        # into this payload. `provider_operation`/`provider_status` are
        # the already-bounded `GmailOutcomeStatus` vocabulary threaded
        # through from `GmailMailboxAdapter.exchange_code_and_verify_identity`'s
        # own `_CallbackOutcome` — see that dataclass's own docstring.
        payload: dict[str, Any] = {"mailbox_id": mailbox_id, "reason": reason_key, "provider": "GOOGLE_GMAIL"}
        if provider_operation is not None:
            payload["provider_operation"] = provider_operation
        if provider_status is not None:
            payload["provider_status"] = provider_status
        composition.api.record_audit_event(
            event_type="MAILBOX_AUTH_FAILED",
            actor_type="EXTERNAL_SYSTEM",
            actor_id="gmail-oauth-callback",
            subject_type="MailboxSource",
            subject_id=mailbox.mailbox_id,
            correlation_id=mailbox.mailbox_id,
            causation_id=None,
            payload=payload,
        )
        return _redirect_to_result(ok=False, reason=reason_key)

    if not code:
        return _fail("missing_code", "Google did not return an authorization code.")

    outcome = composition.gmail_mailbox_adapter.exchange_code_and_verify_identity(
        mailbox_id=mailbox_id, expected_email_address=mailbox.email_address, code=code, redirect_uri=_gmail_redirect_uri()
    )

    if not outcome.ok:
        return _fail(
            outcome.reason, outcome.detail, provider_operation=outcome.provider_operation, provider_status=outcome.provider_status
        )

    composition.api.record_audit_event(
        event_type="MAILBOX_CONNECTED",
        actor_type="EXTERNAL_SYSTEM",
        actor_id="gmail-oauth-callback",
        subject_type="MailboxSource",
        subject_id=mailbox_id,
        correlation_id=mailbox_id,
        causation_id=None,
        payload={"mailbox_id": mailbox_id},
    )
    return _redirect_to_result(ok=True, reason="connected")


@router.post("/{mailbox_id}/gmail/disconnect")
async def disconnect_gmail(mailbox_id: str, payload: DisconnectGmailRequest) -> dict[str, Any]:
    composition = get_composition()
    _require_gmail_mailbox(composition, mailbox_id)

    updated = composition.mailbox_source_repository.disconnect_microsoft(mailbox_id)
    composition.gmail_token_store.delete(mailbox_id)

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


@router.post("/{mailbox_id}/gmail/sweep")
async def sweep_gmail(mailbox_id: str, payload: SweepGmailRequest) -> dict[str, Any]:
    """"Sweep now". Always returns a terminal `MailboxSweepRun` on a
    normal 200 — mirrors `mailboxes_microsoft.py::sweep_microsoft`'s
    identical doctrine (a provider/auth/rate-limit/malformed-response
    failure is data, never an HTTP error)."""
    composition = get_composition()
    mailbox = _require_gmail_mailbox(composition, mailbox_id)
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
            adapter=composition.gmail_mailbox_adapter,
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


@router.get("/{mailbox_id}/gmail/sweeps")
async def list_gmail_sweeps(mailbox_id: str, limit: int = 20) -> dict[str, Any]:
    composition = get_composition()
    _require_gmail_mailbox(composition, mailbox_id)
    if limit <= 0 or limit > 200:
        raise ValidationError(f"limit must be between 1 and 200 (got {limit})")
    runs = composition.mailbox_sweep_run_repository.list_runs(mailbox_id=mailbox_id, limit=limit)
    return {"mailbox_id": mailbox_id, "items": [r.to_dict() for r in runs], "count": len(runs)}


@router.get("/{mailbox_id}/gmail/messages")
async def list_gmail_messages(mailbox_id: str, limit: int = 50) -> dict[str, Any]:
    composition = get_composition()
    _require_gmail_mailbox(composition, mailbox_id)
    if limit <= 0 or limit > 200:
        raise ValidationError(f"limit must be between 1 and 200 (got {limit})")
    messages = composition.mailbox_message_repository.list_messages(mailbox_id=mailbox_id, limit=limit)
    return {"mailbox_id": mailbox_id, "items": [m.to_dict() for m in messages], "count": len(messages)}


@router.get("/{mailbox_id}/gmail/domain-rules")
async def list_gmail_domain_rules(mailbox_id: str) -> dict[str, Any]:
    """A plain read — see module docstring: writing a rule goes through
    the existing provider-neutral `POST /{mailbox_id}/policy-rules`
    endpoint, never a second write surface here."""
    composition = get_composition()
    _require_gmail_mailbox(composition, mailbox_id)
    rules = composition.mailbox_domain_rule_repository.list_rules(mailbox_id=mailbox_id)
    return {"mailbox_id": mailbox_id, "items": [r.to_dict() for r in rules], "count": len(rules)}


@router.get("/{mailbox_id}/gmail/domain-review")
async def list_gmail_domain_review_items(mailbox_id: str, status: str = "OPEN") -> dict[str, Any]:
    composition = get_composition()
    _require_gmail_mailbox(composition, mailbox_id)
    items = [
        item
        for item in composition.needs_you_repository.list_needs_you_items(
            item_type=ITEM_TYPE_MAILBOX_DOMAIN_REVIEW, domain="MAILBOX", status=status
        )
        if item.metadata.get("mailbox_id") == mailbox_id
    ]
    return {"mailbox_id": mailbox_id, "items": [i.to_dict() for i in items], "count": len(items)}


class ResolveGmailDomainReviewRequest(BaseModel):
    """Mirrors `mailboxes_microsoft.py::ResolveMailboxDomainReviewRequest`/
    `mailboxes_imap.py::ResolveImapDomainReviewRequest` field-for-field —
    see either model's own docstring for what each field means."""

    actor_type: str
    actor_id: str
    decision: str  # "ALLOW" | "IGNORE" | "KEEP_GRAY"
    destination_entity_id: Optional[str] = None
    destination_mode: Optional[str] = None
    match_mode: str = MATCH_MODE_EXACT
    processor_hint: Optional[str] = None
    sender_address: Optional[str] = None
    #: Deterministic subject-aware mailbox domain policy — required, and
    #: ONLY accepted, when `match_mode == "EXACT_DOMAIN_SUBJECT"`. See
    #: `services.mailbox.review_resolution.resolve_domain_review`'s own
    #: docstring.
    subject_predicate_type: Optional[str] = None
    subject_predicate_value: Optional[str] = None


def _resolve_gmail_domain_review_core(
    composition, *, mailbox_id: str, mailbox, item_id: str, payload: ResolveGmailDomainReviewRequest
) -> dict[str, Any]:
    """Thin Gmail-specific wrapper around
    ``services.mailbox.review_resolution.resolve_domain_review`` — the
    ONE real, provider-neutral implementation of this workflow, shared
    with Microsoft's and IMAP's own equivalents. This function's only
    remaining job is Gmail-specific HTTP-boundary plumbing: resolving
    `mailbox_source_id` and supplying `composition.gmail_mailbox_adapter`
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
        adapter=composition.gmail_mailbox_adapter,
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
        subject_predicate_type=payload.subject_predicate_type,
        subject_predicate_value=payload.subject_predicate_value,
    )


@router.post("/{mailbox_id}/gmail/domain-review/{item_id}/resolve")
async def resolve_gmail_domain_review(mailbox_id: str, item_id: str, payload: ResolveGmailDomainReviewRequest) -> dict[str, Any]:
    composition = get_composition()
    mailbox = _require_gmail_mailbox(composition, mailbox_id)
    return _resolve_gmail_domain_review_core(composition, mailbox_id=mailbox_id, mailbox=mailbox, item_id=item_id, payload=payload)


class BatchResolveGmailDomainReviewItem(BaseModel):
    item_id: str
    decision: str
    destination_entity_id: Optional[str] = None
    destination_mode: Optional[str] = None
    match_mode: str = MATCH_MODE_EXACT
    processor_hint: Optional[str] = None
    sender_address: Optional[str] = None
    subject_predicate_type: Optional[str] = None
    subject_predicate_value: Optional[str] = None


class BatchResolveGmailDomainReviewRequest(BaseModel):
    actor_type: str
    actor_id: str
    items: list[BatchResolveGmailDomainReviewItem]


@router.post("/{mailbox_id}/gmail/domain-review/batch-resolve")
async def batch_resolve_gmail_domain_review(mailbox_id: str, payload: BatchResolveGmailDomainReviewRequest) -> dict[str, Any]:
    composition = get_composition()
    mailbox = _require_gmail_mailbox(composition, mailbox_id)
    if not payload.items:
        raise ValidationError("batch-resolve requires at least one item in `items`")

    results: list[dict[str, Any]] = []
    for entry in payload.items:
        try:
            outcome = _resolve_gmail_domain_review_core(
                composition,
                mailbox_id=mailbox_id,
                mailbox=mailbox,
                item_id=entry.item_id,
                payload=ResolveGmailDomainReviewRequest(
                    actor_type=payload.actor_type,
                    actor_id=payload.actor_id,
                    decision=entry.decision,
                    destination_entity_id=entry.destination_entity_id,
                    destination_mode=entry.destination_mode,
                    match_mode=entry.match_mode,
                    processor_hint=entry.processor_hint,
                    sender_address=entry.sender_address,
                    subject_predicate_type=entry.subject_predicate_type,
                    subject_predicate_value=entry.subject_predicate_value,
                ),
            )
            results.append({"item_id": entry.item_id, "ok": True, **outcome})
        except BagmanError as exc:
            results.append({"item_id": entry.item_id, "ok": False, "error": str(exc), "error_type": type(exc).__name__})

    succeeded_count = sum(1 for r in results if r["ok"])
    return {
        "mailbox_id": mailbox_id,
        "results": results,
        "count": len(results),
        "succeeded_count": succeeded_count,
        "failed_count": len(results) - succeeded_count,
    }


@router.get("/{mailbox_id}/gmail/security-review")
async def list_gmail_security_review_items(mailbox_id: str, status: str = "OPEN") -> dict[str, Any]:
    composition = get_composition()
    _require_gmail_mailbox(composition, mailbox_id)
    items = [
        item
        for item in composition.needs_you_repository.list_needs_you_items(
            item_type=ITEM_TYPE_MAILBOX_AUTHENTICATION_ESCALATION, domain="MAILBOX", status=status
        )
        if item.metadata.get("mailbox_id") == mailbox_id
    ]
    return {"mailbox_id": mailbox_id, "items": [i.to_dict() for i in items], "count": len(items)}


class ResolveGmailSecurityReviewRequest(BaseModel):
    actor_type: str
    actor_id: str
    decision: str  # "PROCESS_THIS_MESSAGE_ONCE" | "DO_NOT_PROCESS_THIS_MESSAGE"


@router.post("/{mailbox_id}/gmail/security-review/{item_id}/resolve")
async def resolve_gmail_security_review(mailbox_id: str, item_id: str, payload: ResolveGmailSecurityReviewRequest) -> dict[str, Any]:
    """Thin Gmail-specific wrapper around
    ``services.mailbox.review_resolution.resolve_security_review`` —
    mirrors `mailboxes_imap.py::resolve_imap_security_review`'s own
    identical shape."""
    composition = get_composition()
    mailbox = _require_gmail_mailbox(composition, mailbox_id)
    mailbox_source_id = get_mailbox_source_id(composition, mailbox)
    return resolve_security_review(
        needs_you_repository=composition.needs_you_repository,
        mailbox_message_repository=composition.mailbox_message_repository,
        mailbox_domain_rule_repository=composition.mailbox_domain_rule_repository,
        api=composition.api,
        object_store=composition.object_store,
        scanner=composition.scanner,
        adapter=composition.gmail_mailbox_adapter,
        mailbox=mailbox,
        mailbox_id=mailbox_id,
        mailbox_source_id=mailbox_source_id,
        item_id=item_id,
        actor_type=payload.actor_type,
        actor_id=payload.actor_id,
        decision=payload.decision,
    )
