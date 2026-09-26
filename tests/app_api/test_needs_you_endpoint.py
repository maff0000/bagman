"""HTTP-level tests for the universal Needs You queue API (CD-6 Slice
1, PID §98.2/§98.5) — ``app/api/routers/needs_you.py``, plus the one
real trigger this delivery adds to ``app/api/routers/intake.py``, and
the new ``GET /internal/entities`` route.

Runs against the SAME real, disposable PostgreSQL + MinIO + ClamAV
stack ``tests/app_api/conftest.py``'s ``runtime_stack``/``client``
fixtures already stand up for ``test_intake_endpoint.py`` — never
mocked, never the in-memory dev composition (mirrors that module's own
module docstring/rationale exactly).
"""
from __future__ import annotations

import json

SYNTHETIC_PDF = b"%PDF-1.4\nSYNTHETIC TEST DATA - not a real document\n%%EOF"


def _post_intake(client, *, content: bytes = SYNTHETIC_PDF, filename: str = "receipt.pdf", idempotency_key=None):
    metadata = {
        "entity_hint": None,
        "evidence_type": "RECEIPT",
        "actor_type": "USER",
        "actor_id": "test-operator",
        "note": None,
    }
    headers = {"Idempotency-Key": idempotency_key} if idempotency_key else {}
    return client.post(
        "/internal/intake/evidence",
        data={"metadata": json.dumps(metadata)},
        files={"file": (filename, content, "application/pdf")},
        headers=headers,
    )


# ---------------------------------------------------------------------
# GET /internal/entities — canonical entity seed
# ---------------------------------------------------------------------


def test_list_entities_seeds_and_returns_the_three_canonical_entities(client):
    response = client.get("/internal/entities")
    assert response.status_code == 200
    body = response.json()
    canonical_names = {e["canonical_name"] for e in body["items"]}
    assert canonical_names == {"INFOSECURS_LIMITED", "NOUSTAI_LIMITED", "MATTHEW_SCOTT_PERSONAL"}
    entity_types = {e["canonical_name"]: e["entity_type"] for e in body["items"]}
    assert entity_types["INFOSECURS_LIMITED"] == "COMPANY"
    assert entity_types["NOUSTAI_LIMITED"] == "COMPANY"
    assert entity_types["MATTHEW_SCOTT_PERSONAL"] == "PERSON"
    # every entity_id is a real, distinct canonical identifier — never a
    # hardcoded literal the GUI would have to know in advance
    entity_ids = {e["entity_id"] for e in body["items"]}
    assert len(entity_ids) == 3


def test_list_entities_is_idempotent_across_repeated_calls(client):
    first = client.get("/internal/entities").json()
    second = client.get("/internal/entities").json()
    assert {e["entity_id"] for e in first["items"]} == {e["entity_id"] for e in second["items"]}
    assert len(first["items"]) == 3


# ---------------------------------------------------------------------
# The intake -> Needs You trigger
# ---------------------------------------------------------------------


def test_accepted_intake_creates_exactly_one_open_needs_you_item(client):
    response = _post_intake(client)
    assert response.status_code == 201
    evidence_id = response.json()["evidence"]["evidence_id"]

    listing = client.get("/internal/needs-you", params={"status": "OPEN"}).json()
    matching = [i for i in listing["items"] if i.get("source_object_reference") == evidence_id]
    assert len(matching) == 1
    item = matching[0]
    assert item["item_type"] == "COMPANY_REQUIRED"
    assert item["domain"] == "EVIDENCE_INTAKE"
    assert item["status"] == "OPEN"
    assert item["allowed_action_type"] == "COMPANY_WHAT_WHY"
    assert item["metadata"]["evidence_id"] == evidence_id

    detail = client.get(f"/internal/needs-you/{item['item_id']}").json()
    assert detail["item_id"] == item["item_id"]


def test_replayed_intake_request_does_not_create_a_duplicate_needs_you_item(client):
    """The CD-6 acceptance requirement: 'a test proving the intake ->
    Needs You item creation trigger is idempotent (replayed intake
    request does not create a duplicate item)' — proved here at the
    real HTTP boundary, using the SAME Idempotency-Key BAGMAN's own
    intake replay contract already relies on (PID §25/§53)."""
    key = "cd6-needs-you-idempotency-proof-0001"  # gitleaks:allow

    first = _post_intake(client, idempotency_key=key)
    assert first.status_code == 201
    evidence_id = first.json()["evidence"]["evidence_id"]

    # A genuine replay: identical idempotency key, identical request.
    second = _post_intake(client, idempotency_key=key)
    assert second.status_code == 200
    assert second.json()["evidence"]["evidence_id"] == evidence_id

    listing = client.get("/internal/needs-you").json()
    matching = [i for i in listing["items"] if i.get("source_object_reference") == evidence_id]
    assert len(matching) == 1, f"expected exactly one NeedsYouItem for {evidence_id}, found {len(matching)}"


def test_rejected_upload_creates_no_needs_you_item(client):
    """An intake attempt that never reaches REGISTERED must never raise
    a Needs You item — nothing canonical exists yet to ask about (this
    module's own docstring's 'never before' doctrine)."""
    unsupported_binary = bytes(range(256)) * 4  # no recognised magic bytes at all
    response = _post_intake(client, content=unsupported_binary, filename="mystery.bin")
    assert response.status_code in (422, 200)  # REJECTED (422) or QUARANTINED (200), never a registration
    assert response.json()["evidence"] is None

    listing_before = client.get("/internal/needs-you").json()
    # No item anywhere references an evidence_id that does not exist.
    for item in listing_before["items"]:
        if item["metadata"].get("original_filename") == "mystery.bin":
            raise AssertionError("a Needs You item was raised for evidence that was never registered")


# ---------------------------------------------------------------------
# Resolution
# ---------------------------------------------------------------------


def test_resolving_a_needs_you_item_answers_company_what_why(client):
    upload = _post_intake(client)
    evidence_id = upload.json()["evidence"]["evidence_id"]
    entities = client.get("/internal/entities").json()["items"]
    infosecurs = next(e for e in entities if e["canonical_name"] == "INFOSECURS_LIMITED")

    listing = client.get("/internal/needs-you", params={"status": "OPEN"}).json()
    item = next(i for i in listing["items"] if i["source_object_reference"] == evidence_id)

    resolution = {"entity_id": infosecurs["entity_id"], "what": "Software subscription", "why": "R&D tooling"}
    resolve_response = client.post(
        f"/internal/needs-you/{item['item_id']}/resolve",
        json={"new_status": "RESOLVED", "resolution": resolution, "actor_type": "USER", "actor_id": "matt"},
    )
    assert resolve_response.status_code == 200
    resolved = resolve_response.json()
    assert resolved["status"] == "RESOLVED"
    assert resolved["resolution"] == resolution
    assert resolved["resolved_by_actor_id"] == "matt"
    assert resolved["resolved_at"] is not None

    # No longer counted among OPEN items.
    still_open = client.get("/internal/needs-you", params={"status": "OPEN"}).json()
    assert item["item_id"] not in {i["item_id"] for i in still_open["items"]}


def test_double_submitting_the_same_resolution_is_idempotent_not_an_error(client):
    upload = _post_intake(client)
    evidence_id = upload.json()["evidence"]["evidence_id"]
    entities = client.get("/internal/entities").json()["items"]
    infosecurs = next(e for e in entities if e["canonical_name"] == "INFOSECURS_LIMITED")
    listing = client.get("/internal/needs-you", params={"status": "OPEN"}).json()
    item = next(i for i in listing["items"] if i["source_object_reference"] == evidence_id)

    # Finding 1 fix — a COMPANY_REQUIRED item's RESOLVED path now
    # requires a real entity_id (see the dedicated validation tests
    # below); the double-submit proof therefore uses a real one, same
    # as every other RESOLVED-path fixture in this file.
    body = {
        "new_status": "RESOLVED",
        "resolution": {"entity_id": infosecurs["entity_id"], "what": "Office supplies", "why": "General expense"},
        "actor_type": "USER",
        "actor_id": "matt",
    }
    first = client.post(f"/internal/needs-you/{item['item_id']}/resolve", json=body)
    assert first.status_code == 200

    second = client.post(f"/internal/needs-you/{item['item_id']}/resolve", json=body)
    assert second.status_code == 200
    assert second.json()["item_id"] == first.json()["item_id"]
    assert second.json()["resolution"] == first.json()["resolution"]


def test_resolving_an_already_resolved_item_with_a_different_outcome_conflicts(client):
    upload = _post_intake(client)
    evidence_id = upload.json()["evidence"]["evidence_id"]
    entities = client.get("/internal/entities").json()["items"]
    infosecurs = next(e for e in entities if e["canonical_name"] == "INFOSECURS_LIMITED")
    listing = client.get("/internal/needs-you", params={"status": "OPEN"}).json()
    item = next(i for i in listing["items"] if i["source_object_reference"] == evidence_id)

    first = client.post(
        f"/internal/needs-you/{item['item_id']}/resolve",
        json={
            "new_status": "RESOLVED",
            "resolution": {"entity_id": infosecurs["entity_id"], "what": "A", "why": "A"},
            "actor_type": "USER",
            "actor_id": "matt",
        },
    )
    assert first.status_code == 200

    conflicting = client.post(
        f"/internal/needs-you/{item['item_id']}/resolve",
        json={
            "new_status": "RESOLVED",
            "resolution": {"entity_id": infosecurs["entity_id"], "what": "B", "why": "B"},
            "actor_type": "USER",
            "actor_id": "matt",
        },
    )
    assert conflicting.status_code == 409


def test_unknown_item_id_returns_404(client):
    response = client.get("/internal/needs-you/018f5b3e-3c2a-7a4e-8b2d-6f1a2c3d4e5f")
    assert response.status_code == 404


# ---------------------------------------------------------------------
# Restart survival (PID §98.5 acceptance point 8): the resolution must
# be readable again from a BRAND NEW composition/process attached to
# the SAME durable Postgres — the real full-container restart proof is
# the acceptance script's job (tests/acceptance/); this is the
# app_api-tier equivalent every other durability proof in this
# directory already uses.
# ---------------------------------------------------------------------


def test_resolution_survives_a_fresh_composition_against_the_same_database(client):
    from app.api.composition import reset_composition_for_tests
    from fastapi.testclient import TestClient
    from app.api.main import app

    upload = _post_intake(client)
    evidence_id = upload.json()["evidence"]["evidence_id"]
    entities = client.get("/internal/entities").json()["items"]
    infosecurs = next(e for e in entities if e["canonical_name"] == "INFOSECURS_LIMITED")
    listing = client.get("/internal/needs-you", params={"status": "OPEN"}).json()
    item = next(i for i in listing["items"] if i["source_object_reference"] == evidence_id)

    # Finding 1 fix — RESOLVED now requires a real entity_id.
    resolution = {"entity_id": infosecurs["entity_id"], "what": "GPU hardware", "why": "local inference R&D"}
    client.post(
        f"/internal/needs-you/{item['item_id']}/resolve",
        json={"new_status": "RESOLVED", "resolution": resolution, "actor_type": "USER", "actor_id": "matt"},
    )

    # A genuinely fresh process-wide composition — no Python-level
    # cache anywhere in PostgresNeedsYouRepository, so re-reading
    # through a BRAND NEW TestClient/composition is what actually
    # proves durability, not in-process memoization.
    reset_composition_for_tests()
    with TestClient(app) as fresh_client:
        refetched = fresh_client.get(f"/internal/needs-you/{item['item_id']}").json()
        assert refetched["status"] == "RESOLVED"
        assert refetched["resolution"] == resolution


# ---------------------------------------------------------------------
# CD-6 GUI-operations-foundation follow-on WO (item E) —
# `COMPANY_REQUIRED` resolution must actually assign the `EvidenceItem`
# entity, through the canonical layer (`core.api.BagmanCanonicalAPI
# .assign_evidence_entity`, previously implemented with zero real
# callers anywhere).
# ---------------------------------------------------------------------


def test_resolving_company_required_to_resolved_assigns_the_evidence_entity(client):
    upload = _post_intake(client)
    evidence_id = upload.json()["evidence"]["evidence_id"]
    entities = client.get("/internal/entities").json()["items"]
    infosecurs = next(e for e in entities if e["canonical_name"] == "INFOSECURS_LIMITED")

    listing = client.get("/internal/needs-you", params={"status": "OPEN"}).json()
    item = next(i for i in listing["items"] if i["source_object_reference"] == evidence_id)

    resolve_response = client.post(
        f"/internal/needs-you/{item['item_id']}/resolve",
        json={
            "new_status": "RESOLVED",
            "resolution": {"entity_id": infosecurs["entity_id"], "what": "Software subscription", "why": "R&D tooling"},
            "actor_type": "USER", "actor_id": "matt",
        },
    )
    assert resolve_response.status_code == 200, resolve_response.text

    evidence_after = client.get(f"/internal/evidence/{evidence_id}").json()
    assert evidence_after["entity_id"] == infosecurs["entity_id"]


def test_resolving_company_required_with_same_entity_twice_is_idempotent(client):
    upload = _post_intake(client)
    evidence_id = upload.json()["evidence"]["evidence_id"]
    entities = client.get("/internal/entities").json()["items"]
    infosecurs = next(e for e in entities if e["canonical_name"] == "INFOSECURS_LIMITED")

    listing = client.get("/internal/needs-you", params={"status": "OPEN"}).json()
    item = next(i for i in listing["items"] if i["source_object_reference"] == evidence_id)

    body = {
        "new_status": "RESOLVED",
        "resolution": {"entity_id": infosecurs["entity_id"], "what": "Software subscription", "why": "R&D tooling"},
        "actor_type": "USER", "actor_id": "matt",
    }
    first = client.post(f"/internal/needs-you/{item['item_id']}/resolve", json=body)
    assert first.status_code == 200, first.text

    # A genuine retry of the SAME resolve call (mirrors the double-submit
    # doctrine already proven above for the non-entity case) — the
    # entity is already correctly assigned; `assign_evidence_entity`'s
    # own idempotent same-entity no-op lets this complete cleanly.
    second = client.post(f"/internal/needs-you/{item['item_id']}/resolve", json=body)
    assert second.status_code == 200, second.text

    evidence_after = client.get(f"/internal/evidence/{evidence_id}").json()
    assert evidence_after["entity_id"] == infosecurs["entity_id"]


def test_resolving_company_required_with_a_different_entity_after_assignment_conflicts(client):
    """A conflicting reassignment attempt fails loudly — reassignment is
    an explicit, separate, NOT-built-here governed correction workflow."""
    upload = _post_intake(client)
    evidence_id = upload.json()["evidence"]["evidence_id"]
    entities = client.get("/internal/entities").json()["items"]
    infosecurs = next(e for e in entities if e["canonical_name"] == "INFOSECURS_LIMITED")
    noustai = next(e for e in entities if e["canonical_name"] == "NOUSTAI_LIMITED")

    listing = client.get("/internal/needs-you", params={"status": "OPEN"}).json()
    item = next(i for i in listing["items"] if i["source_object_reference"] == evidence_id)

    first = client.post(
        f"/internal/needs-you/{item['item_id']}/resolve",
        json={
            "new_status": "RESOLVED",
            "resolution": {"entity_id": infosecurs["entity_id"], "what": "A", "why": "A"},
            "actor_type": "USER", "actor_id": "matt",
        },
    )
    assert first.status_code == 200, first.text

    # A genuinely different resolution on an already-RESOLVED item is
    # ALREADY a 409 under the pre-existing "different outcome" doctrine
    # (proven above) — this test's own real point is that even if the
    # entity-assignment step itself were reached again with a DIFFERENT
    # entity_id, `assign_evidence_entity` raises `ConflictError` loudly
    # rather than silently overwriting. Exercise that directly through
    # `core.api` to prove the assignment layer's own contract, since the
    # HTTP layer's own item-status guard would otherwise short-circuit
    # first for this same fixture.
    from app.api.composition import get_composition
    from core.errors import ConflictError

    composition = get_composition()
    try:
        composition.api.assign_evidence_entity(
            evidence_id=evidence_id, entity_id=noustai["entity_id"], actor_type="USER", actor_id="matt",
        )
        raised = False
    except ConflictError:
        raised = True
    assert raised is True

    evidence_after = client.get(f"/internal/evidence/{evidence_id}").json()
    assert evidence_after["entity_id"] == infosecurs["entity_id"]  # unchanged


def test_dismissing_a_company_required_item_never_assigns_an_entity(client):
    upload = _post_intake(client)
    evidence_id = upload.json()["evidence"]["evidence_id"]
    entities = client.get("/internal/entities").json()["items"]
    infosecurs = next(e for e in entities if e["canonical_name"] == "INFOSECURS_LIMITED")

    listing = client.get("/internal/needs-you", params={"status": "OPEN"}).json()
    item = next(i for i in listing["items"] if i["source_object_reference"] == evidence_id)

    # A DISMISSED resolution, even carrying an entity_id in its payload,
    # must never assign one — DISMISSED explicitly does not assign an
    # entity (architect's own explicit requirement).
    dismiss_response = client.post(
        f"/internal/needs-you/{item['item_id']}/resolve",
        json={
            "new_status": "DISMISSED",
            "resolution": {"entity_id": infosecurs["entity_id"], "what": "N/A", "why": "N/A"},
            "actor_type": "USER", "actor_id": "matt",
        },
    )
    assert dismiss_response.status_code == 200, dismiss_response.text
    assert dismiss_response.json()["status"] == "DISMISSED"

    evidence_after = client.get(f"/internal/evidence/{evidence_id}").json()
    assert evidence_after["entity_id"] is None


# ---------------------------------------------------------------------
# CD-6 second architect review — Finding 1: a COMPANY_REQUIRED item must
# not be RESOLVED without a real entity assignment actually taking
# effect. Every check below must fail BEFORE any mutation — the item
# stays OPEN and the evidence stays unchanged.
# ---------------------------------------------------------------------


def test_resolving_company_required_with_missing_entity_id_fails_validation_and_stays_open(client):
    upload = _post_intake(client)
    evidence_id = upload.json()["evidence"]["evidence_id"]
    listing = client.get("/internal/needs-you", params={"status": "OPEN"}).json()
    item = next(i for i in listing["items"] if i["source_object_reference"] == evidence_id)

    response = client.post(
        f"/internal/needs-you/{item['item_id']}/resolve",
        json={
            "new_status": "RESOLVED",
            "resolution": {"entity_id": None, "what": "Office supplies", "why": "General expense"},
            "actor_type": "USER", "actor_id": "matt",
        },
    )
    assert response.status_code == 422, response.text

    still_open = client.get(f"/internal/needs-you/{item['item_id']}").json()
    assert still_open["status"] == "OPEN"
    evidence_after = client.get(f"/internal/evidence/{evidence_id}").json()
    assert evidence_after["entity_id"] is None


def test_resolving_company_required_with_no_resolution_at_all_fails_validation_and_stays_open(client):
    upload = _post_intake(client)
    evidence_id = upload.json()["evidence"]["evidence_id"]
    listing = client.get("/internal/needs-you", params={"status": "OPEN"}).json()
    item = next(i for i in listing["items"] if i["source_object_reference"] == evidence_id)

    response = client.post(
        f"/internal/needs-you/{item['item_id']}/resolve",
        json={"new_status": "RESOLVED", "actor_type": "USER", "actor_id": "matt"},
    )
    assert response.status_code == 422, response.text

    still_open = client.get(f"/internal/needs-you/{item['item_id']}").json()
    assert still_open["status"] == "OPEN"
    evidence_after = client.get(f"/internal/evidence/{evidence_id}").json()
    assert evidence_after["entity_id"] is None


def test_resolving_company_required_with_unknown_entity_id_fails_and_stays_open(client):
    upload = _post_intake(client)
    evidence_id = upload.json()["evidence"]["evidence_id"]
    listing = client.get("/internal/needs-you", params={"status": "OPEN"}).json()
    item = next(i for i in listing["items"] if i["source_object_reference"] == evidence_id)

    response = client.post(
        f"/internal/needs-you/{item['item_id']}/resolve",
        json={
            "new_status": "RESOLVED",
            "resolution": {"entity_id": "018f5b3e-0000-7a4e-8b2d-000000000001", "what": "A", "why": "A"},
            "actor_type": "USER", "actor_id": "matt",
        },
    )
    assert response.status_code == 404, response.text

    still_open = client.get(f"/internal/needs-you/{item['item_id']}").json()
    assert still_open["status"] == "OPEN"
    evidence_after = client.get(f"/internal/evidence/{evidence_id}").json()
    assert evidence_after["entity_id"] is None


def test_resolving_company_required_with_no_source_object_reference_fails_validation(client):
    """A caller-constructed COMPANY_REQUIRED item with no evidence anchor
    at all cannot be meaningfully resolved to RESOLVED — mirrors
    `tests/app_api/test_xero_endpoints.py`'s own style of constructing a
    NeedsYouItem straight through the repository, without a real
    intake."""
    from app.api.composition import get_composition

    composition = get_composition()
    item = composition.needs_you_repository.create_needs_you_item(
        item_type="COMPANY_REQUIRED",
        domain="EVIDENCE_INTAKE",
        question="Which company is this for?",
        allowed_action_type="COMPANY_WHAT_WHY",
    )
    entities = client.get("/internal/entities").json()["items"]
    infosecurs = next(e for e in entities if e["canonical_name"] == "INFOSECURS_LIMITED")

    response = client.post(
        f"/internal/needs-you/{item.item_id}/resolve",
        json={
            "new_status": "RESOLVED",
            "resolution": {"entity_id": infosecurs["entity_id"], "what": "A", "why": "A"},
            "actor_type": "USER", "actor_id": "matt",
        },
    )
    assert response.status_code == 422, response.text

    still_open = client.get(f"/internal/needs-you/{item.item_id}").json()
    assert still_open["status"] == "OPEN"


def test_resolving_company_required_with_bogus_source_object_reference_fails(client):
    """A `source_object_reference` present but not resolving to a REAL
    EvidenceItem must fail honestly (404 via `NotFoundError`), never
    silently resolve the item nor surface a confusing error."""
    from app.api.composition import get_composition

    composition = get_composition()
    item = composition.needs_you_repository.create_needs_you_item(
        item_type="COMPANY_REQUIRED",
        domain="EVIDENCE_INTAKE",
        question="Which company is this for?",
        allowed_action_type="COMPANY_WHAT_WHY",
        source_object_reference="018f5b3e-0000-7a4e-8b2d-000000000002",
    )
    entities = client.get("/internal/entities").json()["items"]
    infosecurs = next(e for e in entities if e["canonical_name"] == "INFOSECURS_LIMITED")

    response = client.post(
        f"/internal/needs-you/{item.item_id}/resolve",
        json={
            "new_status": "RESOLVED",
            "resolution": {"entity_id": infosecurs["entity_id"], "what": "A", "why": "A"},
            "actor_type": "USER", "actor_id": "matt",
        },
    )
    assert response.status_code == 404, response.text

    still_open = client.get(f"/internal/needs-you/{item.item_id}").json()
    assert still_open["status"] == "OPEN"
