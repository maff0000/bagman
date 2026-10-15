"""WO-BAGMAN-117 §6.A″ (2026-10-15 Architect correction, additive to
§6.A/§6.A′/§12) — two narrow proofs:

1. Middleware ordering / correlation-ID wrapping (§12 criterion 26):
   `correlation_id_middleware` genuinely wraps `auth_enforcement_middleware`
   in actual request-execution order, not merely file/registration
   order — every 401 (unauthenticated), 403 (CSRF-rejected), 403
   (Origin-rejected), and 503 (session-store-unavailable) response the
   auth-enforcement layer itself returns still carries
   `X-Correlation-Id` and produces the existing sanitized, path-only
   `REQUEST_COMPLETED` log line, exactly like an ordinary 2xx request
   already did before this correction.

2. Sanitised authentication 5xx responses (§12 criterion 27): a
   `PersistenceError`/`RuntimeError` carrying raw SQL/driver/
   filesystem/credential detail, raised from the session-repository
   failure path (enforcement middleware, login session-creation) or
   the missing/unreadable operator-credential-configuration path,
   never lets that detail reach the HTTP response (status, headers, or
   body) — only a stable, generic, client-safe error code/message.

Mirrors two existing precedents exactly rather than reinventing them:
`tests/app_api/test_access_log_sanitization.py`'s `_CapturingHandler` +
`bagman`-namespace-scoped log-capture fixture (empirical proof of what
is actually emitted, not a structural guess), and
`tests/security/test_auth_access_control.py`'s `dev_client`/
`_FailingSessionRepository` pattern (a minimal fake
`core.auth.SessionRepository` wired in via monkeypatching
`app.api.routers.auth.get_session_repository` — the one, single
function both the auth-enforcement middleware and `POST /auth/login`
call to reach the session store).
"""
from __future__ import annotations

import logging

import pytest
from fastapi.testclient import TestClient

from app.api.composition import get_composition, reset_composition_for_tests
from app.api.logging_config import configure_logging
from app.api.main import app
from app.api.routers import auth as auth_router
from core.auth import SessionRepository, hash_password
from core.errors import PersistenceError

_TEST_PASSWORD = "a genuinely random correlation/sanitisation test password 4kq8"

#: Adversarial markers (§6.A″'s own required test shape: "a fake SQL
#: fragment, a fake DB host/credential-shaped string") — unique enough
#: that an accidental substring collision with ordinary response
#: content is not a realistic concern.
_SQL_MARKER = "psycopg2.OperationalError: FATAL_CANARY_9f3e2 password=CanaryS3cr3t_do_not_leak host=db-canary-7f3x.internal port=5432"
_CREDENTIAL_PATH_MARKER = "CANARY_CREDENTIAL_PATH_MARKER_7c1d_do_not_leak"


class _CapturingHandler(logging.Handler):
    """Mirrors `tests/app_api/test_access_log_sanitization.py`'s own
    handler exactly — records every formatted line a logger actually
    emits to THIS handler, an empirical capture, not a structural
    inspection."""

    def __init__(self) -> None:
        super().__init__()
        self.lines: list[str] = []

    def emit(self, record: logging.LogRecord) -> None:  # noqa: D102
        self.lines.append(self.format(record))


@pytest.fixture
def root_capture():
    """Same `bagman`-namespace scoping rationale as
    `test_access_log_sanitization.py::root_capture` — every logger this
    application itself writes to (`bagman.runtime.api`,
    `bagman.runtime.api.auth`, ...) is a descendant of this namespace
    and propagates to it by default.

    Also defensively re-enables those exact loggers first. This guards
    against a pre-existing, unrelated cross-test leak this investigation
    traced precisely: `alembic/env.py`'s `fileConfig(config.config_file_name)`
    call (run by `tests/app_api/conftest.py`'s real-Postgres
    `runtime_stack` fixture whenever a migration-backed test runs
    earlier in the same pytest process) uses Python stdlib
    `logging.config.fileConfig`'s own DEFAULT `disable_existing_loggers=True`
    — which sets `.disabled = True` on every already-existing logger not
    named in `alembic.ini`'s `[loggers]` section (`bagman.runtime.api`/
    `bagman.runtime.api.auth` included, since `app.api.main` is already
    imported by then) and never re-enables them afterwards. That is an
    alembic/migration-side defect entirely outside WO-BAGMAN-117's own
    authorised scope (migrations are explicitly excluded from this
    correction) — not something fixed here — so this fixture only
    neutralises its effect on the two loggers this correction's own
    proof depends on, for this test's own duration, rather than letting
    an unrelated fixture elsewhere in the suite make this file's
    otherwise-correct proof order-dependent."""
    configure_logging()
    for name in ("bagman", "bagman.runtime.api", "bagman.runtime.api.auth"):
        logging.getLogger(name).disabled = False
    bagman_logger = logging.getLogger("bagman")
    capture = _CapturingHandler()
    bagman_logger.addHandler(capture)
    try:
        yield capture
    finally:
        bagman_logger.removeHandler(capture)


@pytest.fixture
def dev_client(monkeypatch, tmp_path):
    """Mirrors `tests/security/test_auth_access_control.py::dev_client`
    exactly (HTTPS-modeled `TestClient`, a real bootstrapped Argon2id
    operator credential, `BAGMAN_AUTH_ENABLED=true`)."""
    monkeypatch.setenv("BAGMAN_RUNTIME_ENV", "development")
    monkeypatch.setenv("BAGMAN_AUTH_ENABLED", "true")

    hash_file = tmp_path / "operator_password_hash"
    hash_file.write_text(hash_password(_TEST_PASSWORD), encoding="utf-8")
    monkeypatch.setenv("BAGMAN_OPERATOR_PASSWORD_HASH_FILE", str(hash_file))

    reset_composition_for_tests()
    with TestClient(app, base_url="https://testserver") as test_client:
        yield test_client
    reset_composition_for_tests()


def _logged_in_client(dev_client) -> TestClient:
    response = dev_client.post("/auth/login", json={"password": _TEST_PASSWORD})
    assert response.status_code == 200, response.text
    return dev_client


def _csrf_headers(client) -> dict:
    csrf_token = client.cookies.get("__Host-bagman_csrf")
    return {"x-bagman-csrf-token": csrf_token, "origin": "https://testserver"}


class _FailingSessionRepository(SessionRepository):
    """Mirrors `test_auth_access_control.py::_FailingSessionRepository`
    exactly, except the message carries an adversarial SQL/credential-
    shaped marker instead of a generic placeholder — the whole point of
    the test using it is to prove that marker never reaches an HTTP
    response."""

    def create_session(self, *, token_digest, csrf_token, created_at, expires_at):
        raise PersistenceError(_SQL_MARKER)

    def get_by_token_digest(self, token_digest):
        raise PersistenceError(_SQL_MARKER)

    def revoke_session(self, token_digest):
        raise PersistenceError(_SQL_MARKER)

    def revoke_all_sessions(self, *, now=None):
        raise PersistenceError(_SQL_MARKER)


# ---------------------------------------------------------------------
# §12 criterion 26 (part 1) — the real, actual middleware execution
# order, verified directly via `app.user_middleware` introspection
# (the exact technique the Independent Auditor/Delivery Controller used
# to find this defect — WO §12 criterion 26 explicitly requires this,
# not file/registration order reasoning alone).
# ---------------------------------------------------------------------


def test_correlation_id_middleware_is_genuinely_outermost_in_app_user_middleware():
    dispatch_names = [
        middleware.kwargs["dispatch"].__name__
        for middleware in app.user_middleware
        if "dispatch" in middleware.kwargs
    ]
    assert "correlation_id_middleware" in dispatch_names
    assert "auth_enforcement_middleware" in dispatch_names
    # `app.user_middleware[0]` is the OUTERMOST layer actually wrapping
    # every request (see `Starlette.build_middleware_stack`) — this is
    # the exact assertion that was FALSE before this correction (it was
    # `auth_enforcement_middleware` at index 0).
    assert dispatch_names[0] == "correlation_id_middleware", dispatch_names
    assert dispatch_names.index("correlation_id_middleware") < dispatch_names.index(
        "auth_enforcement_middleware"
    ), dispatch_names


# ---------------------------------------------------------------------
# §12 criterion 26 (part 2) — every 401/403/503 the auth-enforcement
# layer itself returns genuinely carries `X-Correlation-Id` and
# produces the existing sanitized, path-only `REQUEST_COMPLETED` log
# line — proven via a REAL `TestClient` request plus the empirical log
# capture above, never merely asserted from the ordering proof alone.
# ---------------------------------------------------------------------


def test_unauthenticated_401_carries_correlation_id_and_sanitized_log_line(dev_client, root_capture):
    response = dev_client.get("/internal/entities")
    assert response.status_code == 401
    assert response.headers.get("x-correlation-id"), "401 rejection must still carry X-Correlation-Id"

    rendered = "\n".join(root_capture.lines)
    assert "GET /internal/entities -> 401" in rendered


def test_csrf_rejected_403_carries_correlation_id_and_sanitized_log_line(dev_client, root_capture):
    _logged_in_client(dev_client)
    response = dev_client.post("/auth/logout", headers={"origin": "https://testserver"})  # no CSRF header
    assert response.status_code == 403
    assert response.json()["error_code"] == "CSRF_REJECTED"
    assert response.headers.get("x-correlation-id"), "CSRF-rejected 403 must still carry X-Correlation-Id"

    rendered = "\n".join(root_capture.lines)
    assert "POST /auth/logout -> 403" in rendered


def test_origin_rejected_403_carries_correlation_id_and_sanitized_log_line(dev_client, root_capture):
    _logged_in_client(dev_client)
    csrf_token = dev_client.cookies.get("__Host-bagman_csrf")
    response = dev_client.post(
        "/auth/logout",
        headers={"x-bagman-csrf-token": csrf_token, "origin": "https://attacker.example"},
    )
    assert response.status_code == 403
    assert response.json()["error_code"] == "ORIGIN_REJECTED"
    assert response.headers.get("x-correlation-id"), "Origin-rejected 403 must still carry X-Correlation-Id"

    rendered = "\n".join(root_capture.lines)
    assert "POST /auth/logout -> 403" in rendered


def test_session_store_unavailable_503_carries_correlation_id_and_sanitized_log_line(
    dev_client, root_capture, monkeypatch
):
    monkeypatch.setattr(auth_router, "get_session_repository", lambda: _FailingSessionRepository())

    response = dev_client.get(
        "/internal/entities",
        headers={"Cookie": "__Host-bagman_session=forged-or-stale-token-value"},
    )
    assert response.status_code == 503
    assert response.json()["error_code"] == "SESSION_STORE_UNAVAILABLE"
    assert response.headers.get("x-correlation-id"), "session-store-unavailable 503 must still carry X-Correlation-Id"

    rendered = "\n".join(root_capture.lines)
    assert "GET /internal/entities -> 503" in rendered


def test_a_passing_2xx_request_still_carries_correlation_id_and_sanitized_log_line_unchanged(
    dev_client, root_capture
):
    """Positive control: the already-working 2xx case this correction
    must NOT regress."""
    response = dev_client.get("/health")
    assert response.status_code == 200
    assert response.headers.get("x-correlation-id")

    rendered = "\n".join(root_capture.lines)
    assert "GET /health -> 200" in rendered


# ---------------------------------------------------------------------
# §12 criterion 27 — adversarial marker proofs: an authentication 5xx
# response never exposes SQL/DB/credential/filesystem detail.
# ---------------------------------------------------------------------


def test_session_store_unavailable_during_enforcement_never_leaks_the_marker(dev_client, monkeypatch):
    monkeypatch.setattr(auth_router, "get_session_repository", lambda: _FailingSessionRepository())

    response = dev_client.get(
        "/internal/entities",
        headers={"Cookie": "__Host-bagman_session=forged-or-stale-token-value"},
    )

    assert response.status_code == 503
    assert response.json()["error_code"] == "SESSION_STORE_UNAVAILABLE"
    assert _SQL_MARKER not in response.text
    assert _SQL_MARKER not in str(response.headers)


def test_session_creation_failure_at_login_never_leaks_the_marker(dev_client, monkeypatch):
    monkeypatch.setattr(auth_router, "get_session_repository", lambda: _FailingSessionRepository())

    response = dev_client.post("/auth/login", json={"password": _TEST_PASSWORD})

    assert response.status_code == 503
    assert response.json()["error_code"] == "SESSION_STORE_UNAVAILABLE"
    assert "set-cookie" not in {k.lower() for k in response.headers.keys()}
    assert _SQL_MARKER not in response.text
    assert _SQL_MARKER not in str(response.headers)


def test_missing_operator_credential_configuration_never_leaks_the_marker_shaped_path(monkeypatch, tmp_path):
    """A distinct code path from the two above (§6.A″'s own "missing/
    unreadable operator-credential configuration" case,
    `core.auth.read_operator_password_hash` raising a plain
    `RuntimeError`, never a `core.errors.PersistenceError`): the raw
    `RuntimeError` message embeds the configured
    `BAGMAN_OPERATOR_PASSWORD_HASH_FILE` filesystem path verbatim (see
    that function's own docstring) — proven here never to reach the
    HTTP response, using a path whose own name carries the marker so a
    real, non-redacted leak would be unambiguous."""
    monkeypatch.setenv("BAGMAN_RUNTIME_ENV", "development")
    monkeypatch.setenv("BAGMAN_AUTH_ENABLED", "true")
    missing_hash_file = tmp_path / f"{_CREDENTIAL_PATH_MARKER}_operator_password_hash"
    monkeypatch.setenv("BAGMAN_OPERATOR_PASSWORD_HASH_FILE", str(missing_hash_file))
    assert not missing_hash_file.exists()

    reset_composition_for_tests()
    with TestClient(app, base_url="https://testserver") as client:
        response = client.post("/auth/login", json={"password": "anything"})
    reset_composition_for_tests()

    assert response.status_code == 503
    assert response.json()["error_code"] == "OPERATOR_CREDENTIAL_NOT_CONFIGURED"
    assert _CREDENTIAL_PATH_MARKER not in response.text
    assert _CREDENTIAL_PATH_MARKER not in str(response.headers)


def test_missing_operator_credential_configuration_message_is_generic_and_stable(monkeypatch, tmp_path):
    monkeypatch.setenv("BAGMAN_RUNTIME_ENV", "development")
    monkeypatch.setenv("BAGMAN_AUTH_ENABLED", "true")
    missing_hash_file = tmp_path / "operator_password_hash_never_bootstrapped"
    monkeypatch.setenv("BAGMAN_OPERATOR_PASSWORD_HASH_FILE", str(missing_hash_file))

    reset_composition_for_tests()
    with TestClient(app, base_url="https://testserver") as client:
        response = client.post("/auth/login", json={"password": "anything"})
    reset_composition_for_tests()

    body = response.json()
    assert body["message"] == "the operator credential is not configured; see server logs for detail"
    assert str(missing_hash_file) not in response.text


def test_session_store_unavailable_messages_are_identical_stable_text_across_both_call_sites(
    dev_client, monkeypatch
):
    """Both `auth_enforcement_middleware` (`app/api/main.py`) and
    `POST /auth/login` (`app/api/routers/auth.py`) must return the
    exact same stable, generic message for the same underlying
    failure — one sanitisation convention, not two independently
    worded ones."""
    monkeypatch.setattr(auth_router, "get_session_repository", lambda: _FailingSessionRepository())

    enforcement_response = dev_client.get(
        "/internal/entities",
        headers={"Cookie": "__Host-bagman_session=forged-or-stale-token-value"},
    )
    login_response = dev_client.post("/auth/login", json={"password": _TEST_PASSWORD})

    expected_message = "the session store is unreachable; access denied (fail-closed); see server logs for detail"
    assert enforcement_response.json()["message"] == expected_message
    assert login_response.json()["message"] == expected_message
