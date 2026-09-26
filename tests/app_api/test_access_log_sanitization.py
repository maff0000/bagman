"""Canary-based proof that BAGMAN never leaks an OAuth callback's query
string (``code``/``state``) into container logs (CD-6 GUI-operations-
foundation follow-on WO — architect-mandated fix for the real leak the
first live Gmail OAuth attempt hit: uvicorn's own ``uvicorn.access``
logger captured the full raw callback URL, credentials included, in
plaintext container logs).

This protection is GENERIC (``app/api/main.py::correlation_id_middleware``
logs method + URL PATH ONLY for every route, and
``app/api/logging_config.py::configure_logging`` disables
``uvicorn.access`` entirely) — it protects Gmail's, Microsoft's, and
Xero's own OAuth callbacks identically, and every other route besides.
The Gmail callback route is used below only because it is this
delivery's own real, live-incident-triggering example; nothing here is
Gmail-specific.

Two independent proofs:

1. A DIRECT, empirical proof that ``uvicorn.access`` is genuinely
   DISABLED (not merely re-formatted) — a record manually emitted on
   that exact logger, carrying the canary values, produces ZERO output
   on a handler attached straight to it. This is the mechanism-level
   proof the architect spec explicitly asked for ("verify empirically,
   not just something that looks like it should work").
2. An END-TO-END proof via a real request through BAGMAN's own ASGI
   app (``TestClient``): a request carrying the canary ``code``/
   ``state`` query params never leaves either value anywhere in
   whatever the root JSON-formatted log stream actually emits,
   including BAGMAN's own new sanitized request-completion line.
"""
from __future__ import annotations

import logging

import pytest
from fastapi.testclient import TestClient

from app.api.composition import reset_composition_for_tests
from app.api.logging_config import configure_logging
from app.api.main import app

#: Architect spec's own exact canary values (items 16-18).
CANARY_CODE = "CANARY_SECRET_CODE_DO_NOT_LOG"
CANARY_STATE = "CANARY_SECRET_STATE_DO_NOT_LOG"


class _CapturingHandler(logging.Handler):
    """Records every formatted line a logger actually emits to THIS
    handler — an empirical capture, not a structural inspection."""

    def __init__(self) -> None:
        super().__init__()
        self.lines: list[str] = []

    def emit(self, record: logging.LogRecord) -> None:  # noqa: D102
        self.lines.append(self.format(record))


@pytest.fixture
def dev_client(monkeypatch):
    monkeypatch.setenv("BAGMAN_RUNTIME_ENV", "development")
    reset_composition_for_tests()
    with TestClient(app) as test_client:
        yield test_client
    reset_composition_for_tests()


@pytest.fixture
def root_capture():
    """Attach a capturing handler directly to the ``bagman`` logger
    namespace (the ancestor of every logger this application itself
    ever writes to — see e.g. ``app/api/main.py``'s own
    ``logging.getLogger("bagman.runtime.api")``) for the duration of
    one test. Deliberately NOT the bare root logger: under
    `TestClient`, third-party client libraries (e.g. `httpx`, which
    `TestClient` itself uses to issue the in-process request this test
    drives) also propagate to root and log their OWN outgoing request
    line at INFO level — a test-harness artifact with no production
    analogue (bagman-api's real container stdout is written by BAGMAN's
    own code and uvicorn only, never by an HTTP client library BAGMAN
    happens to be calling). Scoping to the ``bagman`` namespace keeps
    this test's canary proof about BAGMAN's OWN log output, exactly
    matching what `configure_logging`'s JSON handler on the root logger
    actually renders for every ``bagman.*`` record (via ordinary
    logging propagation) — see `configure_logging`'s own docstring."""
    configure_logging()
    bagman_logger = logging.getLogger("bagman")
    capture = _CapturingHandler()
    bagman_logger.addHandler(capture)
    try:
        yield capture
    finally:
        bagman_logger.removeHandler(capture)


# -- mechanism-level proof: uvicorn.access is genuinely DISABLED ----------


def test_uvicorn_access_logger_is_disabled_not_merely_reformatted():
    """`disabled = True` stops `logging.Logger.handle()` before
    `callHandlers` ever runs — so a handler attached directly to
    `uvicorn.access` sees NOTHING, even for a record manually carrying
    the canary code/state in the shape a real access-log line would."""
    configure_logging()
    access_logger = logging.getLogger("uvicorn.access")
    assert access_logger.disabled is True

    capture = _CapturingHandler()
    access_logger.addHandler(capture)
    try:
        access_logger.info(
            'GET /internal/mailboxes/gmail/oauth/callback?code=%s&state=%s HTTP/1.1" 303' % (CANARY_CODE, CANARY_STATE)
        )
    finally:
        access_logger.removeHandler(capture)

    assert capture.lines == []


def test_uvicorn_access_logger_has_no_handlers_of_its_own():
    configure_logging()
    assert logging.getLogger("uvicorn.access").handlers == []


def test_uvicorn_access_logger_does_not_propagate():
    configure_logging()
    assert logging.getLogger("uvicorn.access").propagate is False


# -- end-to-end canary: a real request through the real app ---------------


def test_real_callback_request_with_canary_query_params_never_logs_them(dev_client, root_capture):
    response = dev_client.get(
        "/internal/mailboxes/gmail/oauth/callback",
        params={"code": CANARY_CODE, "state": CANARY_STATE},
        follow_redirects=False,
    )
    assert response.status_code == 303  # unknown state — the flow's outcome is irrelevant to this proof

    rendered = "\n".join(root_capture.lines)
    assert CANARY_CODE not in rendered
    assert CANARY_STATE not in rendered


def test_canary_code_value_never_appears_in_captured_logs(dev_client, root_capture):
    """Explicit, single-purpose assertion (architect spec item 17)."""
    dev_client.get(
        "/internal/mailboxes/gmail/oauth/callback",
        params={"code": CANARY_CODE, "state": CANARY_STATE},
        follow_redirects=False,
    )
    assert CANARY_CODE not in "\n".join(root_capture.lines)


def test_canary_state_value_never_appears_in_captured_logs(dev_client, root_capture):
    """Explicit, single-purpose assertion (architect spec item 18)."""
    dev_client.get(
        "/internal/mailboxes/gmail/oauth/callback",
        params={"code": CANARY_CODE, "state": CANARY_STATE},
        follow_redirects=False,
    )
    assert CANARY_STATE not in "\n".join(root_capture.lines)


def test_canary_on_a_non_oauth_route_is_also_never_logged(dev_client, root_capture):
    """The protection is generic — never hard-coded to the Gmail
    callback path alone (architect's own explicit requirement)."""
    dev_client.get(
        "/internal/mailboxes/gmail/oauth/result",
        params={"ok": "false", "reason": CANARY_CODE},
    )
    assert CANARY_CODE not in "\n".join(root_capture.lines)


def test_sanitized_request_completion_line_carries_path_only_never_query(dev_client, root_capture):
    """Positive proof of the replacement mechanism: BAGMAN's own
    request-completion log line (from `correlation_id_middleware`)
    names the path, but a harmless, non-secret query param on the same
    request never appears anywhere in the log stream."""
    dev_client.get("/internal/mailboxes/gmail/oauth/result", params={"ok": "true", "reason": "connected"})

    rendered = "\n".join(root_capture.lines)
    assert "/internal/mailboxes/gmail/oauth/result" in rendered
    assert "ok=true" not in rendered
    assert "reason=connected" not in rendered


# -- Microsoft/IMAP/Xero are unaffected: the fix is generic, not Gmail-only --


def test_microsoft_callback_route_query_params_are_also_never_logged(dev_client, root_capture):
    dev_client.get(
        "/internal/mailboxes/microsoft/oauth/callback",
        params={"code": CANARY_CODE, "state": CANARY_STATE},
        follow_redirects=False,
    )
    rendered = "\n".join(root_capture.lines)
    assert CANARY_CODE not in rendered
    assert CANARY_STATE not in rendered
