"""``/internal/xero/*`` — the Xero OAuth connection lifecycle and
reference-data HTTP surface (CD-6 Slice 2, PID §98.4, architect spec
§1-24).

Thin router, no business logic (the same "thin router, all domain
logic elsewhere" convention ``app/api/routers/needs_you.py``'s own
module docstring establishes, named directly by this WI's own dispatch
as the discipline to follow): every handler here validates/parses the
HTTP request, calls into ``services.xero.*``/
``core.api.BagmanCanonicalAPI``, and renders the returned domain
object's own ``to_dict()``. No canonical invariant (state-machine
legality, uniqueness, idempotency, tenant-substitution prevention) is
enforced here — it all lives in ``services/xero/*.py``.

Server-side-only OAuth exchange (architect spec §3 — critical)
------------------------------------------------------------------------
The browser reaches exactly three of these endpoints directly:
``POST /connect`` (returns a URL to navigate to — never receives a
client_secret or a token), the Xero-redirected
``GET /oauth/callback`` (receives ``code``/``state`` only — the actual
token EXCHANGE, which needs ``client_secret``, happens entirely inside
this server process, never forwarded to or executable from the
browser), and ``POST /{entity_id}/disconnect``. No endpoint in this
router ever accepts a ``tenant_id`` as a REQUEST parameter — the tenant
is always resolved server-side from a prior ``GET /connections`` call
(architect spec §3's explicit anti-tenant-substitution instruction; see
``services/xero/client.py``'s own module docstring).

No raw token value crosses this router in either direction — every
response here renders a domain object's ``to_dict()``, and NONE of
``XeroConnection``/``XeroAccount``/``XeroSyncRun``'s own contracts carry
a token field at all (see ``services/xero/connection.py``'s module
docstring — architect spec §24's "no credentials in browser storage",
which starts with "no credentials ever reach the browser response body
to begin with").

Endpoints
---------
* ``POST /internal/xero/connect`` — begin (or re-begin) a connect flow
  for one entity; returns the Xero authorize URL for the browser to
  navigate to.
* ``GET /internal/xero/oauth/callback`` — the exact path registered in
  the real Xero Developer App, per PID §102.1's own topology decision —
  do not rename without updating that PID section too. Performs all
  real processing (state consumption, token exchange, tenant
  resolution, persistence) then ALWAYS 303-redirects to
  ``/oauth/result`` — never renders HTML at this URL itself (architect
  hardening finding, PID §102.4: OAuth credential material must not
  remain in the browser URL/address bar at rest).
* ``GET /internal/xero/oauth/result`` — the clean, code/state-free
  landing page every ``/oauth/callback`` outcome redirects to. Inert:
  no domain mutation, no state consumption, reachable directly and
  unauthenticated, but only ever displays one of a closed set of
  fixed, hand-written strings (see ``_RESULT_REASONS``) — or, for the
  genuinely-ambiguous multi-tenant-candidate case, an interactive
  picker (see below).
* ``GET /internal/xero/oauth/pending-selection/{selection_id}`` /
  ``POST /internal/xero/oauth/pending-selection/{selection_id}/resolve``
  — the governed tenant-selection broker (architect finding, real live
  acceptance run: ``GET /connections`` can return more than one
  authorised Xero organisation in a single consent grant; array order
  is never identity — see ``services.xero.tenant_selection``'s own
  module docstring for the full three-way resolution).
* ``POST /internal/xero/{entity_id}/disconnect``
* ``POST /internal/xero/{entity_id}/sync`` — "Sync now" (architect spec
  §16).
* ``GET /internal/xero/{entity_id}`` — this entity's connection status,
  honestly reporting "not connected" (architect spec §9) rather than a
  fake/fallback state.
* ``GET /internal/xero/{entity_id}/accounts`` — the synced, eligible
  (by default) account list the GUI's coding dropdown renders from.
* ``GET /internal/xero/{entity_id}/syncs`` — recent sync-run history
  (Settings/Connections tab drill-down, architect spec §16).
"""
from __future__ import annotations

import json
import os
import urllib.parse
from typing import Any, Optional

from fastapi import APIRouter, HTTPException
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from pydantic import BaseModel

from app.api.composition import get_composition
from core import identity
from core.errors import (
    ConflictError,
    InvalidStateTransitionError,
    NotFoundError,
    OAuthStateError,
    TenantSelectionError,
    ValidationError,
)
from services.xero.ai_suggestion import UNRESOLVED, resolve_ai_suggested_account
from services.xero.client import XeroConnectionInfo, XeroOutcomeStatus
from services.xero.connection import XeroConnection
from services.xero.eligibility import list_eligible_accounts
from services.xero.oauth_state import consume_state
from services.xero.sync import is_reference_data_stale, run_sync
from services.xero.tenant_selection import TenantCandidate

router = APIRouter(prefix="/internal/xero")

#: The EXACT path Xero's own OAuth redirect targets — PID §102.2's
#: CORRECTED topology (superseding §102.1's original `http://
#: localhost:8200/...` plan): Xero's `http://localhost` redirect-URI
#: testing exception applies only to its PKCE-only "Mobile or desktop
#: app" client type, never to BAGMAN's server-side confidential "Web
#: app" registration — confirmed live when Matt's own attempt to
#: register the `http://localhost:8200/...` value was rejected by
#: Xero's own app-registration UI ("must use https"). The real,
#: registered redirect URI is therefore HTTPS, on the dedicated
#: loopback-only TLS terminator (`bagman-xero-oauth-tls`, Caddy, port
#: 8543 — see the Mac's own `bagman.docker-compose.yml`), reached via
#: an SSH local port-forward from Matt's machine
#: (`ssh -L 8543:localhost:8543 matt@<mac-host>`), NOT the plain-HTTP
#: LAN-facing GUI port 8200. Overridable via `BAGMAN_XERO_REDIRECT_URI`
#: purely so a disposable test/dev instance on a different port never
#: has to fight this literal; production never sets the override — the
#: registered Xero Developer App redirect URI must match this exact
#: default.
_DEFAULT_REDIRECT_URI = "https://localhost:8543/internal/xero/oauth/callback"


def _redirect_uri() -> str:
    return os.environ.get("BAGMAN_XERO_REDIRECT_URI", _DEFAULT_REDIRECT_URI)


class ConnectRequest(BaseModel):
    entity_id: str
    actor_type: str
    actor_id: str


class DisconnectRequest(BaseModel):
    actor_type: str
    actor_id: str


class SyncRequest(BaseModel):
    actor_type: str
    actor_id: str


class ResolveTenantSelectionRequest(BaseModel):
    #: Validated server-side against the EXACT candidate set Xero
    #: authorised for this flow — never trusted as an arbitrary
    #: browser-supplied tenant_id (architect requirement, verbatim:
    #: "the browser must not be able to substitute an arbitrary tenant
    #: ID" — see `resolve_pending_tenant_selection`'s own docstring).
    tenant_id: str


class ResolveSuggestionRequest(BaseModel):
    """Body for the AI-suggestion-constraint check endpoint (architect
    spec §6) — see :func:`resolve_suggested_account`'s own docstring."""

    suggested_account_id: Optional[str] = None


def _require_entity(composition, entity_id: str) -> None:
    """Confirm `entity_id` is a real canonical `GovernedEntity` before
    doing anything else — an honest 404 for a typo'd/unknown entity_id,
    never a connection created against nothing."""
    composition.api.entity_repository.get_entity(entity_id)  # raises NotFoundError if unknown


@router.post("/connect", status_code=201)
async def connect(payload: ConnectRequest) -> dict[str, Any]:
    """Begin (or re-begin) an OAuth connect flow for `entity_id`
    (architect spec §3): mints a real, cryptographically random `state`
    value tied to `entity_id`, transitions the entity's `XeroConnection`
    into `PENDING`, and returns the full Xero authorize URL.

    Raises `core.errors.ValidationError` (-> HTTP 422) if Xero is not
    configured yet (no `client_id`/`client_secret` at
    `/opt/bagman/secrets/xero/` — real for this dispatch: no Xero
    Developer App has been registered yet) — checked BEFORE any state
    is minted, so a connect attempt while unconfigured never leaves a
    dangling `PENDING` row nothing can ever complete.
    """
    composition = get_composition()
    _require_entity(composition, payload.entity_id)

    if not composition.xero_oauth_client.is_configured():
        raise ValidationError(
            "Xero is not configured yet — no client_id/client_secret found at "
            "/opt/bagman/secrets/xero/ (no Xero Developer App has been registered). "
            "This is expected until the PL provisions real credentials; see the delivery "
            "report for exactly what needs to be placed there."
        )

    connection = composition.xero_connection_repository.begin_connect(entity_id=payload.entity_id)
    oauth_state = composition.oauth_state_repository.create_state(entity_id=payload.entity_id)
    authorize_url = composition.xero_oauth_client.build_authorize_url(
        state=oauth_state.state, redirect_uri=_redirect_uri()
    )

    composition.api.record_audit_event(
        event_type="XERO_CONNECTION_CONNECT_INITIATED",
        actor_type=payload.actor_type,
        actor_id=payload.actor_id,
        subject_type="XeroConnection",
        subject_id=connection.xero_connection_id,
        # `correlation_id` = this connection's OWN canonical id — the
        # one stable, valid-shaped identifier (unlike the OAuth `state`
        # token, which is deliberately NOT canonical-identifier-shaped,
        # see `services.xero.oauth_state`'s own docstring) shared by
        # every audit event across this connect attempt AND the later
        # `GET /oauth/callback` that completes it (both reference the
        # SAME `xero_connection_id`, since `begin_connect` reuses one
        # row per entity rather than minting a new one each time).
        correlation_id=connection.xero_connection_id,
        causation_id=None,
        payload={"entity_id": payload.entity_id, "state": oauth_state.state},
    )

    return {
        "xero_connection_id": connection.xero_connection_id,
        "entity_id": payload.entity_id,
        "authorize_url": authorize_url,
        "state_expires_at": oauth_state.expires_at.isoformat(),
    }


#: Closed set of landing-page reason keys (architect hardening finding —
#: "the OAuth callback endpoint itself should not return a normal HTML
#: page while OAuth credential material remains in the browser URL").
#: `GET /oauth/callback` now redirects (303) to `GET /oauth/result`
#: instead of rendering HTML directly, so the outcome text has to
#: travel as a query parameter on that redirect — but `/oauth/result`
#: is itself a public, unauthenticated GET endpoint an attacker can hit
#: DIRECTLY, bypassing `/oauth/callback` entirely. A free-text
#: `message` query parameter would therefore reopen exactly the
#: "never render anything caller-controlled via unescaped HTML"
#: invariant this module always documented (see the prior
#: `_callback_page` docstring this replaces) — an attacker could inject
#: arbitrary HTML/JS via `?message=<script>...`. This closed KEY set
#: closes that: only a recognised key selects a fixed, hand-written
#: string below; an unrecognised key (or none) falls back to a generic
#: message, never anything request-supplied. Any dynamic detail (a real
#: provider error string, a specific tenant name, a conflicting
#: entity_id) stays server-side only — in the audit event payload and
#: the `XeroConnection.error_detail` field, both reachable only through
#: authenticated internal endpoints, never through this public landing
#: page.
_RESULT_REASONS: dict[str, str] = {
    "missing_state": "Xero did not return a state value — rejected.",
    "invalid_state": "This connection link is invalid, expired, or already used.",
    "no_pending_connection": "No pending connection was found for this flow.",
    "missing_code": "Xero did not return an authorization code.",
    "token_exchange_failed": "Could not complete the Xero token exchange. Please try connecting again.",
    "tenant_lookup_failed": "Could not resolve your Xero organisation. Please try connecting again.",
    "no_organisation_authorised": "No Xero organisation was authorised during consent. Please try connecting again.",
    "tenant_conflict": "This Xero organisation is already connected to a different BAGMAN company.",
    "superseded": "This connection was already completed by another request.",
    "connected": "BAGMAN is now connected to Xero.",
    # Architect finding, real live acceptance run (Infosecurs +
    # NoustAI): see services.xero.tenant_selection's own module
    # docstring for the full three-way resolution this closes.
    "no_eligible_tenant": "No newly eligible Xero organisation was available for this company.",
    "tenant_selection_required": "More than one Xero organisation was authorised — choose which one.",
}
_UNKNOWN_REASON_MESSAGE = "Something went wrong with this connection attempt."


def _result_page(*, ok: bool, reason: str, selection_id: str = "") -> HTMLResponse:
    """The plain, honest landing page a human completing the SSH-tunnel
    OAuth consent step (PID §102.1) actually sees — served at
    `GET /oauth/result`, never at `/oauth/callback` itself (see that
    endpoint's own docstring for why). `reason` is looked up against
    the closed :data:`_RESULT_REASONS` set above; never rendered as
    raw request-supplied text. `reason == "tenant_selection_required"`
    with a non-empty `selection_id` renders the real operator picker
    (:func:`_tenant_selection_page`) instead of the plain static
    message — the ONE case where this landing page is interactive
    rather than inert (architect requirement: "create a governed
    tenant-selection step... show the operator the returned Xero
    organisation names")."""
    if reason == "tenant_selection_required" and selection_id:
        return _tenant_selection_page(selection_id)
    colour = "#1a7f37" if ok else "#b42318"
    message = _RESULT_REASONS.get(reason, _UNKNOWN_REASON_MESSAGE)
    return HTMLResponse(
        f"<!doctype html><html><body style='font-family: system-ui; padding: 2rem;'>"
        f"<h1 style='color:{colour}'>{'Connected' if ok else 'Connection failed'}</h1>"
        f"<p>{message}</p><p>You can close this tab and return to BAGMAN.</p>"
        f"</body></html>"
    )


def _tenant_selection_page(selection_id: str) -> HTMLResponse:
    """A minimal, self-contained operator picker for the genuinely
    ambiguous multi-candidate case — no SPA route change needed, this
    is a standalone plain-HTML page exactly like every other
    `/oauth/result` outcome, just an interactive one. Fetches the real
    candidate list from `GET /oauth/pending-selection/{id}` on load
    (never trusts anything embedded at render time) and submits the
    operator's choice to
    `POST /oauth/pending-selection/{id}/resolve`, which re-validates it
    server-side against the EXACT authorised candidate set (see that
    endpoint's own docstring — architect requirement: "the browser must
    not be able to substitute an arbitrary tenant ID"). This page
    itself decides nothing; it only presents choices and relays the
    operator's pick. `selection_id` is safely encoded via `json.dumps`
    before embedding regardless of its actual character content (in
    practice always a `secrets.token_urlsafe` value, but this page does
    not trust that when rendering — it is, after all, reachable with an
    attacker-supplied `selection_id` too; the WORST case of a bogus one
    here is simply an honest "invalid or expired" message from the read
    endpoint, since no mutation happens until a real resolve call
    re-validates everything again)."""
    safe_selection_id = json.dumps(selection_id)
    return HTMLResponse(
        "<!doctype html><html><body style='font-family: system-ui; padding: 2rem; max-width: 32rem;'>"
        "<h1>Choose the Xero organisation</h1>"
        "<p>More than one Xero organisation was authorised. Choose which one this BAGMAN company connects to.</p>"
        "<div id='candidates'>Loading…</div>"
        "<p id='status'></p>"
        "<script>"
        f"const selectionId = {safe_selection_id};"
        """
const candidatesEl = document.getElementById('candidates');
const statusEl = document.getElementById('status');

async function load() {
  const r = await fetch('/internal/xero/oauth/pending-selection/' + encodeURIComponent(selectionId));
  if (!r.ok) {
    candidatesEl.textContent = 'This selection link is invalid, expired, or already used.';
    return;
  }
  const body = await r.json();
  candidatesEl.innerHTML = '';
  body.candidates.forEach(function (c) {
    const btn = document.createElement('button');
    btn.textContent = c.tenant_name || c.tenant_id;
    btn.style.display = 'block';
    btn.style.margin = '0.5rem 0';
    btn.addEventListener('click', function () { choose(c.tenant_id); });
    candidatesEl.appendChild(btn);
  });
}

async function choose(tenantId) {
  statusEl.textContent = 'Connecting…';
  const r = await fetch('/internal/xero/oauth/pending-selection/' + encodeURIComponent(selectionId) + '/resolve', {
    method: 'POST',
    headers: {'Content-Type': 'application/json'},
    body: JSON.stringify({tenant_id: tenantId}),
  });
  if (r.ok) {
    statusEl.textContent = 'Connected. You can close this tab and return to BAGMAN.';
    candidatesEl.innerHTML = '';
  } else {
    const body = await r.json().catch(function () { return {}; });
    statusEl.textContent = 'Could not complete this connection: ' + (body.message || 'please try again.');
  }
}

load();
"""
        "</script>"
        "</body></html>"
    )


def _redirect_to_result(*, ok: bool, reason: str, selection_id: str = "") -> RedirectResponse:
    """Every outcome of `GET /oauth/callback` — success or failure —
    ends here instead of rendering HTML directly at the callback URL
    itself (architect hardening finding, Slice 2 live acceptance
    review: 'the OAuth callback endpoint itself should not return a
    normal HTML page while OAuth credential material remains in the
    browser URL'). A 303 redirect means the address bar Matt actually
    SEES at rest — after the redirect completes, on refresh, if
    bookmarked — is the clean `/oauth/result?...` URL, which never
    contains Xero's `code`/`state` (those were only ever query
    parameters on the ORIGINAL `/oauth/callback` request, consumed
    entirely server-side before this redirect is issued; no HTML is
    ever rendered at that URL for a Referer header or a rendered-page
    bookmark to later leak them from). `reason` is one of the closed
    :data:`_RESULT_REASONS` keys — never raw text — so this redirect
    target carries no caller-controlled content either (see
    `_result_page`'s own docstring for why that matters: `/oauth/result`
    is a public GET endpoint an attacker could hit directly).
    `selection_id` is only ever a value THIS server just minted via
    :class:`services.xero.tenant_selection.PendingTenantSelectionStore`
    — an opaque, unguessable correlation token, not caller-controlled
    content in the injection sense (see `_tenant_selection_page`'s own
    docstring for how it is still handled defensively regardless)."""
    query = {"ok": "true" if ok else "false", "reason": reason}
    if selection_id:
        query["selection_id"] = selection_id
    return RedirectResponse(url=f"/internal/xero/oauth/result?{urllib.parse.urlencode(query)}", status_code=303)


@router.get("/oauth/result")
async def oauth_result(ok: bool = False, reason: str = "", selection_id: str = "") -> HTMLResponse:
    """The clean, code/state-free landing page every `GET
    /oauth/callback` outcome redirects to (see
    :func:`_redirect_to_result`). Reads ONLY `ok`/`reason`/
    `selection_id` from ITS OWN query string — never `code`/`state` —
    and performs NO domain mutation, no state consumption, no token
    exchange at all; this endpoint is purely a static, inert rendering
    step (the one exception — `reason == "tenant_selection_required"`
    renders an INTERACTIVE picker, see `_tenant_selection_page` — is
    still itself inert; the actual mutation happens only via a
    separate, server-validated `POST .../resolve` call). A direct,
    unauthenticated GET here (bypassing `/oauth/callback` entirely) can
    therefore only ever display one of this module's own fixed,
    hand-written strings — never inject content or change any real
    connection state — which is exactly why `reason` is validated
    against the closed :data:`_RESULT_REASONS` set rather than trusted
    as free text."""
    return _result_page(ok=ok, reason=reason, selection_id=selection_id)


def _eligible_candidates(
    composition: Any, entity_id: str, connections: tuple[XeroConnectionInfo, ...]
) -> list[XeroConnectionInfo]:
    """Architect finding, real live acceptance run (Infosecurs +
    NoustAI): `GET /connections` can return MORE than one Xero
    organisation authorised in a single consent grant, and array order
    carries no identity meaning — never select `connections[0]`
    unconditionally (the original implementation's real defect: with
    Infosecurs already connected, a NoustAI consent grant whose Xero
    login also has access to Infosecurs could return BOTH organisations
    here; `connections[0]` could then have attempted to remap
    Infosecurs's own already-bound tenant onto NoustAI — the existing
    tenant-uniqueness constraint correctly REJECTED that, but left the
    operator with an opaque conflict instead of a real resolution).

    Narrows to tenants NOT already mapped to a DIFFERENT BAGMAN entity.
    A tenant already mapped to THIS SAME `entity_id` is also eligible —
    a harmless re-confirmation (e.g. a restarted flow re-authorising
    the same organisation), never a conflict."""
    eligible: list[XeroConnectionInfo] = []
    for candidate in connections:
        existing = composition.xero_connection_repository.get_by_tenant(candidate.tenant_id)
        if existing is None or existing.entity_id == entity_id:
            eligible.append(candidate)
    return eligible


def _complete_with_tenant(
    composition: Any,
    connection: XeroConnection,
    entity_id: str,
    tenant: TenantCandidate,
    *,
    access_token: str,
    refresh_token: str,
    token_expires_at: Any,
) -> tuple[bool, str]:
    """The actual tenant-binding completion — shared by the
    auto-selected single-eligible-candidate path in `oauth_callback`
    AND the operator-driven
    `POST /oauth/pending-selection/{selection_id}/resolve` endpoint.
    Both reach exactly this same logic once a SPECIFIC tenant has been
    resolved, whether automatically (it was the only eligible
    candidate) or by explicit, server-validated operator choice.
    Returns `(ok, reason_key)` — `reason_key` is always one of the
    closed :data:`_RESULT_REASONS` keys. Never itself builds an HTTP
    response: callers render for their own transport (a browser
    redirect for the callback path, a JSON body for the picker's own
    fetch-driven resolve endpoint)."""
    try:
        completed = composition.xero_connection_repository.complete_connect(
            connection.xero_connection_id,
            tenant_id=tenant.tenant_id,
            tenant_name=tenant.tenant_name,
            token_expires_at=token_expires_at,
        )
    except ConflictError as exc:
        composition.xero_connection_repository.fail_connect(connection.xero_connection_id, error_detail=str(exc))
        composition.api.record_audit_event(
            event_type="XERO_CONNECTION_CONNECT_FAILED",
            actor_type="EXTERNAL_SYSTEM",
            actor_id="xero-oauth-callback",
            subject_type="XeroConnection",
            subject_id=connection.xero_connection_id,
            correlation_id=connection.xero_connection_id,
            causation_id=None,
            payload={"entity_id": entity_id, "reason": str(exc)},
        )
        return False, "tenant_conflict"
    except InvalidStateTransitionError:
        # See the module's own long-form note (originally on this exact
        # except-clause in `oauth_callback`) for the full "superseded
        # callback" reasoning — identical here: never flip an
        # already-good CONNECTED row to ERROR just because this
        # particular completion attempt lost a race.
        composition.api.record_audit_event(
            event_type="XERO_CONNECTION_CALLBACK_SUPERSEDED",
            actor_type="EXTERNAL_SYSTEM",
            actor_id="xero-oauth-callback",
            subject_type="XeroConnection",
            subject_id=connection.xero_connection_id,
            correlation_id=connection.xero_connection_id,
            causation_id=None,
            payload={"entity_id": entity_id, "connection_status_at_callback": connection.status},
        )
        return False, "superseded"

    composition.xero_token_store.write(
        entity_id, access_token=access_token, refresh_token=refresh_token, expires_at=token_expires_at
    )
    composition.api.record_audit_event(
        event_type="XERO_CONNECTION_CONNECTED",
        actor_type="EXTERNAL_SYSTEM",
        actor_id="xero-oauth-callback",
        subject_type="XeroConnection",
        subject_id=completed.xero_connection_id,
        correlation_id=connection.xero_connection_id,
        causation_id=None,
        payload={"entity_id": entity_id, "tenant_id": tenant.tenant_id, "tenant_name": tenant.tenant_name},
    )
    return True, "connected"


@router.get("/oauth/callback")
async def oauth_callback(code: Optional[str] = None, state: Optional[str] = None) -> RedirectResponse:
    """The exact path registered in the real Xero Developer App (PID
    §102.1) — receives `code`/`state` from Xero's own redirect.

    Never trusts a browser-supplied `entity_id`/`tenant_id` — `state`
    is the ONLY thing this handler trusts to know which entity this
    flow was for (via :func:`services.xero.oauth_state.consume_state`),
    and `tenant_id` is resolved entirely server-side via
    `GET /connections` after a successful token exchange (architect
    spec §3).

    Always ends in a 303 redirect to `GET /oauth/result` — never
    renders HTML at this URL itself (architect hardening finding: "the
    OAuth callback endpoint itself should not return a normal HTML page
    while OAuth credential material remains in the browser URL"; see
    :func:`_redirect_to_result`'s own docstring for the full reasoning).
    All processing — state consumption, token exchange, tenant
    resolution, persisting the governed connection/tokens — happens
    here, entirely server-side, BEFORE that redirect is issued; the
    landing page itself performs none of it.
    """
    composition = get_composition()

    if not state:
        return _redirect_to_result(ok=False, reason="missing_state")

    try:
        consumed_state = consume_state(composition.oauth_state_repository, state)
    except OAuthStateError as exc:
        # No real canonical subject exists for a rejected/unknown state
        # (that is exactly WHY it is rejected — see
        # `services.xero.oauth_state.consume_state`'s own docstring) —
        # `subject_id`/`correlation_id` are contract-required to be
        # real, canonical-identifier-shaped values
        # (`contracts/audit/bagman.audit_event.v1.schema.json`), which
        # the raw `state` string deliberately is NOT (it is a
        # `secrets.token_urlsafe` value, not a UUIDv7). A fresh
        # synthetic id is minted for THIS audit event alone; the actual
        # rejected `state` value is preserved verbatim in `payload`
        # (open, unconstrained) so the forensic record still names
        # exactly what was presented.
        synthetic_id = identity.generate_id()
        composition.api.record_audit_event(
            event_type="XERO_CONNECTION_OAUTH_STATE_REJECTED",
            actor_type="EXTERNAL_SYSTEM",
            actor_id="xero-oauth-callback",
            subject_type="OAuthState",
            subject_id=synthetic_id,
            correlation_id=synthetic_id,
            causation_id=None,
            payload={"reason": str(exc), "rejected_state_value": state},
        )
        return _redirect_to_result(ok=False, reason="invalid_state")

    entity_id = consumed_state.entity_id
    connection = composition.xero_connection_repository.get_by_entity(entity_id)
    if connection is None:
        return _redirect_to_result(ok=False, reason="no_pending_connection")

    def _fail(reason_key: str, detail: str) -> RedirectResponse:
        composition.xero_connection_repository.fail_connect(connection.xero_connection_id, error_detail=detail)
        composition.api.record_audit_event(
            event_type="XERO_CONNECTION_CONNECT_FAILED",
            actor_type="EXTERNAL_SYSTEM",
            actor_id="xero-oauth-callback",
            subject_type="XeroConnection",
            subject_id=connection.xero_connection_id,
            correlation_id=connection.xero_connection_id,
            causation_id=None,
            payload={"entity_id": entity_id, "reason": detail},
        )
        return _redirect_to_result(ok=False, reason=reason_key)

    if not code:
        return _fail("missing_code", "Xero did not return an authorization code.")

    token_result = composition.xero_oauth_client.exchange_code(code=code, redirect_uri=_redirect_uri())
    if token_result.status != XeroOutcomeStatus.OK or token_result.tokens is None:
        return _fail(
            "token_exchange_failed",
            f"token exchange failed: {token_result.error_detail or token_result.status.value}",
        )

    connections_result = composition.xero_oauth_client.list_connections(
        access_token=token_result.tokens.access_token
    )
    if connections_result.status != XeroOutcomeStatus.OK:
        return _fail(
            "tenant_lookup_failed",
            f"could not resolve your Xero organisation: {connections_result.error_detail}",
        )
    if not connections_result.connections:
        return _fail("no_organisation_authorised", "no Xero organisation was authorised during consent.")

    # Architect finding, real live acceptance run (Infosecurs +
    # NoustAI): `connections_result.connections[0]` (the original
    # implementation) is a genuine defect — array order carries no
    # identity meaning, and with Infosecurs already connected, a
    # NoustAI consent grant whose Xero login also has visibility into
    # Infosecurs could return BOTH organisations here. Deterministic,
    # governed resolution instead — see `_eligible_candidates`'s own
    # docstring for the full reasoning.
    eligible = _eligible_candidates(composition, entity_id, connections_result.connections)

    if not eligible:
        # Zero eligible candidates: every authorised tenant is already
        # bound to a DIFFERENT BAGMAN entity. Never mutate another
        # company's connection, never persist these freshly-exchanged
        # tokens as ANYONE's usable credentials (architect requirement,
        # verbatim: "do not mutate another company's connection... do
        # not persist the newly exchanged token set... fail honestly").
        return _fail(
            "no_eligible_tenant",
            "no eligible Xero organisation remained unmapped after excluding tenants already bound to a "
            "different BAGMAN entity",
        )

    if len(eligible) == 1:
        # Exactly one eligible candidate: no ambiguity — complete
        # directly, exactly as the single-tenant path always has.
        chosen = eligible[0]
        ok, reason_key = _complete_with_tenant(
            composition,
            connection,
            entity_id,
            TenantCandidate(tenant_id=chosen.tenant_id, tenant_name=chosen.tenant_name),
            access_token=token_result.tokens.access_token,
            refresh_token=token_result.tokens.refresh_token,
            token_expires_at=token_result.tokens.expires_at,
        )
        return _redirect_to_result(ok=ok, reason=reason_key)

    # More than one eligible, unmapped candidate: genuinely ambiguous —
    # architect requirement, verbatim: "NEVER choose by ordering;
    # create a governed tenant-selection step." The freshly-exchanged
    # tokens are held ONLY on this in-memory bridge record until the
    # operator's choice is server-validated (see
    # `services.xero.tenant_selection`'s own module docstring) — no
    # token is written as this entity's usable credentials yet.
    selection = composition.xero_pending_tenant_selection_store.create(
        entity_id=entity_id,
        xero_connection_id=connection.xero_connection_id,
        candidates=tuple(TenantCandidate(tenant_id=c.tenant_id, tenant_name=c.tenant_name) for c in eligible),
        access_token=token_result.tokens.access_token,
        refresh_token=token_result.tokens.refresh_token,
        token_expires_at=token_result.tokens.expires_at,
    )
    composition.api.record_audit_event(
        event_type="XERO_CONNECTION_TENANT_SELECTION_REQUIRED",
        actor_type="EXTERNAL_SYSTEM",
        actor_id="xero-oauth-callback",
        subject_type="XeroConnection",
        subject_id=connection.xero_connection_id,
        correlation_id=connection.xero_connection_id,
        causation_id=None,
        payload={"entity_id": entity_id, "candidate_tenant_ids": [c.tenant_id for c in eligible]},
    )
    return _redirect_to_result(ok=False, reason="tenant_selection_required", selection_id=selection.selection_id)


@router.get("/oauth/pending-selection/{selection_id}")
async def get_pending_tenant_selection(selection_id: str) -> dict:
    """Read-only: the real candidate tenant id/name pairs for a
    genuinely-ambiguous multi-organisation OAuth consent (see
    `services.xero.tenant_selection`'s own module docstring). Public,
    unauthenticated, and reachable directly — but performs NO mutation
    and discloses only tenant NAME/ID pairs Xero itself already showed
    the human operator on its own consent screen moments earlier for
    THIS exact flow; never a token, never a client_secret."""
    composition = get_composition()
    selection = composition.xero_pending_tenant_selection_store.get(selection_id)
    if selection is None:
        # Unknown, already consumed, or expired -- indistinguishable
        # by design (see `services.xero.tenant_selection`'s own module
        # docstring: a consumed selection is fully removed, not merely
        # flagged, and an expired one is purged on every access).
        raise TenantSelectionError(f"no pending Xero tenant selection '{selection_id}'")
    return {
        "selection_id": selection.selection_id,
        "candidates": [{"tenant_id": c.tenant_id, "tenant_name": c.tenant_name} for c in selection.candidates],
        "expires_at": selection.expires_at.isoformat(),
    }


@router.post("/oauth/pending-selection/{selection_id}/resolve")
async def resolve_pending_tenant_selection(selection_id: str, payload: ResolveTenantSelectionRequest) -> Any:
    """The operator's governed choice, from the picker
    (`_tenant_selection_page`) — architect requirements, verbatim:
    "selection must be validated server-side against the exact
    candidate set returned by Xero for this OAuth flow; the browser
    must not be able to substitute an arbitrary tenant ID."

    :meth:`PendingTenantSelectionStore.consume` is the ONE authoritative
    operation here — under a single lock it finds the record, rejects
    unknown/expired, validates `payload.tenant_id` against the exact
    frozen candidate set, and ATOMICALLY removes the record from the
    store before returning it (architect correction, PID §102.4: an
    earlier version of this module kept a token-bearing record around
    after resolution — real process-lifetime raw-token retention,
    fixed by making resolution consume-and-remove, never mark-and-
    retain; see `services.xero.tenant_selection`'s own module
    docstring for the full reasoning). After this call returns
    successfully, the store holds no raw token for this selection
    anywhere — a replay of the same `selection_id` fails simply because
    the record no longer exists.

    On success, completes the connection via the SAME
    :func:`_complete_with_tenant` logic the single-eligible-candidate
    auto-path uses — the access/refresh tokens used are the ones THIS
    server captured at the original callback, from THIS now-removed
    selection's own local copy, never anything the browser supplies. If
    that completion unexpectedly fails (a tenant conflict, a superseded
    connection), the tokens simply fall out of scope with this request
    and are never retained for a retry — the operator restarts the
    OAuth flow from the GUI's own already-supported "PENDING → Restart
    Xero connection" action (correctness/security over retry
    convenience, a deliberate trade-off, not an oversight).
    """
    composition = get_composition()
    store = composition.xero_pending_tenant_selection_store

    consumed_selection = store.consume(selection_id, payload.tenant_id)

    connection = composition.xero_connection_repository.get_connection(consumed_selection.xero_connection_id)
    chosen = next(c for c in consumed_selection.candidates if c.tenant_id == payload.tenant_id)

    ok, reason_key = _complete_with_tenant(
        composition,
        connection,
        consumed_selection.entity_id,
        chosen,
        access_token=consumed_selection.access_token,
        refresh_token=consumed_selection.refresh_token,
        token_expires_at=consumed_selection.token_expires_at,
    )
    if not ok:
        message = _RESULT_REASONS.get(reason_key, _UNKNOWN_REASON_MESSAGE)
        return JSONResponse(status_code=409, content={"error_code": reason_key.upper(), "message": message})

    return {
        "ok": True,
        "entity_id": consumed_selection.entity_id,
        "tenant_id": chosen.tenant_id,
        "tenant_name": chosen.tenant_name,
    }


@router.post("/{entity_id}/disconnect")
async def disconnect(entity_id: str, payload: DisconnectRequest) -> dict:
    composition = get_composition()
    _require_entity(composition, entity_id)

    connection = composition.xero_connection_repository.get_by_entity(entity_id)
    if connection is None:
        raise NotFoundError(f"entity '{entity_id}' has no XeroConnection to disconnect")

    updated = composition.xero_connection_repository.disconnect(connection.xero_connection_id)
    composition.xero_token_store.delete(entity_id)

    composition.api.record_audit_event(
        event_type="XERO_CONNECTION_DISCONNECTED",
        actor_type=payload.actor_type,
        actor_id=payload.actor_id,
        subject_type="XeroConnection",
        subject_id=updated.xero_connection_id,
        correlation_id=updated.xero_connection_id,
        causation_id=None,
        payload={"entity_id": entity_id},
    )
    return updated.to_dict()


@router.post("/{entity_id}/sync")
async def sync_now(entity_id: str, payload: SyncRequest) -> dict:
    """"Sync now" (architect spec §16). Always returns a terminal
    `XeroSyncRun` — see `services.xero.sync.run_sync`'s own docstring
    for the full outcome taxonomy; a provider/auth/rate-limit failure
    is a normal 200 response carrying a `FAILED` run, never an HTTP
    error, since the request itself was handled correctly (the SYNC
    failed, the API call did not)."""
    composition = get_composition()
    _require_entity(composition, entity_id)

    run = run_sync(
        entity_id=entity_id,
        connection_repository=composition.xero_connection_repository,
        account_repository=composition.xero_account_repository,
        sync_run_repository=composition.xero_sync_run_repository,
        accounting_client=composition.xero_accounting_client,
        oauth_client=composition.xero_oauth_client,
        token_store=composition.xero_token_store,
    )

    composition.api.record_audit_event(
        event_type="XERO_SYNC_SUCCEEDED" if run.status == "SUCCEEDED" else "XERO_SYNC_FAILED",
        actor_type=payload.actor_type,
        actor_id=payload.actor_id,
        subject_type="XeroSyncRun",
        subject_id=run.sync_run_id,
        correlation_id=run.sync_run_id,
        causation_id=None,
        payload={
            "entity_id": entity_id,
            "status": run.status,
            "accounts_seen_count": run.accounts_seen_count,
            "error_code": run.error_code,
        },
    )
    return run.to_dict()


@router.get("/{entity_id}")
async def get_connection_status(entity_id: str) -> dict[str, Any]:
    """Honest connection status for `entity_id` (architect spec §9 —
    "Xero not connected" shown exactly as that, never a fake/placeholder
    fallback). Never renders a token — see module docstring."""
    composition = get_composition()
    _require_entity(composition, entity_id)

    connection = composition.xero_connection_repository.get_by_entity(entity_id)
    if connection is None:
        # Architect finding, live acceptance run: the GUI rendered the
        # literal string "undefined account(s) synced" for an entity
        # with no XeroConnection at all (e.g. Matthew Scott Personal) —
        # root cause was THIS response omitting `account_count`/
        # `reference_data_stale` entirely rather than reporting them
        # honestly as zero/false, unlike `GET /{entity_id}/accounts`
        # just below, which already returns `count: 0, items: []` for
        # its own disconnected case. A never-connected entity genuinely
        # has zero synced accounts — reporting that plainly is honest,
        # not a fake fallback (architect spec §9's own doctrine),
        # and gives every caller (GUI included) one uniform response
        # shape regardless of connection state.
        return {
            "entity_id": entity_id,
            "connected": False,
            "connection": None,
            "account_count": 0,
            "reference_data_stale": False,
        }

    account_count = len(composition.xero_account_repository.list_accounts(entity_id=entity_id))
    return {
        "entity_id": entity_id,
        "connected": connection.status == "CONNECTED",
        "connection": connection.to_dict(),
        "account_count": account_count,
        "reference_data_stale": is_reference_data_stale(connection),
    }


@router.get("/{entity_id}/accounts")
async def list_accounts(entity_id: str, eligible_only: bool = True) -> dict[str, Any]:
    """The synced Chart-of-Accounts for `entity_id` (architect spec
    §8/§9/§10's coding dropdown). `eligible_only=true` (default) applies
    `services.xero.eligibility.list_eligible_accounts`'s documented
    default-visibility policy; `eligible_only=false` returns every
    synced account regardless (used by the Settings/Connections tab's
    own account-count display, never by the ordinary coding dropdown).

    Returns `connected: False` honestly (never a fake/fallback list)
    when `entity_id` has no `XeroConnection` at all — architect spec
    §9's exact "Xero chart of accounts not connected" requirement.
    """
    composition = get_composition()
    _require_entity(composition, entity_id)

    connection = composition.xero_connection_repository.get_by_entity(entity_id)
    if connection is None:
        return {"entity_id": entity_id, "connected": False, "items": [], "count": 0}

    accounts = composition.xero_account_repository.list_accounts(entity_id=entity_id)
    if eligible_only:
        accounts = list_eligible_accounts(accounts)

    return {
        "entity_id": entity_id,
        "connected": True,
        "tenant_name": connection.tenant_name,
        "items": [a.to_dict() for a in accounts],
        "count": len(accounts),
    }


@router.get("/{entity_id}/syncs")
async def list_syncs(entity_id: str, limit: int = 20) -> dict[str, Any]:
    composition = get_composition()
    _require_entity(composition, entity_id)

    if limit <= 0 or limit > 200:
        raise HTTPException(status_code=422, detail=f"limit must be between 1 and 200 (got {limit})")

    runs = composition.xero_sync_run_repository.list_runs(entity_id=entity_id, limit=limit)
    return {"entity_id": entity_id, "items": [r.to_dict() for r in runs], "count": len(runs)}


@router.post("/{entity_id}/resolve-suggestion")
async def resolve_suggested_account(entity_id: str, payload: ResolveSuggestionRequest) -> dict[str, Any]:
    """Validate one AI-proposed `AccountID` against `entity_id`'s
    CURRENT synced, eligible account set (architect spec §6) — the real
    HTTP-reachable form of `services.xero.ai_suggestion
    .resolve_ai_suggested_account`, exposed so any future AI-integration
    caller (in-process or not) gets the exact same closed-candidate-set
    guarantee this slice's own test suite proves, without needing to
    import `services/xero/` Python directly.
    """
    composition = get_composition()
    _require_entity(composition, entity_id)

    connection = composition.xero_connection_repository.get_by_entity(entity_id)
    candidates = (
        list_eligible_accounts(composition.xero_account_repository.list_accounts(entity_id=entity_id))
        if connection is not None
        else []
    )
    resolution = resolve_ai_suggested_account(payload.suggested_account_id, eligible_candidates=candidates)
    return {
        "entity_id": entity_id,
        "resolved": resolution.resolved,
        "account_id": resolution.account_id if resolution.resolved else UNRESOLVED,
        "reason": resolution.reason,
    }
