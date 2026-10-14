"""``bagman-api`` — the FastAPI application instance (CD-3 WI-3, PID §24;
static UI mounting added CD-4 WI-4, PID §36-42).

Responsibilities of this module, and only this module:

* build the ``FastAPI`` app and include every router in ``routers/``;
* configure structured logging (``app.api.logging_config``) once,
  at import time, before anything else runs;
* attach one request-scoped correlation-id (PID §27) to every request/
  response, generating a fresh one when a caller does not supply
  ``X-Correlation-Id``, and emit ONE sanitized, generic request-
  completion log line per request (method + URL path ONLY, status,
  duration — NEVER the query string, for any route: see
  ``correlation_id_middleware``'s own inline comment and
  ``app/api/logging_config.py``'s own docstring for the access-log leak
  this replaces);
* serve the first BAGMAN Documents GUI (CD-4 WI-4) as plain static
  assets (HTML/CSS/vanilla-JS — no framework, no build step, no Node
  runtime; PID §42) from ``app/api/static/``, mounted at ``/`` via
  Starlette's ``StaticFiles`` — deliberately mounted LAST, after every
  ``app.include_router(...)`` call below, so every existing JSON route
  (``/internal/*``, ``/health``, ``/ready``, ``/version``, plus
  FastAPI's own ``/docs``/``/openapi.json``) is matched first and keeps
  working exactly as before; the static mount only ever answers a
  request no earlier route claimed. ``html=True`` makes ``GET /``
  serve ``static/index.html`` (the BAGMAN shell) and any other
  unmatched path fall through to a 404 from ``StaticFiles`` itself,
  never from the JSON API;
* centralise translation of ``core.errors.BagmanError`` (and any
  unexpected exception) into an HTTP response — the exact mapping the
  WI-3 contract specifies:

  =================================  ===========
  error                              HTTP status
  =================================  ===========
  ``ValidationError``                422
  ``NotFoundError``                  404
  ``ConflictError``                  409
  ``DuplicateExternalReferenceError``409
  ``ImmutabilityViolationError``     409
  ``InvalidProvenanceError``         422
  ``PersistenceError``               503
  ``StorageError``                   503
  ``IdempotencyConflictError``       409
  ``InvalidStateTransitionError``    500
  anything else (incl. IntegrityError)  500
  =================================  ===========

  CD-4 WI-3 additions: ``IdempotencyConflictError`` (PID §25/§35/§53 —
  "a conflicting reuse of a key with different content must fail
  loudly") is a genuine, client-actionable conflict, exactly like
  ``ConflictError``/``ImmutabilityViolationError`` above — 409.
  ``InvalidStateTransitionError`` is explicitly listed here (rather
  than left to fall through to the generic-exception 500 default)
  because it signals an internal orchestration bug (this codebase's own
  state machine attempting an edge its own transition table forbids),
  never a caller error — 500 is the correct, and intentional, mapping,
  spelled out so a future reader does not mistake the omission for an
  oversight.

  A 5xx response body never contains the underlying exception's raw
  message (which, for ``PersistenceError``/``StorageError`` in
  particular, may itself embed a driver/SQLAlchemy/botocore exception
  string — see ``core/errors.py``) — only a generic, safe message plus
  the correlation_id, so an operator can find the full detail in the
  structured server-side log for that same correlation_id. A 4xx
  response body (422/404/409) IS considered safe to return verbatim:
  those messages are constructed by ``core``/``persistence`` from
  caller-supplied field names and canonical IDs only (e.g. "entity_id
  'x' already exists"), never from raw driver/provider exception text.
"""
from __future__ import annotations

import logging
import os
import time
import urllib.parse
import uuid
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from starlette.staticfiles import StaticFiles

from core.auth import SessionStatus, evaluate_session_token, tokens_match
from core.errors import (
    ActiveInvocationConflictError,
    BagmanError,
    ConflictError,
    DuplicateExternalReferenceError,
    IdempotencyConflictError,
    ImmutabilityViolationError,
    InvalidProvenanceError,
    InvalidStateTransitionError,
    NotFoundError,
    OAuthStateError,
    PersistenceError,
    StorageError,
    TenantSelectionError,
    ValidationError,
)
from app.api.logging_config import configure_logging
from app.api.routers import (
    activity,
    ai,
    auth,
    documents,
    evidence_classification,
    health,
    intake,
    internal,
    mailboxes,
    mailboxes_gmail,
    mailboxes_imap,
    mailboxes_microsoft,
    needs_you,
    operator,
    version,
    xero,
)

configure_logging(level=os.environ.get("BAGMAN_LOG_LEVEL", "INFO"))
logger = logging.getLogger("bagman.runtime.api")

app = FastAPI(title="BAGMAN Runtime API", version="1")

app.include_router(health.router)
app.include_router(version.router)
#: WO-BAGMAN-117 §6.A.3/§117.C/§117.G — single-operator
#: login/logout/status. `POST /auth/login` and `GET /auth/status` are
#: public (see `auth_enforcement_middleware`'s own allow-list below);
#: `POST /auth/logout` is protected like every other state-changing
#: route.
app.include_router(auth.router)
app.include_router(internal.router)
app.include_router(intake.router)
#: CD-5 WI-2 — the background AI gateway HTTP surface (PID §68).
app.include_router(ai.router)
#: CD-5 WI-3 — Ask BAGMAN (PID §42-44/§68). New file (app/api/routers/
#: operator.py), never added to routers/ai.py (WI-2's own in-parallel
#: file) — see that router's own docstring.
app.include_router(operator.router)
#: CD-6 Slice 1 — the universal Needs You queue HTTP surface (PID
#: §98.2/§98.5) and the cross-BAGMAN activity/audit stream (PID §98.8).
app.include_router(needs_you.router)
app.include_router(activity.router)
#: CD-6 Slice 2 — the Xero OAuth connection lifecycle and
#: read-only Chart-of-Accounts reference-data HTTP surface (PID
#: §98.4, architect spec §1-24).
app.include_router(xero.router)
#: CD-6 Slice 3 — the mailbox-definition registry HTTP surface (Mailbox
#: Management, TAB 1 / Email): create/list/get/edit/enable/disable/
#: retire only — no provider connectivity of any kind exists behind
#: this router (see app/api/routers/mailboxes.py's own module
#: docstring).
app.include_router(mailboxes.router)
#: CD-6 Slice 4 — the first real mailbox connection lifecycle + sweep
#: engine HTTP surface (Microsoft Graph adapter).
app.include_router(mailboxes_microsoft.router)
#: CD-6 GUI-operations-foundation follow-on WO — the SECOND real
#: mailbox connection lifecycle + sweep engine HTTP surface (plain IMAP
#: adapter, `matt@noust.ai`). Mirrors `mailboxes_microsoft.router`'s own
#: shape exactly — see `app/api/routers/mailboxes_imap.py`'s own module
#: docstring.
app.include_router(mailboxes_imap.router)
#: CD-6 GUI-operations-foundation follow-on WO — the THIRD real mailbox
#: connection lifecycle + sweep engine HTTP surface (Gmail API adapter,
#: two independent accounts). Mirrors `mailboxes_microsoft.router`'s own
#: shape exactly — see `app/api/routers/mailboxes_gmail.py`'s own module
#: docstring.
app.include_router(mailboxes_gmail.router)
#: CD-6 Slice 5 WI-2 — the deterministic (non-AI) evidence-classification
#: HTTP surface (rule preview/create/list/detail/retire, and the
#: manually-invoked deterministic classification endpoint). See
#: app/api/routers/evidence_classification.py's own module docstring —
#: never wired into any automatic sweep/intake path.
app.include_router(evidence_classification.router)
#: CD-6 Slice 5 WI-5 — the read-only, evidence-first Documents
#: projection HTTP surface (GET /internal/documents[/{evidence_id}]).
#: See app/api/routers/documents.py's own module docstring — a pure
#: read composition over existing canonical repositories, never a new
#: persistence authority.
app.include_router(documents.router)

#: CD-4 WI-4 — the BAGMAN Documents GUI (PID §36-42), served as plain
#: static assets. Mounted LAST and at "/" so it never shadows any
#: route registered above (Starlette matches mounted/declared routes
#: in registration order) — see this module's own docstring.
_STATIC_DIR = Path(__file__).resolve().parent / "static"
app.mount("/", StaticFiles(directory=str(_STATIC_DIR), html=True), name="ui")

#: Ordered so a subclass is matched by the most specific applicable
#: entry — every current core.errors.* type is a direct, flat subclass
#: of BagmanError (see core/errors.py's own module docstring), so
#: ordering does not currently matter for correctness, but the explicit
#: per-type mapping (rather than an error_code string switch) is kept
#: so a future error subclass fails loudly (falls through to the 500
#: catch-all below) rather than silently inheriting an unrelated status.
_STATUS_BY_ERROR_TYPE: dict[type[BagmanError], int] = {
    ValidationError: 422,
    NotFoundError: 404,
    ConflictError: 409,
    DuplicateExternalReferenceError: 409,
    ImmutabilityViolationError: 409,
    InvalidProvenanceError: 422,
    PersistenceError: 503,
    StorageError: 503,
    # CD-4 WI-3 additions — see module docstring.
    IdempotencyConflictError: 409,
    InvalidStateTransitionError: 500,
    # CD-5 WI-2/WI-3 addition (both independently needed it): a genuine,
    # client-actionable conflict — a non-terminal AIInvocation already
    # exists for this exact (task_id, task_version,
    # primary_input_reference) subject (PID §73) — exactly the same
    # class of thing ConflictError/IdempotencyConflictError above
    # already map to 409 for. A WI-1 mapping gap this closes rather than
    # works around, since either /internal/ai/tasks (WI-2) or Ask
    # BAGMAN's own /internal/operator/chat (WI-3) would otherwise
    # incorrectly 500 on a duplicate rapid double-submit.
    ActiveInvocationConflictError: 409,
    # CD-6 Slice 2 (PID §98.4, architect spec §3): a rejected OAuth
    # anti-CSRF/replay `state` value is a genuine SECURITY rejection —
    # mapped to 403 (Forbidden), distinct from the 422 an ordinary
    # malformed-input `ValidationError` gets, and distinct from the 409
    # a same-resource-state conflict gets (this is never "resubmit with
    # different content", it is "this request must not be honoured at
    # all").
    OAuthStateError: 403,
    # Architect finding, real live acceptance run (Infosecurs+NoustAI):
    # the governed Xero tenant-selection broker (a rejected/expired/
    # already-resolved selection, or a browser-submitted tenant_id
    # outside the exact authorised candidate set) is the same class of
    # genuine SECURITY rejection OAuthStateError already gets 403 for —
    # see services.xero.tenant_selection's own module docstring.
    TenantSelectionError: 403,
}

#: 403 is client-actionable in the same sense 404/409/422 are (see
#: `_CLIENT_SAFE_STATUSES` below) — the message names exactly what was
#: wrong (unknown/expired/replayed state), never a raw driver/provider
#: exception string.

#: 5xx statuses never return the raw exception message to the client
#: (see module docstring) — only these client-actionable statuses do.
_CLIENT_SAFE_STATUSES = frozenset({403, 404, 409, 422})


def _status_for(exc: BagmanError) -> int:
    for error_type, status in _STATUS_BY_ERROR_TYPE.items():
        if isinstance(exc, error_type):
            return status
    return 500  # anything else, e.g. IntegrityError — PID's literal mapping table


@app.middleware("http")
async def correlation_id_middleware(request: Request, call_next):
    correlation_id = request.headers.get("x-correlation-id") or str(uuid.uuid4())
    request.state.correlation_id = correlation_id
    started_at = time.monotonic()
    response = await call_next(request)
    duration_ms = (time.monotonic() - started_at) * 1000.0
    response.headers["X-Correlation-Id"] = correlation_id

    # BAGMAN's own sanitized, generic (never Gmail-only) request-
    # completion log line — method + URL PATH ONLY + status + duration.
    # NEVER `request.url.query`/the raw query string in any form, for
    # ANY route: a query string may carry an OAuth `code`/`state`
    # (Gmail's, Microsoft's, and Xero's own callbacks all pass these
    # through this exact middleware) and must never reach a log line.
    # This replaces uvicorn's own "uvicorn.access" logger, which is
    # disabled entirely (not merely reformatted) in
    # `app/api/logging_config.py::configure_logging` — see that
    # module's own docstring for why re-routing it through the JSON
    # formatter alone would not have been a real fix.
    logger.info(
        f"{request.method} {request.url.path} -> {response.status_code}",
        extra={
            "component": "bagman.runtime.http",
            "correlation_id": correlation_id,
            "event_type": "REQUEST_COMPLETED",
            "duration_ms": round(duration_ms, 1),
        },
    )
    return response


#: WO-BAGMAN-117 §6.A.6/§117.G — the exhaustive, individually-justified
#: public-route allow-list. Every entry below is named EXACTLY, never a
#: wildcard under "/" (the one deliberate exception, `_PUBLIC_PREFIX`,
#: is documented separately below and is itself scoped to one single,
#: named feature's own two sub-routes — never a generic prefix).
#:
#: Re-verified directly against the real, current router registration
#: order in this exact file (see the `app.include_router(...)` calls
#: above) and the real `@router.get`/`@router.post` decorator lines in
#: each named module, at the exact dispatch SHA this delivery was built
#: against — NOT copied from the WO's own citation without
#: independent re-confirmation (the WO explicitly required this; the
#: WO's own citation turned out to already be stale in one respect: it
#: named bare paths like `/oauth/result`, but every one of these
#: routers is mounted with its own `APIRouter(prefix=...)`, so the REAL
#: path Starlette ever sees is the prefixed one below):
#:
#: * `/health`                                    — health.py:64  `@router.get("/health")` (no prefix) — container healthchecks, run intra-container, never through a browser session.
#: * `/version`                                   — version.py:73 `@router.get("/version")` (no prefix) — same: an operational/ops probe, never a browser-session-gated action.
#: * `/ready`                                      — health.py:69  `@router.get("/ready")` (no prefix) — readiness-equivalent; same reasoning as `/health`.
#: * `/auth/login`                                 — auth.py `@router.post("/login")`, router prefix `/auth` — the login mechanism itself cannot require a session to reach.
#: * `/auth/status`                                — auth.py `@router.get("/status")`, router prefix `/auth` — must be reachable with no session cookie (it is what tells the GUI shell/an operator whether one exists at all).
#: * `/login.html`                                 — `app/api/static/login.html`, served by the `StaticFiles` mount at `/` (see this module's own docstring) — the one page an unauthenticated browser must be able to load. Fully self-contained (inline CSS/JS, no separate `.css`/`.js` asset of its own), so this is the ONLY static path this delivery exempts — never a wildcard under the static mount.
#: * `/internal/xero/oauth/result`                 — xero.py:407 `@router.get("/oauth/result")`, router prefix `/internal/xero` (xero.py:105) — the clean, code/state-free OAuth landing page every `/oauth/callback` outcome redirects to; reachable directly and unauthenticated today (xero.py's own module docstring).
#: * `/internal/xero/oauth/callback`               — xero.py:531 `@router.get("/oauth/callback")`, same prefix — the exact path Xero's own OAuth redirect targets; hit by Xero's authorization server, never by an authenticated browser session.
#: * `/internal/xero/oauth/pending-selection/{selection_id}` and `/internal/xero/oauth/pending-selection/{selection_id}/resolve` — xero.py:692/716, same prefix — the governed multi-tenant candidate-selection broker, reached mid-OAuth-round-trip; covered by `_PUBLIC_PREFIX` below (both the `GET .../{selection_id}` and `POST .../{selection_id}/resolve` legs), not an exact-path entry, since the `{selection_id}` segment is a real path parameter, not static text — the prefix is scoped to exactly this one feature's own sub-path, never a wildcard under `/internal/xero` as a whole. This flow's OWN `selection_id`-scoped state/expiry/replay protection and `_eligible_candidates` multi-tenant-consent disambiguation (xero.py, `services.xero.tenant_selection`) are completely untouched by this middleware — it only ever decides whether the SESSION layer applies to a request, never touching that flow's own independent anti-replay/anti-substitution checks.
#: * `/internal/mailboxes/microsoft/oauth/result`  — mailboxes_microsoft.py:338 `@router.get("/microsoft/oauth/result")`, router prefix `/internal/mailboxes` (mailboxes_microsoft.py:191).
#: * `/internal/mailboxes/microsoft/oauth/callback`— mailboxes_microsoft.py:343 `@router.get("/microsoft/oauth/callback")`, same prefix.
#: * `/internal/mailboxes/gmail/oauth/result`      — mailboxes_gmail.py:222 `@router.get("/gmail/oauth/result")`, router prefix `/internal/mailboxes` (mailboxes_gmail.py:122).
#: * `/internal/mailboxes/gmail/oauth/callback`    — mailboxes_gmail.py:227 `@router.get("/gmail/oauth/callback")`, same prefix.
#:
#: Everything else — the entire rest of the static GUI mount (`/`,
#: `/index.html`, `/app.js`, `/style.css`, `/shell/*`, `/features/*`),
#: every `/internal/*` JSON API (entities, evidence, needs-you,
#: activity, xero connection-management, mailboxes, mailbox provider
#: connect/disconnect/sweep/messages/domain-review/security-review,
#: evidence-classification, ai, documents, operator chat),
#: `POST /auth/logout`, and FastAPI's own `/docs`/`/redoc`/
#: `/openapi.json` — is protected WITHOUT exception (PID §117.G), with
#: zero special-casing required: it simply is not in this allow-list.
_PUBLIC_EXACT_PATHS: frozenset[str] = frozenset(
    {
        "/health",
        "/version",
        "/ready",
        "/auth/login",
        "/auth/status",
        "/login.html",
        "/internal/xero/oauth/result",
        "/internal/xero/oauth/callback",
        "/internal/mailboxes/microsoft/oauth/result",
        "/internal/mailboxes/microsoft/oauth/callback",
        "/internal/mailboxes/gmail/oauth/result",
        "/internal/mailboxes/gmail/oauth/callback",
    }
)

#: Xero's governed multi-tenant candidate-selection broker — the ONE
#: prefix exemption, deliberately narrow (scoped to this one feature's
#: own sub-path, never a generic wildcard under `/` or even under
#: `/internal/xero`): covers both
#: `GET /internal/xero/oauth/pending-selection/{selection_id}` and
#: `POST /internal/xero/oauth/pending-selection/{selection_id}/resolve`
#: (the `{selection_id}` path parameter cannot be named as an exact
#: string above).
_PUBLIC_PREFIX = "/internal/xero/oauth/pending-selection/"

#: Methods that never carry a CSRF-relevant side effect (PID §117.E:
#: CSRF/Origin verification applies to "state-changing browser
#: requests" only).
_SAFE_METHODS = frozenset({"GET", "HEAD", "OPTIONS"})


def _is_public_path(path: str) -> bool:
    return path in _PUBLIC_EXACT_PATHS or path.startswith(_PUBLIC_PREFIX)


#: WO-BAGMAN-117 §6.A'.14 (Architect correction 2026-10-14) — `https://`
#: and `http://` imply 443/80 respectively when no port is explicit in
#: either the `Origin` header or the `Host` header, so an explicit
#: `:443`/`:80` and an elided one must compare equal (RFC 6454 origin
#: equality — "an explicit port MUST be considered the same as its
#: scheme's default port").
_DEFAULT_PORT_BY_SCHEME: dict[str, int] = {"https": 443, "http": 80}


def _effective_port(scheme: str, explicit_port: int | None) -> int | None:
    if explicit_port is not None:
        return explicit_port
    return _DEFAULT_PORT_BY_SCHEME.get(scheme)


def _origin_is_trusted(origin_header: str, request: Request) -> bool:
    """PID §117.E's independent Origin-header safeguard (alongside, not
    instead of, the CSRF token below).

    WO-BAGMAN-117 §6.A'.14 (Architect correction, 2026-10-14): the
    ORIGINAL version of this function compared only the `Origin`
    header's `netloc` (host[:port]) against the `Host` header this
    request arrived with — it never checked SCHEME at all, so
    `Origin: http://realhost` was wrongly accepted against an
    HTTPS-modeled application. This version validates the FULL expected
    origin — scheme, hostname, AND effective (default-normalised) port
    — never a hardcoded expected production hostname (PID §117.D: no
    DNS name exists yet for BAGMAN's private HTTPS entry point at the
    time of this delivery; the real hostname/port is a Stage E,
    production-executor-verified fact, not something Stage A's
    repository code can safely assume). This remains a same-origin
    check, not an allowlist of one specific domain — it rejects a
    request whose `Origin` does not match the scheme/host/port this
    request itself actually arrived on, regardless of what that turns
    out to be in any given environment (development, test, or the
    eventual production HTTPS entry point).

    Still deliberately reads the request's own `Host` header directly
    (never `X-Forwarded-Host`/`Forwarded`) — PID §117.D's own explicit
    instruction that this application "must not blindly trust
    X-Forwarded-*/Forwarded headers from any source other than the
    one, known, narrowly-scoped Caddy proxy" (a Stage E production
    concern to configure/verify on the proxy side; this function does
    not assume that hardening already exists elsewhere, and does not
    reference either header anywhere in this function — not even to
    deliberately ignore a specific value — so there is nothing here an
    attacker-supplied forwarded header could ever influence). The
    SCHEME half of the comparison is, symmetrically, `request.url.scheme`
    — whatever Starlette/ASGI reports this exact request as having
    actually arrived over — never a header a browser-originated request
    could set itself; the same "read the request's own truth, never a
    client-controlled proxy header" discipline PID §117.D already
    requires for `Host`, extended to scheme. Once the narrowly-trusted
    HTTPS reverse-proxy topology (Caddy) exists (a separately-governed
    Stage E production action), that proxy — not this function — is the
    place `request.url.scheme` would be made to honestly reflect the
    real external scheme (e.g. via an ASGI-server-level trusted-proxy
    configuration scoped to exactly that one proxy's address); this
    repository-level code makes no assumption about how, or whether,
    that is already wired, exactly mirroring this function's pre-
    existing Host-header discipline.

    The independent CSRF-token check in `auth_enforcement_middleware`
    below is retained UNCHANGED alongside this — Origin verification is
    strengthened additively here, it does not replace or weaken CSRF.
    """
    host_header = request.headers.get("host", "")
    if not host_header:
        return False
    try:
        origin_parts = urllib.parse.urlsplit(origin_header)
        # "//" + host_header lets urlsplit parse a bare "host[:port]"
        # (correctly handling a bracketed IPv6 literal too) exactly the
        # way it would inside a full URL's own netloc.
        host_parts = urllib.parse.urlsplit("//" + host_header)
    except ValueError:
        return False

    origin_scheme = origin_parts.scheme
    origin_hostname = origin_parts.hostname
    host_hostname = host_parts.hostname
    if not origin_scheme or not origin_hostname or not host_hostname:
        return False
    # A well-formed `Origin` header is EXACTLY `scheme://host[:port]` —
    # no path/query/fragment/userinfo. Anything else is malformed or
    # suspicious and must fail CLOSED rather than be partially parsed
    # into a false match.
    if origin_parts.path not in ("", "/") or origin_parts.query or origin_parts.fragment or origin_parts.username:
        return False

    request_scheme = request.url.scheme
    if origin_scheme.lower() != request_scheme.lower():
        return False
    if origin_hostname.lower() != host_hostname.lower():
        return False

    origin_port = _effective_port(origin_scheme.lower(), origin_parts.port)
    host_port = _effective_port(request_scheme.lower(), host_parts.port)
    return origin_port is not None and origin_port == host_port


@app.middleware("http")
async def auth_enforcement_middleware(request: Request, call_next):
    """WO-BAGMAN-117 §6.A.6/§117.C/§117.E/§117.G — server-side session
    enforcement on every request except the narrow, individually-
    justified allow-list above. Registered ALONGSIDE, never replacing,
    `correlation_id_middleware` above (declared first in this file, so
    it remains the OUTERMOST layer — every response, including a
    401/403/503 this middleware itself returns, still gets a
    correlation id and the one sanitized completion log line).

    Feature-flagged via `auth.auth_enabled()` (`BAGMAN_AUTH_ENABLED`,
    default `false` — WO-BAGMAN-117 §6.D's "deploy the merged image
    with the new middleware feature-flagged off" staged-activation
    requirement): when disabled, every request passes straight through
    completely unchanged, so this code can be MERGED and DEPLOYED
    (Stage D) with zero behavioural change until a later, separately
    governed production action (Stage F) turns it on.

    Fail-closed on session-store unavailability (PID §117.C): a
    `core.errors.PersistenceError` from the session repository is
    translated to `503`, never silently treated as "no session" (which
    would be a silent ALLOW-shaped outcome for every protected route
    the moment the database is unreachable — the opposite of fail-
    closed).
    """
    if not auth.auth_enabled() or _is_public_path(request.url.path):
        return await call_next(request)

    repository = auth.get_session_repository()
    raw_token = request.cookies.get(auth.SESSION_COOKIE_NAME)
    try:
        evaluation = evaluate_session_token(raw_token, repository=repository)
    except PersistenceError as exc:
        return JSONResponse(
            status_code=503,
            content={
                "error_code": "SESSION_STORE_UNAVAILABLE",
                "message": f"the session store is unreachable; access denied (fail-closed): {exc.message}",
            },
        )

    if evaluation.status is not SessionStatus.VALID:
        return JSONResponse(
            status_code=401,
            content={
                "error_code": "UNAUTHENTICATED",
                "message": "authentication required",
                "reason": evaluation.status.value,
            },
        )

    if request.method not in _SAFE_METHODS:
        origin_header = request.headers.get("origin")
        if not origin_header or not _origin_is_trusted(origin_header, request):
            return JSONResponse(
                status_code=403,
                content={"error_code": "ORIGIN_REJECTED", "message": "request Origin did not match this server"},
            )
        csrf_cookie = request.cookies.get(auth.CSRF_COOKIE_NAME)
        csrf_header = request.headers.get(auth.CSRF_HEADER_NAME)
        session_csrf_token = evaluation.session.csrf_token if evaluation.session is not None else None
        if (
            not csrf_cookie
            or not csrf_header
            or session_csrf_token is None
            or not tokens_match(csrf_cookie, csrf_header)
            or not tokens_match(csrf_cookie, session_csrf_token)
        ):
            return JSONResponse(
                status_code=403,
                content={"error_code": "CSRF_REJECTED", "message": "missing or invalid CSRF token"},
            )

    request.state.operator_session = evaluation.session
    return await call_next(request)


@app.exception_handler(BagmanError)
async def bagman_error_handler(request: Request, exc: BagmanError) -> JSONResponse:
    status_code = _status_for(exc)
    correlation_id = getattr(request.state, "correlation_id", None)

    logger.log(
        logging.WARNING if status_code < 500 else logging.ERROR,
        "bagman_error",
        extra={
            "component": "bagman.runtime.api",
            "correlation_id": correlation_id,
            "event_type": exc.error_code,
        },
        exc_info=status_code >= 500,
    )

    if status_code in _CLIENT_SAFE_STATUSES:
        body = {"error_code": exc.error_code, "message": exc.message}
    else:
        body = {
            "error_code": exc.error_code,
            "message": "an internal error occurred; see server logs for detail",
            "correlation_id": correlation_id,
        }
    return JSONResponse(status_code=status_code, content=body)


@app.exception_handler(Exception)
async def unhandled_error_handler(request: Request, exc: Exception) -> JSONResponse:
    correlation_id = getattr(request.state, "correlation_id", None)
    logger.error(
        "unhandled_error",
        extra={
            "component": "bagman.runtime.api",
            "correlation_id": correlation_id,
            "event_type": "UNHANDLED_ERROR",
        },
        exc_info=True,
    )
    return JSONResponse(
        status_code=500,
        content={
            "error_code": "INTERNAL_ERROR",
            "message": "an internal error occurred; see server logs for detail",
            "correlation_id": correlation_id,
        },
    )
