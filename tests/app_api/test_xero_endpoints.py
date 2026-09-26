"""HTTP-level tests for ``/internal/xero/*`` (CD-6 Slice 2, PID §98.4,
architect spec §1-24).

Runs against DEVELOPMENT/TEST composition — in-memory Xero repositories
plus ``FakeXeroOAuthClient``/``FakeXeroAccountingClient`` (this
codebase's established ``Fake*`` pattern; no real Xero Developer App
exists yet, PID §102.1's own stated constraint). Mirrors
``tests/app_api/test_ai_endpoints.py``'s own lightweight
`TestClient`-only style — no Docker required.
"""
from __future__ import annotations

import hashlib
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.api.composition import get_composition, reset_composition_for_tests
from app.api.main import app
from services.xero.client import (
    XeroAccountsResult,
    XeroConnectionInfo,
    XeroConnectionsResult,
    XeroOutcomeStatus,
    XeroTokenResult,
)
from services.xero.account import RawXeroAccount
from services.xero.fake_client import fake_token_bundle

ACTOR_ID = "bagman-xero-endpoint-tests"


@pytest.fixture
def dev_client(monkeypatch):
    monkeypatch.setenv("BAGMAN_RUNTIME_ENV", "development")
    reset_composition_for_tests()
    with TestClient(app) as test_client:
        yield test_client
    reset_composition_for_tests()


def _register_text_evidence(composition, content: bytes = b"Synthetic Xero-endpoint test evidence.") -> str:
    """Register a real, minimal `EvidenceItem` (mirrors
    `tests/app_api/test_ai_endpoints.py::_register_text_evidence`) — a
    COMPANY_REQUIRED item's RESOLVED path (Finding 1 fix) now requires a
    real `source_object_reference` anchoring a real EvidenceItem, so
    this file's own directly-constructed NeedsYouItem fixture needs one
    too."""
    from core import identity

    content_hash = {"algorithm": "SHA-256", "value": hashlib.sha256(content).hexdigest()}
    source = composition.api.register_source(
        source_type="MANUAL_UPLOAD",
        provider="bagman-xero-endpoint-tests",
        status="ACTIVE",
        actor_type="USER",
        actor_id=ACTOR_ID,
    )
    storage_reference = composition.object_store.put(identity.generate_id(), content_hash, content)
    evidence = composition.api.register_evidence(
        entity_id=None,
        evidence_type="INVOICE",
        source_id=source.source_id,
        observed_at=datetime.now(timezone.utc),
        received_at=datetime.now(timezone.utc),
        content_hash=content_hash,
        mime_type="text/plain",
        size_bytes=len(content),
        actor_type="USER",
        actor_id=ACTOR_ID,
        storage_reference=storage_reference,
        status="OBSERVED",
    )
    return evidence.evidence_id


def _first_entity_id(client) -> str:
    return client.get("/internal/entities").json()["items"][0]["entity_id"]


def _entity_id_by_name(client, canonical_name: str) -> str:
    items = client.get("/internal/entities").json()["items"]
    return next(e["entity_id"] for e in items if e["canonical_name"] == canonical_name)


def _begin_connect(client, entity_id: str) -> str:
    """Returns the fresh `state` value for a new connect flow."""
    import urllib.parse

    r = client.post("/internal/xero/connect", json={"entity_id": entity_id, "actor_type": "USER", "actor_id": ACTOR_ID})
    assert r.status_code == 201, r.text
    return urllib.parse.parse_qs(urllib.parse.urlparse(r.json()["authorize_url"]).query)["state"][0]


def _connect_and_complete(client, entity_id, *, tenant_id="tenant-xyz", tenant_name="Acme Ltd") -> None:
    import urllib.parse

    comp = get_composition()
    r = client.post("/internal/xero/connect", json={"entity_id": entity_id, "actor_type": "USER", "actor_id": ACTOR_ID})
    assert r.status_code == 201, r.text
    state = urllib.parse.parse_qs(urllib.parse.urlparse(r.json()["authorize_url"]).query)["state"][0]

    comp.xero_oauth_client.queue_exchange_result(XeroTokenResult(status=XeroOutcomeStatus.OK, tokens=fake_token_bundle()))
    comp.xero_oauth_client.queue_connections_result(
        XeroConnectionsResult(
            status=XeroOutcomeStatus.OK,
            connections=(XeroConnectionInfo(connection_id="c1", tenant_id=tenant_id, tenant_name=tenant_name, tenant_type="ORGANISATION"),),
        )
    )
    r = client.get("/internal/xero/oauth/callback", params={"code": "abc123", "state": state})
    assert r.status_code == 200
    assert "Connected" in r.text
    comp.xero_token_store.write(entity_id, access_token="tok", refresh_token="ref", expires_at=fake_token_bundle().expires_at)


# ---------------------------------------------------------------------
# Canonical company selector — GET /internal/entities enrichment
# ---------------------------------------------------------------------


def test_entities_endpoint_reports_honest_not_connected_status(dev_client):
    r = dev_client.get("/internal/entities")
    assert r.status_code == 200
    for item in r.json()["items"]:
        assert item["xero_connection"]["connected"] is False
        assert item["xero_connection"]["status"] is None


def test_entities_endpoint_reflects_a_real_connection_after_connect(dev_client):
    entity_id = _first_entity_id(dev_client)
    _connect_and_complete(dev_client, entity_id, tenant_name="Real Org Name")

    r = dev_client.get("/internal/entities")
    entity = next(e for e in r.json()["items"] if e["entity_id"] == entity_id)
    assert entity["xero_connection"]["connected"] is True
    assert entity["xero_connection"]["status"] == "CONNECTED"
    assert entity["xero_connection"]["tenant_name"] == "Real Org Name"


# ---------------------------------------------------------------------
# OAuth connect/callback lifecycle
# ---------------------------------------------------------------------


def test_connect_requires_a_real_canonical_entity(dev_client):
    r = dev_client.post("/internal/xero/connect", json={"entity_id": "not-a-real-entity", "actor_type": "USER", "actor_id": ACTOR_ID})
    assert r.status_code == 404


def test_connect_when_xero_not_configured_returns_honest_422(dev_client):
    entity_id = _first_entity_id(dev_client)
    get_composition().xero_oauth_client.configured = False
    r = dev_client.post("/internal/xero/connect", json={"entity_id": entity_id, "actor_type": "USER", "actor_id": ACTOR_ID})
    assert r.status_code == 422
    assert "not configured" in r.json()["message"]


def test_a_pending_connection_can_be_restarted_via_connect_and_mints_a_fresh_state(dev_client):
    """Architect finding, Slice 2 acceptance review: a PENDING
    connection was a real GUI dead end (no action offered) even though
    `begin_connect`/`connect()` have always supported safely restarting
    an in-flight flow. Proves the BACKEND half of that fix still holds
    (the GUI half is `connections.js`'s own new `PENDING` action
    button, verified live on the Mac, not by an automated JS test --
    this repo has no JS test harness): calling `/connect` again on an
    already-PENDING connection succeeds, mints a genuinely fresh
    `state` (not the same value reused), and reuses the SAME
    `xero_connection_id` rather than creating a second row for the
    entity."""
    import urllib.parse

    entity_id = _first_entity_id(dev_client)
    first = dev_client.post("/internal/xero/connect", json={"entity_id": entity_id, "actor_type": "USER", "actor_id": ACTOR_ID})
    assert first.status_code == 201
    first_state = urllib.parse.parse_qs(urllib.parse.urlparse(first.json()["authorize_url"]).query)["state"][0]

    second = dev_client.post("/internal/xero/connect", json={"entity_id": entity_id, "actor_type": "USER", "actor_id": ACTOR_ID})
    assert second.status_code == 201
    second_state = urllib.parse.parse_qs(urllib.parse.urlparse(second.json()["authorize_url"]).query)["state"][0]

    assert second_state != first_state
    assert second.json()["xero_connection_id"] == first.json()["xero_connection_id"]


def test_a_restarted_pending_flow_completes_successfully_with_its_fresh_state(dev_client):
    """The practical case the GUI fix exists for: an operator abandons
    the first attempt, restarts, and completes the SECOND (fresh)
    flow -- this must reach CONNECTED exactly like a first-attempt
    completion always has."""
    entity_id = _first_entity_id(dev_client)
    dev_client.post("/internal/xero/connect", json={"entity_id": entity_id, "actor_type": "USER", "actor_id": ACTOR_ID})
    _connect_and_complete(dev_client, entity_id, tenant_name="Restarted Org")

    r = dev_client.get(f"/internal/xero/{entity_id}")
    assert r.json()["connected"] is True
    assert r.json()["connection"]["tenant_name"] == "Restarted Org"


def test_a_superseded_callback_after_restart_fails_honestly_without_corrupting_the_connection(dev_client):
    """The exact race the architect asked to have verified: restarting
    a PENDING flow mints a SECOND, independently valid `state` while
    the FIRST one (from the abandoned attempt) is still live and
    unconsumed. If the operator completes the second flow (the normal
    case) and then a stale browser tab from the FIRST, abandoned
    attempt is also completed -- a lingering tab, browser back/forward,
    or Xero's own still-authenticated session auto-completing -- that
    second completion must fail honestly (never a raw 500) and must
    NEVER corrupt the already-successful CONNECTED row (never flip it
    to ERROR just because a duplicate/stale browser tab also
    finished). `consume_state` itself correctly, independently
    single-use-consumes EACH state value; this proves the layer above
    it (the connection-status guard) closes the remaining gap."""
    import urllib.parse

    comp = get_composition()
    entity_id = _first_entity_id(dev_client)

    # First attempt (state A) -- abandoned, never completed.
    first = dev_client.post("/internal/xero/connect", json={"entity_id": entity_id, "actor_type": "USER", "actor_id": ACTOR_ID})
    state_a = urllib.parse.parse_qs(urllib.parse.urlparse(first.json()["authorize_url"]).query)["state"][0]

    # Restart (state B) -- this is the one the operator actually completes.
    second = dev_client.post("/internal/xero/connect", json={"entity_id": entity_id, "actor_type": "USER", "actor_id": ACTOR_ID})
    state_b = urllib.parse.parse_qs(urllib.parse.urlparse(second.json()["authorize_url"]).query)["state"][0]
    assert state_b != state_a

    comp.xero_oauth_client.queue_exchange_result(XeroTokenResult(status=XeroOutcomeStatus.OK, tokens=fake_token_bundle()))
    comp.xero_oauth_client.queue_connections_result(
        XeroConnectionsResult(status=XeroOutcomeStatus.OK, connections=(XeroConnectionInfo("c1", "tenant-real", "Real Org", "ORGANISATION"),))
    )
    completed = dev_client.get("/internal/xero/oauth/callback", params={"code": "code-b", "state": state_b})
    assert "Connected" in completed.text

    # Now the STALE, abandoned first tab finally gets completed too --
    # state A itself is still perfectly valid and unconsumed (never
    # replayed), so `consume_state` honestly accepts it; the connection
    # is no longer PENDING, though, so this must fail cleanly.
    comp.xero_oauth_client.queue_exchange_result(XeroTokenResult(status=XeroOutcomeStatus.OK, tokens=fake_token_bundle()))
    comp.xero_oauth_client.queue_connections_result(
        XeroConnectionsResult(status=XeroOutcomeStatus.OK, connections=(XeroConnectionInfo("c1", "tenant-wrong", "Wrong Org", "ORGANISATION"),))
    )
    superseded = dev_client.get("/internal/xero/oauth/callback", params={"code": "code-a", "state": state_a})
    assert superseded.status_code == 200  # honest landing page, never a raw 500
    assert "already completed" in superseded.text.lower()

    # The real, successful connection from state B must be completely
    # untouched -- still CONNECTED, still the real tenant, never
    # flipped to ERROR and never rebound to the second (wrong) tenant.
    r = dev_client.get(f"/internal/xero/{entity_id}")
    assert r.json()["connected"] is True
    assert r.json()["connection"]["status"] == "CONNECTED"
    assert r.json()["connection"]["tenant_id"] == "tenant-real"

    events = comp.api.audit_repository.list_recent(limit=50, event_type_prefix="XERO_CONNECTION_CALLBACK_SUPERSEDED")
    assert len(events) == 1


def test_full_connect_callback_flow_reaches_connected(dev_client):
    entity_id = _first_entity_id(dev_client)
    _connect_and_complete(dev_client, entity_id)
    r = dev_client.get(f"/internal/xero/{entity_id}")
    assert r.json()["connected"] is True
    assert r.json()["connection"]["tenant_id"] == "tenant-xyz"
    # Never a raw token anywhere in the response — only the metadata
    # field `token_expires_at` is present; no `access_token`/
    # `refresh_token` key exists on the contract at all (architect spec
    # §3/§24).
    assert "access_token" not in r.json()["connection"]
    assert "refresh_token" not in r.json()["connection"]
    assert "token_expires_at" in r.json()["connection"]


def test_oauth_state_mismatched_value_is_rejected_with_403(dev_client):
    entity_id = _first_entity_id(dev_client)
    dev_client.post("/internal/xero/connect", json={"entity_id": entity_id, "actor_type": "USER", "actor_id": ACTOR_ID})
    r = dev_client.get("/internal/xero/oauth/callback", params={"code": "abc", "state": "totally-made-up-state-value"})
    assert r.status_code == 200  # a landing page, not a raw error — see router docstring
    assert "failed" in r.text.lower()


def test_oauth_state_expired_value_is_rejected(dev_client, monkeypatch):
    import urllib.parse
    from services.xero import oauth_state as oauth_state_module

    entity_id = _first_entity_id(dev_client)
    r = dev_client.post("/internal/xero/connect", json={"entity_id": entity_id, "actor_type": "USER", "actor_id": ACTOR_ID})
    state = urllib.parse.parse_qs(urllib.parse.urlparse(r.json()["authorize_url"]).query)["state"][0]

    comp = get_composition()
    real_state = comp.oauth_state_repository.get_state(state)
    far_future = real_state.expires_at + timedelta(seconds=1)

    from core.errors import OAuthStateError
    with pytest.raises(OAuthStateError):
        from services.xero.oauth_state import consume_state
        consume_state(comp.oauth_state_repository, state, now=far_future)


def test_oauth_state_replay_is_rejected_at_http_level(dev_client):
    entity_id = _first_entity_id(dev_client)
    _connect_and_complete(dev_client, entity_id)

    comp = get_composition()
    # Re-mint a fresh connect to get a state, consume it once via the
    # real endpoint, then replay the EXACT same query string a second
    # time — the real adversarial HTTP-level proof.
    import urllib.parse
    r = dev_client.post("/internal/xero/connect", json={"entity_id": entity_id, "actor_type": "USER", "actor_id": ACTOR_ID})
    state = urllib.parse.parse_qs(urllib.parse.urlparse(r.json()["authorize_url"]).query)["state"][0]
    comp.xero_oauth_client.queue_exchange_result(XeroTokenResult(status=XeroOutcomeStatus.OK, tokens=fake_token_bundle()))
    comp.xero_oauth_client.queue_connections_result(
        XeroConnectionsResult(status=XeroOutcomeStatus.OK, connections=(XeroConnectionInfo("c1", "tenant-xyz", "Acme Ltd", "ORGANISATION"),))
    )
    first = dev_client.get("/internal/xero/oauth/callback", params={"code": "abc", "state": state})
    assert "Connected" in first.text

    replay = dev_client.get("/internal/xero/oauth/callback", params={"code": "abc", "state": state})
    assert replay.status_code == 200
    assert "failed" in replay.text.lower()


def test_state_rejection_is_audited(dev_client):
    entity_id = _first_entity_id(dev_client)
    dev_client.get("/internal/xero/oauth/callback", params={"code": "abc", "state": "bogus-never-minted"})
    comp = get_composition()
    events = comp.api.audit_repository.list_recent(limit=50, event_type_prefix="XERO_CONNECTION_OAUTH_STATE_REJECTED")
    assert len(events) == 1
    assert events[0].payload["rejected_state_value"] == "bogus-never-minted"


# ---------------------------------------------------------------------
# Sync / accounts / eligibility
# ---------------------------------------------------------------------


def test_accounts_endpoint_honest_when_not_connected(dev_client):
    entity_id = _first_entity_id(dev_client)
    r = dev_client.get(f"/internal/xero/{entity_id}/accounts")
    assert r.json() == {"entity_id": entity_id, "connected": False, "items": [], "count": 0}


def test_sync_now_and_accounts_endpoint_round_trip(dev_client):
    entity_id = _first_entity_id(dev_client)
    _connect_and_complete(dev_client, entity_id)
    comp = get_composition()
    comp.xero_accounting_client.queue_accounts_result(
        XeroAccountsResult(
            status=XeroOutcomeStatus.OK,
            accounts=(
                RawXeroAccount(account_id="A1", code="400", name="Advertising", type="EXPENSE", account_class="EXPENSE", tax_type="NONE", status="ACTIVE", show_in_expense_claims=True, reporting_code=None, reporting_code_name=None, updated_date_utc=None),
            ),
        )
    )
    r = dev_client.post(f"/internal/xero/{entity_id}/sync", json={"actor_type": "USER", "actor_id": ACTOR_ID})
    assert r.status_code == 200
    assert r.json()["status"] == "SUCCEEDED"

    r = dev_client.get(f"/internal/xero/{entity_id}/accounts")
    assert r.json()["connected"] is True
    assert r.json()["count"] == 1
    assert r.json()["items"][0]["account_id"] == "A1"


def test_sync_events_are_audited(dev_client):
    entity_id = _first_entity_id(dev_client)
    _connect_and_complete(dev_client, entity_id)
    comp = get_composition()
    comp.xero_accounting_client.queue_accounts_result(XeroAccountsResult(status=XeroOutcomeStatus.OK, accounts=()))
    dev_client.post(f"/internal/xero/{entity_id}/sync", json={"actor_type": "USER", "actor_id": ACTOR_ID})

    events = comp.api.audit_repository.list_recent(limit=50, event_type_prefix="XERO_SYNC_")
    assert any(e.event_type == "XERO_SYNC_SUCCEEDED" for e in events)


def test_disconnect_events_are_audited_and_clears_status(dev_client):
    entity_id = _first_entity_id(dev_client)
    _connect_and_complete(dev_client, entity_id)
    comp = get_composition()
    r = dev_client.post(f"/internal/xero/{entity_id}/disconnect", json={"actor_type": "USER", "actor_id": ACTOR_ID})
    assert r.json()["status"] == "DISCONNECTED"

    events = comp.api.audit_repository.list_recent(limit=50, event_type_prefix="XERO_CONNECTION_DISCONNECTED")
    assert len(events) == 1

    status = dev_client.get(f"/internal/xero/{entity_id}").json()
    assert status["connected"] is False


def test_connection_events_are_audited_across_the_full_flow(dev_client):
    entity_id = _first_entity_id(dev_client)
    _connect_and_complete(dev_client, entity_id)
    comp = get_composition()
    initiated = comp.api.audit_repository.list_recent(limit=50, event_type_prefix="XERO_CONNECTION_CONNECT_INITIATED")
    connected = comp.api.audit_repository.list_recent(limit=50, event_type_prefix="XERO_CONNECTION_CONNECTED")
    assert len(initiated) == 1
    assert len(connected) == 1
    # Same correlation_id across the whole connect->callback story
    # (architect spec §22).
    assert initiated[0].correlation_id == connected[0].correlation_id


# ---------------------------------------------------------------------
# AI-suggestion candidate-set constraint, over HTTP
# ---------------------------------------------------------------------


def test_resolve_suggestion_endpoint_rejects_out_of_set_value(dev_client):
    entity_id = _first_entity_id(dev_client)
    _connect_and_complete(dev_client, entity_id)
    comp = get_composition()
    comp.xero_accounting_client.queue_accounts_result(
        XeroAccountsResult(status=XeroOutcomeStatus.OK, accounts=(RawXeroAccount("A1", "400", "Advertising", "EXPENSE", "EXPENSE", "NONE", "ACTIVE", True, None, None, None),))
    )
    dev_client.post(f"/internal/xero/{entity_id}/sync", json={"actor_type": "USER", "actor_id": ACTOR_ID})

    r = dev_client.post(f"/internal/xero/{entity_id}/resolve-suggestion", json={"suggested_account_id": "INVENTED"})
    assert r.json()["resolved"] is False
    assert r.json()["account_id"] == "UNRESOLVED"

    r = dev_client.post(f"/internal/xero/{entity_id}/resolve-suggestion", json={"suggested_account_id": "A1"})
    assert r.json()["resolved"] is True
    assert r.json()["account_id"] == "A1"


# ---------------------------------------------------------------------
# Integration-level proof: the GUI's stored selector value is the real
# Xero AccountID, never a display string (architect spec §8/§9/§10)
# ---------------------------------------------------------------------


def test_needs_you_resolution_stores_the_real_account_id_not_a_display_string(dev_client):
    """Simulates exactly what the GUI does (features/needs-you/needs-you.js):
    fetch real eligible accounts for the chosen company, then submit the
    review resolution with the raw `account_id` (never the rendered
    `[Code] Name` label) as `xero_account_id`."""
    entity_id = _first_entity_id(dev_client)
    _connect_and_complete(dev_client, entity_id)
    comp = get_composition()
    comp.xero_accounting_client.queue_accounts_result(
        XeroAccountsResult(
            status=XeroOutcomeStatus.OK,
            accounts=(RawXeroAccount("REAL-ACCOUNT-ID-XYZ", "453", "Hosting & Cloud Services", "EXPENSE", "EXPENSE", "NONE", "ACTIVE", True, None, None, None),),
        )
    )
    dev_client.post(f"/internal/xero/{entity_id}/sync", json={"actor_type": "USER", "actor_id": ACTOR_ID})

    accounts_response = dev_client.get(f"/internal/xero/{entity_id}/accounts").json()
    chosen_account = accounts_response["items"][0]
    display_label = f"[{chosen_account['code']}] {chosen_account['name']}"
    assert display_label == "[453] Hosting & Cloud Services"

    # The item this delivery does not itself need to create via a real
    # intake — created directly, exactly as app/api/routers/intake.py's
    # own COMPANY_REQUIRED hook would. Anchored to a real EvidenceItem
    # (Finding 1 fix requires a real `source_object_reference` to
    # resolve a COMPANY_REQUIRED item to RESOLVED).
    evidence_id = _register_text_evidence(comp)
    item = comp.needs_you_repository.create_needs_you_item(
        item_type="COMPANY_REQUIRED",
        domain="EVIDENCE_INTAKE",
        question="Which company is this for?",
        allowed_action_type="COMPANY_WHAT_WHY",
        source_object_reference=evidence_id,
    )

    r = dev_client.post(
        f"/internal/needs-you/{item.item_id}/resolve",
        json={
            "new_status": "RESOLVED",
            "resolution": {
                "entity_id": entity_id,
                "what": "GPU hosting",
                "why": "R&D infrastructure",
                # The GUI stores `account.account_id` (the SELECT's
                # `value`), never `display_label` (the option's text).
                "xero_account_id": chosen_account["account_id"],
            },
            "actor_type": "USER",
            "actor_id": ACTOR_ID,
        },
    )
    assert r.status_code == 200
    stored = r.json()["resolution"]["xero_account_id"]
    assert stored == "REAL-ACCOUNT-ID-XYZ"
    assert stored != display_label  # the critical negative assertion

    # And it survives a re-read (not just the immediate response).
    refetched = dev_client.get(f"/internal/needs-you/{item.item_id}").json()
    assert refetched["resolution"]["xero_account_id"] == "REAL-ACCOUNT-ID-XYZ"


def test_default_redirect_uri_matches_the_actually_registered_xero_value():
    """PL-review regression guard: PID §102.1's original topology plan
    (`http://localhost:8200/...`) was corrected in §102.2 after Xero's
    own live app-registration UI rejected it ("must use https") -- the
    `http://localhost` exception only applies to Xero's PKCE-only
    "Mobile or desktop app" client type, never to BAGMAN's server-side
    confidential "Web app" registration. `_DEFAULT_REDIRECT_URI` was
    found, during PL review, to still hold the OLD superseded value
    even though PID.md's prose had already been corrected -- a real,
    live-blocking defect (a genuine OAuth attempt would have failed
    with a redirect_uri mismatch against Xero's actually-registered
    URI). This pins the constant to the real, currently-registered
    value so this exact class of "prose corrected, code not" drift can
    never silently recur."""
    from app.api.routers.xero import _DEFAULT_REDIRECT_URI

    assert _DEFAULT_REDIRECT_URI == "https://localhost:8543/internal/xero/oauth/callback"


def test_redirect_uri_override_env_var_takes_effect(monkeypatch):
    """`BAGMAN_XERO_REDIRECT_URI` exists purely for a disposable test/
    dev instance on a different port (the module's own docstring) --
    proves it genuinely overrides the default, live, not just by
    reading the source. Architect finding, live OAuth attempt: verify
    whether an override is active on the real deployment is exactly
    the kind of question that must be answerable by a real check, not
    an assumption -- this is that check, permanently, as a test."""
    from app.api.routers.xero import _redirect_uri, _DEFAULT_REDIRECT_URI

    monkeypatch.delenv("BAGMAN_XERO_REDIRECT_URI", raising=False)
    assert _redirect_uri() == _DEFAULT_REDIRECT_URI

    monkeypatch.setenv("BAGMAN_XERO_REDIRECT_URI", "https://localhost:9999/some/other/callback")
    assert _redirect_uri() == "https://localhost:9999/some/other/callback"


def test_oauth_callback_returns_a_redirect_not_html_with_no_code_or_state_in_the_location(dev_client):
    """Architect hardening finding: 'the OAuth callback endpoint itself
    should not return a normal HTML page while OAuth credential
    material remains in the browser URL.' Proves the actual HTTP
    contract, with redirects NOT auto-followed (unlike every other test
    in this file, which relies on TestClient's default
    follow_redirects=True and only ever sees the FINAL page): a
    successful callback is a 303 to a clean `/oauth/result` URL that
    contains neither `code` nor `state` anywhere in it."""
    import urllib.parse

    comp = get_composition()
    entity_id = _first_entity_id(dev_client)
    connect = dev_client.post("/internal/xero/connect", json={"entity_id": entity_id, "actor_type": "USER", "actor_id": ACTOR_ID})
    state = urllib.parse.parse_qs(urllib.parse.urlparse(connect.json()["authorize_url"]).query)["state"][0]

    comp.xero_oauth_client.queue_exchange_result(XeroTokenResult(status=XeroOutcomeStatus.OK, tokens=fake_token_bundle()))
    comp.xero_oauth_client.queue_connections_result(
        XeroConnectionsResult(status=XeroOutcomeStatus.OK, connections=(XeroConnectionInfo("c1", "tenant-xyz", "Acme Ltd", "ORGANISATION"),))
    )
    r = dev_client.get(
        "/internal/xero/oauth/callback",
        params={"code": "a-real-looking-authorization-code", "state": state},
        follow_redirects=False,
    )
    assert r.status_code == 303
    location = r.headers["location"]
    assert location.startswith("/internal/xero/oauth/result?")
    assert "code=" not in location
    assert "a-real-looking-authorization-code" not in location
    assert "state=" not in location
    assert state not in location
    assert "reason=connected" in location
    assert "ok=true" in location

    # And the redirect target itself renders the honest confirmation,
    # entirely from its OWN query string -- never re-deriving anything
    # from the original code/state.
    landing = dev_client.get(location)
    assert landing.status_code == 200
    assert "Connected" in landing.text


def test_oauth_callback_failure_redirect_also_carries_no_code_or_state(dev_client):
    """Same proof, the failure path: a rejected/replayed state must
    redirect just as cleanly as a success -- never leak `code`/`state`
    into the Location header regardless of outcome."""
    resp = dev_client.get(
        "/internal/xero/oauth/callback",
        params={"code": "whatever-code-value", "state": "a-value-nobody-ever-minted"},
        follow_redirects=False,
    )
    assert resp.status_code == 303
    location = resp.headers["location"]
    assert location.startswith("/internal/xero/oauth/result?")
    assert "whatever-code-value" not in location
    assert "a-value-nobody-ever-minted" not in location
    assert "reason=invalid_state" in location
    assert "ok=false" in location

    landing = dev_client.get(location)
    assert landing.status_code == 200
    assert "failed" in landing.text.lower()


def test_oauth_result_page_ignores_an_unrecognised_reason_key(dev_client):
    """`/oauth/result` is public and unauthenticated (see its own
    docstring) -- a direct GET with an unrecognised `reason` value
    (never producible by BAGMAN's own redirect, but not something an
    attacker is prevented from typing) must fall back to the generic
    message, never echo the raw value back into the page."""
    r = dev_client.get("/internal/xero/oauth/result", params={"ok": "false", "reason": "<script>alert(1)</script>"})
    assert r.status_code == 200
    assert "<script>" not in r.text
    assert "Something went wrong" in r.text


def test_replayed_state_redirect_cannot_establish_a_connection(dev_client):
    """The exact architect ask: 'ensure replayed/expired superseded
    callbacks fail honestly' -- now proven against the redirect
    contract specifically (the existing
    test_oauth_state_replay_is_rejected_at_http_level proves the
    follow-redirects-and-read-the-final-page version of this; this
    proves the connection itself never becomes CONNECTED from the
    replay, checked via the authoritative status endpoint, not just
    the rendered text)."""
    import urllib.parse

    comp = get_composition()
    entity_id = _first_entity_id(dev_client)
    connect = dev_client.post("/internal/xero/connect", json={"entity_id": entity_id, "actor_type": "USER", "actor_id": ACTOR_ID})
    state = urllib.parse.parse_qs(urllib.parse.urlparse(connect.json()["authorize_url"]).query)["state"][0]

    comp.xero_oauth_client.queue_exchange_result(XeroTokenResult(status=XeroOutcomeStatus.OK, tokens=fake_token_bundle()))
    comp.xero_oauth_client.queue_connections_result(
        XeroConnectionsResult(status=XeroOutcomeStatus.OK, connections=(XeroConnectionInfo("c1", "tenant-xyz", "Acme Ltd", "ORGANISATION"),))
    )
    first = dev_client.get("/internal/xero/oauth/callback", params={"code": "abc", "state": state}, follow_redirects=False)
    assert first.status_code == 303
    assert "reason=connected" in first.headers["location"]

    replay = dev_client.get("/internal/xero/oauth/callback", params={"code": "abc", "state": state}, follow_redirects=False)
    assert replay.status_code == 303
    assert "reason=invalid_state" in replay.headers["location"]

    status = dev_client.get(f"/internal/xero/{entity_id}").json()
    assert status["connection"]["tenant_id"] == "tenant-xyz"  # the real, FIRST completion -- untouched by the replay


# ---------------------------------------------------------------------
# Governed tenant selection — architect finding, real live acceptance
# run (Infosecurs + NoustAI): GET /connections can return more than one
# authorised Xero organisation in a single consent grant; array order
# is never identity. See services/xero/tenant_selection.py's own
# module docstring for the full three-way resolution these cases prove
# (architect spec cases A-H, verbatim).
# ---------------------------------------------------------------------


def test_case_a_second_entity_selects_the_unmapped_tenant_irrespective_of_order(dev_client):
    """Case A: returned connections [Infosecurs, NoustAI], Infosecurs
    already bound, connecting NoustAI -> NoustAI selected, irrespective
    of array order."""
    comp = get_composition()
    infosecurs_id = _entity_id_by_name(dev_client, "INFOSECURS_LIMITED")
    noustai_id = _entity_id_by_name(dev_client, "NOUSTAI_LIMITED")

    _connect_and_complete(dev_client, infosecurs_id, tenant_id="tenant-infosecurs", tenant_name="Infosecurs Limited")

    state = _begin_connect(dev_client, noustai_id)
    comp.xero_oauth_client.queue_exchange_result(XeroTokenResult(status=XeroOutcomeStatus.OK, tokens=fake_token_bundle()))
    comp.xero_oauth_client.queue_connections_result(
        XeroConnectionsResult(
            status=XeroOutcomeStatus.OK,
            connections=(
                XeroConnectionInfo("c1", "tenant-infosecurs", "Infosecurs Limited", "ORGANISATION"),
                XeroConnectionInfo("c2", "tenant-noustai", "NoustAI Limited", "ORGANISATION"),
            ),
        )
    )
    r = dev_client.get("/internal/xero/oauth/callback", params={"code": "code-a", "state": state}, follow_redirects=False)
    assert r.status_code == 303
    assert "reason=connected" in r.headers["location"]

    status = dev_client.get(f"/internal/xero/{noustai_id}").json()
    assert status["connected"] is True
    assert status["connection"]["tenant_id"] == "tenant-noustai"
    assert status["connection"]["tenant_name"] == "NoustAI Limited"


def test_case_b_same_result_with_reversed_array_order(dev_client):
    """Case B: returned connections [NoustAI, Infosecurs] (reversed) ->
    same result as case A."""
    comp = get_composition()
    infosecurs_id = _entity_id_by_name(dev_client, "INFOSECURS_LIMITED")
    noustai_id = _entity_id_by_name(dev_client, "NOUSTAI_LIMITED")

    _connect_and_complete(dev_client, infosecurs_id, tenant_id="tenant-infosecurs", tenant_name="Infosecurs Limited")

    state = _begin_connect(dev_client, noustai_id)
    comp.xero_oauth_client.queue_exchange_result(XeroTokenResult(status=XeroOutcomeStatus.OK, tokens=fake_token_bundle()))
    comp.xero_oauth_client.queue_connections_result(
        XeroConnectionsResult(
            status=XeroOutcomeStatus.OK,
            connections=(
                XeroConnectionInfo("c2", "tenant-noustai", "NoustAI Limited", "ORGANISATION"),
                XeroConnectionInfo("c1", "tenant-infosecurs", "Infosecurs Limited", "ORGANISATION"),
            ),
        )
    )
    r = dev_client.get("/internal/xero/oauth/callback", params={"code": "code-b", "state": state}, follow_redirects=False)
    assert r.status_code == 303
    assert "reason=connected" in r.headers["location"]

    status = dev_client.get(f"/internal/xero/{noustai_id}").json()
    assert status["connected"] is True
    assert status["connection"]["tenant_id"] == "tenant-noustai"


def test_case_c_only_already_bound_tenant_returned_fails_honestly_no_cross_mapping(dev_client):
    """Case C: only Infosecurs returned, Infosecurs already bound,
    connecting NoustAI -> honest failure, no cross-company mapping."""
    comp = get_composition()
    infosecurs_id = _entity_id_by_name(dev_client, "INFOSECURS_LIMITED")
    noustai_id = _entity_id_by_name(dev_client, "NOUSTAI_LIMITED")

    _connect_and_complete(dev_client, infosecurs_id, tenant_id="tenant-infosecurs", tenant_name="Infosecurs Limited")

    state = _begin_connect(dev_client, noustai_id)
    comp.xero_oauth_client.queue_exchange_result(XeroTokenResult(status=XeroOutcomeStatus.OK, tokens=fake_token_bundle()))
    comp.xero_oauth_client.queue_connections_result(
        XeroConnectionsResult(
            status=XeroOutcomeStatus.OK,
            connections=(XeroConnectionInfo("c1", "tenant-infosecurs", "Infosecurs Limited", "ORGANISATION"),),
        )
    )
    r = dev_client.get("/internal/xero/oauth/callback", params={"code": "code-c", "state": state}, follow_redirects=False)
    assert r.status_code == 303
    assert "reason=no_eligible_tenant" in r.headers["location"]

    # NoustAI never became connected, and Infosecurs's own binding is
    # completely untouched -- the exact "no cross-company mapping"
    # guarantee this case exists to prove.
    noustai_status = dev_client.get(f"/internal/xero/{noustai_id}").json()
    assert noustai_status["connected"] is False

    infosecurs_status = dev_client.get(f"/internal/xero/{infosecurs_id}").json()
    assert infosecurs_status["connected"] is True
    assert infosecurs_status["connection"]["tenant_id"] == "tenant-infosecurs"


def test_case_d_two_unmapped_tenants_require_governed_operator_choice(dev_client):
    """Case D: two or more unmapped tenants returned -> no implicit
    selection; governed operator choice required."""
    comp = get_composition()
    noustai_id = _entity_id_by_name(dev_client, "NOUSTAI_LIMITED")

    state = _begin_connect(dev_client, noustai_id)
    comp.xero_oauth_client.queue_exchange_result(XeroTokenResult(status=XeroOutcomeStatus.OK, tokens=fake_token_bundle()))
    comp.xero_oauth_client.queue_connections_result(
        XeroConnectionsResult(
            status=XeroOutcomeStatus.OK,
            connections=(
                XeroConnectionInfo("c1", "tenant-x", "Organisation X", "ORGANISATION"),
                XeroConnectionInfo("c2", "tenant-y", "Organisation Y", "ORGANISATION"),
            ),
        )
    )
    r = dev_client.get("/internal/xero/oauth/callback", params={"code": "code-d", "state": state}, follow_redirects=False)
    assert r.status_code == 303
    assert "reason=tenant_selection_required" in r.headers["location"]

    # No implicit selection happened -- NoustAI is still unconnected.
    assert dev_client.get(f"/internal/xero/{noustai_id}").json()["connected"] is False

    import urllib.parse
    selection_id = urllib.parse.parse_qs(urllib.parse.urlparse(r.headers["location"]).query)["selection_id"][0]

    candidates = dev_client.get(f"/internal/xero/oauth/pending-selection/{selection_id}").json()
    assert {c["tenant_id"] for c in candidates["candidates"]} == {"tenant-x", "tenant-y"}

    # The operator's real, governed choice.
    resolved = dev_client.post(f"/internal/xero/oauth/pending-selection/{selection_id}/resolve", json={"tenant_id": "tenant-y"})
    assert resolved.status_code == 200
    assert resolved.json()["tenant_id"] == "tenant-y"

    status = dev_client.get(f"/internal/xero/{noustai_id}").json()
    assert status["connected"] is True
    assert status["connection"]["tenant_id"] == "tenant-y"
    assert status["connection"]["tenant_name"] == "Organisation Y"


def test_case_e_browser_cannot_substitute_an_arbitrary_tenant_id(dev_client):
    """Case E: browser attempts tenant not in authorised candidate set
    -> rejected."""
    comp = get_composition()
    noustai_id = _entity_id_by_name(dev_client, "NOUSTAI_LIMITED")

    state = _begin_connect(dev_client, noustai_id)
    comp.xero_oauth_client.queue_exchange_result(XeroTokenResult(status=XeroOutcomeStatus.OK, tokens=fake_token_bundle()))
    comp.xero_oauth_client.queue_connections_result(
        XeroConnectionsResult(
            status=XeroOutcomeStatus.OK,
            connections=(
                XeroConnectionInfo("c1", "tenant-x", "Organisation X", "ORGANISATION"),
                XeroConnectionInfo("c2", "tenant-y", "Organisation Y", "ORGANISATION"),
            ),
        )
    )
    r = dev_client.get("/internal/xero/oauth/callback", params={"code": "code-e", "state": state}, follow_redirects=False)
    import urllib.parse
    selection_id = urllib.parse.parse_qs(urllib.parse.urlparse(r.headers["location"]).query)["selection_id"][0]

    hijack = dev_client.post(
        f"/internal/xero/oauth/pending-selection/{selection_id}/resolve",
        json={"tenant_id": "tenant-attacker-supplied-not-authorised"},
    )
    assert hijack.status_code == 403
    assert dev_client.get(f"/internal/xero/{noustai_id}").json()["connected"] is False

    # The real, legitimate candidates are still resolvable afterward --
    # a rejected substitution attempt does not itself burn the
    # selection (only a SUCCESSFUL resolve consumes it).
    real = dev_client.post(f"/internal/xero/oauth/pending-selection/{selection_id}/resolve", json={"tenant_id": "tenant-x"})
    assert real.status_code == 200


def test_case_f_infosecurs_connection_and_accounts_untouched_by_noustai_attempts(dev_client):
    """Case F: existing Infosecurs connection, tokens and account
    projection remain untouched throughout both a failed and a
    successful NoustAI attempt."""
    comp = get_composition()
    infosecurs_id = _entity_id_by_name(dev_client, "INFOSECURS_LIMITED")
    noustai_id = _entity_id_by_name(dev_client, "NOUSTAI_LIMITED")

    _connect_and_complete(dev_client, infosecurs_id, tenant_id="tenant-infosecurs", tenant_name="Infosecurs Limited")
    comp.xero_accounting_client.queue_accounts_result(
        XeroAccountsResult(
            status=XeroOutcomeStatus.OK,
            accounts=(
                RawXeroAccount("A1", "400", "Advertising", "EXPENSE", "EXPENSE", "NONE", "ACTIVE", True, None, None, None),
                RawXeroAccount("A2", "200", "Sales", "REVENUE", "REVENUE", "OUTPUT2", "ACTIVE", False, None, None, None),
            ),
        )
    )
    sync = dev_client.post(f"/internal/xero/{infosecurs_id}/sync", json={"actor_type": "USER", "actor_id": ACTOR_ID})
    assert sync.status_code == 200 and sync.json()["status"] == "SUCCEEDED"

    infosecurs_before = dev_client.get(f"/internal/xero/{infosecurs_id}").json()
    accounts_before = dev_client.get(f"/internal/xero/{infosecurs_id}/accounts").json()
    assert accounts_before["count"] == 2

    # A FAILED NoustAI attempt (case C's own shape).
    state = _begin_connect(dev_client, noustai_id)
    comp.xero_oauth_client.queue_exchange_result(XeroTokenResult(status=XeroOutcomeStatus.OK, tokens=fake_token_bundle()))
    comp.xero_oauth_client.queue_connections_result(
        XeroConnectionsResult(status=XeroOutcomeStatus.OK, connections=(XeroConnectionInfo("c1", "tenant-infosecurs", "Infosecurs Limited", "ORGANISATION"),))
    )
    dev_client.get("/internal/xero/oauth/callback", params={"code": "code-f1", "state": state}, follow_redirects=False)

    # A SUCCESSFUL NoustAI attempt right after.
    _connect_and_complete(dev_client, noustai_id, tenant_id="tenant-noustai", tenant_name="NoustAI Limited")

    infosecurs_after = dev_client.get(f"/internal/xero/{infosecurs_id}").json()
    accounts_after = dev_client.get(f"/internal/xero/{infosecurs_id}/accounts").json()
    assert infosecurs_after == infosecurs_before
    assert accounts_after == accounts_before


def test_case_g_no_token_written_for_noustai_until_tenant_resolution_succeeds(dev_client):
    """Case G: no token is written to NoustAI until tenant resolution
    has succeeded."""
    comp = get_composition()
    noustai_id = _entity_id_by_name(dev_client, "NOUSTAI_LIMITED")

    state = _begin_connect(dev_client, noustai_id)
    comp.xero_oauth_client.queue_exchange_result(XeroTokenResult(status=XeroOutcomeStatus.OK, tokens=fake_token_bundle()))
    comp.xero_oauth_client.queue_connections_result(
        XeroConnectionsResult(
            status=XeroOutcomeStatus.OK,
            connections=(
                XeroConnectionInfo("c1", "tenant-x", "Organisation X", "ORGANISATION"),
                XeroConnectionInfo("c2", "tenant-y", "Organisation Y", "ORGANISATION"),
            ),
        )
    )
    r = dev_client.get("/internal/xero/oauth/callback", params={"code": "code-g", "state": state}, follow_redirects=False)
    assert "reason=tenant_selection_required" in r.headers["location"]

    # Ambiguity alone must never write a token.
    assert comp.xero_token_store.read(noustai_id) is None

    import urllib.parse
    selection_id = urllib.parse.parse_qs(urllib.parse.urlparse(r.headers["location"]).query)["selection_id"][0]
    resolved = dev_client.post(f"/internal/xero/oauth/pending-selection/{selection_id}/resolve", json={"tenant_id": "tenant-x"})
    assert resolved.status_code == 200

    # Only NOW, after a successful, validated resolution, is a token written.
    assert comp.xero_token_store.read(noustai_id) is not None


def test_case_h_state_expiry_replay_and_supersession_protections_remain_intact(dev_client):
    """Case H: existing state expiry/replay/supersession protections
    remain intact through the new multi-candidate code path -- the
    `state` value is still consumed exactly once at the TOP of
    `oauth_callback`, before any tenant-resolution branching, so a
    second presentation of the SAME state (regardless of which
    resolution branch the first presentation took) is rejected
    identically to every other callback outcome."""
    comp = get_composition()
    noustai_id = _entity_id_by_name(dev_client, "NOUSTAI_LIMITED")

    state = _begin_connect(dev_client, noustai_id)
    comp.xero_oauth_client.queue_exchange_result(XeroTokenResult(status=XeroOutcomeStatus.OK, tokens=fake_token_bundle()))
    comp.xero_oauth_client.queue_connections_result(
        XeroConnectionsResult(
            status=XeroOutcomeStatus.OK,
            connections=(
                XeroConnectionInfo("c1", "tenant-x", "Organisation X", "ORGANISATION"),
                XeroConnectionInfo("c2", "tenant-y", "Organisation Y", "ORGANISATION"),
            ),
        )
    )
    first = dev_client.get("/internal/xero/oauth/callback", params={"code": "code-h", "state": state}, follow_redirects=False)
    assert "reason=tenant_selection_required" in first.headers["location"]

    replay = dev_client.get("/internal/xero/oauth/callback", params={"code": "code-h", "state": state}, follow_redirects=False)
    assert replay.status_code == 303
    assert "reason=invalid_state" in replay.headers["location"]


def test_connection_status_reports_account_count_zero_for_a_never_connected_entity(dev_client):
    """Architect finding, live acceptance run: 'Matthew Scott Personal
    currently renders undefined account(s) synced.' Root cause: `GET
    /internal/xero/{entity_id}` omitted `account_count`/
    `reference_data_stale` entirely when `connection is None` (an
    entity with no XeroConnection row at all), so the GUI's
    `status.account_count` was JS `undefined`, rendered verbatim into
    the template string. Proves the real HTTP contract now always
    includes these fields, honestly zero/false, never absent."""
    entity_id = _entity_id_by_name(dev_client, "MATTHEW_SCOTT_PERSONAL")
    r = dev_client.get(f"/internal/xero/{entity_id}")
    assert r.status_code == 200
    body = r.json()
    assert body["connected"] is False
    assert body["connection"] is None
    assert body["account_count"] == 0
    assert body["reference_data_stale"] is False


def test_connections_js_never_renders_the_literal_string_undefined_for_account_count():
    """Belt-and-braces on the same finding, at the GUI layer: even if a
    future response shape regressed, the rendering line itself must
    coalesce a non-numeric `account_count` to a real fallback before
    ever reaching template interpolation."""
    source = (Path(__file__).resolve().parents[2] / "app/api/static/features/xero/connections.js").read_text()
    assert 'typeof status.account_count === "number" ? status.account_count : 0' in source
    assert "${status.account_count} account" not in source
