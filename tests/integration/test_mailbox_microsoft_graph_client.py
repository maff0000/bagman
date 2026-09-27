"""Slice 3/4/5 governance-reconciliation delta — a narrowly-scoped test
for `services.mailbox.microsoft.graph_client._is_resync_required`'s new
bounded-read fix. NO real network call to Microsoft is ever made by
this test file.

This does not attempt to close the broader "zero test coverage on the
real HTTP-handling path" gap the Slice 3/4/5 governance audit
independently found in `services.mailbox.microsoft.graph_client` —
that is a genuine, larger, disclosed gap left for a future delivery
(see the reconciliation record in `PID.md`). This file covers only the
one concrete defect corrected here: `_is_resync_required` previously
read a 410 response body via a bare, unbounded `exc.read()` before
`json.loads()`-parsing it for structured `ResyncRequired` classification
— a real memory-exhaustion risk against a hostile/malformed body,
mirroring (in shape, not in the specific misclassification Gmail hit)
`services.mailbox.gmail.gmail_client._read_body_bounded`'s own already-
tested "bounded read before structured parse" discipline.
"""
from __future__ import annotations

import io
import json
import urllib.error
from typing import Optional

from services.mailbox.microsoft.graph_client import _MAX_RESYNC_BODY_BYTES, _is_resync_required


def _fake_http_error(code: int, *, body: bytes = b"{}") -> urllib.error.HTTPError:
    return urllib.error.HTTPError(url="https://example.com", code=code, msg="err", hdrs=None, fp=io.BytesIO(body))


def test_is_resync_required_true_for_a_real_shaped_410_body():
    body = json.dumps({"error": {"code": "ResyncRequired", "message": "delta token expired"}}).encode("utf-8")
    assert _is_resync_required(_fake_http_error(410, body=body)) is True


def test_is_resync_required_false_for_a_410_with_a_different_error_code():
    body = json.dumps({"error": {"code": "SomethingElse", "message": "x"}}).encode("utf-8")
    assert _is_resync_required(_fake_http_error(410, body=body)) is False


def test_is_resync_required_false_for_a_non_410_status():
    body = json.dumps({"error": {"code": "ResyncRequired"}}).encode("utf-8")
    assert _is_resync_required(_fake_http_error(403, body=body)) is False


def test_is_resync_required_false_for_malformed_json():
    assert _is_resync_required(_fake_http_error(410, body=b"not json at all")) is False


def test_is_resync_required_never_reads_more_than_the_bounded_ceiling_even_for_an_oversized_body():
    """The real, independently-found gap this delta fixes: a bare
    `exc.read()` with no bound at all. An oversized (but validly
    JSON-shaped, to isolate the oversize path from the malformed-JSON
    path) 410 body must be treated as "not resync" — a truncated
    prefix of a real ResyncRequired document is not itself valid JSON,
    and must never be mistaken for one — never crash, never consume
    unbounded memory."""
    huge_message = "x" * (_MAX_RESYNC_BODY_BYTES * 2)
    body = json.dumps({"error": {"code": "ResyncRequired", "message": huge_message}}).encode("utf-8")
    assert len(body) > _MAX_RESYNC_BODY_BYTES
    assert _is_resync_required(_fake_http_error(410, body=body)) is False


def test_is_resync_required_true_for_a_body_exactly_at_the_bound():
    # A real ResyncRequired body well under the bound, padded with
    # trailing whitespace up to (but not past) the ceiling — still
    # genuinely parseable, still correctly classified.
    payload = {"error": {"code": "ResyncRequired", "message": "delta token expired"}}
    base = json.dumps(payload).encode("utf-8")
    padded = base + b" " * (_MAX_RESYNC_BODY_BYTES - len(base))
    assert len(padded) == _MAX_RESYNC_BODY_BYTES
    assert _is_resync_required(_fake_http_error(410, body=padded)) is True
