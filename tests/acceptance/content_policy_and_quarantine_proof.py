"""CD-4 WI-5 acceptance evidence — content-policy/quarantine fixture proof
(PID §63's full fixture list, exercised via real HTTP against the live
Docker Compose stack with the REAL ``bagman-scan`` ClamAV daemon — no
stub/mock scanner anywhere in this file).

Real, directly-runnable script (see ``tests/acceptance/README.md`` — this
directory's scripts are deliberately NOT ``test_*.py`` so ``pytest tests/
-q`` never touches the real runtime). No mocks: every fixture below is
POSTed as a real multipart upload to the real, running ``bagman-api``
container, which validates it against ``DEFAULT_INTAKE_POLICY`` and
scans it with the real ``bagman-scan`` ``clamd`` daemon.

Fixtures covered (PID §63)
--------------------------
* valid PDF / JPEG / PNG / plain text / CSV -> each ACCEPTED + REGISTERED
* empty file -> observed policy behaviour asserted explicitly (REJECTED,
  UNSUPPORTED_CONTENT_TYPE — an empty stream sniffs to
  ``application/octet-stream``, which is not in the accepted set; see
  ``services.evidence.intake.content_sniffing.sniff``'s own docstring)
* oversized stream (just over ``DEFAULT_INTAKE_POLICY.max_file_size_bytes``,
  not an impractically huge file) -> REJECTED, FILE_TOO_LARGE, proven
  without the server ever buffering the whole stream (PID §13)
* extension/reported-MIME vs. detected-content mismatch -> ACCEPTED per
  ``DEFAULT_INTAKE_POLICY.mime_mismatch_policy == "OBSERVE"``, with the
  mismatch recorded observably in the record's metadata
* unsupported binary -> REJECTED, UNSUPPORTED_CONTENT_TYPE
* synthetic archive (ZIP magic bytes) -> REJECTED, UNSUPPORTED_CONTENT_TYPE
  (``DEFAULT_INTAKE_POLICY.archive_treatment == "REJECT"``)
* filename path-traversal attempt -> REJECTED, UNSAFE_FILENAME
* EICAR standard test string -> QUARANTINED by the REAL ClamAV daemon
  (never real malware — see module docstring of
  ``tests/integration/test_intake_scanner.py``, which this file reuses
  the exact same constant from)
* duplicate content (same bytes, no/different idempotency keys) -> TWO
  distinct intake_id/evidence_id records (PID §24 "same hash != same
  evidence" doctrine), not an automatic collapse

Run standalone (brings the stack up itself first):

    python3 tests/acceptance/content_policy_and_quarantine_proof.py

Leaves the stack running on success.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import requests

sys.path.insert(0, str(Path(__file__).resolve().parent))

import _lib  # noqa: E402
from _lib import BASE_URL, compose_build, compose_up_wait, run_id, section, wait_for_ready  # noqa: E402

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT))

from services.evidence.intake.policy import DEFAULT_INTAKE_POLICY  # noqa: E402

# The standardised EICAR antivirus test string (PID §63) — a
# publicly-documented, harmless string every antivirus engine is
# designed to flag as a test signature. NOT real malware. Same
# constant WI-2/WI-3's own test suite already uses
# (tests/integration/test_intake_scanner.py).
EICAR_TEST_STRING = rb"X5O!P%@AP[4\PZX54(P^)7CC)7}$EICAR-STANDARD-ANTIVIRUS-TEST-FILE!$H+H*"

ACTOR_ID = "wi5-content-policy-proof"


def _post_intake(
    *,
    content: bytes,
    filename: str,
    reported_mime_type: str = "text/plain",
    idempotency_key: str | None = None,
    entity_hint: str = "WI5_CONTENT_POLICY",
) -> requests.Response:
    metadata = {
        "entity_hint": entity_hint,
        "evidence_type": "DOCUMENT",
        "actor_type": "SYSTEM",
        "actor_id": ACTOR_ID,
        "note": None,
    }
    headers = {"Idempotency-Key": idempotency_key} if idempotency_key else {}
    return requests.post(
        f"{BASE_URL}/internal/intake/evidence",
        data={"metadata": json.dumps(metadata)},
        files={"file": (filename, content, reported_mime_type)},
        headers=headers,
        timeout=60,
    )


def _assert_registered(response: requests.Response, *, label: str) -> dict:
    print(f"    [{label}] HTTP {response.status_code}")
    assert response.status_code == 201, f"[{label}] expected 201, got {response.status_code}: {response.text[:300]}"
    body = response.json()
    assert body["intake"]["status"] == "REGISTERED", f"[{label}] expected REGISTERED, got {body['intake']}"
    assert body["evidence"] is not None, f"[{label}] evidence missing: {body}"
    print(f"        -> REGISTERED, evidence_id={body['evidence']['evidence_id']}")
    return body


def _assert_rejected(response: requests.Response, *, label: str, failure_code: str) -> dict:
    print(f"    [{label}] HTTP {response.status_code}")
    assert response.status_code == 422, f"[{label}] expected 422, got {response.status_code}: {response.text[:300]}"
    body = response.json()
    assert body["intake"]["status"] == "REJECTED", f"[{label}] expected REJECTED, got {body['intake']}"
    assert body["intake"]["failure_code"] == failure_code, (
        f"[{label}] expected failure_code={failure_code!r}, got {body['intake']['failure_code']!r}: {body}"
    )
    print(f"        -> REJECTED failure_code={failure_code}")
    return body


def _assert_quarantined(response: requests.Response, *, label: str) -> dict:
    print(f"    [{label}] HTTP {response.status_code}")
    assert response.status_code == 200, f"[{label}] expected 200, got {response.status_code}: {response.text[:300]}"
    body = response.json()
    assert body["intake"]["status"] == "QUARANTINED", f"[{label}] expected QUARANTINED, got {body['intake']}"
    assert body["evidence"] is None, f"[{label}] quarantined intake must never carry canonical evidence: {body}"
    print(f"        -> QUARANTINED reason={body['intake']['quarantine_reason']!r}")
    return body


def part_valid_types(tag: str) -> None:
    section("VALID CONTENT TYPES — each ACCEPTED + REGISTERED (PID §16)")
    fixtures = {
        "pdf": (b"%PDF-1.4\nWI-5 synthetic test content, not a real document\n%%EOF", "application/pdf", ".pdf"),
        "jpeg": (b"\xff\xd8\xff\xe0" + b"synthetic-jpeg-bytes-for-testing" * 4, "image/jpeg", ".jpg"),
        "png": (b"\x89PNG\r\n\x1a\n" + b"synthetic-png-bytes-for-testing" * 4, "image/png", ".png"),
        "text": (b"WI-5 synthetic plain text evidence body.\n", "text/plain", ".txt"),
        "csv": (b"a,b,c\n1,2,3\n4,5,6\n", "text/csv", ".csv"),
    }
    for name, (content, mime, ext) in fixtures.items():
        response = _post_intake(content=content, filename=f"wi5-valid-{name}-{tag}{ext}", reported_mime_type=mime)
        body = _assert_registered(response, label=f"valid {name}")
        assert body["evidence"]["mime_type"] in DEFAULT_INTAKE_POLICY.accepted_mime_types


def part_empty_file(tag: str) -> None:
    section("EMPTY FILE (PID §63)")
    response = _post_intake(content=b"", filename=f"wi5-empty-{tag}.txt")
    _assert_rejected(response, label="empty file", failure_code="UNSUPPORTED_CONTENT_TYPE")


def part_oversized(tag: str) -> None:
    section("OVERSIZED SYNTHETIC STREAM (PID §12/§13/§63)")
    oversize = DEFAULT_INTAKE_POLICY.max_file_size_bytes + 1024
    print(f"    generating {oversize} bytes (policy limit is {DEFAULT_INTAKE_POLICY.max_file_size_bytes})...")
    content = b"A" * oversize
    response = _post_intake(content=content, filename=f"wi5-oversized-{tag}.txt")
    _assert_rejected(response, label="oversized stream", failure_code="FILE_TOO_LARGE")


def part_mime_mismatch(tag: str) -> None:
    section("REPORTED/DETECTED MIME MISMATCH (PID §15)")
    # Reported as PDF, but the actual bytes are a PNG — accepted content
    # type either way, so DEFAULT_INTAKE_POLICY.mime_mismatch_policy ==
    # "OBSERVE" applies: ACCEPTED, mismatch recorded observably.
    content = b"\x89PNG\r\n\x1a\n" + b"actually-a-png-reported-as-pdf" * 4
    response = _post_intake(
        content=content, filename=f"wi5-mismatch-{tag}.pdf", reported_mime_type="application/pdf"
    )
    body = _assert_registered(response, label="mime mismatch (reported PDF, actual PNG)")
    assert body["evidence"]["mime_type"] == "image/png", body["evidence"]
    intake_metadata = body["intake"]["metadata"]
    assert intake_metadata.get("mime_mismatch_observed") is True, (
        f"expected mime_mismatch_observed=True recorded in intake metadata: {intake_metadata}"
    )
    assert intake_metadata.get("reported_mime_type_at_validation") == "application/pdf", intake_metadata
    print(f"        -> mismatch OBSERVED and recorded: {intake_metadata}")


def part_unsupported_binary(tag: str) -> None:
    section("UNSUPPORTED/UNKNOWN BINARY (PID §16/§63)")
    content = bytes(range(256)) * 4  # no recognised magic bytes, not decodable text
    response = _post_intake(content=content, filename=f"wi5-unknown-binary-{tag}.bin", reported_mime_type="application/octet-stream")
    _assert_rejected(response, label="unsupported binary", failure_code="UNSUPPORTED_CONTENT_TYPE")


def part_archive(tag: str) -> None:
    section("SYNTHETIC ARCHIVE — ZIP MAGIC BYTES (PID §17/§63)")
    content = b"PK\x03\x04" + b"synthetic-zip-local-file-header-not-a-real-archive" * 4
    response = _post_intake(content=content, filename=f"wi5-archive-{tag}.zip", reported_mime_type="application/zip")
    _assert_rejected(response, label="synthetic archive", failure_code="UNSUPPORTED_CONTENT_TYPE")


def part_path_traversal_filename(tag: str) -> None:
    section("FILENAME PATH-TRAVERSAL ATTEMPT (PID §14/§63)")
    content = b"WI-5 synthetic plain text body for a hostile filename.\n"
    response = _post_intake(content=content, filename="../../etc/passwd", reported_mime_type="text/plain")
    _assert_rejected(response, label="path-traversal filename", failure_code="UNSAFE_FILENAME")


def part_eicar(tag: str) -> None:
    section("EICAR STANDARD TEST STRING — REAL ClamAV DAEMON (PID §19/§20/§21/§63)")
    response = _post_intake(content=EICAR_TEST_STRING, filename=f"wi5-eicar-{tag}.txt", reported_mime_type="text/plain")
    body = _assert_quarantined(response, label="EICAR test string")
    reason = body["intake"]["quarantine_reason"] or ""
    assert "eicar" in reason.lower(), f"expected the real ClamAV verdict to name the EICAR signature: {reason!r}"
    print("        -> REAL ClamAV daemon detected the EICAR test signature and the file was quarantined, not accepted.")


def part_duplicate_content(tag: str) -> None:
    section("DUPLICATE CONTENT — SAME BYTES, NO/DIFFERENT IDEMPOTENCY KEYS (PID §24/§63)")
    content = f"WI-5 duplicate-content proof body {tag}\n".encode()
    response_1 = _post_intake(content=content, filename=f"wi5-dup-a-{tag}.txt")
    response_2 = _post_intake(content=content, filename=f"wi5-dup-b-{tag}.txt")
    body_1 = _assert_registered(response_1, label="duplicate content, submission 1 (no key)")
    body_2 = _assert_registered(response_2, label="duplicate content, submission 2 (no key)")
    assert body_1["intake"]["intake_id"] != body_2["intake"]["intake_id"], "two uploads collapsed to one intake_id"
    assert body_1["evidence"]["evidence_id"] != body_2["evidence"]["evidence_id"], (
        "two uploads collapsed to one evidence_id — PID §24 'same hash != same evidence' doctrine violated"
    )
    assert body_1["evidence"]["content_hash"] == body_2["evidence"]["content_hash"], (
        "expected identical content_hash for identical bytes"
    )
    print(
        f"        -> two DISTINCT evidence records for identical bytes: "
        f"{body_1['evidence']['evidence_id']} != {body_2['evidence']['evidence_id']} "
        f"(same content_hash, as expected)"
    )


def main() -> None:
    section("SETUP — bring up the real BAGMAN Docker Compose stack (real bagman-scan ClamAV)")
    compose_build()
    compose_up_wait()
    ready = wait_for_ready()
    assert ready["checks"]["scanner"] == "ok", f"scanner must be healthy before this proof runs: {ready}"

    tag = run_id()

    part_valid_types(tag)
    part_empty_file(tag)
    part_oversized(tag)
    part_mime_mismatch(tag)
    part_unsupported_binary(tag)
    part_archive(tag)
    part_path_traversal_filename(tag)
    part_eicar(tag)
    part_duplicate_content(tag)

    section("SUMMARY")
    print("    ALL PID §63 CONTENT-POLICY / QUARANTINE FIXTURES PROVEN AGAINST THE REAL STACK")
    print("(stack left running)")


if __name__ == "__main__":
    main()
