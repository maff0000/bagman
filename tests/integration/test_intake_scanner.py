"""Tests for `services.evidence.intake.scanner` (CD-4 WI-2, PID
§19/§20/§63).

Two genuinely distinct groups of tests, clearly separated so it is
never ambiguous which is which (PID §63's own instruction: "be
explicit in your tests about which is which"):

* **Real-daemon tests** (`TestClamAVScannerAgainstARealDaemon`) —
  exercise `ClamAVScanner` against an ACTUAL running `clamd` over a
  real TCP socket. Skipped (not failed, not mocked) if nothing answers
  at the configured endpoint — see `requires_live_clamav` below for the
  disposable container command this delivery actually used and
  verified against. The EICAR string used here is the standardised,
  publicly-documented antivirus test signature every scanner is
  designed to detect (https://www.eicar.org/) — NOT real malware, safe
  to commit (PID §63).

* **Pure unit tests of `_parse_instream_response`** — no network at
  all, exercising the protocol-response-parsing logic directly against
  synthetic clamd wire-protocol strings. These do NOT prove the real
  daemon integration; they prove this module's own parsing logic in
  isolation.

Disposable `clamd` container used to build/verify this module
------------------------------------------------------------------
    docker run -d --name bagman-test-clamav-wi2 \\
        -p 33100:3310 clamav/clamav:stable

The `clamav/clamav:stable` image ships with virus definitions already
baked in at image-build time, so `clamd` becomes reachable and able to
recognise EICAR within seconds of starting even with no further
network access for `freshclam` updates (verified during this delivery
— see the delivery report). Tear down with
`docker rm -f bagman-test-clamav-wi2`.
"""
from __future__ import annotations

import os
import socket

import pytest

from services.evidence.intake.scanner import ClamAVScanner, ScanResult, ScanVerdict

#: The standardised EICAR antivirus test string (PID §63) — a
#: publicly-documented, harmless string every antivirus engine is
#: designed to flag as a test signature. NOT real malware.
EICAR_TEST_STRING = (
    rb"X5O!P%@AP[4\PZX54(P^)7CC)7}$EICAR-STANDARD-ANTIVIRUS-TEST-FILE!$H+H*"
)

_HOST = os.environ.get("BAGMAN_TEST_CLAMAV_HOST", "127.0.0.1")
_PORT = int(os.environ.get("BAGMAN_TEST_CLAMAV_PORT", "33100"))


def _clamav_is_up() -> bool:
    try:
        with socket.create_connection((_HOST, _PORT), timeout=2) as sock:
            sock.sendall(b"zPING\0")
            return sock.recv(64).strip(b"\0") == b"PONG"
    except OSError:
        return False


requires_live_clamav = pytest.mark.skipif(
    not _clamav_is_up(),
    reason=(
        f"no disposable clamd answering at {_HOST}:{_PORT} — these tests require a "
        "REAL scanner daemon, not a mock (PID §20/§63); see this file's module "
        "docstring for the disposable container command"
    ),
)


@pytest.fixture
def scanner() -> ClamAVScanner:
    return ClamAVScanner(host=_HOST, port=_PORT, connect_timeout=5.0, scan_timeout=30.0)


# ---------------------------------------------------------------------
# Real daemon (skipped if unreachable)
# ---------------------------------------------------------------------


@requires_live_clamav
class TestClamAVScannerAgainstARealDaemon:
    def test_is_available_true_against_a_real_reachable_daemon(self, scanner):
        assert scanner.is_available() is True

    def test_clean_synthetic_content_is_clean(self, scanner):
        result = scanner.scan(b"synthetic clean evidence content, nothing malicious here")
        assert result.verdict == ScanVerdict.CLEAN

    def test_eicar_test_string_is_detected_as_malicious(self, scanner):
        result = scanner.scan(EICAR_TEST_STRING)
        assert result.verdict == ScanVerdict.MALICIOUS
        assert "eicar" in result.detail.lower()

    def test_eicar_test_string_detected_when_scanning_from_a_path(self, scanner, tmp_path):
        path = tmp_path / "synthetic_eicar_test_file"
        path.write_bytes(EICAR_TEST_STRING)
        result = scanner.scan(path)
        assert result.verdict == ScanVerdict.MALICIOUS

    def test_clean_content_from_a_larger_synthetic_file_streams_in_chunks(self, scanner, tmp_path):
        # Bigger than one INSTREAM chunk (_STREAM_CHUNK_SIZE = 64 KiB)
        # so this genuinely proves chunked streaming works end-to-end
        # against the real daemon, not just a single small write.
        path = tmp_path / "synthetic_large_clean_file"
        path.write_bytes(b"synthetic clean content. " * 10_000)  # ~250 KB
        result = scanner.scan(path)
        assert result.verdict == ScanVerdict.CLEAN


@pytest.mark.skipif(_clamav_is_up(), reason="this test specifically requires an UNREACHABLE daemon")
def test_is_available_false_when_daemon_unreachable():
    scanner = ClamAVScanner(host="127.0.0.1", port=1, connect_timeout=1.0)
    assert scanner.is_available() is False


def test_scan_returns_scan_error_never_raises_when_daemon_unreachable():
    """Fail-closed at the transport layer: an unreachable daemon must
    come back as a SCAN_ERROR *value*, never a raised exception (PID
    §20) — this specifically targets an address nothing is listening
    on, so it exercises real connection-refused/timeout behaviour, not
    a mock."""
    scanner = ClamAVScanner(host="127.0.0.1", port=1, connect_timeout=1.0, scan_timeout=1.0)
    result = scanner.scan(b"irrelevant content")
    assert result.verdict == ScanVerdict.SCAN_ERROR
    assert result.detail  # some explanatory detail was captured


# ---------------------------------------------------------------------
# Pure protocol-parsing unit tests — no network
# ---------------------------------------------------------------------


@pytest.mark.parametrize(
    "raw_response, expected_verdict",
    [
        (b"stream: OK\0", ScanVerdict.CLEAN),
        (b"stream: Eicar-Test-Signature FOUND\0", ScanVerdict.MALICIOUS),
        (b"stream: Some.Heuristics.Match FOUND\0", ScanVerdict.SUSPICIOUS),
        (b"stream: Some error occurred ERROR\0", ScanVerdict.SCAN_ERROR),
        (b"INSTREAM size limit exceeded. ERROR\0", ScanVerdict.SCAN_ERROR),
        (b"", ScanVerdict.SCAN_ERROR),
        (b"something entirely unexpected\0", ScanVerdict.UNSUPPORTED),
    ],
)
def test_parse_instream_response(raw_response, expected_verdict):
    result = ClamAVScanner._parse_instream_response(raw_response)
    assert isinstance(result, ScanResult)
    assert result.verdict == expected_verdict


def test_parse_instream_response_extracts_the_bare_signature_name():
    result = ClamAVScanner._parse_instream_response(b"stream: Eicar-Test-Signature FOUND\0")
    assert result.detail == "Eicar-Test-Signature"
