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
* ``GET /internal/mailboxes/{mailbox_id}/microsoft/domain-review`` — the
  mailbox's own ``MAILBOX_DOMAIN_REVIEW`` Needs You items (default
  ``status=OPEN``; CD-6 GUI-operations-foundation WO), a plain read used
  by the new domain-review batch-triage GUI page to render the operator
  worklist the 90 real Phase A discovery items produced.
* ``POST /internal/mailboxes/{mailbox_id}/microsoft/domain-review/xero-correlate``
  — trigger one bounded Xero-assisted supplier-domain correlation run
  (``services.xero.supplier_correlation``) against an explicitly
  operator-supplied ``entity_id``'s Xero connection, enriching every
  still-OPEN domain-review item's own ``metadata`` with a review-aid
  ``xero_*`` block (CD-6 GUI-operations-foundation WO). A genuine
  ``XeroSupplierCorrelationFailedError`` (e.g. the real Infosecurs
  connection's current scope shortfall) is DATA (``{"ok": false, ...}``
  on a normal 200), never an HTTP-level failure — see
  ``xero_correlate_mailbox_domain_review``'s own docstring.
* ``POST /internal/mailboxes/{mailbox_id}/microsoft/domain-review/{item_id}/resolve``
  — resolve one ``ITEM_TYPE_MAILBOX_DOMAIN_REVIEW`` Needs You item
  (architect spec §4's operator decision: Allow -> a real canonical
  entity / Allow -> destination review required / Ignore domain — "Review
  candidate"/defer needs no call at all, the item simply stays OPEN).
  Creates/updates the real ``MailboxDomainRule`` and, on ALLOW,
  immediately back-processes EVERY historical candidate BAGMAN has
  already discovered for that domain (operational addendum, ahead of
  the first real large historical sweep — not merely the one triggering
  message) — see ``ResolveMailboxDomainReviewRequest``'s own docstring
  below.

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
sender"). `MAILBOX_XERO_CORRELATION_SUCCEEDED`/`_FAILED` (CD-6
GUI-operations-foundation WO addition, not part of the original
architect-named list above — a documented judgment call, mirroring the
same "every mutating/attempted action here is audited" discipline every
other endpoint in this router already follows) are emitted around the
call to `services.xero.supplier_correlation
.correlate_xero_suppliers_for_open_domain_review_items` in
`xero_correlate_mailbox_domain_review`.
"""
from __future__ import annotations

import dataclasses
import os
import urllib.parse
from datetime import datetime
from typing import Any, Optional

from fastapi import APIRouter
from fastapi.responses import HTMLResponse, RedirectResponse
from pydantic import BaseModel

from app.api.composition import ensure_seed_entities, get_composition, get_mailbox_source_id
from core.errors import BagmanError, ConflictError, NotFoundError, OAuthStateError, ValidationError
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
from services.mailbox.domain_review_priority import aggregate_discovery_reasons, compute_review_priority
from services.mailbox.microsoft.oauth_state import consume_state
from services.mailbox.sweep import reprocess_all_historical_candidates_for_domain, run_sweep
from services.mailbox.sweep_run import TRIGGER_MANUAL
from services.needs_you.needs_you import (
    ALLOWED_ACTION_CONNECT_MICROSOFT_MAILBOX,
    ITEM_TYPE_MAILBOX_AUTH_REQUIRED,
    ITEM_TYPE_MAILBOX_DOMAIN_REVIEW,
)
from services.xero.supplier_correlation import (
    XeroSupplierCorrelationFailedError,
    correlate_xero_suppliers_for_open_domain_review_items,
)
from services.xero.sync import resolve_fresh_access_token

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
    # The governed-entity accounting-period configuration
    # (`fiscal_year_start_month_day`, required for
    # `services.mailbox.sweep.compute_bootstrap_floor`'s own mailbox-
    # scoped, per-entity derivation — see that function's own
    # docstring) must exist before any sweep can compute its historical
    # boundary — ensure the canonical seed has run rather than
    # requiring a prior, unrelated `GET /internal/entities` call first.
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


def _parse_metadata_timestamp(value: Optional[str]) -> Optional[datetime]:
    """`item.metadata["first_seen_at"]`/`["last_seen_at"]` are stored as
    contract (RFC 3339, `Z`-suffixed) strings — see
    `services/mailbox/sweep.py::_create_or_reuse_domain_review_item`'s
    own `received_at_str` writes. Mirrors the established
    `datetime.fromisoformat(value.replace("Z", "+00:00"))` parse-back
    convention this codebase already uses elsewhere for the identical
    shape (e.g. `services/xero/client.py`,
    `services/mailbox/microsoft/graph_client.py`) — never a new/bespoke
    parser. `None` in, `None` out (defensive; both metadata keys are
    always populated by the sweep engine before a domain-review item
    ever exists, but this enrichment must never crash a GET on a
    surprising/missing value)."""
    if not value:
        return None
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


@router.get("/{mailbox_id}/microsoft/domain-review")
async def list_microsoft_domain_review_items(mailbox_id: str, status: str = "OPEN") -> dict[str, Any]:
    """List this mailbox's own ``MAILBOX_DOMAIN_REVIEW`` Needs You
    items — a plain read, no side effects (CD-6 GUI-operations-
    foundation WO), feeding the new domain-review batch-triage GUI page
    (``app/api/static/features/mailbox/domain-review.js``).

    ``status`` mirrors ``GET /internal/needs-you``'s own established
    query-param convention/naming exactly (a plain equality filter
    passed straight to ``NeedsYouRepository.list_needs_you_items``) —
    the one deliberate difference from that general-purpose endpoint is
    the DEFAULT: this endpoint defaults to ``status="OPEN"`` rather than
    "every status", since the real, immediate operator need this WO
    exists for is "show me what still needs a decision" (the 90 real
    OPEN Phase A discovery items) — an operator who wants the full
    history (RESOLVED/DISMISSED items too) passes ``?status=`` a
    different explicit value, or a future ``status=ALL``-style extension
    if that need arises; not built speculatively here.

    Filters to `item.metadata["mailbox_id"] == mailbox_id` in Python —
    the EXACT SAME filter
    ``services.xero.supplier_correlation.correlate_xero_suppliers_for_open_domain_review_items``
    and ``services/mailbox/sweep.py``'s own
    ``_find_open_domain_review_item`` already use (see that module's own
    docstring) — a second mailbox's items are never even considered, let
    alone returned.

    Mailbox-evidence-based triage enrichment (mailbox-evidence-based
    triage addendum, on top of the GUI-operations-foundation WO) —
    ``review_priority``/``discovery_reason_counts`` are added as NEW
    TOP-LEVEL keys on each returned item dict, computed fresh on every
    call via ``services.mailbox.domain_review_priority`` from this
    domain's own candidate messages
    (``MailboxMessageRepository.list_candidate_messages_for_domain`` —
    the SAME existing query
    ``services/mailbox/sweep.py::reprocess_all_historical_candidates_for_domain``
    already uses) and the item's own already-populated
    ``candidate_message_count``/``attachment_bearing_count``/
    ``first_seen_at``/``last_seen_at``/``xero_correlation_class``
    metadata. PRESENTATION-LAYER ONLY: never written into
    ``item["metadata"]``, never persisted back via
    ``update_item_metadata`` — see
    ``services/mailbox/domain_review_priority.py``'s own module
    docstring for the full "never persisted" discipline. An item with
    no ``sender_domain`` metadata at all (should not happen for a real
    ``MAILBOX_DOMAIN_REVIEW`` item, but defended against) gets
    ``review_priority: "LOW"``/empty ``discovery_reason_counts`` rather
    than a crash.
    """
    composition = get_composition()
    _require_microsoft_mailbox(composition, mailbox_id)
    items = [
        item
        for item in composition.needs_you_repository.list_needs_you_items(
            item_type=ITEM_TYPE_MAILBOX_DOMAIN_REVIEW, domain="MAILBOX", status=status
        )
        if item.metadata.get("mailbox_id") == mailbox_id
    ]

    enriched_items = []
    for item in items:
        sender_domain = item.metadata.get("sender_domain")
        candidate_messages = (
            composition.mailbox_message_repository.list_candidate_messages_for_domain(
                mailbox_id=mailbox_id, sender_domain=sender_domain
            )
            if sender_domain
            else []
        )
        discovery_reason_counts = aggregate_discovery_reasons(candidate_messages)
        review_priority = compute_review_priority(
            candidate_message_count=item.metadata.get("candidate_message_count") or 0,
            attachment_bearing_count=item.metadata.get("attachment_bearing_count") or 0,
            first_seen_at=_parse_metadata_timestamp(item.metadata.get("first_seen_at")) or utc_now(),
            last_seen_at=_parse_metadata_timestamp(item.metadata.get("last_seen_at")) or utc_now(),
            xero_correlation_class=item.metadata.get("xero_correlation_class"),
            discovery_reason_counts=discovery_reason_counts,
        )
        item_dict = item.to_dict()
        item_dict["review_priority"] = review_priority
        item_dict["discovery_reason_counts"] = discovery_reason_counts
        enriched_items.append(item_dict)

    return {"mailbox_id": mailbox_id, "items": enriched_items, "count": len(enriched_items)}


class XeroCorrelateMailboxDomainReviewRequest(BaseModel):
    """Request body for
    ``POST /internal/mailboxes/{mailbox_id}/microsoft/domain-review/xero-correlate``
    (CD-6 GUI-operations-foundation WO).

    ``entity_id`` is the ``GovernedEntity`` whose Xero connection to
    correlate the mailbox's own OPEN domain-review items against —
    ALWAYS explicit, operator-supplied, NEVER inferred from the
    mailbox's own optional ``default_entity_id`` hint. This mirrors an
    established doctrine already applied elsewhere in this same router
    (``ResolveMailboxDomainReviewRequest.destination_entity_id`` is
    likewise never defaulted from the mailbox) — a mailbox's own
    "likely destination" hint is a GUI convenience for pre-selecting a
    dropdown, never something a server-side endpoint silently trusts as
    the real decision.
    """

    entity_id: str
    actor_type: str
    actor_id: str


@router.post("/{mailbox_id}/microsoft/domain-review/xero-correlate")
async def xero_correlate_mailbox_domain_review(
    mailbox_id: str, payload: XeroCorrelateMailboxDomainReviewRequest
) -> dict[str, Any]:
    """Trigger one bounded Xero-assisted supplier-domain correlation run
    (``services.xero.supplier_correlation
    .correlate_xero_suppliers_for_open_domain_review_items``) for this
    mailbox's currently-OPEN ``MAILBOX_DOMAIN_REVIEW`` items, against
    ``payload.entity_id``'s Xero connection (CD-6 GUI-operations-
    foundation WO).

    Token resolution — reuses `run_sync`'s own proven sequence
    ------------------------------------------------------------------
    Resolves ``payload.entity_id``'s ``CONNECTED`` ``XeroConnection``
    and a fresh access token via the SAME
    ``services.xero.sync.resolve_fresh_access_token`` helper
    ``services.xero.sync.run_sync`` itself now calls (extracted from
    that function for exactly this reuse — see that module's own
    docstring for the extraction-boundary judgment call) — never a
    reimplementation of that refresh-then-write-back sequence. No
    connection at all, a non-``CONNECTED`` connection, missing stored
    tokens, or a failed refresh are ALL genuine request-level
    preconditions this endpoint cannot proceed past — each raises
    ``core.errors.ConflictError`` (-> HTTP 409 via this app's existing
    ``core.errors.BagmanError`` exception-handler mapping in
    ``app/api/main.py`` — no new special-case mapping added here), the
    SAME status a caller already gets from every other "no usable Xero
    connection" precondition in this codebase
    (``services.xero.sync.run_sync``'s own identical "no CONNECTED
    connection" `ConflictError`). This is a deliberate, documented
    judgment call: `run_sync` itself treats "missing tokens"/"refresh
    failed" as an ordinary FAILED sync run rather than a raised error
    (because a sync run object already exists to record it in) — THIS
    endpoint has no such run object to record into, so it raises
    instead, exactly as `run_sync` already does for its own "no
    connection at all" case.

    Xero data-access failure is DATA, not an HTTP failure
    ------------------------------------------------------------------
    Once a fresh access token is in hand, a genuine
    ``XeroSupplierCorrelationFailedError`` (Contacts/Invoices access
    itself failing — e.g. the REAL current Infosecurs Xero connection,
    which only has ``accounting.settings.read`` scope today and will
    hit exactly this until a real, later, operator-driven re-consent
    grants Contacts/Invoices access) is caught here and returned as
    ``{"ok": false, "error": ..., "error_type":
    "XeroSupplierCorrelationFailedError"}`` on a normal HTTP 200 — this
    mirrors ``batch_resolve_mailbox_domain_review``'s own established
    "ok:false is data, not a request-level failure" precedent in this
    same router file exactly: the HTTP REQUEST was handled correctly
    (mailbox resolved, entity resolved, a real token obtained, a real
    call attempted); it is the underlying CORRELATION that failed, an
    entirely expected, common, pre-re-authorization outcome the GUI
    must render calmly, never as an alarming error banner.
    """
    composition = get_composition()
    mailbox = _require_microsoft_mailbox(composition, mailbox_id)
    # Real existence check before anything else — mirrors
    # `app/api/routers/xero.py::_require_entity`'s own pattern (never
    # trust a caller-supplied entity_id without proving it real first).
    composition.api.entity_repository.get_entity(payload.entity_id)

    connection = composition.xero_connection_repository.get_by_entity(payload.entity_id)
    if connection is None or connection.status != "CONNECTED" or not connection.tenant_id:
        raise ConflictError(
            f"entity '{payload.entity_id}' has no CONNECTED XeroConnection — cannot run Xero "
            "supplier-domain correlation (the caller should not offer this action in this state)"
        )

    token_resolution = resolve_fresh_access_token(
        entity_id=payload.entity_id,
        connection=connection,
        connection_repository=composition.xero_connection_repository,
        token_store=composition.xero_token_store,
        oauth_client=composition.xero_oauth_client,
    )
    if not token_resolution.ok:
        raise ConflictError(
            f"could not resolve a fresh Xero access token for entity '{payload.entity_id}': "
            f"{token_resolution.error_detail or token_resolution.failure_reason}"
        )

    try:
        summary = correlate_xero_suppliers_for_open_domain_review_items(
            mailbox_id=mailbox_id,
            entity_id=payload.entity_id,
            tenant_id=connection.tenant_id,
            access_token=token_resolution.access_token,
            xero_client=composition.xero_accounting_client,
            needs_you_repository=composition.needs_you_repository,
            entity_repository=composition.api.entity_repository,
        )
    except XeroSupplierCorrelationFailedError as exc:
        composition.api.record_audit_event(
            event_type="MAILBOX_XERO_CORRELATION_FAILED",
            actor_type=payload.actor_type,
            actor_id=payload.actor_id,
            subject_type="MailboxSource",
            subject_id=mailbox.mailbox_id,
            correlation_id=mailbox.mailbox_id,
            causation_id=None,
            payload={"mailbox_id": mailbox_id, "entity_id": payload.entity_id, "error": str(exc)},
        )
        return {"ok": False, "error": str(exc), "error_type": "XeroSupplierCorrelationFailedError"}

    composition.api.record_audit_event(
        event_type="MAILBOX_XERO_CORRELATION_SUCCEEDED",
        actor_type=payload.actor_type,
        actor_id=payload.actor_id,
        subject_type="MailboxSource",
        subject_id=mailbox.mailbox_id,
        correlation_id=mailbox.mailbox_id,
        causation_id=None,
        payload={
            "mailbox_id": mailbox_id,
            "entity_id": payload.entity_id,
            "domain_review_items_updated": summary.domain_review_items_updated,
            "strong_purchase_bill_count": summary.strong_purchase_bill_count,
            "strong_bank_spend_count": summary.strong_bank_spend_count,
        },
    )
    return {"ok": True, **dataclasses.asdict(summary)}


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


def _resolve_mailbox_domain_review_core(
    composition, *, mailbox_id: str, mailbox, item_id: str, payload: ResolveMailboxDomainReviewRequest
) -> dict[str, Any]:
    """The real business logic behind
    ``POST /{mailbox_id}/microsoft/domain-review/{item_id}/resolve`` —
    extracted so ``resolve_mailbox_domain_review`` (single-item) and
    ``batch_resolve_mailbox_domain_review`` (below) call the EXACT SAME
    governed path, never a parallel/cheaper "bulk mode" (the WO's own
    explicit instruction: every batch-approved item must go through the
    identical back-processing-only-of-`discovery_candidate=True`
    behaviour and the identical audit-event emission a single approval
    gets). See ``resolve_mailbox_domain_review``'s own docstring for the
    full behavioural description — unchanged by this extraction, this
    is a pure "move the body into a function, call it from two places"
    refactor with no logic change.
    """
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
            return {"needs_you_item": item.to_dict(), "mailbox_domain_rule": None, "reprocessed_messages": []}
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

    # Operational addendum (ahead of the first real large historical
    # sweep) — back-process EVERY historical candidate BAGMAN has
    # already discovered for this domain, not merely the one message
    # that happened to trigger this item. `sender_domain` is always
    # present on a real `MAILBOX_DOMAIN_REVIEW` item's own metadata (see
    # `_create_or_reuse_domain_review_item`); the guard below is
    # defensive, never expected to be exercised for a well-formed item.
    reprocessed: list = []
    if policy == POLICY_ALLOWED and sender_domain:
        mailbox_source_id = get_mailbox_source_id(composition, mailbox)
        reprocessed = reprocess_all_historical_candidates_for_domain(
            mailbox=mailbox,
            mailbox_source_id=mailbox_source_id,
            sender_domain=sender_domain,
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
        "reprocessed_messages": [m.to_dict() for m in reprocessed],
    }


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
    ``ALLOW``, EVERY historical candidate message BAGMAN has already
    discovered for this ``(mailbox_id, sender_domain)`` — the item's own
    triggering ``source_object_reference`` message INCLUDED, since it is
    also ``CHECKED_NOT_CANDIDATE`` and therefore always covered by the
    same query — is reprocessed IMMEDIATELY: fetched and ingested now,
    not deferred to the next sweep (architect spec's own explicit
    requirement, corrected/broadened by the operational addendum ahead
    of the first real large historical sweep — "the domain-learning
    mechanism is not useful if it only affects future mail"; see
    `services.mailbox.sweep.reprocess_all_historical_candidates_for_domain`).

    Idempotent-safe against a genuine double-submit of the exact same
    decision (mirrors `app/api/routers/needs_you.py::resolve_needs_you_item`'s
    own doctrine) — a real attempt to change an already-decided item to
    a DIFFERENT outcome raises `ConflictError` -> HTTP 409.

    Business logic lives in `_resolve_mailbox_domain_review_core` — this
    handler only resolves the mailbox and re-raises whatever that
    function raises (unchanged HTTP behaviour from before this WO's
    extraction).
    """
    composition = get_composition()
    mailbox = _require_microsoft_mailbox(composition, mailbox_id)
    return _resolve_mailbox_domain_review_core(
        composition, mailbox_id=mailbox_id, mailbox=mailbox, item_id=item_id, payload=payload
    )


class BatchResolveMailboxDomainReviewItem(BaseModel):
    """One entry of a
    ``POST /{mailbox_id}/microsoft/domain-review/batch-resolve`` request
    — the exact same per-item fields
    ``ResolveMailboxDomainReviewRequest`` accepts (see that model's own
    docstring for what each means), minus ``actor_type``/``actor_id``
    (shared once across the whole batch — see the batch request's own
    docstring)."""

    item_id: str
    decision: str  # "ALLOW" | "IGNORE"
    destination_entity_id: Optional[str] = None
    destination_mode: Optional[str] = None  # "FIXED" | "REVIEW_REQUIRED" — required when decision == "ALLOW"
    match_mode: str = MATCH_MODE_EXACT  # "EXACT" | "INCLUDE_SUBDOMAINS"
    processor_hint: Optional[str] = None


class BatchResolveMailboxDomainReviewRequest(BaseModel):
    """Request body for
    ``POST /internal/mailboxes/{mailbox_id}/microsoft/domain-review/batch-resolve``
    — CD-6 bounded Xero-assisted supplier-domain-correlation addendum
    (architect's own framing: "Operator should be able to: select
    several strong Xero-correlated domains; Allow selected -> Infosecurs
    Limited; Ignore selected..."). A real operator action, explicit PER
    ITEM (each entry carries its own `decision`/`destination_*`), batched
    only for UI convenience — never a single blanket decision silently
    applied to every item. `actor_type`/`actor_id` are shared once
    across the whole batch (the same human operator performed every
    decision in one batch submission)."""

    actor_type: str
    actor_id: str
    items: list[BatchResolveMailboxDomainReviewItem]


@router.post("/{mailbox_id}/microsoft/domain-review/batch-resolve")
async def batch_resolve_mailbox_domain_review(mailbox_id: str, payload: BatchResolveMailboxDomainReviewRequest) -> dict[str, Any]:
    """Resolve several ``ITEM_TYPE_MAILBOX_DOMAIN_REVIEW`` Needs You
    items in one call — built to help an operator work through the 90
    domains a real Phase A historical mailbox discovery sweep produced
    more efficiently, once Xero-assisted correlation has enriched each
    item's own metadata (see ``services.xero.supplier_correlation``).

    Every item in `payload.items` goes through the EXACT SAME governed
    path as `resolve_mailbox_domain_review` (single-item) —
    `_resolve_mailbox_domain_review_core`, shared by both endpoints —
    including the same `MailboxDomainRule` creation/update, the same
    immediate historical back-processing on ALLOW
    (`reprocess_all_historical_candidates_for_domain`), and the same
    audit-event emission. There is no cheaper "bulk mode" that skips any
    of that.

    Partial batch success is normal and expected — a documented judgment
    call (this WO's own explicit instruction): a single bad item (a
    non-existent `item_id`, an `item_id` belonging to a DIFFERENT
    mailbox, an already-DISMISSED item, an invalid `decision`, ...) must
    never silently lose or corrupt the other items in the same batch.
    Each entry is processed INDEPENDENTLY, in list order; a
    `core.errors.BagmanError` raised by one entry's own
    `_resolve_mailbox_domain_review_core` call is caught and recorded as
    that entry's own `error`/`error_type` in the response — never
    allowed to abort the loop or roll back any entry already applied.
    This mirrors the ordinary single-item endpoint's own error contract
    per entry (the same exception types, the same messages) — a caller
    that wants "was THIS item ok" reads `results[i]["ok"]`, never the
    overall HTTP status alone (this endpoint always returns 200 as long
    as the request body itself parses — a per-item failure is DATA, not
    a request-level failure).
    """
    composition = get_composition()
    mailbox = _require_microsoft_mailbox(composition, mailbox_id)
    if not payload.items:
        raise ValidationError("batch-resolve requires at least one item in `items`")

    results: list[dict[str, Any]] = []
    for entry in payload.items:
        try:
            single_payload = ResolveMailboxDomainReviewRequest(
                actor_type=payload.actor_type,
                actor_id=payload.actor_id,
                decision=entry.decision,
                destination_entity_id=entry.destination_entity_id,
                destination_mode=entry.destination_mode,
                match_mode=entry.match_mode,
                processor_hint=entry.processor_hint,
            )
            outcome = _resolve_mailbox_domain_review_core(
                composition, mailbox_id=mailbox_id, mailbox=mailbox, item_id=entry.item_id, payload=single_payload
            )
            results.append(
                {
                    "item_id": entry.item_id,
                    "ok": True,
                    "needs_you_item": outcome["needs_you_item"],
                    "mailbox_domain_rule": outcome["mailbox_domain_rule"],
                    "reprocessed_messages": outcome["reprocessed_messages"],
                    "error": None,
                    "error_type": None,
                }
            )
        except BagmanError as exc:
            results.append(
                {
                    "item_id": entry.item_id,
                    "ok": False,
                    "needs_you_item": None,
                    "mailbox_domain_rule": None,
                    "reprocessed_messages": None,
                    "error": str(exc),
                    "error_type": type(exc).__name__,
                }
            )

    succeeded_count = sum(1 for r in results if r["ok"])
    return {
        "mailbox_id": mailbox_id,
        "results": results,
        "count": len(results),
        "succeeded_count": succeeded_count,
        "failed_count": len(results) - succeeded_count,
    }
