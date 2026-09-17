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
  fixed, hand-written strings (see ``_RESULT_REASONS``).
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

import os
import urllib.parse
from typing import Any, Optional

from fastapi import APIRouter, HTTPException
from fastapi.responses import HTMLResponse, RedirectResponse
from pydantic import BaseModel

from app.api.composition import get_composition
from core import identity
from core.errors import ConflictError, InvalidStateTransitionError, NotFoundError, OAuthStateError, ValidationError
from services.xero.ai_suggestion import UNRESOLVED, resolve_ai_suggested_account
from services.xero.client import XeroOutcomeStatus
from services.xero.eligibility import list_eligible_accounts
from services.xero.oauth_state import consume_state
from services.xero.sync import is_reference_data_stale, run_sync

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
}
_UNKNOWN_REASON_MESSAGE = "Something went wrong with this connection attempt."


def _result_page(*, ok: bool, reason: str) -> HTMLResponse:
    """The plain, honest landing page a human completing the SSH-tunnel
    OAuth consent step (PID §102.1) actually sees — served at
    `GET /oauth/result`, never at `/oauth/callback` itself (see that
    endpoint's own docstring for why). `reason` is looked up against
    the closed :data:`_RESULT_REASONS` set above; never rendered as
    raw request-supplied text."""
    colour = "#1a7f37" if ok else "#b42318"
    message = _RESULT_REASONS.get(reason, _UNKNOWN_REASON_MESSAGE)
    return HTMLResponse(
        f"<!doctype html><html><body style='font-family: system-ui; padding: 2rem;'>"
        f"<h1 style='color:{colour}'>{'Connected' if ok else 'Connection failed'}</h1>"
        f"<p>{message}</p><p>You can close this tab and return to BAGMAN.</p>"
        f"</body></html>"
    )


def _redirect_to_result(*, ok: bool, reason: str) -> RedirectResponse:
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
    is a public GET endpoint an attacker could hit directly)."""
    query = urllib.parse.urlencode({"ok": "true" if ok else "false", "reason": reason})
    return RedirectResponse(url=f"/internal/xero/oauth/result?{query}", status_code=303)


@router.get("/oauth/result")
async def oauth_result(ok: bool = False, reason: str = "") -> HTMLResponse:
    """The clean, code/state-free landing page every `GET
    /oauth/callback` outcome redirects to (see
    :func:`_redirect_to_result`). Reads ONLY `ok`/`reason` from ITS OWN
    query string — never `code`/`state` — and performs NO domain
    mutation, no state consumption, no token exchange at all; this
    endpoint is purely a static, inert rendering step. A direct,
    unauthenticated GET here (bypassing `/oauth/callback` entirely) can
    therefore only ever display one of this module's own fixed,
    hand-written strings — never inject content or change any real
    connection state — which is exactly why `reason` is validated
    against the closed :data:`_RESULT_REASONS` set rather than trusted
    as free text."""
    return _result_page(ok=ok, reason=reason)


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

    # Architect spec §3/§17: exactly one BAGMAN entity <-> one Xero
    # tenant. If more than one organisation was authorised in one
    # consent grant, this slice connects to the FIRST one Xero reports
    # — a documented judgment call (see the delivery report): Xero's
    # own consent screen lets an operator select which organisation(s)
    # to authorise, and a future delivery could offer a picker here if
    # multi-organisation consent turns out to be common; today's single
    # BAGMAN-entity-per-connect flow has no natural place to ask "which
    # one" mid-callback without a second round trip.
    tenant = connections_result.connections[0]

    try:
        completed = composition.xero_connection_repository.complete_connect(
            connection.xero_connection_id,
            tenant_id=tenant.tenant_id,
            tenant_name=tenant.tenant_name,
            token_expires_at=token_result.tokens.expires_at,
        )
    except ConflictError as exc:
        return _fail("tenant_conflict", str(exc))
    except InvalidStateTransitionError:
        # A genuinely valid, honestly-consumed `state` (never a replay
        # of an already-consumed value -- `consume_state` above already
        # proved that) whose CONNECTION is no longer in a status
        # `complete_connect` can act on. The real, live-findable shape
        # of this (architect finding, Slice 2 acceptance review, "a
        # PENDING connection can become an operator dead-end... verify
        # the existing OAuth-state race/replay protections... ensure
        # replayed/expired superseded callbacks fail honestly"): an
        # operator restarts a PENDING flow (a second, independently
        # valid `state` is minted -- see `connect()` above, which now
        # supports this from the GUI), and a STALE browser tab from the
        # FIRST, abandoned attempt is also later completed (a lingering
        # tab, browser back/forward, or Xero's own still-authenticated
        # session auto-completing). Both `state` values are correctly,
        # independently single-use-consumed by `consume_state` -- but
        # only the FIRST callback to actually reach this point may
        # transition the connection; a second, superseded completion
        # must fail honestly WITHOUT corrupting an already-successful
        # connection. Deliberately NOT `_fail(...)`: that call flips the
        # connection to `ERROR`, which would be actively wrong here --
        # the connection this second callback names is, in the case
        # that actually matters, already genuinely `CONNECTED`, and a
        # duplicate/stale browser tab is not evidence anything is
        # broken with it. No connection mutation occurs on this path at
        # all.
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
        return _redirect_to_result(ok=False, reason="superseded")

    composition.xero_token_store.write(
        entity_id,
        access_token=token_result.tokens.access_token,
        refresh_token=token_result.tokens.refresh_token,
        expires_at=token_result.tokens.expires_at,
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

    return _redirect_to_result(ok=True, reason="connected")


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
        return {"entity_id": entity_id, "connected": False, "connection": None}

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
