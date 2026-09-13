"""HTTP-level tests for the governed Evidence Intake API (CD-4 WI-3,
PID §26-33, §44-54) — ``app/api/routers/intake.py``.

Runs against the SAME real, disposable PostgreSQL + MinIO + ClamAV
stack ``tests/app_api/conftest.py``'s ``runtime_stack``/``client``
fixtures already stand up for ``test_no_fallback.py`` (CD-3 WI-3),
extended by this WI to also include a real, disposable ``clamd``
daemon (``bagman-test-clamav-wi3``) — never mocked. Every test in this
module therefore exercises the REAL ``ClamAVScanner`` end-to-end
through the actual HTTP intake endpoint (PID §20/§63's "a stub is used
in dev must never become the real scanner is never exercised"
doctrine) — there is no dev-stub-scanner test anywhere in this file;
the dev-only ``_AlwaysCleanDevelopmentScanner`` stub
(``app/api/composition.py``) is exercised only implicitly, by every
OTHER test module in this repo that uses development/test composition
(e.g. ``tests/integration/``), never here.
"""
from __future__ import annotations

import json

import pytest

SYNTHETIC_PDF = b"%PDF-1.4\nSYNTHETIC TEST DATA - not a real document\n%%EOF"
UNSUPPORTED_BINARY = bytes(range(256)) * 4  # no recognised magic bytes at all

#: The standardised EICAR antivirus test string (PID §63) — a
#: publicly-documented, harmless string every antivirus engine is
#: designed to flag as a test signature. NOT real malware.
EICAR_TEST_STRING = (
    rb"X5O!P%@AP[4\PZX54(P^)7CC)7}$EICAR-STANDARD-ANTIVIRUS-TEST-FILE!$H+H*"
)


def _post_intake(
    client,
    *,
    content: bytes,
    filename: str,
    content_type: str = "application/pdf",
    entity_hint=None,
    evidence_type=None,
    actor_id: str = "test-operator",
    note=None,
    idempotency_key=None,
):
    metadata = {
        "entity_hint": entity_hint,
        "evidence_type": evidence_type,
        "actor_type": "USER",
        "actor_id": actor_id,
        "note": note,
    }
    headers = {"Idempotency-Key": idempotency_key} if idempotency_key else {}
    return client.post(
        "/internal/intake/evidence",
        data={"metadata": json.dumps(metadata)},
        files={"file": (filename, content, content_type)},
        headers=headers,
    )


# ---------------------------------------------------------------------
# Successful upload -> ACCEPTED -> REGISTERED (real ClamAVScanner, CLEAN)
# ---------------------------------------------------------------------


def test_successful_upload_registers_evidence_retrievable_and_downloadable(client):
    response = _post_intake(
        client,
        content=SYNTHETIC_PDF,
        filename="invoice.pdf",
        content_type="application/pdf",
        entity_hint="UNRESOLVED",
        evidence_type="INVOICE",
        actor_id="matt",
        note="synthetic acceptance upload",
    )
    assert response.status_code == 201
    body = response.json()
    intake = body["intake"]
    evidence = body["evidence"]

    assert intake["status"] == "REGISTERED"
    assert intake["entity_hint"] == "UNRESOLVED"
    assert intake["failure_code"] is None
    assert intake["quarantine_reason"] is None
    assert evidence is not None
    assert intake["evidence_id"] == evidence["evidence_id"]

    # CD-4 entity-resolution decision (recorded, not relitigated here):
    # intake always registers canonical evidence with entity_id=None.
    assert evidence["entity_id"] is None
    assert evidence["evidence_type"] == "INVOICE"
    assert evidence["original_name"] == "invoice.pdf"
    assert evidence["mime_type"] == "application/pdf"
    assert evidence["size_bytes"] == len(SYNTHETIC_PDF)

    evidence_id = evidence["evidence_id"]

    get_resp = client.get(f"/internal/evidence/{evidence_id}")
    assert get_resp.status_code == 200
    assert get_resp.json()["evidence_id"] == evidence_id

    content_resp = client.get(f"/internal/evidence/{evidence_id}/content")
    assert content_resp.status_code == 200
    assert content_resp.content == SYNTHETIC_PDF

    intake_detail = client.get(f"/internal/intake/{intake['intake_id']}")
    assert intake_detail.status_code == 200
    assert intake_detail.json()["status"] == "REGISTERED"


# ---------------------------------------------------------------------
# Rejected uploads never create canonical evidence
# ---------------------------------------------------------------------


@pytest.mark.parametrize(
    "filename, content, content_type, expected_failure_code",
    [
        ("../../etc/passwd", SYNTHETIC_PDF, "application/pdf", "UNSAFE_FILENAME"),
        ("unknown.dat", UNSUPPORTED_BINARY, "application/octet-stream", "UNSUPPORTED_CONTENT_TYPE"),
    ],
)
def test_rejected_upload_never_creates_canonical_evidence(
    client, filename, content, content_type, expected_failure_code
):
    response = _post_intake(
        client, content=content, filename=filename, content_type=content_type, actor_id="matt"
    )
    assert response.status_code == 422
    body = response.json()
    assert body["intake"]["status"] == "REJECTED"
    assert body["intake"]["failure_code"] == expected_failure_code
    assert body["evidence"] is None


def test_oversized_upload_is_rejected_never_creates_canonical_evidence(client):
    """PID §12/§13: the backend is authoritative on size limits, and
    must reject an oversized upload before uncontrolled memory/disk
    consumption — here proven against the real HTTP layer using the
    intake pipeline's actual default policy limit
    (``DEFAULT_INTAKE_POLICY.max_file_size_bytes``, 50 MiB), not a
    scaled-down test-only policy (the router does not expose a way to
    override policy per-request, by design — PID §34)."""
    from services.evidence.intake.policy import DEFAULT_INTAKE_POLICY

    oversized_content = b"\x00" * (DEFAULT_INTAKE_POLICY.max_file_size_bytes + 1)
    response = _post_intake(
        client,
        content=oversized_content,
        filename="oversized.pdf",
        content_type="application/pdf",
        actor_id="matt",
    )
    assert response.status_code == 422
    body = response.json()
    assert body["intake"]["status"] == "REJECTED"
    assert body["intake"]["failure_code"] == "FILE_TOO_LARGE"
    assert body["evidence"] is None


# ---------------------------------------------------------------------
# Quarantined upload (REAL ClamAV, genuine EICAR detection) never
# creates canonical evidence, durably visible via intake detail.
# ---------------------------------------------------------------------


def test_quarantined_upload_via_real_clamav_never_creates_evidence(client):
    response = _post_intake(
        client, content=EICAR_TEST_STRING, filename="suspicious.txt", content_type="text/plain", actor_id="matt"
    )
    assert response.status_code == 200
    body = response.json()
    assert body["intake"]["status"] == "QUARANTINED"
    assert "eicar" in body["intake"]["quarantine_reason"].lower()
    assert body["evidence"] is None

    intake_id = body["intake"]["intake_id"]
    detail = client.get(f"/internal/intake/{intake_id}")
    assert detail.status_code == 200
    detail_body = detail.json()
    assert detail_body["status"] == "QUARANTINED"
    assert "eicar" in detail_body["quarantine_reason"].lower()
    assert detail_body["evidence_id"] is None


# ---------------------------------------------------------------------
# Idempotent replay / idempotency conflict (PID §25/§52/§53)
# ---------------------------------------------------------------------


def test_idempotent_replay_same_key_same_file_returns_same_result_no_duplicates(client):
    key = "test-intake-idem-replay-0001"  # gitleaks:allow
    first = _post_intake(client, content=SYNTHETIC_PDF, filename="dup.pdf", actor_id="matt", idempotency_key=key)
    assert first.status_code == 201
    first_body = first.json()

    second = _post_intake(client, content=SYNTHETIC_PDF, filename="dup.pdf", actor_id="matt", idempotency_key=key)
    # A replay landing on an already-REGISTERED record is honestly NOT
    # a new creation this request — see app/api/routers/intake.py's
    # own `_http_status_for` doctrine.
    assert second.status_code == 200
    second_body = second.json()

    assert second_body["intake"]["intake_id"] == first_body["intake"]["intake_id"]
    assert second_body["evidence"]["evidence_id"] == first_body["evidence"]["evidence_id"]

    list_resp = client.get("/internal/intake", params={"limit": 200})
    assert list_resp.status_code == 200
    matching = [r for r in list_resp.json()["items"] if r["intake_id"] == first_body["intake"]["intake_id"]]
    assert len(matching) == 1  # no duplicate IntakeRecord

    ev_list_resp = client.get("/internal/evidence", params={"limit": 200})
    assert ev_list_resp.status_code == 200
    matching_evidence = [
        e for e in ev_list_resp.json()["items"] if e["evidence_id"] == first_body["evidence"]["evidence_id"]
    ]
    assert len(matching_evidence) == 1  # no duplicate EvidenceItem


def test_idempotency_conflict_same_key_different_file_returns_409(client):
    key = "test-intake-idem-conflict-0001"  # gitleaks:allow
    first = _post_intake(client, content=SYNTHETIC_PDF, filename="conflict-a.pdf", actor_id="matt", idempotency_key=key)
    assert first.status_code == 201

    second = _post_intake(client, content=SYNTHETIC_PDF, filename="conflict-b.pdf", actor_id="matt", idempotency_key=key)
    assert second.status_code == 409
    body = second.json()
    assert body["error_code"] == "IDEMPOTENCY_CONFLICT"


# ---------------------------------------------------------------------
# GET /internal/intake and GET /internal/evidence — pagination/filtering
# ---------------------------------------------------------------------


def test_list_intake_pagination_and_status_filter(client):
    for i in range(3):
        resp = _post_intake(client, content=SYNTHETIC_PDF, filename=f"list-page-test-{i}.pdf", actor_id="matt")
        assert resp.status_code == 201

    page = client.get("/internal/intake", params={"limit": 2, "offset": 0})
    assert page.status_code == 200
    page_body = page.json()
    assert page_body["limit"] == 2
    assert page_body["offset"] == 0
    assert len(page_body["items"]) == 2

    filtered = client.get("/internal/intake", params={"status": "REGISTERED", "limit": 200})
    assert filtered.status_code == 200
    assert all(item["status"] == "REGISTERED" for item in filtered.json()["items"])
    assert len(filtered.json()["items"]) >= 3

    bad_limit = client.get("/internal/intake", params={"limit": 0})
    assert bad_limit.status_code == 422

    too_large_limit = client.get("/internal/intake", params={"limit": 100_000})
    assert too_large_limit.status_code == 422


def test_list_evidence_pagination_and_type_filter(client):
    for i in range(2):
        resp = _post_intake(
            client,
            content=SYNTHETIC_PDF,
            filename=f"evidence-list-test-{i}.pdf",
            evidence_type="RECEIPT",
            actor_id="matt",
        )
        assert resp.status_code == 201

    page = client.get("/internal/evidence", params={"limit": 1})
    assert page.status_code == 200
    assert len(page.json()["items"]) == 1

    filtered = client.get("/internal/evidence", params={"evidence_type": "RECEIPT", "limit": 200})
    assert filtered.status_code == 200
    assert all(item["evidence_type"] == "RECEIPT" for item in filtered.json()["items"])
    assert len(filtered.json()["items"]) >= 2


def test_get_intake_detail_404_for_unknown_id(client):
    import uuid

    response = client.get(f"/internal/intake/{uuid.uuid4()}")
    assert response.status_code == 404


# ---------------------------------------------------------------------
# Stable MANUAL_UPLOAD source lifecycle (PID §9)
# ---------------------------------------------------------------------


def test_manual_upload_source_created_exactly_once_across_multiple_requests(client):
    first = _post_intake(client, content=SYNTHETIC_PDF, filename="source-lifecycle-1.pdf", actor_id="matt")
    second = _post_intake(client, content=SYNTHETIC_PDF, filename="source-lifecycle-2.pdf", actor_id="matt")
    assert first.status_code == 201
    assert second.status_code == 201

    source_id_1 = first.json()["evidence"]["source_id"]
    source_id_2 = second.json()["evidence"]["source_id"]
    assert source_id_1 == source_id_2

    third = _post_intake(client, content=SYNTHETIC_PDF, filename="source-lifecycle-3.pdf", actor_id="matt")
    assert third.status_code == 201
    assert third.json()["evidence"]["source_id"] == source_id_1


# ---------------------------------------------------------------------
# Direct-upload bypass CLOSED (PID §26/§27) — a POSITIVE proof, not
# merely the absence of a test that used to exist.
# ---------------------------------------------------------------------


def test_direct_upload_bypass_is_closed(client):
    """The CD-3 byte-accepting ``POST /internal/evidence`` route has
    been removed entirely (``app/api/routers/internal.py``) — this
    proves it, rather than merely omitting a test that used to cover
    it. FastAPI/Starlette returns 405 when a path exists for other
    methods (``GET /internal/evidence`` — the CD-4 list endpoint —
    still does) but not for POST; either 404 or 405 honestly proves no
    write capability remains at this path."""
    response = client.post(
        "/internal/evidence",
        data={"metadata": json.dumps({"evidence_type": "DOCUMENT"})},
        files={"file": ("bypass-attempt.pdf", SYNTHETIC_PDF, "application/pdf")},
    )
    assert response.status_code in (404, 405)

    # And, positively: no EvidenceItem was created by this attempt —
    # confirmed by the response itself never being a 2xx creation.
    assert response.status_code not in (200, 201)
