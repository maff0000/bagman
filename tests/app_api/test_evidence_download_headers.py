"""Tests for the CD-4 PR #4 Architect delta (2026-09-13): safe,
server-side download headers on ``GET /internal/evidence/{evidence_id}
/content`` (``app/api/routers/internal.py``) and the header-encoding
helper it delegates to (``app/api/http_headers
.safe_content_disposition_header`` — ``app/api/http_headers.py``).

Two genuinely distinct groups of tests here, same discipline as
``tests/integration/test_intake_scanner.py``'s own module docstring
insists on for its two groups — be explicit about which is which:

* **Real-stack, end-to-end tests** (upload via the real
  ``POST /internal/intake/evidence`` HTTP endpoint, against the real
  disposable Postgres+MinIO+ClamAV stack already stood up by
  ``tests/app_api/conftest.py``'s ``runtime_stack``/``client``
  fixtures, then download via the real
  ``GET /internal/evidence/{id}/content`` endpoint) — these prove the
  header is genuinely produced by the real route for filenames that
  can genuinely reach it through the real intake pipeline today.

* **Direct unit tests of ``safe_content_disposition_header`` itself**
  — for a raw CR/LF control character and a ``../`` path-traversal
  segment. ``services.evidence.intake.filename_safety
  .assert_filename_safe`` already rejects BOTH of those at INTAKE time
  (WI-2) — confirmed directly: a control character is caught by
  ``_has_control_character``, and ``../../etc/passwd`` is exercised
  end-to-end in ``tests/app_api/test_intake_endpoint.py``'s
  ``test_rejected_upload_never_creates_canonical_evidence`` as an
  ``UNSAFE_FILENAME`` rejection. There is therefore no way to get
  either string into ``EvidenceItem.original_name`` through the real
  HTTP intake pipeline to exercise the route end-to-end with them. That
  is exactly why they are tested here as direct, synthetic calls to the
  helper function instead — bypassing the intake pipeline entirely — to
  prove the HEADER-ENCODING LAYER ITSELF is defensive on its own terms,
  not merely lucky because something upstream currently happens to
  filter this input first. If upstream filtering is ever weakened,
  bypassed, or a new producer of ``EvidenceItem`` is ever added that
  does not run through WI-2's validation pipeline, this layer must
  still hold on its own.
"""
from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from urllib.parse import unquote

import pytest

from app.api.composition import get_composition, get_manual_upload_source_id
from app.api.http_headers import safe_content_disposition_header
from core.identity import generate_id

SYNTHETIC_PDF = b"%PDF-1.4\nSYNTHETIC TEST DATA - not a real document\n%%EOF"


def _post_intake(client, *, content: bytes, filename: str, content_type: str = "application/pdf"):
    metadata = {
        "entity_hint": "UNRESOLVED",
        "evidence_type": "INVOICE",
        "actor_type": "USER",
        "actor_id": "test-operator",
        "note": "download-header test upload",
    }
    return client.post(
        "/internal/intake/evidence",
        data={"metadata": json.dumps(metadata)},
        files={"file": (filename, content, content_type)},
    )


def _upload_and_get_evidence_id(client, *, filename: str) -> str:
    response = _post_intake(client, content=SYNTHETIC_PDF, filename=filename)
    assert response.status_code == 201, response.text
    return response.json()["evidence"]["evidence_id"]


def _register_evidence_directly(client, *, filename: str, content: bytes = SYNTHETIC_PDF) -> str:
    """Register a real ``EvidenceItem`` (real Postgres + real MinIO,
    via ``composition.api``/``composition.object_store`` directly —
    the same calls ``app/api/routers/intake.py`` itself makes) with
    ``original_name=filename``, bypassing the HTTP multipart intake
    endpoint entirely.

    Used only for the double-quote case below. Reason: this repo's
    pinned test-HTTP-client stack (``httpx2`` — see
    ``requirements-dev.txt``) percent-encodes a literal ``"`` in a
    multipart file part's own ``filename=`` parameter when *sending*
    the upload request, and Starlette's multipart form parser does not
    reverse that percent-encoding back into a literal ``"`` when
    receiving it — confirmed directly against this exact client/server
    pair during this delivery. That is a property of the multipart
    request-encoding step, wholly unrelated to the response-header
    encoding this delta is about, and it would make an HTTP-round-trip
    upload silently test the wrong string. Registering the
    ``EvidenceItem`` directly (still against the real, disposable
    Postgres/MinIO stack) gets a literal ``"`` into
    ``EvidenceItem.original_name`` reliably, so the real
    ``GET /internal/evidence/{id}/content`` route can still be
    exercised end-to-end against it.
    """
    composition = get_composition()
    content_hash = {"algorithm": "SHA-256", "value": hashlib.sha256(content).hexdigest()}
    storage_reference = composition.object_store.put(generate_id(), content_hash, content)
    source_id = get_manual_upload_source_id(composition)
    evidence = composition.api.register_evidence(
        entity_id=None,
        evidence_type="INVOICE",
        source_id=source_id,
        observed_at=datetime.now(timezone.utc),
        received_at=datetime.now(timezone.utc),
        content_hash=content_hash,
        mime_type="application/pdf",
        size_bytes=len(content),
        actor_type="USER",
        actor_id="test-operator",
        original_name=filename,
        storage_reference=storage_reference,
        status="OBSERVED",
    )
    return evidence.evidence_id


# ---------------------------------------------------------------------
# Real-stack, end-to-end: ordinary filename
# ---------------------------------------------------------------------


def test_ordinary_filename_produces_correct_content_disposition(client):
    evidence_id = _upload_and_get_evidence_id(client, filename="invoice.pdf")

    resp = client.get(f"/internal/evidence/{evidence_id}/content")
    assert resp.status_code == 200

    disposition = resp.headers["content-disposition"]
    assert disposition == 'attachment; filename="invoice.pdf"; filename*=UTF-8\'\'invoice.pdf'

    # Byte-identical (delta only adds headers — it must not touch the
    # byte-serving logic at all).
    assert resp.content == SYNTHETIC_PDF


# ---------------------------------------------------------------------
# Real-stack, end-to-end: Unicode filename round-trips safely
# ---------------------------------------------------------------------


def test_unicode_filename_round_trips_via_filename_star_parameter(client):
    # é (precomposed, NFKC-stable), 日本語, and an emoji — all pass
    # WI-2's filename_safety (no control chars/separators/'..', and
    # each is already in NFKC normal form) so this genuinely reaches
    # the real endpoint through the real intake pipeline.
    original = "café-日本語-😀.pdf"
    evidence_id = _upload_and_get_evidence_id(client, filename=original)

    resp = client.get(f"/internal/evidence/{evidence_id}/content")
    assert resp.status_code == 200
    disposition = resp.headers["content-disposition"]

    assert disposition.startswith("attachment; filename=")
    assert "filename*=UTF-8''" in disposition

    star_param = disposition.split("filename*=UTF-8''", 1)[1]
    decoded = unquote(star_param, encoding="utf-8")
    assert decoded == original

    # The quoted-string ASCII fallback must still be well-formed (a
    # single quoted token, no stray unescaped '"') even though it can't
    # carry the non-ASCII characters itself.
    ascii_part = disposition.split(";")[1].strip()
    assert ascii_part.startswith('filename="')
    assert ascii_part.endswith('"')
    assert ascii_part.count('"') == 2

    assert resp.content == SYNTHETIC_PDF


# ---------------------------------------------------------------------
# Real-stack, end-to-end: a double-quote character in the filename
# (WI-2's filename_safety does NOT reject '"' at intake — only control
# characters, path separators, '..' segments, absolute paths, and
# NFKC-ambiguous names — so this one genuinely reaches the real route)
# ---------------------------------------------------------------------


def test_filename_with_double_quote_cannot_break_out_of_quoted_string(client):
    original = 'he said "hello".pdf'
    # Registered directly (real Postgres + real MinIO), not via HTTP
    # multipart upload — see _register_evidence_directly's own
    # docstring for why the HTTP path is unsuitable for this specific
    # character.
    evidence_id = _register_evidence_directly(client, filename=original)

    resp = client.get(f"/internal/evidence/{evidence_id}/content")
    assert resp.status_code == 200
    disposition = resp.headers["content-disposition"]

    ascii_part = disposition.split(";")[1].strip()
    assert ascii_part.startswith('filename="')
    assert ascii_part.endswith('"')
    # Exactly the two quotes that open/close the token — none of the
    # filename's own embedded quotes survived into the ASCII fallback.
    assert ascii_part.count('"') == 2

    # The real value is still fully recoverable, safely, from filename*.
    star_param = disposition.split("filename*=UTF-8''", 1)[1]
    assert unquote(star_param, encoding="utf-8") == original

    assert resp.content == SYNTHETIC_PDF


# ---------------------------------------------------------------------
# nosniff present on every response from this endpoint
# ---------------------------------------------------------------------


def test_nosniff_header_present(client):
    evidence_id = _upload_and_get_evidence_id(client, filename="report.pdf")
    resp = client.get(f"/internal/evidence/{evidence_id}/content")
    assert resp.status_code == 200
    assert resp.headers["x-content-type-options"] == "nosniff"


@pytest.mark.parametrize(
    "filename",
    ["plain.pdf", "café-日本語-😀.pdf", 'he said "hello".pdf'],
)
def test_nosniff_header_present_across_filename_shapes(client, filename):
    evidence_id = _upload_and_get_evidence_id(client, filename=filename)
    resp = client.get(f"/internal/evidence/{evidence_id}/content")
    assert resp.status_code == 200
    assert resp.headers["x-content-type-options"] == "nosniff"


# ---------------------------------------------------------------------
# Existing hash headers untouched by this delta
# ---------------------------------------------------------------------


def test_existing_evidence_and_hash_headers_unchanged(client):
    evidence_id = _upload_and_get_evidence_id(client, filename="invoice.pdf")
    resp = client.get(f"/internal/evidence/{evidence_id}/content")
    assert resp.status_code == 200
    assert resp.headers["x-bagman-evidence-id"] == evidence_id
    assert resp.headers["x-bagman-content-hash-algorithm"] == "SHA-256"
    assert resp.headers["x-bagman-content-hash-value"]


# ---------------------------------------------------------------------
# Direct unit tests of safe_content_disposition_header itself — see
# module docstring for why these bypass the intake pipeline entirely.
# ---------------------------------------------------------------------


def test_helper_rejects_crlf_header_injection_defensively():
    """A raw CR/LF cannot inject another header. WI-2's own
    filename_safety already rejects control characters (CR/LF
    included) at INTAKE time — this cannot be reproduced through the
    real HTTP pipeline — so this proves the encoding layer itself,
    calling it as a plain Python function, is defensive on its own even
    if that upstream filtering were ever weakened or bypassed."""
    hostile = 'evil.pdf"\r\nX-Injected-Header: pwned\r\nSet-Cookie: a=b'

    header_value = safe_content_disposition_header(hostile, fallback="evidence-abc123")

    # The header value itself is inherently a single line: if it
    # contained a raw CR or LF, it would no longer even be usable as one
    # HTTP header line at all.
    assert "\r" not in header_value
    assert "\n" not in header_value

    # And no attacker-controlled text leaked into the ASCII
    # quoted-string fallback either (exactly two quote characters: the
    # ones this function itself adds to open/close the token).
    ascii_part = header_value.split(";")[1].strip()
    assert ascii_part.startswith('filename="')
    assert ascii_part.endswith('"')
    assert ascii_part.count('"') == 2

    # The hostile bytes are still fully, safely recoverable from
    # filename* (percent-encoded — never literal CR/LF in the header).
    star_param = header_value.split("filename*=UTF-8''", 1)[1]
    assert unquote(star_param, encoding="utf-8") == hostile
    assert "%0D" in star_param or "%0d" in star_param.lower()
    assert "%0A" in star_param or "%0a" in star_param.lower()


def test_helper_neutralises_path_traversal_defensively():
    """A path-like filename cannot affect response semantics. WI-2's
    own filename_safety already rejects '../' at INTAKE time (see
    tests/app_api/test_intake_endpoint.py's
    test_rejected_upload_never_creates_canonical_evidence, which proves
    exactly this string is rejected as UNSAFE_FILENAME through the real
    HTTP pipeline) — so, again, this is most meaningfully a direct unit
    test of the header helper itself: defense-in-depth proof that even
    if upstream filtering were ever bypassed or weakened, this layer
    alone still neutralises path semantics in the header it produces."""
    hostile = "../../etc/passwd"

    header_value = safe_content_disposition_header(hostile, fallback="evidence-abc123")

    ascii_part = header_value.split(";")[1].strip()
    assert ascii_part.startswith('filename="')
    assert ascii_part.endswith('"')
    # No literal path separator survives into the ASCII fallback token
    # a naive/legacy HTTP client might otherwise honour at face value.
    inner = ascii_part[len('filename="'):-1]
    assert "/" not in inner
    assert "\\" not in inner

    # filename* percent-encodes the slashes rather than passing them
    # through as literal path separators.
    star_param = header_value.split("filename*=UTF-8''", 1)[1]
    assert "/" not in star_param
    assert unquote(star_param, encoding="utf-8") == hostile


def test_helper_falls_back_when_filename_is_none_or_empty():
    assert safe_content_disposition_header(None, fallback="evidence-abc123") == (
        'attachment; filename="evidence-abc123"'
    )
    assert safe_content_disposition_header("", fallback="evidence-abc123") == (
        'attachment; filename="evidence-abc123"'
    )


def test_helper_falls_back_when_every_character_is_stripped():
    # An all-control-character name: nothing usable survives the ASCII
    # fallback pass, so the caller-supplied fallback is used for the
    # quoted-string parameter instead of an empty filename="".
    header_value = safe_content_disposition_header("\r\n\x00\x01", fallback="evidence-xyz789")
    ascii_part = header_value.split(";")[1].strip()
    assert ascii_part == 'filename="evidence-xyz789"'
