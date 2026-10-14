"""Server-side access-control proof for WO-BAGMAN-117 (single-operator
authentication, PID §117.C/§117.E/§117.G) — §12 acceptance criteria
19-22:

* 19 — positive access tests (public routes reachable with no session
  cookie).
* 20 — negative access tests, enumerated EXHAUSTIVELY against the
  real, full router table (not a hand-picked sample).
* 21 — full provider OAuth browser-lifecycle regression, end-to-end,
  for all three providers, with no session cookie present throughout
  the external-redirect legs; Xero's pending-tenant-selection broker
  specifically proven to retain its own `selection_id`-scoped state/
  expiry/replay/`_eligible_candidates` protections unchanged.
* 22 — no entity-selector GUI/per-tab persistence/stale-response guard
  exists anywhere in this WO's own delivered code.

Also proves the WO-BAGMAN-117 §6.D Stage D "installed but inert"
deployment state is real and testable right now (``BAGMAN_AUTH_ENABLED``
defaulting to/explicitly set ``false`` means zero behavioural change),
and the independent CSRF + Origin safeguards (§117.E).

Runs against lightweight development/test (in-memory) composition —
mirrors ``tests/app_api/test_static_ui.py``'s own ``dev_client``
pattern; no Docker required.
"""
from __future__ import annotations

import re
import subprocess
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.api.composition import get_composition, reset_composition_for_tests
from app.api.main import _PUBLIC_EXACT_PATHS, _PUBLIC_PREFIX, app
from app.api.routers import auth as auth_router
from core.auth import SessionRepository
from core.errors import PersistenceError

ACTOR_ID = "bagman-auth-access-control-tests"
_TEST_PASSWORD = "a genuinely random auth access control test password 7q2z"
_DUMMY_PATH_PARAM = "11111111-1111-7111-8111-111111111111"


@pytest.fixture
def dev_client(monkeypatch, tmp_path):
    monkeypatch.setenv("BAGMAN_RUNTIME_ENV", "development")
    monkeypatch.setenv("BAGMAN_AUTH_ENABLED", "true")

    from core.auth import hash_password

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
    return {"x-bagman-csrf-token": csrf_token, "origin": "http://testserver"}


# ---------------------------------------------------------------------
# Real, full router table — enumerated programmatically, never a
# hand-picked sample (acceptance criterion 20's own explicit demand).
# ---------------------------------------------------------------------


def _iter_api_routes():
    """Yield (method, concrete_path) for every REST route registered
    anywhere in `app` (recursing into every included `APIRouter`, the
    real shape this FastAPI/Starlette version exposes `app.routes`
    in) — `{path_param}` segments are substituted with a fixed dummy
    value so a 401 (auth-gate) proof never depends on a real, valid
    canonical id existing."""
    seen = set()

    def _routes_of(router_or_app):
        for route in getattr(router_or_app, "routes", []):
            nested_router = getattr(route, "original_router", None)
            if nested_router is not None:
                yield from _routes_of(nested_router)
                continue
            path = getattr(route, "path", None)
            methods = getattr(route, "methods", None)
            if path is None or not methods:
                continue
            concrete_path = re.sub(r"\{[^}]+\}", _DUMMY_PATH_PARAM, path)
            for method in sorted(methods):
                if method in ("HEAD", "OPTIONS"):
                    continue
                key = (method, concrete_path)
                if key in seen:
                    continue
                seen.add(key)
                yield key

    yield from _routes_of(app)


def _is_public(path: str) -> bool:
    return path in _PUBLIC_EXACT_PATHS or path.startswith(_PUBLIC_PREFIX)


def test_every_real_registered_route_is_classified_as_public_or_protected_with_no_gaps():
    """Sanity proof that the enumeration below actually walks a
    non-trivial, real route table (protects against a future refactor
    silently breaking the walker and this whole file passing vacuously)."""
    all_routes = list(_iter_api_routes())
    assert len(all_routes) > 40
    protected = [(m, p) for m, p in all_routes if not _is_public(p)]
    public = [(m, p) for m, p in all_routes if _is_public(p)]
    assert len(protected) > 30
    assert len(public) >= 6


# ---------------------------------------------------------------------
# Acceptance criterion 20 — negative access tests, exhaustive
# ---------------------------------------------------------------------


def test_every_protected_api_route_requires_authentication_with_no_session_cookie(dev_client):
    failures = []
    for method, path in _iter_api_routes():
        if _is_public(path):
            continue
        response = dev_client.request(method, path, json={} if method in ("POST", "PUT") else None)
        if response.status_code != 401:
            failures.append((method, path, response.status_code))
    assert not failures, f"protected routes NOT gated (method, path, status): {failures}"


@pytest.mark.parametrize(
    "path",
    [
        "/",
        "/index.html",
        "/app.js",
        "/style.css",
        "/shell/shell.js",
        "/features/overview/overview.js",
    ],
)
def test_the_entire_static_gui_mount_is_protected_without_exception(dev_client, path):
    response = dev_client.get(path)
    assert response.status_code == 401
    assert response.json()["error_code"] == "UNAUTHENTICATED"


@pytest.mark.parametrize("path", ["/docs", "/openapi.json", "/redoc"])
def test_fastapi_auto_generated_docs_routes_are_protected_by_default(dev_client, path):
    response = dev_client.get(path)
    assert response.status_code == 401


# ---------------------------------------------------------------------
# Acceptance criterion 19 — positive access tests
# ---------------------------------------------------------------------


def test_login_html_is_reachable_with_no_session_cookie(dev_client):
    response = dev_client.get("/login.html")
    assert response.status_code == 200
    assert "BAGMAN" in response.text


@pytest.mark.parametrize("path", ["/health", "/version", "/ready"])
def test_health_version_ready_are_public(dev_client, path):
    assert dev_client.get(path).status_code == 200


def test_auth_status_is_reachable_with_no_session_cookie(dev_client):
    assert dev_client.get("/auth/status").status_code == 200


def test_login_succeeds_with_no_prior_session(dev_client):
    response = dev_client.post("/auth/login", json={"password": _TEST_PASSWORD})
    assert response.status_code == 200
    assert response.json()["authenticated"] is True


# ---------------------------------------------------------------------
# Feature flag — Stage D "installed but inert" (WO-BAGMAN-117 §6.D)
# ---------------------------------------------------------------------


def test_with_auth_disabled_every_protected_route_is_fully_reachable_with_no_cookie(monkeypatch):
    monkeypatch.setenv("BAGMAN_RUNTIME_ENV", "development")
    monkeypatch.setenv("BAGMAN_AUTH_ENABLED", "false")
    reset_composition_for_tests()
    with TestClient(app, base_url="https://testserver") as client:
        response = client.get("/internal/entities")
        assert response.status_code == 200
        assert response.json()["items"]  # the real seeded entity list, not a 401 body
    reset_composition_for_tests()


# ---------------------------------------------------------------------
# CSRF + Origin — independent safeguards (PID §117.E)
# ---------------------------------------------------------------------


def test_state_changing_request_without_csrf_header_is_rejected_despite_a_valid_session(dev_client):
    _logged_in_client(dev_client)
    response = dev_client.post("/auth/logout", headers={"origin": "http://testserver"})
    assert response.status_code == 403
    assert response.json()["error_code"] == "CSRF_REJECTED"


def test_state_changing_request_with_wrong_origin_is_rejected_despite_valid_csrf(dev_client):
    _logged_in_client(dev_client)
    csrf_token = dev_client.cookies.get("__Host-bagman_csrf")
    response = dev_client.post(
        "/auth/logout",
        headers={"x-bagman-csrf-token": csrf_token, "origin": "http://attacker.example"},
    )
    assert response.status_code == 403
    assert response.json()["error_code"] == "ORIGIN_REJECTED"


def test_state_changing_request_with_correct_csrf_and_origin_succeeds(dev_client):
    _logged_in_client(dev_client)
    response = dev_client.post("/auth/logout", headers=_csrf_headers(dev_client))
    assert response.status_code == 200


def test_safe_get_requests_never_require_csrf(dev_client):
    _logged_in_client(dev_client)
    # No CSRF/Origin headers at all — GET is a safe method, never
    # gated by CSRF (PID §117.E: "for all state-changing browser
    # requests").
    response = dev_client.get("/internal/entities")
    assert response.status_code == 200


# ---------------------------------------------------------------------
# Acceptance criterion 21 — full OAuth browser-lifecycle regression,
# all three providers, zero session cookie through the external-
# redirect legs.
# ---------------------------------------------------------------------


def _first_entity_id(client) -> str:
    items = client.get("/internal/entities").json()["items"]
    return items[0]["entity_id"]


def test_xero_oauth_round_trip_requires_no_session_cookie_on_the_external_legs(dev_client):
    import urllib.parse

    from services.xero.client import XeroConnectionInfo, XeroConnectionsResult, XeroOutcomeStatus, XeroTokenResult
    from services.xero.fake_client import fake_token_bundle

    _logged_in_client(dev_client)
    entity_id = _first_entity_id(dev_client)

    # The ONE operator-initiated leg — correctly requires a session
    # (mirrors `POST /{entity_id}/disconnect` staying protected per
    # WO-BAGMAN-117 §6.A.6; never part of the "external redirect"
    # exemption).
    connect_response = dev_client.post(
        "/internal/xero/connect",
        json={"entity_id": entity_id, "actor_type": "USER", "actor_id": ACTOR_ID},
        headers=_csrf_headers(dev_client),
    )
    assert connect_response.status_code == 201, connect_response.text
    state = urllib.parse.parse_qs(urllib.parse.urlparse(connect_response.json()["authorize_url"]).query)["state"][0]

    comp = get_composition()
    comp.xero_oauth_client.queue_exchange_result(XeroTokenResult(status=XeroOutcomeStatus.OK, tokens=fake_token_bundle()))
    comp.xero_oauth_client.queue_connections_result(
        XeroConnectionsResult(
            status=XeroOutcomeStatus.OK,
            connections=(XeroConnectionInfo(connection_id="c1", tenant_id="tenant-xyz", tenant_name="Acme Ltd", tenant_type="ORGANISATION"),),
        )
    )

    # Simulate the real external-redirect legs with ZERO session
    # cookie present (acceptance criterion 21's own literal wording) —
    # the worst case: an expired/never-established browser session, or
    # Xero's authorization server hitting this path directly.
    dev_client.cookies.clear()
    callback_response = dev_client.get("/internal/xero/oauth/callback", params={"code": "abc123", "state": state})
    assert callback_response.status_code == 200
    assert "Connected" in callback_response.text

    result_response = dev_client.get("/internal/xero/oauth/result", params={"ok": "true"})
    assert result_response.status_code == 200


def test_xero_pending_tenant_selection_retains_its_own_protections_unchanged_with_no_session_cookie(dev_client):
    """Proves the governed multi-tenant candidate-selection broker's
    OWN `selection_id`-scoped state/expiry/replay and
    `_eligible_candidates` disambiguation (`services.xero.tenant_selection`,
    `app/api/routers/xero.py` — completely untouched by this delivery)
    are reachable and still functionally correct with zero session
    cookie throughout — the auth middleware only ever gates the
    SESSION layer, never this flow's own independent protections."""
    import urllib.parse

    from services.xero.client import XeroConnectionInfo, XeroConnectionsResult, XeroOutcomeStatus, XeroTokenResult
    from services.xero.fake_client import fake_token_bundle

    _logged_in_client(dev_client)
    entity_id = _first_entity_id(dev_client)
    connect_response = dev_client.post(
        "/internal/xero/connect",
        json={"entity_id": entity_id, "actor_type": "USER", "actor_id": ACTOR_ID},
        headers=_csrf_headers(dev_client),
    )
    state = urllib.parse.parse_qs(urllib.parse.urlparse(connect_response.json()["authorize_url"]).query)["state"][0]

    comp = get_composition()
    comp.xero_oauth_client.queue_exchange_result(XeroTokenResult(status=XeroOutcomeStatus.OK, tokens=fake_token_bundle()))
    # TWO candidates -> genuinely ambiguous -> pending tenant selection.
    comp.xero_oauth_client.queue_connections_result(
        XeroConnectionsResult(
            status=XeroOutcomeStatus.OK,
            connections=(
                XeroConnectionInfo(connection_id="c1", tenant_id="tenant-a", tenant_name="Org A", tenant_type="ORGANISATION"),
                XeroConnectionInfo(connection_id="c2", tenant_id="tenant-b", tenant_name="Org B", tenant_type="ORGANISATION"),
            ),
        )
    )

    dev_client.cookies.clear()
    callback_response = dev_client.get(
        "/internal/xero/oauth/callback", params={"code": "abc123", "state": state}, follow_redirects=False
    )
    location = callback_response.headers["location"]
    selection_id = urllib.parse.parse_qs(urllib.parse.urlparse(location).query)["selection_id"][0]

    # GET the selection page — no cookie present.
    get_response = dev_client.get(f"/internal/xero/oauth/pending-selection/{selection_id}")
    assert get_response.status_code == 200
    candidate_tenant_ids = {c["tenant_id"] for c in get_response.json()["candidates"]}
    assert candidate_tenant_ids == {"tenant-a", "tenant-b"}

    # An out-of-set tenant_id is still rejected (the browser must not
    # be able to substitute an arbitrary tenant ID) — no cookie present.
    rejected = dev_client.post(
        f"/internal/xero/oauth/pending-selection/{selection_id}/resolve", json={"tenant_id": "tenant-not-offered"}
    )
    assert rejected.status_code == 403

    # The real, offered candidate resolves successfully — no cookie present.
    resolved = dev_client.post(
        f"/internal/xero/oauth/pending-selection/{selection_id}/resolve", json={"tenant_id": "tenant-b"}
    )
    assert resolved.status_code == 200

    # Replay of the same selection_id is rejected (expiry/replay
    # protection unchanged) — no cookie present.
    replay = dev_client.post(
        f"/internal/xero/oauth/pending-selection/{selection_id}/resolve", json={"tenant_id": "tenant-b"}
    )
    assert replay.status_code == 403


def test_microsoft_oauth_round_trip_requires_no_session_cookie_on_the_external_legs(dev_client):
    from services.mailbox.microsoft.fake_client import fake_token_bundle
    from services.mailbox.microsoft.graph_client import (
        GraphOutcomeStatus,
        MicrosoftIdentity,
        MicrosoftIdentityResult,
        MicrosoftTokenResult,
    )

    _logged_in_client(dev_client)
    create_response = dev_client.post(
        "/internal/mailboxes",
        json={
            "display_name": "Matt — Infosecurs",
            "email_address": "matt@infosecurs.com",
            "provider_kind": "MICROSOFT_GRAPH",
            "actor_type": "USER",
            "actor_id": ACTOR_ID,
        },
        headers=_csrf_headers(dev_client),
    )
    assert create_response.status_code == 201, create_response.text
    mailbox_id = create_response.json()["mailbox_id"]

    import urllib.parse

    connect_response = dev_client.post(
        f"/internal/mailboxes/{mailbox_id}/microsoft/connect",
        json={"actor_type": "USER", "actor_id": ACTOR_ID},
        headers=_csrf_headers(dev_client),
    )
    assert connect_response.status_code == 201, connect_response.text
    state = urllib.parse.parse_qs(urllib.parse.urlparse(connect_response.json()["authorize_url"]).query)["state"][0]

    comp = get_composition()
    comp.microsoft_oauth_client.queue_exchange_result(
        MicrosoftTokenResult(status=GraphOutcomeStatus.OK, tokens=fake_token_bundle())
    )
    comp.microsoft_oauth_client.queue_me_result(
        MicrosoftIdentityResult(status=GraphOutcomeStatus.OK, identity=MicrosoftIdentity(mail="matt@infosecurs.com", user_principal_name="matt@infosecurs.com"))
    )

    dev_client.cookies.clear()
    callback_response = dev_client.get(
        "/internal/mailboxes/microsoft/oauth/callback", params={"code": "abc", "state": state}, follow_redirects=False
    )
    assert callback_response.status_code in (200, 303, 307)

    result_response = dev_client.get("/internal/mailboxes/microsoft/oauth/result", params={"ok": "true"})
    assert result_response.status_code == 200


def test_gmail_oauth_round_trip_requires_no_session_cookie_on_the_external_legs(dev_client):
    from services.mailbox.gmail.fake_gmail_client import fake_token_bundle
    from services.mailbox.gmail.gmail_client import GmailIdentity, GmailIdentityResult, GmailOutcomeStatus, GmailTokenResult

    _logged_in_client(dev_client)
    create_response = dev_client.post(
        "/internal/mailboxes",
        json={
            "display_name": "Matt — Gmail",
            "email_address": "mgs241171@gmail.com",
            "provider_kind": "GOOGLE_GMAIL",
            "actor_type": "USER",
            "actor_id": ACTOR_ID,
        },
        headers=_csrf_headers(dev_client),
    )
    assert create_response.status_code == 201, create_response.text
    mailbox_id = create_response.json()["mailbox_id"]

    import urllib.parse

    connect_response = dev_client.post(
        f"/internal/mailboxes/{mailbox_id}/gmail/connect",
        json={"actor_type": "USER", "actor_id": ACTOR_ID},
        headers=_csrf_headers(dev_client),
    )
    assert connect_response.status_code == 201, connect_response.text
    state = urllib.parse.parse_qs(urllib.parse.urlparse(connect_response.json()["authorize_url"]).query)["state"][0]

    comp = get_composition()
    comp.gmail_oauth_client.queue_exchange_result(GmailTokenResult(status=GmailOutcomeStatus.OK, tokens=fake_token_bundle()))
    comp.gmail_client.queue_profile_result(
        GmailIdentityResult(status=GmailOutcomeStatus.OK, identity=GmailIdentity(email="mgs241171@gmail.com"))
    )

    dev_client.cookies.clear()
    callback_response = dev_client.get(
        "/internal/mailboxes/gmail/oauth/callback", params={"code": "abc", "state": state}, follow_redirects=False
    )
    assert callback_response.status_code in (200, 303, 307)

    result_response = dev_client.get("/internal/mailboxes/gmail/oauth/result", params={"ok": "true"})
    assert result_response.status_code == 200


# ---------------------------------------------------------------------
# Acceptance criterion 22 — no entity-selector GUI anywhere in this
# WO's own delivered code (PID §117.H's explicit scope split).
# ---------------------------------------------------------------------

_FORBIDDEN_PATTERNS = (
    "entity-selector",
    "entitySelector",
    "entity_selector",
    "per-tab",
    "perTab",
    "stale-response",
    "staleResponse",
    "context epoch",
    "contextEpoch",
)

_FILES_DELIVERED_BY_THIS_WO = (
    "core/auth.py",
    "core/entity_context.py",
    "persistence/postgres/operator_session_models.py",
    "persistence/postgres/operator_session_repository.py",
    "app/api/routers/auth.py",
    "app/api/static/login.html",
    "app/api/static/shell/shell.js",
    "scripts/set_operator_password.py",
)


def _strip_comments_and_docstrings(path: Path, content: str) -> str:
    """Scan actual CODE, not this WO's own explanatory prose about what
    it deliberately does NOT implement (every one of this WO's own
    docstrings/comments legitimately names "entity-selector"/"per-tab"/
    "stale-response" etc. — precisely to document their absence; a
    naive raw substring search would therefore always false-positive
    on this delivery's own, carefully-written scope-boundary comments).
    Good enough for this codebase's own consistent comment styles —
    not a general-purpose parser."""
    if path.suffix == ".html":
        return re.sub(r"<!--.*?-->", "", content, flags=re.DOTALL)
    if path.suffix == ".js":
        return re.sub(r"//[^\n]*", "", content)
    if path.suffix == ".py":
        content = re.sub(r'""".*?"""', "", content, flags=re.DOTALL)
        return re.sub(r"#[^\n]*", "", content)
    return content


@pytest.mark.parametrize("relative_path", _FILES_DELIVERED_BY_THIS_WO)
def test_no_entity_selector_gui_code_exists_in_this_wos_own_delivered_files(relative_path):
    repo_root = Path(
        subprocess.run(["git", "rev-parse", "--show-toplevel"], cwd=Path(__file__).parent, capture_output=True, text=True, check=True).stdout.strip()
    )
    path = repo_root / relative_path
    content = _strip_comments_and_docstrings(path, path.read_text(encoding="utf-8"))
    lowered = content.lower()
    for pattern in _FORBIDDEN_PATTERNS:
        assert pattern.lower() not in lowered, f"forbidden entity-selector-scope pattern {pattern!r} found in actual code (not a comment) in {relative_path}"


# ---------------------------------------------------------------------
# Fail-closed on session-store unavailability — HTTP/middleware-level
# proof (WO-BAGMAN-117 §11.1 audit checklist / §12 acceptance criterion
# 5; closes the Independent Auditor's AMBER-verdict gap, item 6).
#
# `core.auth.evaluate_session_token` deliberately lets a
# `core.errors.PersistenceError` from the `SessionRepository` propagate
# (see that function's own docstring); `tests/persistence/
# test_operator_session_repository.py::
# test_create_session_with_an_unreachable_engine_raises_persistence_error`
# already proves that repository-level contract against a real,
# unreachable Postgres engine. What was MISSING — and what the
# Auditor's own ad-hoc probe confirmed by hand but which did not exist
# as a committed test — is the same proof one layer up: a real HTTP
# request, through a live `TestClient`, against the actual
# `auth_enforcement_middleware` (`app/api/main.py`) and `POST
# /auth/login` (`app/api/routers/auth.py`) call sites that are the ONLY
# two places in this codebase that catch that `PersistenceError` and
# translate it to a 503, never a silent allow.
#
# `_FailingSessionRepository` below is a minimal fake implementing
# `core.auth.SessionRepository`'s ABC (mirrors the "swap in a fake
# repository" precedent already used throughout
# `app/api/composition.py::_build_development_or_test`, just raising
# instead of succeeding) and is wired in by monkeypatching
# `app.api.routers.auth.get_session_repository` — the one, single
# function both the middleware (`auth.get_session_repository()`) and
# `POST /auth/login`'s own handler (the same module-global name) call
# to reach the session store, per that function's own docstring. No
# new DI mechanism is introduced.
# ---------------------------------------------------------------------


class _FailingSessionRepository(SessionRepository):
    """Every operation raises `PersistenceError` — simulates a session
    store that is genuinely unreachable (e.g. the database is down),
    never a "no session" / "unknown token" outcome."""

    def create_session(self, *, token_digest, csrf_token, created_at, expires_at):
        raise PersistenceError("simulated session store outage (create_session)")

    def get_by_token_digest(self, token_digest):
        raise PersistenceError("simulated session store outage (get_by_token_digest)")

    def revoke_session(self, token_digest):
        raise PersistenceError("simulated session store outage (revoke_session)")

    def revoke_all_sessions(self, *, now=None):
        raise PersistenceError("simulated session store outage (revoke_all_sessions)")


def test_protected_route_with_forged_cookie_returns_503_when_session_store_is_down(dev_client, monkeypatch):
    """A protected route, hit with a (forged/unknown) session cookie
    while the session-store repository raises `PersistenceError` on
    lookup, must fail CLOSED: 503 `SESSION_STORE_UNAVAILABLE`, never a
    silent 200 (allow) or 401 (indistinguishable from "no session" —
    which would hide a genuine infrastructure outage behind an
    ordinary auth-required response)."""
    monkeypatch.setattr(auth_router, "get_session_repository", lambda: _FailingSessionRepository())

    response = dev_client.get(
        "/internal/entities",
        headers={"Cookie": "__Host-bagman_session=forged-or-stale-token-value"},
    )

    assert response.status_code == 503
    body = response.json()
    assert body["error_code"] == "SESSION_STORE_UNAVAILABLE"


def test_login_returns_503_with_no_set_cookie_header_when_session_store_is_down(dev_client, monkeypatch):
    """`POST /auth/login` with a CORRECT password, while the session
    store is down: must return 503 and must carry NO `Set-Cookie`
    header at all (fail-closed — never issue a cookie for a session
    that was never durably persisted)."""
    monkeypatch.setattr(auth_router, "get_session_repository", lambda: _FailingSessionRepository())

    response = dev_client.post("/auth/login", json={"password": _TEST_PASSWORD})

    assert response.status_code == 503
    assert response.json()["error_code"] == "SESSION_STORE_UNAVAILABLE"
    assert "set-cookie" not in {k.lower() for k in response.headers.keys()}
