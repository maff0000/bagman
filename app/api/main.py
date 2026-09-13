"""``bagman-api`` — the FastAPI application instance (CD-3 WI-3, PID §24;
static UI mounting added CD-4 WI-4, PID §36-42).

Responsibilities of this module, and only this module:

* build the ``FastAPI`` app and include every router in ``routers/``;
* configure structured logging (``app.api.logging_config``) once,
  at import time, before anything else runs;
* attach one request-scoped correlation-id (PID §27) to every request/
  response, generating a fresh one when a caller does not supply
  ``X-Correlation-Id``;
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
import uuid
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from starlette.staticfiles import StaticFiles

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
    PersistenceError,
    StorageError,
    ValidationError,
)
from app.api.logging_config import configure_logging
from app.api.routers import ai, health, intake, internal, operator, version

configure_logging(level=os.environ.get("BAGMAN_LOG_LEVEL", "INFO"))
logger = logging.getLogger("bagman.runtime.api")

app = FastAPI(title="BAGMAN Runtime API", version="1")

app.include_router(health.router)
app.include_router(version.router)
app.include_router(internal.router)
app.include_router(intake.router)
#: CD-5 WI-2 — the background AI gateway HTTP surface (PID §68).
app.include_router(ai.router)
#: CD-5 WI-3 — Ask BAGMAN (PID §42-44/§68). New file (app/api/routers/
#: operator.py), never added to routers/ai.py (WI-2's own in-parallel
#: file) — see that router's own docstring.
app.include_router(operator.router)

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
}

#: 5xx statuses never return the raw exception message to the client
#: (see module docstring) — only these client-actionable statuses do.
_CLIENT_SAFE_STATUSES = frozenset({404, 409, 422})


def _status_for(exc: BagmanError) -> int:
    for error_type, status in _STATUS_BY_ERROR_TYPE.items():
        if isinstance(exc, error_type):
            return status
    return 500  # anything else, e.g. IntegrityError — PID's literal mapping table


@app.middleware("http")
async def correlation_id_middleware(request: Request, call_next):
    correlation_id = request.headers.get("x-correlation-id") or str(uuid.uuid4())
    request.state.correlation_id = correlation_id
    response = await call_next(request)
    response.headers["X-Correlation-Id"] = correlation_id
    return response


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
