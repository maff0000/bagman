"""HTTP-level tests for ``/internal/mailboxes/*`` (CD-6 Slice 3: Mailbox
Management, TAB 1 / Email).

Runs against DEVELOPMENT/TEST composition — the in-memory
``MailboxSourceRepository`` (no real provider of any kind is ever
constructed for this feature — see ``app/api/composition.py``'s own
"CD-6 Slice 3" note). Mirrors ``tests/app_api/test_xero_endpoints.py``'s
own lightweight ``TestClient``-only style — no Docker required.
"""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from app.api.composition import get_composition, reset_composition_for_tests
from app.api.main import app

ACTOR_ID = "bagman-mailbox-endpoint-tests"


@pytest.fixture
def dev_client(monkeypatch):
    monkeypatch.setenv("BAGMAN_RUNTIME_ENV", "development")
    reset_composition_for_tests()
    with TestClient(app) as test_client:
        yield test_client
    reset_composition_for_tests()


def _first_entity_id(client) -> str:
    return client.get("/internal/entities").json()["items"][0]["entity_id"]


def _create(client, *, email="matt@infosecurs.com", display_name="Matt — Infosecurs", provider_kind="MICROSOFT_GRAPH", default_entity_id=None):
    return client.post(
        "/internal/mailboxes",
        json={
            "display_name": display_name,
            "email_address": email,
            "provider_kind": provider_kind,
            "default_entity_id": default_entity_id,
            "actor_type": "USER",
            "actor_id": ACTOR_ID,
        },
    )


# ---------------------------------------------------------------------
# Create / list / get
# ---------------------------------------------------------------------


def test_create_mailbox_returns_201_active_not_configured(dev_client):
    r = _create(dev_client)
    assert r.status_code == 201, r.text
    body = r.json()
    assert body["status"] == "ACTIVE"
    assert body["enabled"] is True
    assert body["connection_state"] == "NOT_CONFIGURED"
    assert body["email_address"] == "matt@infosecurs.com"


def test_list_mailboxes_includes_the_created_row(dev_client):
    _create(dev_client)
    r = dev_client.get("/internal/mailboxes")
    assert r.status_code == 200
    assert r.json()["count"] == 1
    assert r.json()["items"][0]["email_address"] == "matt@infosecurs.com"


def test_get_single_mailbox(dev_client):
    created = _create(dev_client).json()
    r = dev_client.get(f"/internal/mailboxes/{created['mailbox_id']}")
    assert r.status_code == 200
    assert r.json()["mailbox_id"] == created["mailbox_id"]


def test_get_unknown_mailbox_id_returns_404(dev_client):
    r = dev_client.get("/internal/mailboxes/not-a-real-id")
    assert r.status_code == 404


def test_create_with_real_entity_hint_is_accepted_and_resolvable(dev_client):
    entity_id = _first_entity_id(dev_client)
    r = _create(dev_client, default_entity_id=entity_id)
    assert r.status_code == 201
    assert r.json()["default_entity_id"] == entity_id


def test_create_with_unknown_entity_hint_returns_404(dev_client):
    r = _create(dev_client, default_entity_id="not-a-real-entity")
    assert r.status_code == 404


def test_create_with_malformed_email_returns_422(dev_client):
    r = _create(dev_client, email="not-an-email")
    assert r.status_code == 422


def test_create_with_ungoverned_provider_kind_returns_422(dev_client):
    r = _create(dev_client, provider_kind="SOME_OTHER_PROVIDER")
    assert r.status_code == 422


def test_duplicate_email_returns_409(dev_client):
    _create(dev_client, email="matt@infosecurs.com")
    r = _create(dev_client, email="MATT@INFOSECURS.COM", display_name="Second attempt")
    assert r.status_code == 409


# ---------------------------------------------------------------------
# Update (PUT — full replace)
# ---------------------------------------------------------------------


def test_update_mailbox_full_replace(dev_client):
    created = _create(dev_client).json()
    r = dev_client.put(
        f"/internal/mailboxes/{created['mailbox_id']}",
        json={
            "display_name": "Renamed",
            "email_address": created["email_address"],
            "provider_kind": "IMAP",
            "default_entity_id": None,
            "actor_type": "USER",
            "actor_id": ACTOR_ID,
        },
    )
    assert r.status_code == 200
    assert r.json()["display_name"] == "Renamed"
    assert r.json()["provider_kind"] == "IMAP"


def test_update_with_unknown_entity_hint_returns_404(dev_client):
    created = _create(dev_client).json()
    r = dev_client.put(
        f"/internal/mailboxes/{created['mailbox_id']}",
        json={
            "display_name": created["display_name"],
            "email_address": created["email_address"],
            "provider_kind": created["provider_kind"],
            "default_entity_id": "not-a-real-entity",
            "actor_type": "USER",
            "actor_id": ACTOR_ID,
        },
    )
    assert r.status_code == 404


def test_update_unknown_mailbox_id_returns_404(dev_client):
    r = dev_client.put(
        "/internal/mailboxes/not-a-real-id",
        json={
            "display_name": "X",
            "email_address": "x@infosecurs.com",
            "provider_kind": "IMAP",
            "default_entity_id": None,
            "actor_type": "USER",
            "actor_id": ACTOR_ID,
        },
    )
    assert r.status_code == 404


# ---------------------------------------------------------------------
# Lifecycle: enable / disable / retire
# ---------------------------------------------------------------------


def test_enable_disable_round_trip(dev_client):
    created = _create(dev_client).json()
    mailbox_id = created["mailbox_id"]

    r = dev_client.post(f"/internal/mailboxes/{mailbox_id}/disable", json={"actor_type": "USER", "actor_id": ACTOR_ID})
    assert r.status_code == 200
    assert r.json()["status"] == "DISABLED"
    assert r.json()["enabled"] is False

    r = dev_client.post(f"/internal/mailboxes/{mailbox_id}/enable", json={"actor_type": "USER", "actor_id": ACTOR_ID})
    assert r.status_code == 200
    assert r.json()["status"] == "ACTIVE"
    assert r.json()["enabled"] is True


def test_retire_preserves_the_row_and_reports_retired(dev_client):
    created = _create(dev_client).json()
    mailbox_id = created["mailbox_id"]

    r = dev_client.post(f"/internal/mailboxes/{mailbox_id}/retire", json={"actor_type": "USER", "actor_id": ACTOR_ID})
    assert r.status_code == 200
    assert r.json()["status"] == "RETIRED"

    # Still resolvable — never physically deleted.
    r = dev_client.get(f"/internal/mailboxes/{mailbox_id}")
    assert r.status_code == 200
    assert r.json()["status"] == "RETIRED"


def test_retired_mailbox_cannot_be_re_enabled(dev_client):
    created = _create(dev_client).json()
    mailbox_id = created["mailbox_id"]
    dev_client.post(f"/internal/mailboxes/{mailbox_id}/retire", json={"actor_type": "USER", "actor_id": ACTOR_ID})

    r = dev_client.post(f"/internal/mailboxes/{mailbox_id}/enable", json={"actor_type": "USER", "actor_id": ACTOR_ID})
    assert r.status_code == 500  # InvalidStateTransitionError -> 500, mirrors XeroSyncRun's own terminal-state mapping


def test_re_retiring_an_already_retired_mailbox_is_a_safe_200_not_a_500(dev_client):
    """PL-review finding: the exact 'second stale browser tab' class of
    bug Slice 2 caught twice — retiring an already-RETIRED mailbox a
    second time (e.g. a double-click, or two tabs open on the same
    mailbox) must be a safe, idempotent 200, never a raw 500."""
    created = _create(dev_client).json()
    mailbox_id = created["mailbox_id"]
    dev_client.post(f"/internal/mailboxes/{mailbox_id}/retire", json={"actor_type": "USER", "actor_id": ACTOR_ID})

    r = dev_client.post(f"/internal/mailboxes/{mailbox_id}/retire", json={"actor_type": "USER", "actor_id": ACTOR_ID})
    assert r.status_code == 200
    assert r.json()["status"] == "RETIRED"


def test_re_enabling_an_already_active_mailbox_is_a_safe_200_not_a_500(dev_client):
    created = _create(dev_client).json()
    mailbox_id = created["mailbox_id"]
    assert created["status"] == "ACTIVE"

    r = dev_client.post(f"/internal/mailboxes/{mailbox_id}/enable", json={"actor_type": "USER", "actor_id": ACTOR_ID})
    assert r.status_code == 200
    assert r.json()["status"] == "ACTIVE"


def test_re_disabling_an_already_disabled_mailbox_is_a_safe_200_not_a_500(dev_client):
    created = _create(dev_client).json()
    mailbox_id = created["mailbox_id"]
    dev_client.post(f"/internal/mailboxes/{mailbox_id}/disable", json={"actor_type": "USER", "actor_id": ACTOR_ID})

    r = dev_client.post(f"/internal/mailboxes/{mailbox_id}/disable", json={"actor_type": "USER", "actor_id": ACTOR_ID})
    assert r.status_code == 200
    assert r.json()["status"] == "DISABLED"


def test_lifecycle_actions_on_unknown_mailbox_id_return_404(dev_client):
    for action in ("enable", "disable", "retire"):
        r = dev_client.post(f"/internal/mailboxes/not-a-real-id/{action}", json={"actor_type": "USER", "actor_id": ACTOR_ID})
        assert r.status_code == 404


# ---------------------------------------------------------------------
# Audit trail
# ---------------------------------------------------------------------


def test_every_mutation_is_audited(dev_client):
    comp = get_composition()
    created = _create(dev_client).json()
    mailbox_id = created["mailbox_id"]

    dev_client.put(
        f"/internal/mailboxes/{mailbox_id}",
        json={
            "display_name": "Renamed",
            "email_address": created["email_address"],
            "provider_kind": created["provider_kind"],
            "default_entity_id": None,
            "actor_type": "USER",
            "actor_id": ACTOR_ID,
        },
    )
    dev_client.post(f"/internal/mailboxes/{mailbox_id}/disable", json={"actor_type": "USER", "actor_id": ACTOR_ID})
    dev_client.post(f"/internal/mailboxes/{mailbox_id}/enable", json={"actor_type": "USER", "actor_id": ACTOR_ID})
    dev_client.post(f"/internal/mailboxes/{mailbox_id}/retire", json={"actor_type": "USER", "actor_id": ACTOR_ID})

    for event_type in ("MAILBOX_CREATED", "MAILBOX_UPDATED", "MAILBOX_DISABLED", "MAILBOX_ENABLED", "MAILBOX_RETIRED"):
        events = comp.api.audit_repository.list_recent(limit=50, event_type_prefix=event_type)
        assert len(events) == 1, f"expected exactly one {event_type} audit event"


def test_audit_payloads_never_contain_anything_secret_shaped(dev_client):
    comp = get_composition()
    created = _create(dev_client).json()
    mailbox_id = created["mailbox_id"]
    dev_client.post(f"/internal/mailboxes/{mailbox_id}/disable", json={"actor_type": "USER", "actor_id": ACTOR_ID})

    events = comp.api.audit_repository.list_recent(limit=50)
    mailbox_events = [e for e in events if e.subject_type == "MailboxSource"]
    assert mailbox_events
    forbidden = ("password", "secret", "token", "credential", "api_key", "private_key")
    for event in mailbox_events:
        flat = str(event.payload).lower()
        for bad in forbidden:
            assert bad not in flat, f"audit payload contains a secret-shaped token '{bad}': {event.payload}"


# ---------------------------------------------------------------------
# Contract-shape proof: no secret field is ever accepted or emitted
# ---------------------------------------------------------------------


def test_response_shape_never_contains_a_secret_field(dev_client):
    r = _create(dev_client, email="secretcheck@infosecurs.com")
    body = r.json()
    forbidden = ("password", "secret", "token", "credential", "api_key", "private_key")
    for key in body:
        lowered = key.lower()
        for bad in forbidden:
            assert bad not in lowered, f"response contains a secret-shaped field name: {key}"


def test_connection_state_can_never_be_supplied_by_the_caller(dev_client):
    """The create/update request models simply have no
    `connection_state` field at all — supplying one is silently
    ignored by pydantic (unknown field), never honoured. Proven here by
    confirming the created mailbox is NOT_CONFIGURED regardless."""
    r = dev_client.post(
        "/internal/mailboxes",
        json={
            "display_name": "Attempted spoof",
            "email_address": "spoof@infosecurs.com",
            "provider_kind": "MICROSOFT_GRAPH",
            "connection_state": "CONNECTED",
            "actor_type": "USER",
            "actor_id": ACTOR_ID,
        },
    )
    assert r.status_code == 201
    assert r.json()["connection_state"] == "NOT_CONFIGURED"


def test_mailbox_id_and_timestamps_can_never_be_supplied_by_the_caller(dev_client):
    spoofed_id = "01a0b158-0000-7000-8000-000000000000"
    r = dev_client.post(
        "/internal/mailboxes",
        json={
            "mailbox_id": spoofed_id,
            "display_name": "Attempted spoof",
            "email_address": "spoof2@infosecurs.com",
            "provider_kind": "MICROSOFT_GRAPH",
            "created_at": "2000-01-01T00:00:00Z",
            "actor_type": "USER",
            "actor_id": ACTOR_ID,
        },
    )
    assert r.status_code == 201
    assert r.json()["mailbox_id"] != spoofed_id
    assert not r.json()["created_at"].startswith("2000-01-01")


# ---------------------------------------------------------------------
# CD-6 mailbox-list GUI-completion WO — the three new GUI-list-only
# fields ``GET /internal/mailboxes`` attaches to every row
# (``relevant_message_count``/``needs_review_count``/``last_sweep_status``
# — see ``app/api/routers/mailboxes.py::list_mailboxes``'s own
# docstring). Fixtures seed the underlying repositories directly
# (mirrors this file's own ``test_every_mutation_is_audited``'s "reach
# into ``get_composition()`` for state a plain HTTP round trip cannot
# cheaply arrange" convention) rather than running a real sweep, which
# would be a much heavier, adapter-dependent setup for what is really a
# narrow aggregation-logic proof.
# ---------------------------------------------------------------------

from core import identity  # noqa: E402 — grouped with this section's own tests, not the file's top-level imports
from services.mailbox.message import FOLDER_INBOX, INGESTION_STATUS_CHECKED_NOT_CANDIDATE
from services.mailbox.sweep_run import TRIGGER_MANUAL
from services.needs_you.needs_you import (
    ALLOWED_ACTION_MAILBOX_DOMAIN_REVIEW,
    ITEM_TYPE_MAILBOX_AUTHENTICATION_ESCALATION,
    ITEM_TYPE_MAILBOX_DOMAIN_REVIEW,
)


def _seed_message(comp, *, mailbox_id, msg_id, discovery_candidate):
    from core.timestamps import utc_now

    comp.mailbox_message_repository.record_observation(
        mailbox_id=mailbox_id,
        provider_kind="MICROSOFT_GRAPH",
        immutable_provider_message_id=msg_id,
        internet_message_id=None,
        observed_folder=FOLDER_INBOX,
        subject="Invoice",
        sender_address="vendor@example.com",
        sender_display_name="Vendor",
        received_at=utc_now(),
        has_attachments=False,
        ingestion_status=INGESTION_STATUS_CHECKED_NOT_CANDIDATE,
        sender_domain="example.com",
        discovery_candidate=discovery_candidate,
    )


def test_list_mailboxes_relevant_message_count_defaults_to_zero_and_never_null(dev_client):
    _create(dev_client)
    body = dev_client.get("/internal/mailboxes").json()
    assert body["items"][0]["relevant_message_count"] == 0


def test_list_mailboxes_relevant_message_count_counts_only_true_discovery_candidates(dev_client):
    comp = get_composition()
    created = _create(dev_client).json()
    mailbox_id = created["mailbox_id"]
    _seed_message(comp, mailbox_id=mailbox_id, msg_id="c1", discovery_candidate=True)
    _seed_message(comp, mailbox_id=mailbox_id, msg_id="c2", discovery_candidate=False)
    _seed_message(comp, mailbox_id=mailbox_id, msg_id="c3", discovery_candidate=None)
    _seed_message(comp, mailbox_id=mailbox_id, msg_id="c4", discovery_candidate=True)

    body = dev_client.get("/internal/mailboxes").json()
    assert body["items"][0]["relevant_message_count"] == 2


def test_list_mailboxes_needs_review_count_defaults_to_zero(dev_client):
    _create(dev_client)
    body = dev_client.get("/internal/mailboxes").json()
    assert body["items"][0]["needs_review_count"] == 0


def test_list_mailboxes_needs_review_count_sums_domain_review_and_security_review_open_items_scoped_per_mailbox(dev_client):
    comp = get_composition()
    mailbox_id = _create(dev_client, email="scoped-a@infosecurs.com").json()["mailbox_id"]
    other_mailbox_id = _create(dev_client, email="scoped-b@infosecurs.com").json()["mailbox_id"]

    comp.needs_you_repository.create_needs_you_item(
        item_type=ITEM_TYPE_MAILBOX_DOMAIN_REVIEW,
        domain="MAILBOX",
        question="New domain seen",
        allowed_action_type=ALLOWED_ACTION_MAILBOX_DOMAIN_REVIEW,
        source_object_reference=identity.generate_id(),
        metadata={"mailbox_id": mailbox_id, "sender_domain": "example.com"},
    )
    comp.needs_you_repository.create_needs_you_item(
        item_type=ITEM_TYPE_MAILBOX_AUTHENTICATION_ESCALATION,
        domain="MAILBOX",
        question="Auth failed for one message",
        allowed_action_type=ALLOWED_ACTION_MAILBOX_DOMAIN_REVIEW,
        source_object_reference=identity.generate_id(),
        metadata={"mailbox_id": mailbox_id},
    )
    # A second mailbox's own item must never leak into the first
    # mailbox's own count (the exact regression the underlying scan's
    # `item.metadata.get("mailbox_id") == mailbox_id` filter guards).
    comp.needs_you_repository.create_needs_you_item(
        item_type=ITEM_TYPE_MAILBOX_DOMAIN_REVIEW,
        domain="MAILBOX",
        question="A different mailbox's own domain",
        allowed_action_type=ALLOWED_ACTION_MAILBOX_DOMAIN_REVIEW,
        source_object_reference=identity.generate_id(),
        metadata={"mailbox_id": other_mailbox_id, "sender_domain": "other.example"},
    )

    body = dev_client.get("/internal/mailboxes").json()
    items_by_id = {item["mailbox_id"]: item for item in body["items"]}
    assert items_by_id[mailbox_id]["needs_review_count"] == 2
    assert items_by_id[other_mailbox_id]["needs_review_count"] == 1


def test_list_mailboxes_needs_review_count_excludes_resolved_items(dev_client):
    comp = get_composition()
    mailbox_id = _create(dev_client).json()["mailbox_id"]
    item = comp.needs_you_repository.create_needs_you_item(
        item_type=ITEM_TYPE_MAILBOX_DOMAIN_REVIEW,
        domain="MAILBOX",
        question="New domain seen",
        allowed_action_type=ALLOWED_ACTION_MAILBOX_DOMAIN_REVIEW,
        source_object_reference=identity.generate_id(),
        metadata={"mailbox_id": mailbox_id, "sender_domain": "example.com"},
    )
    comp.needs_you_repository.resolve_needs_you_item(
        item.item_id, new_status="RESOLVED", resolution={"ok": True}, actor_type="USER", actor_id=ACTOR_ID
    )

    body = dev_client.get("/internal/mailboxes").json()
    assert body["items"][0]["needs_review_count"] == 0


def test_list_mailboxes_last_sweep_status_is_none_before_any_sweep(dev_client):
    _create(dev_client)
    body = dev_client.get("/internal/mailboxes").json()
    assert body["items"][0]["last_sweep_status"] is None


def test_list_mailboxes_last_sweep_status_reflects_the_most_recent_run(dev_client):
    comp = get_composition()
    mailbox_id = _create(dev_client).json()["mailbox_id"]

    run1 = comp.mailbox_sweep_run_repository.create_run(mailbox_id=mailbox_id, trigger=TRIGGER_MANUAL)
    comp.mailbox_sweep_run_repository.complete_run(
        run1.sweep_run_id,
        new_status="FAILED",
        folders_attempted=[],
        messages_seen=1,
        messages_new=1,
        evidence_created=0,
        duplicates=0,
        quarantined=0,
        failures=1,
        error_code="PROVIDER_ERROR",
        error_detail="boom",
    )
    run2 = comp.mailbox_sweep_run_repository.create_run(mailbox_id=mailbox_id, trigger=TRIGGER_MANUAL)
    comp.mailbox_sweep_run_repository.complete_run(
        run2.sweep_run_id,
        new_status="SUCCEEDED",
        folders_attempted=[],
        messages_seen=5,
        messages_new=5,
        evidence_created=5,
        duplicates=0,
        quarantined=0,
        failures=0,
    )

    body = dev_client.get("/internal/mailboxes").json()
    last_sweep = body["items"][0]["last_sweep_status"]
    assert last_sweep is not None
    assert last_sweep["sweep_run_id"] == run2.sweep_run_id  # the MOST RECENT run, never the first
    assert last_sweep["status"] == "SUCCEEDED"
    assert last_sweep["messages_seen"] == 5


def test_list_mailboxes_needs_review_count_is_accurate_for_an_imap_mailbox(dev_client):
    """The needs-review-count aggregation is provider-neutral by
    construction (``count_needs_review_items_for_mailbox`` has no
    ``provider_kind`` branch at all — it only ever filters Needs You
    items by ``metadata["mailbox_id"]``), but this is proven explicitly
    against a real IMAP-provider mailbox anyway: IMAP has no
    domain-review GUI PAGE wired yet (a separate, disclosed,
    out-of-scope gap — see this WO's own delivery report), and this
    test is what proves that gap never silently extends to the COUNT
    itself, which the underlying data/endpoint already supports for
    IMAP."""
    comp = get_composition()
    imap_mailbox_id = _create(dev_client, email="matt@noust.ai", provider_kind="IMAP").json()["mailbox_id"]

    comp.needs_you_repository.create_needs_you_item(
        item_type=ITEM_TYPE_MAILBOX_DOMAIN_REVIEW,
        domain="MAILBOX",
        question="New domain seen on the IMAP mailbox",
        allowed_action_type=ALLOWED_ACTION_MAILBOX_DOMAIN_REVIEW,
        source_object_reference=identity.generate_id(),
        metadata={"mailbox_id": imap_mailbox_id, "sender_domain": "example.com"},
    )
    comp.needs_you_repository.create_needs_you_item(
        item_type=ITEM_TYPE_MAILBOX_AUTHENTICATION_ESCALATION,
        domain="MAILBOX",
        question="Auth failed for one IMAP message",
        allowed_action_type=ALLOWED_ACTION_MAILBOX_DOMAIN_REVIEW,
        source_object_reference=identity.generate_id(),
        metadata={"mailbox_id": imap_mailbox_id},
    )

    body = dev_client.get("/internal/mailboxes").json()
    items_by_id = {item["mailbox_id"]: item for item in body["items"]}
    assert items_by_id[imap_mailbox_id]["provider_kind"] == "IMAP"
    assert items_by_id[imap_mailbox_id]["needs_review_count"] == 2
