"""HTTP-level tests for the Xero Account Suggestion Producer
(`xero/account-suggestion-producer` WO) — `POST /internal/xero/{entity_id}
/evidence/{evidence_id}/suggest-account`, `GET .../suggestion`, and the
`XERO_ACCOUNT_REQUIRED` branch of `POST /internal/needs-you/{item_id}
/resolve`. Runs against DEVELOPMENT/TEST composition (in-memory
repositories, `FakeLiteLLMClient`) — mirrors
`tests/app_api/test_xero_endpoints.py`'s own lightweight `TestClient`-
only style.
"""
from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone

import pytest
from fastapi.testclient import TestClient

from app.api.composition import get_composition, reset_composition_for_tests
from app.api.main import app
from core import identity
from services.evidence.classification import (
    CLASSIFICATION_TYPE_DOCUMENT_TYPE,
    DOCUMENT_TYPE_SUPPLIER_INVOICE,
    SOURCE_OPERATOR_ASSIGNED,
    STATUS_CLASSIFIED,
)
from services.xero.account import RawXeroAccount

ACTOR_ID = "xero-account-suggestion-endpoint-tests"


@pytest.fixture
def dev_client(monkeypatch):
    monkeypatch.setenv("BAGMAN_RUNTIME_ENV", "development")
    reset_composition_for_tests()
    with TestClient(app) as test_client:
        yield test_client
    reset_composition_for_tests()


def _first_entity_id(client) -> str:
    return client.get("/internal/entities").json()["items"][0]["entity_id"]


def _entity_id_by_name(client, canonical_name: str) -> str:
    items = client.get("/internal/entities").json()["items"]
    return next(e["entity_id"] for e in items if e["canonical_name"] == canonical_name)


def _register_evidence(composition, entity_id: str, content: bytes = b"Invoice #42 for cloud hosting, GBP 10.") -> str:
    content_hash = {"algorithm": "SHA-256", "value": hashlib.sha256(content).hexdigest()}
    source = composition.api.register_source(
        source_type="MANUAL_UPLOAD", provider="bagman-xero-suggestion-endpoint-tests", status="ACTIVE",
        actor_type="USER", actor_id=ACTOR_ID,
    )
    storage_reference = composition.object_store.put(identity.generate_id(), content_hash, content)
    evidence = composition.api.register_evidence(
        entity_id=entity_id, evidence_type="INVOICE", source_id=source.source_id,
        observed_at=datetime.now(timezone.utc), received_at=datetime.now(timezone.utc),
        content_hash=content_hash, mime_type="text/plain", size_bytes=len(content),
        actor_type="USER", actor_id=ACTOR_ID, storage_reference=storage_reference, status="OBSERVED",
    )
    return evidence.evidence_id


def _classify_supplier_invoice(composition, evidence_id: str) -> None:
    composition.classification_repository.create_classification(
        evidence_id=evidence_id, classification_type=CLASSIFICATION_TYPE_DOCUMENT_TYPE,
        document_type=DOCUMENT_TYPE_SUPPLIER_INVOICE, status=STATUS_CLASSIFIED, source=SOURCE_OPERATOR_ASSIGNED,
        operator_action_id="endpoint-test-operator-action",
    )


def _connect_and_sync(composition, entity_id: str, *, tenant_id="tenant-1", account_id="ACC-1"):
    connection = composition.xero_connection_repository.begin_connect(entity_id=entity_id)
    connection = composition.xero_connection_repository.complete_connect(
        connection.xero_connection_id, tenant_id=tenant_id, tenant_name="Test Co (Xero)", token_expires_at=None,
    )
    connection = composition.xero_connection_repository.record_sync_success(
        connection.xero_connection_id, tenant_name="Test Co (Xero)", token_expires_at=None,
    )
    composition.xero_account_repository.upsert_account(
        entity_id=entity_id, tenant_id=connection.tenant_id,
        raw=RawXeroAccount(
            account_id=account_id, code="400", name="IT & Hosting", type="EXPENSE", account_class="EXPENSE",
            tax_type="NONE", status="ACTIVE", show_in_expense_claims=True,
            reporting_code=None, reporting_code_name=None, updated_date_utc=None,
        ),
        sync_run_id=identity.generate_id(),
    )
    return connection


def _queue_suggestion(composition, *, account_id: str, confidence: float = 0.75):
    composition.litellm_client.queue_success(
        capability_alias="bagman-fast",
        content=json.dumps(
            {"suggested_account_id": account_id, "confidence": confidence, "signals": ["match"], "warnings": []}
        ),
    )


def test_suggest_account_endpoint_produces_suggestion_and_needs_you_item(dev_client):
    composition = get_composition()
    entity_id = _first_entity_id(dev_client)
    evidence_id = _register_evidence(composition, entity_id)
    _classify_supplier_invoice(composition, evidence_id)
    _connect_and_sync(composition, entity_id, account_id="ACC-1")
    _queue_suggestion(composition, account_id="ACC-1")

    resp = dev_client.post(
        f"/internal/xero/{entity_id}/evidence/{evidence_id}/suggest-account",
        json={"actor_type": "USER", "actor_id": ACTOR_ID},
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["outcome"] == "SUGGESTION_PRODUCED"
    assert body["suggestion"]["suggested_account_id"] == "ACC-1"
    assert body["needs_you_item_id"]

    # Never any raw AI invocation internals / secret in the response.
    raw = resp.text
    assert "access_token" not in raw
    assert "refresh_token" not in raw
    assert "client_secret" not in raw

    get_resp = dev_client.get(f"/internal/xero/{entity_id}/evidence/{evidence_id}/suggestion")
    assert get_resp.status_code == 200
    assert get_resp.json()["suggestion"]["suggested_account_id"] == "ACC-1"
    assert get_resp.json()["assignment"] is None


def test_suggest_account_endpoint_unknown_entity_returns_404(dev_client):
    resp = dev_client.post(
        f"/internal/xero/{identity.generate_id()}/evidence/{identity.generate_id()}/suggest-account",
        json={"actor_type": "USER", "actor_id": ACTOR_ID},
    )
    assert resp.status_code == 404


def test_resolve_xero_account_required_item_creates_assignment_and_resolves_item(dev_client):
    composition = get_composition()
    entity_id = _first_entity_id(dev_client)
    evidence_id = _register_evidence(composition, entity_id)
    _classify_supplier_invoice(composition, evidence_id)
    _connect_and_sync(composition, entity_id, account_id="ACC-1")
    _queue_suggestion(composition, account_id="ACC-1")

    suggest_resp = dev_client.post(
        f"/internal/xero/{entity_id}/evidence/{evidence_id}/suggest-account",
        json={"actor_type": "USER", "actor_id": ACTOR_ID},
    )
    item_id = suggest_resp.json()["needs_you_item_id"]

    resolve_resp = dev_client.post(
        f"/internal/needs-you/{item_id}/resolve",
        json={
            "new_status": "RESOLVED",
            "resolution": {"account_id": "ACC-1"},
            "actor_type": "USER",
            "actor_id": ACTOR_ID,
        },
    )
    assert resolve_resp.status_code == 200, resolve_resp.text
    resolved_item = resolve_resp.json()
    assert resolved_item["status"] == "RESOLVED"

    get_resp = dev_client.get(f"/internal/xero/{entity_id}/evidence/{evidence_id}/suggestion")
    assignment = get_resp.json()["assignment"]
    assert assignment is not None
    assert assignment["account_id"] == "ACC-1"
    assert assignment["source"] == "AI_ACCEPTED"


def test_resolve_xero_account_required_item_with_invalid_account_stays_open(dev_client):
    composition = get_composition()
    entity_id = _first_entity_id(dev_client)
    evidence_id = _register_evidence(composition, entity_id)
    _classify_supplier_invoice(composition, evidence_id)
    _connect_and_sync(composition, entity_id, account_id="ACC-1")
    _queue_suggestion(composition, account_id="ACC-1")

    suggest_resp = dev_client.post(
        f"/internal/xero/{entity_id}/evidence/{evidence_id}/suggest-account",
        json={"actor_type": "USER", "actor_id": ACTOR_ID},
    )
    item_id = suggest_resp.json()["needs_you_item_id"]

    resolve_resp = dev_client.post(
        f"/internal/needs-you/{item_id}/resolve",
        json={
            "new_status": "RESOLVED",
            "resolution": {"account_id": "NOT-A-REAL-ACCOUNT"},
            "actor_type": "USER",
            "actor_id": ACTOR_ID,
        },
    )
    assert resolve_resp.status_code == 422

    item_resp = dev_client.get(f"/internal/needs-you/{item_id}")
    assert item_resp.json()["status"] == "OPEN"


def test_get_suggestion_endpoint_never_leaks_a_different_entitys_suggestion(dev_client):
    """Independent-audit finding, fixed before merge: the GET read
    endpoint must never return a suggestion/assignment belonging to a
    DIFFERENT entity than the URL's own `entity_id`, even though
    `get_by_evidence` on both repositories filters only by
    `evidence_id` — this test reproduces the exact live scenario the
    audit used (register entity B's own evidence/connection/account,
    produce a real suggestion for it, then request it back through
    entity A's own URL) and asserts it is treated identically to
    "does not exist"."""
    composition = get_composition()
    entity_a_id = _entity_id_by_name(dev_client, "NOUSTAI_LIMITED")
    entity_b_id = _entity_id_by_name(dev_client, "INFOSECURS_LIMITED")
    assert entity_a_id != entity_b_id

    evidence_b_id = _register_evidence(composition, entity_b_id)
    _classify_supplier_invoice(composition, evidence_b_id)
    _connect_and_sync(composition, entity_b_id, tenant_id="tenant-B-secret", account_id="ACC-B-SECRET")
    _queue_suggestion(composition, account_id="ACC-B-SECRET")

    suggest_resp = dev_client.post(
        f"/internal/xero/{entity_b_id}/evidence/{evidence_b_id}/suggest-account",
        json={"actor_type": "USER", "actor_id": ACTOR_ID},
    )
    assert suggest_resp.json()["outcome"] == "SUGGESTION_PRODUCED"

    # The cross-entity read: entity A's own URL, entity B's evidence_id.
    leak_resp = dev_client.get(f"/internal/xero/{entity_a_id}/evidence/{evidence_b_id}/suggestion")
    assert leak_resp.status_code == 200
    body = leak_resp.json()
    assert body["suggestion"] is None
    assert body["assignment"] is None
    assert "ACC-B-SECRET" not in leak_resp.text
    assert "tenant-B-secret" not in leak_resp.text

    # The correct, same-entity URL still works.
    ok_resp = dev_client.get(f"/internal/xero/{entity_b_id}/evidence/{evidence_b_id}/suggestion")
    assert ok_resp.json()["suggestion"]["suggested_account_id"] == "ACC-B-SECRET"


def test_generic_ai_tasks_endpoint_rejects_xero_account_suggestion_task(dev_client):
    resp = dev_client.post(
        "/internal/ai/tasks",
        json={
            "task_id": "XERO_ACCOUNT_SUGGESTION", "task_version": 1,
            "input_references": {"evidence_id": identity.generate_id()},
            "actor_type": "USER", "actor_id": ACTOR_ID,
        },
    )
    assert resp.status_code == 422
