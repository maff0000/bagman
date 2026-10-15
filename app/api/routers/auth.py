"""``/auth/*`` — single-operator login/logout/status (WO-BAGMAN-117
§6.A.3, PID §117.C/§117.E/§117.G).

Thin router: parses/renders HTTP, delegates every real decision to
``core.auth`` (Argon2id verification, session evaluation, token/CSRF
generation) — mirrors ``app/api/routers/xero.py``'s own "thin router,
all domain logic elsewhere" convention.

Public per PID §117.G: ``POST /auth/login`` and ``GET /auth/status``
are the two routes this module registers that MUST be reachable with
no session cookie present (the auth-enforcement middleware in
``app/api/main.py`` carries the authoritative, exhaustive allow-list —
this docstring states the expectation, that module is the one real
enforcement point). ``POST /auth/logout`` is deliberately NOT public —
it requires an existing valid session (enforced by that same
middleware, no special-casing needed here): revoking a session that
was never presented is meaningless, and logout is itself a
state-changing request already covered by the middleware's generic
CSRF+Origin gate.
"""
from __future__ import annotations

import logging
import os
from datetime import timedelta

from fastapi import APIRouter, Request, Response
from fastapi.responses import JSONResponse
from pydantic import BaseModel

from app.api.composition import RuntimeComposition, get_composition
from core import identity
from core.auth import (
    SessionRepository,
    evaluate_session_token,
    generate_csrf_token,
    generate_session_token,
    hash_token,
    read_operator_password_hash,
    session_max_lifetime,
    verify_operator_password,
)
from core.errors import PersistenceError
from core.timestamps import utc_now

router = APIRouter(prefix="/auth")

#: WO-BAGMAN-117 §6.A″ item 2 (2026-10-15 Architect correction) — mirrors
#: ``app/api/routers/health.py``'s own ``logging.getLogger("bagman.runtime.api.<router>")``
#: naming convention exactly, so these records flow through the same
#: root JSON formatter/handler (``app/api/logging_config.py``) every
#: other ``bagman.*`` logger already does.
logger = logging.getLogger("bagman.runtime.api.auth")

#: PID §117.C: ``Secure``, ``HttpOnly``, ``SameSite=Lax``, ``Path=/``,
#: no ``Domain`` attribute, ``__Host-`` prefix where practical. The
#: ``__Host-`` prefix itself REQUIRES exactly this combination (Secure +
#: Path=/ + no Domain) per the browser spec, so using it is both the
#: WO's own preference and a free extra guarantee that a future edit
#: cannot silently add a ``Domain`` attribute without the cookie
#: silently stopping being accepted by the browser at all (a loud,
#: self-enforcing failure rather than a silent widening).
SESSION_COOKIE_NAME = "__Host-bagman_session"
#: The double-submit CSRF cookie (PID §117.E) — deliberately NOT
#: ``HttpOnly`` (the GUI's own JS must read it to echo it back as the
#: ``X-Bagman-Csrf-Token`` header on state-changing requests); still
#: ``Secure``/``Path=/``/no ``Domain`` via the same ``__Host-`` prefix
#: discipline.
CSRF_COOKIE_NAME = "__Host-bagman_csrf"
CSRF_HEADER_NAME = "x-bagman-csrf-token"

#: PID §117.E: "Failed-login throttling (a short backoff after
#: consecutive failures), recorded via the existing audit-event
#: mechanism — no new logging subsystem." A fixed subject identity
#: (there is exactly one operator account, PID §117.A) that every
#: login-attempt audit event is recorded/queried against.
#:
#: ``_LOGIN_AUDIT_SUBJECT_ID`` must be a real, canonical-identifier-
#: shaped (UUIDv7) value — ``contracts/audit/bagman.audit_event.v1
#: .schema.json`` requires both ``subject_id`` and ``correlation_id``
#: to match that exact pattern, so a human-readable literal like
#: ``"operator"`` would fail contract validation the first time any
#: audit event is recorded. Generated ONCE per process (module import
#: time) — stable for the life of one running `bagman-api` process,
#: which is all `_is_login_throttled`'s own `list_by_subject` lookback
#: query needs (every failed-login event in this process must share
#: the same bucketing key).
_LOGIN_AUDIT_ACTOR_ID = "operator"
_LOGIN_AUDIT_SUBJECT_TYPE = "OperatorAccount"
_LOGIN_AUDIT_SUBJECT_ID = identity.generate_id()
_LOGIN_FAILED_EVENT_TYPE = "OPERATOR_LOGIN_FAILED"
_LOGIN_SUCCEEDED_EVENT_TYPE = "OPERATOR_LOGIN_SUCCEEDED"
_LOGOUT_EVENT_TYPE = "OPERATOR_LOGOUT"
#: Throttle: N consecutive-window failures within the lookback window
#: triggers a short backoff — deliberately small, fixed numbers (no env
#: override requested by the WO; a single-operator tool does not need
#: operationally-tunable throttling knobs).
_THROTTLE_FAILURE_THRESHOLD = 5
_THROTTLE_WINDOW = timedelta(minutes=5)


def auth_enabled() -> bool:
    """``BAGMAN_AUTH_ENABLED`` (default ``false``) — WO-BAGMAN-117
    §6.D's own "deploy the merged image with the new middleware
    feature-flagged off" staged-activation requirement. Read fresh on
    every call (never cached) so flipping it (Stage F, a later,
    separately-governed production action) takes effect on the very
    next request with no restart-timing ambiguity, and so a test can
    flip it per-test via ``monkeypatch.setenv``.
    """
    return os.environ.get("BAGMAN_AUTH_ENABLED", "false").strip().lower() in {"1", "true", "yes", "on"}


def get_session_repository() -> SessionRepository:
    """The current `RuntimeComposition`'s own
    `operator_session_repository` — shared, via `get_composition()`'s
    own existing process-wide caching/reset discipline
    (`app.api.composition.reset_composition_for_tests()`), by every
    caller in this process: this router's handlers AND
    `app/api/main.py`'s auth-enforcement middleware both call this
    same function rather than each holding an independent reference."""
    composition: RuntimeComposition = get_composition()
    return composition.operator_session_repository


class LoginRequest(BaseModel):
    password: str


def _set_session_cookies(response: Response, *, raw_token: str, csrf_token: str, max_age_seconds: int) -> None:
    response.set_cookie(
        key=SESSION_COOKIE_NAME,
        value=raw_token,
        max_age=max_age_seconds,
        secure=True,
        httponly=True,
        samesite="lax",
        path="/",
    )
    response.set_cookie(
        key=CSRF_COOKIE_NAME,
        value=csrf_token,
        max_age=max_age_seconds,
        secure=True,
        httponly=False,
        samesite="lax",
        path="/",
    )


def _clear_session_cookies(response: Response) -> None:
    response.delete_cookie(key=SESSION_COOKIE_NAME, path="/", secure=True, samesite="lax")
    response.delete_cookie(key=CSRF_COOKIE_NAME, path="/", secure=True, samesite="lax")


def _is_login_throttled(composition: RuntimeComposition, *, now) -> bool:
    """PID §117.E failed-login throttling, recorded via the existing
    audit-event mechanism (no new logging subsystem/table)."""
    recent_failures = [
        event
        for event in composition.api.audit_repository.list_by_subject(
            _LOGIN_AUDIT_SUBJECT_TYPE, _LOGIN_AUDIT_SUBJECT_ID
        )
        if event.event_type == _LOGIN_FAILED_EVENT_TYPE and (now - event.occurred_at) <= _THROTTLE_WINDOW
    ]
    return len(recent_failures) >= _THROTTLE_FAILURE_THRESHOLD


@router.post("/login")
async def login(payload: LoginRequest, request: Request) -> Response:
    """Verify the single operator's password and, on success, mint a
    brand-new session (rotation: a fresh token every login, never
    reused — PID §117.C)."""
    composition = get_composition()
    now = utc_now()

    if _is_login_throttled(composition, now=now):
        return JSONResponse(
            status_code=429,
            content={
                "error_code": "LOGIN_THROTTLED",
                "message": "too many recent failed login attempts; wait before trying again",
            },
        )

    try:
        stored_hash = read_operator_password_hash()
    except RuntimeError:
        # No credential has been bootstrapped yet (scripts/set_operator_password.py
        # was never run) — an honest 503, never a silent "always reject"
        # that looks indistinguishable from a wrong password.
        #
        # WO-BAGMAN-117 §6.A″ item 2 (2026-10-15 Architect correction):
        # the raw `RuntimeError` message embeds the configured
        # `BAGMAN_OPERATOR_PASSWORD_HASH_FILE` filesystem path and the
        # underlying `OSError` text (see
        # `core.auth.read_operator_password_hash`'s own docstring) —
        # neither may ever reach a browser response. A stable, generic
        # message goes to the client; the full exception (path + OSError
        # detail) goes only to the server-side log, under this request's
        # correlation id, mirroring `bagman_error_handler`'s own
        # established 5xx sanitisation convention.
        correlation_id = getattr(request.state, "correlation_id", None)
        logger.error(
            "operator_credential_not_configured",
            extra={
                "component": "bagman.runtime.api.auth",
                "correlation_id": correlation_id,
                "event_type": "OPERATOR_CREDENTIAL_NOT_CONFIGURED",
            },
            exc_info=True,
        )
        return JSONResponse(
            status_code=503,
            content={
                "error_code": "OPERATOR_CREDENTIAL_NOT_CONFIGURED",
                "message": "the operator credential is not configured; see server logs for detail",
                "correlation_id": correlation_id,
            },
        )

    if not verify_operator_password(payload.password, stored_hash):
        composition.api.record_audit_event(
            event_type=_LOGIN_FAILED_EVENT_TYPE,
            actor_type="USER",
            actor_id=_LOGIN_AUDIT_ACTOR_ID,
            subject_type=_LOGIN_AUDIT_SUBJECT_TYPE,
            subject_id=_LOGIN_AUDIT_SUBJECT_ID,
            correlation_id=_LOGIN_AUDIT_SUBJECT_ID,
            causation_id=None,
            payload={"remote_addr": request.client.host if request.client else None},
        )
        return JSONResponse(
            status_code=401,
            content={"error_code": "INVALID_CREDENTIALS", "message": "incorrect password"},
        )

    repository = get_session_repository()
    raw_token = generate_session_token()
    csrf_token = generate_csrf_token()
    lifetime = session_max_lifetime()
    expires_at = now + lifetime

    try:
        repository.create_session(
            token_digest=hash_token(raw_token),
            csrf_token=csrf_token,
            created_at=now,
            expires_at=expires_at,
        )
    except PersistenceError:
        # Fail-closed (PID §117.C): the session store is unavailable —
        # never issue a cookie for a session that may not actually have
        # been durably persisted.
        #
        # WO-BAGMAN-117 §6.A″ item 2 (2026-10-15 Architect correction):
        # `exc.message` may itself carry raw SQLAlchemy/driver exception
        # text (see `core/errors.py::PersistenceError`'s own docstring)
        # — never returned to the client. Same stable, generic
        # message/error_code as `app/api/main.py
        # ::auth_enforcement_middleware`'s own session-store-unavailable
        # path; full detail logged server-side only, under this
        # request's correlation id.
        correlation_id = getattr(request.state, "correlation_id", None)
        logger.error(
            "session_store_unavailable",
            extra={
                "component": "bagman.runtime.api.auth",
                "correlation_id": correlation_id,
                "event_type": "SESSION_STORE_UNAVAILABLE",
            },
            exc_info=True,
        )
        return JSONResponse(
            status_code=503,
            content={
                "error_code": "SESSION_STORE_UNAVAILABLE",
                "message": "the session store is unreachable; access denied (fail-closed); see server logs for detail",
                "correlation_id": correlation_id,
            },
        )

    composition.api.record_audit_event(
        event_type=_LOGIN_SUCCEEDED_EVENT_TYPE,
        actor_type="USER",
        actor_id=_LOGIN_AUDIT_ACTOR_ID,
        subject_type=_LOGIN_AUDIT_SUBJECT_TYPE,
        subject_id=_LOGIN_AUDIT_SUBJECT_ID,
        correlation_id=_LOGIN_AUDIT_SUBJECT_ID,
        causation_id=None,
        payload={},
    )

    response = JSONResponse(status_code=200, content={"authenticated": True, "expires_at": expires_at.isoformat()})
    _set_session_cookies(
        response,
        raw_token=raw_token,
        csrf_token=csrf_token,
        max_age_seconds=int(lifetime.total_seconds()),
    )
    return response


@router.post("/logout")
async def logout(request: Request) -> Response:
    """Explicit logout immediately revokes the server-side session
    record (PID §117.C). Reachable only with a currently-valid session
    (the auth-enforcement middleware gates every non-public route,
    ``POST /auth/logout`` included — see this module's own docstring)."""
    composition = get_composition()
    repository = get_session_repository()
    raw_token = request.cookies.get(SESSION_COOKIE_NAME)
    if raw_token:
        repository.revoke_session(hash_token(raw_token))
        composition.api.record_audit_event(
            event_type=_LOGOUT_EVENT_TYPE,
            actor_type="USER",
            actor_id=_LOGIN_AUDIT_ACTOR_ID,
            subject_type=_LOGIN_AUDIT_SUBJECT_TYPE,
            subject_id=_LOGIN_AUDIT_SUBJECT_ID,
            correlation_id=_LOGIN_AUDIT_SUBJECT_ID,
            causation_id=None,
            payload={},
        )
    response = JSONResponse(status_code=200, content={"authenticated": False})
    _clear_session_cookies(response)
    return response


@router.get("/status")
async def status(request: Request) -> dict:
    """Public, unauthenticated status check (PID §117.G) — both the
    GUI shell's own on-load redirect-if-unauthenticated convenience
    check (``app/api/static/shell/shell.js``) and an operator/ops
    sanity check read this. ``auth_enabled`` is reported alongside
    ``authenticated`` specifically so a caller can distinguish "you are
    not logged in and enforcement is ACTIVE" from "enforcement is not
    yet turned on at all" (WO-BAGMAN-117 §6.D's Stage D "installed but
    inert" deployment state) — without this, a client-side redirect
    built from `authenticated` alone would send every operator to
    `/login.html` the moment this code is deployed, even before Stage F
    ever flips real enforcement on.
    """
    repository = get_session_repository()
    try:
        evaluation = evaluate_session_token(request.cookies.get(SESSION_COOKIE_NAME), repository=repository)
    except PersistenceError:
        return {"authenticated": False, "auth_enabled": auth_enabled(), "reason": "SESSION_STORE_UNAVAILABLE"}

    return {
        "authenticated": evaluation.is_authenticated,
        "auth_enabled": auth_enabled(),
        "reason": evaluation.status.value,
    }
