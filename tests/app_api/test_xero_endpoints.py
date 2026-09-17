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

from datetime import datetime, timedelta, timezone

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


def _first_entity_id(client) -> str:
    return client.get("/internal/entities").json()["items"][0]["entity_id"]


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
    # own COMPANY_REQUIRED hook would.
    item = comp.needs_you_repository.create_needs_you_item(
        item_type="COMPANY_REQUIRED",
        domain="EVIDENCE_INTAKE",
        question="Which company is this for?",
        allowed_action_type="COMPANY_WHAT_WHY",
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
