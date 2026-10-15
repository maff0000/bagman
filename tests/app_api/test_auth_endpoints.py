"""HTTP-level tests for `POST /auth/login`, `POST /auth/logout`,
`GET /auth/status` (WO-BAGMAN-117 §6.A.3/§6.A.11, acceptance criteria
19-21).

Deliberately independent of `tests/app_api/conftest.py`'s
`runtime_stack`/`client` fixtures (real disposable PostgreSQL/MinIO/
ClamAV containers) — mirrors `tests/app_api/test_static_ui.py`'s own
`dev_client` pattern exactly: a lightweight `TestClient` against
development/test (in-memory) composition, since none of this module's
proofs need a real database (the in-memory `SessionRepository` is
exercised here; `tests/persistence/test_operator_session_repository.py`
separately proves the real Postgres-backed implementation).
"""
from __future__ import annotations

from datetime import timedelta

import pytest

from core.auth import hash_password
from core.timestamps import utc_now

_TEST_PASSWORD = "a genuinely random test operator password 8f3k2"


@pytest.fixture
def dev_client(monkeypatch, tmp_path):
    monkeypatch.setenv("BAGMAN_RUNTIME_ENV", "test")
    monkeypatch.setenv("BAGMAN_AUTH_ENABLED", "true")

    hash_file = tmp_path / "operator_password_hash"
    hash_file.write_text(hash_password(_TEST_PASSWORD), encoding="utf-8")
    monkeypatch.setenv("BAGMAN_OPERATOR_PASSWORD_HASH_FILE", str(hash_file))

    from app.api.composition import reset_composition_for_tests

    reset_composition_for_tests()

    from fastapi.testclient import TestClient

    from app.api.main import app

    with TestClient(app, base_url="https://testserver") as test_client:
        yield test_client

    reset_composition_for_tests()


def _login(client, password=_TEST_PASSWORD):
    return client.post("/auth/login", json={"password": password})


# ---------------------------------------------------------------------
# GET /auth/status — public, no session cookie required
# ---------------------------------------------------------------------


def test_status_reports_unauthenticated_with_no_cookie_present(dev_client):
    response = dev_client.get("/auth/status")
    assert response.status_code == 200
    body = response.json()
    assert body["authenticated"] is False
    assert body["auth_enabled"] is True
    assert body["reason"] == "MISSING"


def test_status_reports_auth_enabled_false_when_the_feature_flag_is_off(monkeypatch, tmp_path):
    monkeypatch.setenv("BAGMAN_RUNTIME_ENV", "test")
    monkeypatch.setenv("BAGMAN_AUTH_ENABLED", "false")

    from app.api.composition import reset_composition_for_tests

    reset_composition_for_tests()
    from fastapi.testclient import TestClient

    from app.api.main import app

    with TestClient(app, base_url="https://testserver") as client:
        body = client.get("/auth/status").json()
        assert body["auth_enabled"] is False
    reset_composition_for_tests()


# ---------------------------------------------------------------------
# POST /auth/login
# ---------------------------------------------------------------------


def test_login_with_correct_password_succeeds_and_sets_cookies(dev_client):
    response = _login(dev_client)
    assert response.status_code == 200
    assert response.json()["authenticated"] is True

    set_cookie_headers = response.headers.get_list("set-cookie")
    joined = " ".join(set_cookie_headers)
    assert "__Host-bagman_session=" in joined
    assert "__Host-bagman_csrf=" in joined
    assert "HttpOnly" in joined  # the session cookie
    assert "Secure" in joined
    assert "samesite=lax" in joined.lower()
    assert "Path=/" in joined
    assert "Domain=" not in joined  # PID §117.C: no Domain attribute


def test_login_with_wrong_password_fails_and_sets_no_cookie(dev_client):
    response = _login(dev_client, password="definitely wrong")
    assert response.status_code == 401
    assert response.json()["error_code"] == "INVALID_CREDENTIALS"
    assert "set-cookie" not in {k.lower() for k in response.headers.keys()}


def test_status_after_successful_login_reports_authenticated(dev_client):
    _login(dev_client)
    response = dev_client.get("/auth/status")
    assert response.json()["authenticated"] is True
    assert response.json()["reason"] == "VALID"


def test_login_without_bootstrapped_credential_returns_503(monkeypatch):
    monkeypatch.setenv("BAGMAN_RUNTIME_ENV", "test")
    monkeypatch.setenv("BAGMAN_AUTH_ENABLED", "true")
    monkeypatch.setenv("BAGMAN_OPERATOR_PASSWORD_HASH_FILE", "/nonexistent/path/to/hash")

    from app.api.composition import reset_composition_for_tests

    reset_composition_for_tests()
    from fastapi.testclient import TestClient

    from app.api.main import app

    with TestClient(app, base_url="https://testserver") as client:
        response = client.post("/auth/login", json={"password": "anything"})
        assert response.status_code == 503
        assert response.json()["error_code"] == "OPERATOR_CREDENTIAL_NOT_CONFIGURED"
    reset_composition_for_tests()


def test_repeated_failed_logins_are_throttled(dev_client):
    for _ in range(5):
        response = _login(dev_client, password="wrong")
        assert response.status_code == 401

    throttled = _login(dev_client, password="wrong")
    assert throttled.status_code == 429
    assert throttled.json()["error_code"] == "LOGIN_THROTTLED"

    # Even the CORRECT password is throttled during the backoff window
    # — a real attacker who happens to also know the right password
    # mid-brute-force is not a scenario this needs to special-case.
    still_throttled = _login(dev_client, password=_TEST_PASSWORD)
    assert still_throttled.status_code == 429


# ---------------------------------------------------------------------
# Session rotation (PID §117.C)
# ---------------------------------------------------------------------


def test_two_logins_issue_different_session_cookies(dev_client):
    first = _login(dev_client)
    first_cookie = dev_client.cookies.get("__Host-bagman_session")
    # A fresh client (same app/composition) performing a second,
    # independent login must get a DIFFERENT token.
    second = _login(dev_client)
    second_cookie = dev_client.cookies.get("__Host-bagman_session")

    assert first.status_code == 200
    assert second.status_code == 200
    assert first_cookie != second_cookie


# ---------------------------------------------------------------------
# POST /auth/logout — protected; requires CSRF + Origin once a session
# exists (exercised fully in tests/security/test_auth_access_control.py);
# here we just prove the happy path with the CSRF/Origin headers a
# well-behaved same-origin browser client would actually send.
# ---------------------------------------------------------------------


def test_logout_revokes_the_session_and_status_reflects_it(dev_client):
    _login(dev_client)
    raw_session_cookie = dev_client.cookies.get("__Host-bagman_session")
    csrf_token = dev_client.cookies.get("__Host-bagman_csrf")

    logout_response = dev_client.post(
        "/auth/logout",
        # `dev_client` is modeled as HTTPS (`base_url="https://testserver"`)
        # — a well-behaved same-origin browser sends an `Origin` whose
        # SCHEME matches the page it was served from, so this must be
        # `https://`, not `http://` (WO-BAGMAN-117 §6.A'.14: full
        # scheme+host+port Origin verification).
        headers={"x-bagman-csrf-token": csrf_token, "origin": "https://testserver"},
    )
    assert logout_response.status_code == 200
    assert logout_response.json()["authenticated"] is False

    # logout clears the cookie client-side (Max-Age=0) — the ordinary
    # browser case: the next status check naturally reports MISSING
    # (no cookie presented at all), never REVOKED.
    status_after_normal_browser_logout = dev_client.get("/auth/status")
    assert status_after_normal_browser_logout.json()["authenticated"] is False
    assert status_after_normal_browser_logout.json()["reason"] == "MISSING"

    # The explicit, distinct REVOKED state (PID §117.E) is proven by
    # presenting the STALE (now-revoked) raw token explicitly — e.g. a
    # second browser tab/device that still held the old cookie, or a
    # replayed/leaked cookie an attacker captured before logout.
    status_with_stale_cookie = dev_client.get(
        "/auth/status", headers={"Cookie": f"__Host-bagman_session={raw_session_cookie}"}
    )
    assert status_with_stale_cookie.json()["authenticated"] is False
    assert status_with_stale_cookie.json()["reason"] == "REVOKED"


def test_logout_without_a_session_cookie_is_gated_by_the_middleware_not_a_500(dev_client):
    """`POST /auth/logout` is itself a protected route (not in the
    public allow-list) — with no session cookie at all, the
    auth-enforcement middleware itself returns 401 before this route's
    own handler ever runs. Whatever the outcome, it must never be a
    500 (``core.auth.SessionRepository.revoke_session``'s own
    idempotent, never-raises contract is what makes the handler itself
    safe to call twice, once a session DOES exist — see
    ``test_logout_revokes_the_session_and_status_reflects_it`` above)."""
    response = dev_client.post("/auth/logout")
    assert response.status_code == 401
    assert response.json()["error_code"] == "UNAUTHENTICATED"
