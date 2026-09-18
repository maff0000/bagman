"""``/internal/mailboxes/*/microsoft/*`` — the first real mailbox
connection lifecycle + sweep-engine HTTP surface (CD-6 Slice 4).

Thin router, no business logic (mirrors ``app/api/routers/xero.py``'s
own established convention exactly, itself following
``app/api/routers/needs_you.py``): every handler here validates/parses
the HTTP request, calls into ``services.mailbox.*``, and renders the
returned domain object's own ``to_dict()``. No canonical invariant
(connection-state legality, OAuth state validation, identity
verification, sweep idempotency/locking) is enforced here — it all
lives in ``services/mailbox/mailbox.py``,
``services/mailbox/microsoft/adapter.py``, and
``services/mailbox/sweep.py``.

Server-side-only OAuth exchange (mirrors architect spec §3's Xero
doctrine exactly)
------------------------------------------------------------------------
The browser reaches exactly three of these endpoints directly:
``POST /{mailbox_id}/microsoft/connect`` (returns a URL to navigate to —
never receives a client_secret or a token), the Microsoft-redirected
``GET /microsoft/oauth/callback`` (receives ``code``/``state`` only —
the actual token EXCHANGE, which needs ``client_secret``, happens
entirely inside this server process), and
``POST /{mailbox_id}/microsoft/disconnect``. No endpoint anywhere here
ever accepts a raw access/refresh token, a Microsoft tenant/user
identity, a delta token, or an evidence object path as a REQUEST
parameter — every one of those is either resolved entirely server-side
or never leaves the server at all.

Callback topology — reuses the exact proven Xero pattern
------------------------------------------------------------------------
``_microsoft_redirect_uri()`` mirrors ``app/api/routers/xero.py
::_redirect_uri()``/``_DEFAULT_REDIRECT_URI`` exactly: the same
loopback TLS terminator (``https://localhost:8543/...``), the same
env-var-override-for-tests discipline
(``BAGMAN_MICROSOFT_MAIL_REDIRECT_URI``). The tunnel is never needed
for an ordinary sweep/refresh — verify architecturally the same way
Xero's own ``run_sync``/``refresh()`` never reference ``redirect_uri``:
neither ``services/mailbox/sweep.py`` nor
``services/mailbox/microsoft/adapter.py``'s token-refresh methods ever
touch this function.

Identity verification, never trusting the callback's own query string
------------------------------------------------------------------------
``GET /microsoft/oauth/callback`` never trusts a browser-supplied
mailbox identity — the ONLY thing it trusts to know which mailbox this
flow was for is the consumed, server-minted
``MailboxOAuthState.mailbox_id`` (via
``services.mailbox.microsoft.oauth_state.consume_state``). The real
account match (``services.mailbox.microsoft.adapter
.MicrosoftGraphMailboxAdapter.exchange_code_and_verify_identity``) is
performed entirely server-side against Graph's own ``/me`` response.

Endpoints
---------
* ``POST /internal/mailboxes/{mailbox_id}/microsoft/connect`` — begin
  (or re-begin) an OAuth connect flow for one mailbox.
* ``GET /internal/mailboxes/microsoft/oauth/callback`` — the exact
  Microsoft-redirected path (not mailbox-id-scoped in the URL — the
  mailbox is resolved from the consumed `state`). Always 303-redirects
  to ``/oauth/result`` (mirrors ``xero.py::oauth_callback``'s own
  hardening finding — OAuth credential material must never remain in
  the browser URL at rest).
* ``GET /internal/mailboxes/microsoft/oauth/result`` — the clean,
  code/state-free landing page.
* ``POST /internal/mailboxes/{mailbox_id}/microsoft/disconnect``
* ``POST /internal/mailboxes/{mailbox_id}/microsoft/sweep`` — "Sweep
  now".
* ``GET /internal/mailboxes/{mailbox_id}/microsoft/sweeps`` — recent
  sweep-run history.
* ``GET /internal/mailboxes/{mailbox_id}/microsoft/messages`` — recent
  message projections (received time/sender/subject/folder/ingestion
  state ONLY — never a body, never a classification field; see
  ``services/mailbox/sweep.py``'s own hard scope boundary).
* ``GET /internal/mailboxes/{mailbox_id}/microsoft/domain-rules`` — the
  mailbox's own governed ``MailboxDomainRule`` list (CD-6 architect
  amendment).
* ``POST /internal/mailboxes/{mailbox_id}/microsoft/domain-review/{item_id}/resolve``
  — resolve one ``ITEM_TYPE_MAILBOX_DOMAIN_REVIEW`` Needs You item
  (architect spec §4's operator decision: Allow -> a real canonical
  entity / Allow -> destination review required / Ignore domain — "Review
  candidate"/defer needs no call at all, the item simply stays OPEN).
  Creates/updates the real ``MailboxDomainRule`` and, on ALLOW,
  immediately reprocesses the one triggering message — see
  ``ResolveMailboxDomainReviewRequest``'s own docstring below.

Needs You integration (architect spec)
------------------------------------------
:func:`_sync_needs_you_for_connection_state` is called after every
mutation here that could change `connection_state`
(connect/callback/disconnect/sweep). It creates exactly ONE unresolved
`MAILBOX_AUTH_REQUIRED` item (idempotent on
`(item_type, mailbox_id)` — see `services.needs_you.needs_you`'s own
dedupe-key doctrine) when an ACTIVE mailbox is not `CONNECTED`, and
auto-resolves it the moment the mailbox reaches `CONNECTED`. Never
creates one for a `DISABLED`/`RETIRED` mailbox, and never for a
non-Microsoft mailbox (NoustAI IMAP has no working connect action —
this whole router is never reached for it).

Audit events (exact names, architect spec)
------------------------------------------------
`MAILBOX_AUTH_INITIATED`, `MAILBOX_CONNECTED`, `MAILBOX_AUTH_FAILED`,
`MAILBOX_DISCONNECTED` are emitted here. `MAILBOX_SWEEP_STARTED`/
`_SUCCEEDED`/`_PARTIAL`/`_FAILED` are emitted here around the call to
`services.mailbox.sweep.run_sweep`. `EMAIL_EVIDENCE_INGESTED`/
`EMAIL_EVIDENCE_QUARANTINED` are emitted by `run_sweep` itself (see
that module's own docstring for why — a future dedicated worker needs
the identical audit trail without this HTTP layer wrapping it). Never
audits body/raw MIME/tokens/codes/OAuth state — payloads carry only
canonical IDs (architect spec: "prefer canonical IDs over subject/
sender").
"""
from __future__ import annotations

import os
import urllib.parse
from typing import Any, Optional

from fastapi import APIRouter
from fastapi.responses import HTMLResponse, RedirectResponse
from pydantic import BaseModel

from app.api.composition import ensure_seed_entities, get_composition, get_mailbox_source_id
from core.errors import ConflictError, NotFoundError, OAuthStateError, ValidationError
from core.timestamps import utc_now
from services.mailbox.domain_rule import (
    DESTINATION_MODE_FIXED,
    DESTINATION_MODE_REVIEW_REQUIRED,
    MATCH_MODE_EXACT,
    MATCH_MODE_INCLUDE_SUBDOMAINS,
    POLICY_ALLOWED,
    POLICY_IGNORED,
    SOURCE_OPERATOR,
)
from services.mailbox.lock import MailboxSweepLockError
from services.mailbox.mailbox import (
    CONNECTION_STATE_CONNECTED,
    PROVIDER_MICROSOFT_GRAPH,
)
from services.mailbox.microsoft.oauth_state import consume_state
from services.mailbox.sweep import reprocess_message_after_domain_rule_approval, run_sweep
from services.mailbox.sweep_run import TRIGGER_MANUAL
from services.needs_you.needs_you import (
    ALLOWED_ACTION_CONNECT_MICROSOFT_MAILBOX,
    ITEM_TYPE_MAILBOX_AUTH_REQUIRED,
    ITEM_TYPE_MAILBOX_DOMAIN_REVIEW,
)

router = APIRouter(prefix="/internal/mailboxes")

#: Mirrors `app/api/routers/xero.py::_DEFAULT_REDIRECT_URI` exactly —
#: the SAME loopback TLS terminator, a DIFFERENT path (this delivery's
#: own callback endpoint). Overridable purely for tests/a disposable
#: dev instance; production never sets the override.
_DEFAULT_REDIRECT_URI = "https://localhost:8543/internal/mailboxes/microsoft/oauth/callback"


def _microsoft_redirect_uri() -> str:
    return os.environ.get("BAGMAN_MICROSOFT_MAIL_REDIRECT_URI", _DEFAULT_REDIRECT_URI)


class ConnectMicrosoftRequest(BaseModel):
    actor_type: str
    actor_id: str


class DisconnectMicrosoftRequest(BaseModel):
    actor_type: str
    actor_id: str


class SweepMicrosoftRequest(BaseModel):
    actor_type: str
    actor_id: str


def _require_microsoft_mailbox(composition, mailbox_id: str):
    mailbox = composition.mailbox_source_repository.get_mailbox(mailbox_id)
    if mailbox.provider_kind != PROVIDER_MICROSOFT_GRAPH:
        raise ValidationError(
            f"mailbox '{mailbox_id}' has provider_kind '{mailbox.provider_kind}', not "
            f"'{PROVIDER_MICROSOFT_GRAPH}' — this endpoint is Microsoft-adapter-only"
        )
    return mailbox


def _sync_needs_you_for_connection_state(composition, mailbox, *, actor_type: str, actor_id: str) -> None:
    """See module docstring's 'Needs You integration' section."""
    existing = composition.needs_you_repository.find_by_dedupe_key(
        ITEM_TYPE_MAILBOX_AUTH_REQUIRED, mailbox.mailbox_id
    )
    if mailbox.connection_state == CONNECTION_STATE_CONNECTED:
        if existing is not None and existing.status == "OPEN":
            composition.needs_you_repository.resolve_needs_you_item(
                existing.item_id,
                new_status="RESOLVED",
                resolution={"mailbox_id": mailbox.mailbox_id, "connected": True},
                actor_type=actor_type,
                actor_id=actor_id,
            )
        return

    if mailbox.status != "ACTIVE":
        return  # a disabled/retired mailbox never raises a fresh Needs You item

    if existing is not None and existing.status == "OPEN":
        return  # already open — never a duplicate unresolved item for the same mailbox

    composition.needs_you_repository.create_needs_you_item(
        item_type=ITEM_TYPE_MAILBOX_AUTH_REQUIRED,
        domain="MAILBOX",
        source_object_reference=mailbox.mailbox_id,
        question=f"Connect Microsoft 365 mailbox — {mailbox.display_name} ({mailbox.email_address})",
        allowed_action_type=ALLOWED_ACTION_CONNECT_MICROSOFT_MAILBOX,
        metadata={"mailbox_id": mailbox.mailbox_id, "email_address": mailbox.email_address},
    )


@router.post("/{mailbox_id}/microsoft/connect", status_code=201)
async def connect_microsoft(mailbox_id: str, payload: ConnectMicrosoftRequest) -> dict[str, Any]:
    composition = get_composition()
    mailbox = _require_microsoft_mailbox(composition, mailbox_id)

    if not composition.microsoft_mailbox_adapter.is_configured():
        raise ValidationError(
            "Microsoft mail is not configured yet — no client_id/client_secret/tenant_id found at "
            "/opt/bagman/secrets/mail/microsoft/ (no Microsoft Entra app has been registered). "
            "This is expected until the PL provisions real credentials."
        )

    mailbox = composition.mailbox_source_repository.begin_microsoft_connect(mailbox_id)
    oauth_state = composition.mailbox_microsoft_oauth_state_repository.create_state(mailbox_id=mailbox_id)
    authorize_url = composition.microsoft_mailbox_adapter.build_authorize_url(
        state=oauth_state.state, redirect_uri=_microsoft_redirect_uri()
    )

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
    _sync_needs_you_for_connection_state(composition, mailbox, actor_type=payload.actor_type, actor_id=payload.actor_id)

    return {
        "mailbox_id": mailbox_id,
        "authorize_url": authorize_url,
        "state_expires_at": oauth_state.expires_at.isoformat(),
    }


#: Closed set of landing-page reason keys — mirrors
#: `app/api/routers/xero.py::_RESULT_REASONS`'s own XSS-closing
#: doctrine exactly (see that module's docstring for the full
#: reasoning: `/oauth/result` is a public GET endpoint reachable
#: directly, so only a recognised key ever selects rendered text).
_RESULT_REASONS: dict[str, str] = {
    "missing_state": "Microsoft did not return a state value — rejected.",
    "invalid_state": "This connection link is invalid, expired, or already used.",
    "no_pending_connection": "No pending connection was found for this flow.",
    "missing_code": "Microsoft did not return an authorization code.",
    "token_exchange_failed": "Could not complete the Microsoft token exchange. Please try connecting again.",
    "identity_lookup_failed": "Could not verify your Microsoft identity. Please try connecting again.",
    "wrong_account": "The Microsoft account you signed in with does not match this mailbox's own address.",
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
    """Mirrors `app/api/routers/xero.py::_redirect_to_result` exactly
    — every outcome ends in a 303 redirect to the clean, code/state-free
    `/oauth/result` URL, never rendered HTML at the callback URL
    itself."""
    query = {"ok": "true" if ok else "false", "reason": reason}
    return RedirectResponse(
        url=f"/internal/mailboxes/microsoft/oauth/result?{urllib.parse.urlencode(query)}", status_code=303
    )


@router.get("/microsoft/oauth/result")
async def microsoft_oauth_result(ok: bool = False, reason: str = "") -> HTMLResponse:
    return _result_page(ok=ok, reason=reason)


@router.get("/microsoft/oauth/callback")
async def microsoft_oauth_callback(code: Optional[str] = None, state: Optional[str] = None) -> RedirectResponse:
    composition = get_composition()

    if not state:
        return _redirect_to_result(ok=False, reason="missing_state")

    try:
        consumed_state = consume_state(composition.mailbox_microsoft_oauth_state_repository, state)
    except OAuthStateError as exc:
        from core import identity

        synthetic_id = identity.generate_id()
        composition.api.record_audit_event(
            event_type="MAILBOX_AUTH_FAILED",
            actor_type="EXTERNAL_SYSTEM",
            actor_id="microsoft-oauth-callback",
            subject_type="MailboxOAuthState",
            subject_id=synthetic_id,
            correlation_id=synthetic_id,
            causation_id=None,
            payload={"reason": "invalid_state", "detail": str(exc)},
        )
        return _redirect_to_result(ok=False, reason="invalid_state")

    mailbox_id = consumed_state.mailbox_id
    try:
        mailbox = composition.mailbox_source_repository.get_mailbox(mailbox_id)
    except NotFoundError:
        return _redirect_to_result(ok=False, reason="no_pending_connection")

    def _fail(reason_key: str, detail: str) -> RedirectResponse:
        composition.api.record_audit_event(
            event_type="MAILBOX_AUTH_FAILED",
            actor_type="EXTERNAL_SYSTEM",
            actor_id="microsoft-oauth-callback",
            subject_type="MailboxSource",
            subject_id=mailbox.mailbox_id,
            correlation_id=mailbox.mailbox_id,
            causation_id=None,
            payload={"mailbox_id": mailbox_id, "reason": reason_key},
        )
        return _redirect_to_result(ok=False, reason=reason_key)

    if not code:
        return _fail("missing_code", "Microsoft did not return an authorization code.")

    outcome = composition.microsoft_mailbox_adapter.exchange_code_and_verify_identity(
        mailbox_id=mailbox_id,
        expected_email_address=mailbox.email_address,
        code=code,
        redirect_uri=_microsoft_redirect_uri(),
    )

    if not outcome.ok:
        return _fail(outcome.reason, outcome.detail)

    updated_mailbox = composition.mailbox_source_repository.get_mailbox(mailbox_id)
    composition.api.record_audit_event(
        event_type="MAILBOX_CONNECTED",
        actor_type="EXTERNAL_SYSTEM",
        actor_id="microsoft-oauth-callback",
        subject_type="MailboxSource",
        subject_id=mailbox_id,
        correlation_id=mailbox_id,
        causation_id=None,
        payload={"mailbox_id": mailbox_id},
    )
    _sync_needs_you_for_connection_state(
        composition, updated_mailbox, actor_type="SYSTEM", actor_id="microsoft-oauth-callback"
    )
    return _redirect_to_result(ok=True, reason="connected")


@router.post("/{mailbox_id}/microsoft/disconnect")
async def disconnect_microsoft(mailbox_id: str, payload: DisconnectMicrosoftRequest) -> dict[str, Any]:
    composition = get_composition()
    _require_microsoft_mailbox(composition, mailbox_id)

    updated = composition.mailbox_source_repository.disconnect_microsoft(mailbox_id)
    composition.microsoft_token_store.delete(mailbox_id)

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
    _sync_needs_you_for_connection_state(composition, updated, actor_type=payload.actor_type, actor_id=payload.actor_id)
    return updated.to_dict()


@router.post("/{mailbox_id}/microsoft/sweep")
async def sweep_microsoft(mailbox_id: str, payload: SweepMicrosoftRequest) -> dict[str, Any]:
    """"Sweep now". Always returns a terminal `MailboxSweepRun` on a
    normal 200 — a provider/auth/rate-limit/malformed-response failure
    is data (a FAILED/PARTIAL run), never an HTTP error (mirrors
    `app/api/routers/xero.py::sync_now`'s identical doctrine). Returns
    HTTP 409 only for the two genuine precondition failures
    (`ConflictError`/`MailboxSweepLockError`) a well-behaved GUI should
    never actually trigger (the Sweep button is only rendered when
    genuinely CONNECTED, and never double-submittable while a sweep is
    already visibly running)."""
    composition = get_composition()
    mailbox = _require_microsoft_mailbox(composition, mailbox_id)
    mailbox_source_id = get_mailbox_source_id(composition, mailbox)
    # The governed-entity bootstrap-floor configuration
    # (`services.mailbox.sweep.compute_bootstrap_floor`) must exist
    # before any sweep can compute its historical boundary — ensure the
    # canonical seed has run rather than requiring a prior, unrelated
    # `GET /internal/entities` call first.
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
            adapter=composition.microsoft_mailbox_adapter,
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

    updated_mailbox = composition.mailbox_source_repository.get_mailbox(mailbox_id)
    _sync_needs_you_for_connection_state(
        composition, updated_mailbox, actor_type=payload.actor_type, actor_id=payload.actor_id
    )
    return run.to_dict()


@router.get("/{mailbox_id}/microsoft/sweeps")
async def list_microsoft_sweeps(mailbox_id: str, limit: int = 20) -> dict[str, Any]:
    composition = get_composition()
    _require_microsoft_mailbox(composition, mailbox_id)
    if limit <= 0 or limit > 200:
        raise ValidationError(f"limit must be between 1 and 200 (got {limit})")
    runs = composition.mailbox_sweep_run_repository.list_runs(mailbox_id=mailbox_id, limit=limit)
    return {"mailbox_id": mailbox_id, "items": [r.to_dict() for r in runs], "count": len(runs)}


@router.get("/{mailbox_id}/microsoft/messages")
async def list_microsoft_messages(mailbox_id: str, limit: int = 50) -> dict[str, Any]:
    """Minimal, NON-classifying recent-mail list (architect spec's hard
    scope boundary — see `services/mailbox/sweep.py`'s own module
    docstring). Never renders raw HTML/body — the projection itself
    never stores a body at all."""
    composition = get_composition()
    _require_microsoft_mailbox(composition, mailbox_id)
    if limit <= 0 or limit > 200:
        raise ValidationError(f"limit must be between 1 and 200 (got {limit})")
    messages = composition.mailbox_message_repository.list_messages(mailbox_id=mailbox_id, limit=limit)
    return {"mailbox_id": mailbox_id, "items": [m.to_dict() for m in messages], "count": len(messages)}


@router.get("/{mailbox_id}/microsoft/domain-rules")
async def list_microsoft_domain_rules(mailbox_id: str) -> dict[str, Any]:
    composition = get_composition()
    _require_microsoft_mailbox(composition, mailbox_id)
    rules = composition.mailbox_domain_rule_repository.list_rules(mailbox_id=mailbox_id)
    return {"mailbox_id": mailbox_id, "items": [r.to_dict() for r in rules], "count": len(rules)}


class ResolveMailboxDomainReviewRequest(BaseModel):
    """Request body for
    ``POST /internal/mailboxes/{mailbox_id}/microsoft/domain-review/{item_id}/resolve``
    — CD-6 architect amendment §4's own operator action set, collapsed
    to two `decision` values (`DEFER`/"Review candidate" needs no
    endpoint call at all — an item simply stays `OPEN` until an
    operator acts; see this router's own module docstring addendum
    below):

    * ``decision="ALLOW"`` + ``destination_mode="FIXED"`` +
      ``destination_entity_id=<Infosecurs|NoustAI|Matthew Scott
      Personal's real entity_id>`` — "Allow -> <company>".
    * ``decision="ALLOW"`` + ``destination_mode="REVIEW_REQUIRED"`` (no
      ``destination_entity_id``) — "Allow -> destination review
      required".
    * ``decision="IGNORE"`` — "Ignore domain".
    """

    actor_type: str
    actor_id: str
    decision: str  # "ALLOW" | "IGNORE"
    destination_entity_id: Optional[str] = None
    destination_mode: Optional[str] = None  # "FIXED" | "REVIEW_REQUIRED" — required when decision == "ALLOW"
    match_mode: str = MATCH_MODE_EXACT  # "EXACT" | "INCLUDE_SUBDOMAINS"
    processor_hint: Optional[str] = None


@router.post("/{mailbox_id}/microsoft/domain-review/{item_id}/resolve")
async def resolve_mailbox_domain_review(
    mailbox_id: str, item_id: str, payload: ResolveMailboxDomainReviewRequest
) -> dict[str, Any]:
    """Resolve one ``ITEM_TYPE_MAILBOX_DOMAIN_REVIEW`` Needs You item —
    the architect spec §4 operator-decision endpoint. On ``ALLOW``/
    ``IGNORE``, creates or updates the real
    ``services.mailbox.domain_rule.MailboxDomainRule`` for this
    ``(mailbox_id, sender_domain)`` (``source="OPERATOR"``,
    ``approved_at=now``) — never auto-adds a domain to any allowlist
    outside this explicit operator action (architect spec §4). On
    ``ALLOW``, the ONE specific triggering message (the item's own
    ``source_object_reference``) is reprocessed IMMEDIATELY — fetched
    and ingested now, not deferred to the next sweep (architect spec's
    own explicit requirement; see
    `services.mailbox.sweep.reprocess_message_after_domain_rule_approval`).

    Idempotent-safe against a genuine double-submit of the exact same
    decision (mirrors `app/api/routers/needs_you.py::resolve_needs_you_item`'s
    own doctrine) — a real attempt to change an already-decided item to
    a DIFFERENT outcome raises `ConflictError` -> HTTP 409.
    """
    composition = get_composition()
    mailbox = _require_microsoft_mailbox(composition, mailbox_id)
    item = composition.needs_you_repository.get_needs_you_item(item_id)

    if item.item_type != ITEM_TYPE_MAILBOX_DOMAIN_REVIEW:
        raise ValidationError(f"NeedsYouItem '{item_id}' is not a {ITEM_TYPE_MAILBOX_DOMAIN_REVIEW} item")
    if item.metadata.get("mailbox_id") != mailbox_id:
        raise ValidationError(f"NeedsYouItem '{item_id}' does not belong to mailbox '{mailbox_id}'")

    if payload.decision not in ("ALLOW", "IGNORE"):
        raise ValidationError(f"decision must be 'ALLOW' or 'IGNORE' (got {payload.decision!r})")
    if payload.match_mode not in (MATCH_MODE_EXACT, MATCH_MODE_INCLUDE_SUBDOMAINS):
        raise ValidationError(f"match_mode must be 'EXACT' or 'INCLUDE_SUBDOMAINS' (got {payload.match_mode!r})")

    sender_domain = item.metadata.get("sender_domain")
    resolution = {
        "decision": payload.decision,
        "destination_entity_id": payload.destination_entity_id,
        "destination_mode": payload.destination_mode,
        "match_mode": payload.match_mode,
    }

    if item.status != "OPEN":
        if item.status == "RESOLVED" and (item.resolution or {}) == resolution:
            return {"needs_you_item": item.to_dict(), "mailbox_domain_rule": None, "reprocessed_message": None}
        raise ConflictError(
            f"NeedsYouItem '{item_id}' is already '{item.status}' with a different resolution — "
            "refusing to silently change an already-decided item; this is a genuine conflict, not "
            "an idempotent retry"
        )

    if payload.decision == "ALLOW":
        policy = POLICY_ALLOWED
        if payload.destination_mode not in (DESTINATION_MODE_FIXED, DESTINATION_MODE_REVIEW_REQUIRED):
            raise ValidationError(
                "destination_mode must be 'FIXED' or 'REVIEW_REQUIRED' when decision is 'ALLOW'"
            )
        if payload.destination_mode == DESTINATION_MODE_FIXED and not payload.destination_entity_id:
            raise ValidationError("destination_entity_id is required when destination_mode is 'FIXED'")
        if payload.destination_entity_id:
            # Real existence check — mirrors `app/api/routers/xero.py
            # ::_require_entity`'s own pattern (never trust a
            # caller-supplied entity_id without proving it real).
            composition.api.entity_repository.get_entity(payload.destination_entity_id)
    else:
        policy = POLICY_IGNORED

    rule = composition.mailbox_domain_rule_repository.upsert_rule(
        mailbox_id=mailbox_id,
        sender_domain=sender_domain,
        match_mode=payload.match_mode,
        policy=policy,
        destination_entity_id=payload.destination_entity_id if policy == POLICY_ALLOWED else None,
        destination_mode=payload.destination_mode if policy == POLICY_ALLOWED else None,
        source=SOURCE_OPERATOR,
        processor_hint=payload.processor_hint,
        approved_at=utc_now(),
    )

    updated_item = composition.needs_you_repository.resolve_needs_you_item(
        item_id, new_status="RESOLVED", resolution=resolution, actor_type=payload.actor_type, actor_id=payload.actor_id
    )
    composition.api.record_audit_event(
        event_type="MAILBOX_DOMAIN_RULE_ALLOWED" if policy == POLICY_ALLOWED else "MAILBOX_DOMAIN_RULE_IGNORED",
        actor_type=payload.actor_type,
        actor_id=payload.actor_id,
        subject_type="MailboxDomainRule",
        subject_id=rule.rule_id,
        correlation_id=updated_item.correlation_id,
        causation_id=None,
        payload={"mailbox_id": mailbox_id, "sender_domain": sender_domain, "needs_you_item_id": item_id},
    )

    reprocessed = None
    if policy == POLICY_ALLOWED and item.source_object_reference is not None:
        mailbox_source_id = get_mailbox_source_id(composition, mailbox)
        reprocessed = reprocess_message_after_domain_rule_approval(
            mailbox=mailbox,
            mailbox_source_id=mailbox_source_id,
            message_id=item.source_object_reference,
            rule=rule,
            adapter=composition.microsoft_mailbox_adapter,
            message_repository=composition.mailbox_message_repository,
            api=composition.api,
            object_store=composition.object_store,
            scanner=composition.scanner,
            actor_type=payload.actor_type,
            actor_id=payload.actor_id,
            correlation_id=updated_item.correlation_id,
        )

    return {
        "needs_you_item": updated_item.to_dict(),
        "mailbox_domain_rule": rule.to_dict(),
        "reprocessed_message": reprocessed.to_dict() if reprocessed is not None else None,
    }
