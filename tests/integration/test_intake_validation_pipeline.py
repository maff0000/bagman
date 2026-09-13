"""Tests for `services.evidence.intake.validation_pipeline` (CD-4
WI-2, PID §68's WI-2 definition, §63 acceptance fixtures).

Uses `InMemoryIntakeRepository` (WI-1) and `InMemoryObjectStore` (CD-3
WI-3) — no Docker/Postgres/MinIO required for the bulk of this file,
mirroring `tests/integration/test_intake_domain.py`'s own choice for
domain-behaviour tests. A `_ScriptedScanner`/`_RaisingScanner` TEST-ONLY
fake drives every `ScanVerdict` branch deterministically; see the
warning in its docstring. A small number of tests additionally use the
REAL `ClamAVScanner` against the disposable daemon documented in
`test_intake_scanner.py` (skipped, not mocked, if unreachable) to prove
the fail-closed contract end-to-end against genuine malware-signature
detection, not just the fake.
"""
from __future__ import annotations

import hashlib
import io

import pytest

from core import identity
from core.errors import ImmutabilityViolationError, NotFoundError
from persistence.objects.memory_store import InMemoryObjectStore
from persistence.objects.store import compute_sha256, quarantine_object_key, staging_object_key
from services.evidence.intake.intake import InMemoryIntakeRepository
from services.evidence.intake.policy import IntakePolicy
from services.evidence.intake.scanner import EvidenceSafetyScanner, ScanResult, ScanVerdict
from services.evidence.intake.validation_pipeline import run_intake_validation
from tests.integration.test_intake_scanner import EICAR_TEST_STRING, requires_live_clamav

SYNTHETIC_PDF = b"%PDF-1.4\nSYNTHETIC TEST DATA - not a real document\n%%EOF"
SYNTHETIC_PNG = b"\x89PNG\r\n\x1a\nsynthetic-png-bytes-for-testing"
SYNTHETIC_ZIP = b"PK\x03\x04synthetic-zip-local-file-header"
SYNTHETIC_ELF = b"\x7fELF\x02\x01\x01\x00synthetic-elf-header-not-real"
SYNTHETIC_UNKNOWN_BINARY = bytes(range(256)) * 4  # no recognised magic bytes at all


class _ScriptedScanner(EvidenceSafetyScanner):
    """TEST-ONLY fake scanner — NOT a real security control. Returns a
    fixed, pre-scripted :class:`ScanResult` regardless of what content
    it is handed, so control-flow tests can deterministically drive
    every :class:`ScanVerdict` branch. Never confuse this with the REAL
    `ClamAVScanner` proofs in `test_intake_scanner.py`/this file's
    `requires_live_clamav`-marked tests, which exercise genuine
    malware-signature detection — this fake proves none of that, only
    that `validation_pipeline` reacts correctly to each possible
    verdict VALUE.
    """

    def __init__(self, verdict: ScanVerdict, detail: str = "scripted-for-test") -> None:
        self._result = ScanResult(verdict, detail)

    def scan(self, content) -> ScanResult:  # noqa: ANN001 - test double
        return self._result

    def is_available(self) -> bool:
        return True


class _RaisingScanner(EvidenceSafetyScanner):
    """TEST-ONLY fake scanner that always raises, proving the pipeline
    fails closed even when the scanner implementation itself crashes
    (not just when it returns SCAN_ERROR as a value)."""

    def scan(self, content):  # noqa: ANN001 - test double
        raise RuntimeError("simulated scanner crash")

    def is_available(self) -> bool:
        return False


@pytest.fixture
def repository() -> InMemoryIntakeRepository:
    return InMemoryIntakeRepository()


@pytest.fixture
def object_store() -> InMemoryObjectStore:
    return InMemoryObjectStore()


@pytest.fixture
def source_id() -> str:
    return identity.generate_id()


def _received(repository, source_id, **kwargs):
    return repository.create_intake_record(source_id=source_id, **kwargs)


# ---------------------------------------------------------------------
# Happy path
# ---------------------------------------------------------------------


def test_clean_pdf_is_accepted_and_staged(repository, object_store, source_id):
    record = _received(
        repository, source_id, original_filename="invoice.pdf", reported_mime_type="application/pdf"
    )
    result = run_intake_validation(
        intake_id=record.intake_id,
        stream=io.BytesIO(SYNTHETIC_PDF),
        repository=repository,
        object_store=object_store,
        scanner=_ScriptedScanner(ScanVerdict.CLEAN),
    )
    assert result.status == "ACCEPTED"
    assert result.detected_mime_type == "application/pdf"
    assert result.size_bytes == len(SYNTHETIC_PDF)
    assert result.content_hash["algorithm"] == "SHA-256"
    assert result.evidence_id is None  # WI-2 never registers evidence itself
    assert result.completed_at is None  # ACCEPTED is not yet terminal

    storage_reference = result.metadata["storage_reference"]
    expected_key = staging_object_key(record.intake_id, result.content_hash["value"])
    assert storage_reference == expected_key
    assert object_store.get(storage_reference) == SYNTHETIC_PDF


def test_accepted_bytes_are_never_stored_under_the_canonical_evidence_prefix(
    repository, object_store, source_id
):
    """WI-2 must not overreach into WI-3's canonical registration —
    proven here by checking the staged key never looks like an
    `evidence/` reference."""
    record = _received(repository, source_id)
    result = run_intake_validation(
        intake_id=record.intake_id,
        stream=io.BytesIO(SYNTHETIC_PDF),
        repository=repository,
        object_store=object_store,
        scanner=_ScriptedScanner(ScanVerdict.CLEAN),
    )
    assert result.metadata["storage_reference"].startswith("intake-staging/")


def test_mime_mismatch_is_observed_not_rejected_under_default_policy(repository, object_store, source_id):
    record = _received(
        repository, source_id, original_filename="photo.png", reported_mime_type="image/png"
    )
    result = run_intake_validation(
        intake_id=record.intake_id,
        stream=io.BytesIO(SYNTHETIC_PDF),  # actually a PDF despite the reported PNG mime
        repository=repository,
        object_store=object_store,
        scanner=_ScriptedScanner(ScanVerdict.CLEAN),
    )
    assert result.status == "ACCEPTED"
    assert result.detected_mime_type == "application/pdf"
    assert record.reported_mime_type == "image/png"
    assert result.metadata["mime_mismatch_observed"] is True
    assert result.metadata["reported_mime_type_at_validation"] == "image/png"


def test_mime_mismatch_is_rejected_under_strict_policy(repository, object_store, source_id):
    record = _received(
        repository, source_id, original_filename="photo.png", reported_mime_type="image/png"
    )
    strict_policy = IntakePolicy(policy_id="test-strict-mismatch", mime_mismatch_policy="REJECT")
    result = run_intake_validation(
        intake_id=record.intake_id,
        stream=io.BytesIO(SYNTHETIC_PDF),
        repository=repository,
        object_store=object_store,
        scanner=_ScriptedScanner(ScanVerdict.CLEAN),
        policy=strict_policy,
    )
    assert result.status == "REJECTED"
    assert result.failure_code == "CONTENT_TYPE_MISMATCH"


def test_scanner_is_never_called_when_scanner_not_required(repository, object_store, source_id):
    record = _received(repository, source_id)
    no_scan_policy = IntakePolicy(policy_id="test-no-scan", scanner_required=False)
    result = run_intake_validation(
        intake_id=record.intake_id,
        stream=io.BytesIO(SYNTHETIC_PDF),
        repository=repository,
        object_store=object_store,
        scanner=_RaisingScanner(),  # would blow up the whole pipeline if ever called
        policy=no_scan_policy,
    )
    assert result.status == "ACCEPTED"


# ---------------------------------------------------------------------
# Filename safety -> REJECTED (PID §63's path-traversal fixture)
# ---------------------------------------------------------------------


def test_path_traversal_filename_is_rejected_before_any_scanning(repository, object_store, source_id):
    record = _received(repository, source_id, original_filename="../../etc/passwd")
    result = run_intake_validation(
        intake_id=record.intake_id,
        stream=io.BytesIO(SYNTHETIC_PDF),
        repository=repository,
        object_store=object_store,
        scanner=_RaisingScanner(),  # must never be reached
    )
    assert result.status == "REJECTED"
    assert result.failure_code == "UNSAFE_FILENAME"
    assert result.content_hash is None  # never even spooled


# ---------------------------------------------------------------------
# Size limit -> REJECTED (PID §63's oversized-stream fixture)
# ---------------------------------------------------------------------


def test_oversized_upload_is_rejected(repository, object_store, source_id):
    record = _received(repository, source_id, original_filename="huge.pdf")
    tiny_limit_policy = IntakePolicy(policy_id="test-tiny-limit", max_file_size_bytes=16)
    result = run_intake_validation(
        intake_id=record.intake_id,
        stream=io.BytesIO(SYNTHETIC_PDF),  # far bigger than 16 bytes
        repository=repository,
        object_store=object_store,
        scanner=_RaisingScanner(),  # must never be reached
        policy=tiny_limit_policy,
    )
    assert result.status == "REJECTED"
    assert result.failure_code == "FILE_TOO_LARGE"


# ---------------------------------------------------------------------
# Empty file (PID §63's empty-file fixture)
# ---------------------------------------------------------------------


def test_empty_file_is_rejected_as_unsupported_content_type(repository, object_store, source_id):
    record = _received(repository, source_id, original_filename="empty.bin")
    result = run_intake_validation(
        intake_id=record.intake_id,
        stream=io.BytesIO(b""),
        repository=repository,
        object_store=object_store,
        scanner=_RaisingScanner(),  # must never be reached — rejected before scanning
    )
    assert result.status == "REJECTED"
    assert result.failure_code == "UNSUPPORTED_CONTENT_TYPE"
    assert result.size_bytes is None  # REJECTED never populates these


# ---------------------------------------------------------------------
# Archive doctrine (PID §17/§63)
# ---------------------------------------------------------------------


def test_archive_is_rejected_under_default_policy(repository, object_store, source_id):
    record = _received(repository, source_id, original_filename="bundle.zip")
    result = run_intake_validation(
        intake_id=record.intake_id,
        stream=io.BytesIO(SYNTHETIC_ZIP),
        repository=repository,
        object_store=object_store,
        scanner=_RaisingScanner(),  # must never be reached
    )
    assert result.status == "REJECTED"
    assert result.failure_code == "UNSUPPORTED_CONTENT_TYPE"


def test_archive_is_quarantined_under_a_quarantine_archive_policy(repository, object_store, source_id):
    record = _received(repository, source_id, original_filename="bundle.zip")
    quarantine_archives = IntakePolicy(policy_id="test-quarantine-archives", archive_treatment="QUARANTINE")
    result = run_intake_validation(
        intake_id=record.intake_id,
        stream=io.BytesIO(SYNTHETIC_ZIP),
        repository=repository,
        object_store=object_store,
        scanner=_RaisingScanner(),  # must never be reached — quarantined before scanning
        policy=quarantine_archives,
    )
    assert result.status == "QUARANTINED"
    assert result.quarantine_reason
    assert result.detected_mime_type == "application/zip"

    key = quarantine_object_key(record.intake_id, result.content_hash["value"])
    assert object_store.get(key) == SYNTHETIC_ZIP


# ---------------------------------------------------------------------
# Executable content doctrine (PID §18/§63)
# ---------------------------------------------------------------------


def test_executable_content_is_quarantined_under_default_policy_and_bytes_are_retained(
    repository, object_store, source_id
):
    """Uploaded with a filename/reported type claiming to be an
    ordinary PDF — detected content must override that (PID §18)."""
    record = _received(
        repository, source_id, original_filename="invoice.pdf", reported_mime_type="application/pdf"
    )
    result = run_intake_validation(
        intake_id=record.intake_id,
        stream=io.BytesIO(SYNTHETIC_ELF),
        repository=repository,
        object_store=object_store,
        scanner=_RaisingScanner(),  # must never be reached — quarantined before scanning
    )
    assert result.status == "QUARANTINED"
    assert result.detected_mime_type == "application/x-elf"
    assert result.evidence_id is None

    key = quarantine_object_key(record.intake_id, result.content_hash["value"])
    assert object_store.get(key) == SYNTHETIC_ELF


def test_executable_content_is_rejected_under_a_reject_executables_policy(repository, object_store, source_id):
    record = _received(repository, source_id, original_filename="tool.bin")
    reject_exe_policy = IntakePolicy(policy_id="test-reject-exe", executable_treatment="REJECT")
    result = run_intake_validation(
        intake_id=record.intake_id,
        stream=io.BytesIO(SYNTHETIC_ELF),
        repository=repository,
        object_store=object_store,
        scanner=_RaisingScanner(),
        policy=reject_exe_policy,
    )
    assert result.status == "REJECTED"
    assert result.failure_code == "UNSUPPORTED_CONTENT_TYPE"


# ---------------------------------------------------------------------
# Unsupported-but-benign content type (PID §16)
# ---------------------------------------------------------------------


def test_unsupported_binary_content_type_is_rejected_under_default_policy(repository, object_store, source_id):
    record = _received(repository, source_id, original_filename="unknown.dat")
    result = run_intake_validation(
        intake_id=record.intake_id,
        stream=io.BytesIO(SYNTHETIC_UNKNOWN_BINARY),
        repository=repository,
        object_store=object_store,
        scanner=_RaisingScanner(),
    )
    assert result.status == "REJECTED"
    assert result.failure_code == "UNSUPPORTED_CONTENT_TYPE"


def test_unsupported_binary_content_type_is_quarantined_under_a_soft_launch_policy(
    repository, object_store, source_id
):
    record = _received(repository, source_id, original_filename="unknown.dat")
    soft_launch_policy = IntakePolicy(
        policy_id="test-soft-launch-unsupported", unsupported_content_treatment="QUARANTINE"
    )
    result = run_intake_validation(
        intake_id=record.intake_id,
        stream=io.BytesIO(SYNTHETIC_UNKNOWN_BINARY),
        repository=repository,
        object_store=object_store,
        scanner=_RaisingScanner(),
        policy=soft_launch_policy,
    )
    assert result.status == "QUARANTINED"


# ---------------------------------------------------------------------
# Scanner verdicts (PID §19/§20) — fake, deterministic control-flow proof
# ---------------------------------------------------------------------


@pytest.mark.parametrize("verdict", [ScanVerdict.SUSPICIOUS, ScanVerdict.MALICIOUS])
def test_suspicious_or_malicious_verdict_is_quarantined_and_bytes_retained(
    repository, object_store, source_id, verdict
):
    record = _received(repository, source_id, original_filename="invoice.pdf")
    result = run_intake_validation(
        intake_id=record.intake_id,
        stream=io.BytesIO(SYNTHETIC_PDF),
        repository=repository,
        object_store=object_store,
        scanner=_ScriptedScanner(verdict, detail="Some.Test.Signature"),
    )
    assert result.status == "QUARANTINED"
    assert "Some.Test.Signature" in result.quarantine_reason
    assert result.evidence_id is None

    key = quarantine_object_key(record.intake_id, result.content_hash["value"])
    assert object_store.get(key) == SYNTHETIC_PDF


def test_scan_error_fails_closed_to_failed_never_accepted(repository, object_store, source_id):
    record = _received(repository, source_id, original_filename="invoice.pdf")
    result = run_intake_validation(
        intake_id=record.intake_id,
        stream=io.BytesIO(SYNTHETIC_PDF),
        repository=repository,
        object_store=object_store,
        scanner=_ScriptedScanner(ScanVerdict.SCAN_ERROR, detail="daemon unreachable"),
    )
    assert result.status == "FAILED"
    assert result.failure_code == "SCAN_FAILED"
    # A pure infrastructure failure never populates these (unlike
    # QUARANTINED/ACCEPTED) — nothing was ever committed downstream.
    assert result.content_hash is None
    assert result.detected_mime_type is None
    assert result.size_bytes is None

    # No bytes were stored anywhere either.
    content_hash_value = hashlib.sha256(SYNTHETIC_PDF).hexdigest()
    with pytest.raises(NotFoundError):
        object_store.get(staging_object_key(record.intake_id, content_hash_value))
    with pytest.raises(NotFoundError):
        object_store.get(quarantine_object_key(record.intake_id, content_hash_value))


def test_a_raising_scanner_also_fails_closed_to_failed(repository, object_store, source_id):
    """Fail-closed must hold even when the scanner implementation
    itself crashes, not only when it returns SCAN_ERROR as a value."""
    record = _received(repository, source_id, original_filename="invoice.pdf")
    result = run_intake_validation(
        intake_id=record.intake_id,
        stream=io.BytesIO(SYNTHETIC_PDF),
        repository=repository,
        object_store=object_store,
        scanner=_RaisingScanner(),
    )
    assert result.status == "FAILED"
    assert result.failure_code == "SCAN_FAILED"


def test_scanner_unsupported_verdict_is_quarantined_under_default_policy(repository, object_store, source_id):
    record = _received(repository, source_id, original_filename="invoice.pdf")
    result = run_intake_validation(
        intake_id=record.intake_id,
        stream=io.BytesIO(SYNTHETIC_PDF),
        repository=repository,
        object_store=object_store,
        scanner=_ScriptedScanner(ScanVerdict.UNSUPPORTED, detail="cannot evaluate this container"),
    )
    assert result.status == "QUARANTINED"


def test_scanner_unsupported_verdict_can_be_configured_to_fail_instead(repository, object_store, source_id):
    record = _received(repository, source_id, original_filename="invoice.pdf")
    strict_unsupported_policy = IntakePolicy(
        policy_id="test-strict-unsupported-scan", scan_unsupported_treatment="FAIL"
    )
    result = run_intake_validation(
        intake_id=record.intake_id,
        stream=io.BytesIO(SYNTHETIC_PDF),
        repository=repository,
        object_store=object_store,
        scanner=_ScriptedScanner(ScanVerdict.UNSUPPORTED),
        policy=strict_unsupported_policy,
    )
    assert result.status == "FAILED"
    assert result.failure_code == "SCAN_FAILED"


# ---------------------------------------------------------------------
# Idempotent re-validation storage behaviour
# ---------------------------------------------------------------------


def test_quarantine_storage_conflict_surfaces_as_immutability_violation_not_silently_swallowed(
    repository, object_store, source_id
):
    """Defensive: if a caller somehow re-ran validation for the same
    intake_id with genuinely different bytes after an earlier
    quarantine already stored something under that intake_id's key
    (same hash would mean same bytes, so this simulates a hash
    collision-shaped disagreement at the object-store layer directly),
    the object store's own immutability guard must still surface as a
    real exception, never a silent overwrite."""
    intake_id = "synthetic-fixed-intake-id-for-this-test"
    first_bytes = b"first bytes at this key"
    content_hash = {"algorithm": "SHA-256", "value": compute_sha256(first_bytes)}
    key = quarantine_object_key(intake_id, content_hash["value"])
    object_store.put_prefixed("quarantine", intake_id, content_hash, first_bytes)

    # Simulate different bytes somehow already occupying the same key
    # (e.g. a hash-collision-shaped disagreement) by writing directly,
    # bypassing put_prefixed()'s own hash check — mirroring
    # tests/persistence/test_object_store_quarantine.py's own
    # immutability-violation test pattern.
    object_store._objects[key] = b"different bytes, same key"  # noqa: SLF001 - test-only

    with pytest.raises(ImmutabilityViolationError):
        object_store.put_prefixed("quarantine", intake_id, content_hash, first_bytes)


# ---------------------------------------------------------------------
# Real ClamAV daemon end-to-end (skipped if unreachable, PID §63/§20)
# ---------------------------------------------------------------------


@requires_live_clamav
def test_real_clamav_quarantines_a_genuine_eicar_upload_end_to_end(repository, object_store, source_id):
    from services.evidence.intake.scanner import ClamAVScanner

    record = _received(repository, source_id, original_filename="suspicious.txt")
    real_scanner = ClamAVScanner(host="127.0.0.1", port=33100, connect_timeout=5.0, scan_timeout=30.0)

    result = run_intake_validation(
        intake_id=record.intake_id,
        stream=io.BytesIO(EICAR_TEST_STRING),
        repository=repository,
        object_store=object_store,
        scanner=real_scanner,
    )
    assert result.status == "QUARANTINED"
    assert "eicar" in result.quarantine_reason.lower()

    key = quarantine_object_key(record.intake_id, result.content_hash["value"])
    assert object_store.get(key) == EICAR_TEST_STRING


@requires_live_clamav
def test_real_clamav_accepts_genuine_clean_content_end_to_end(repository, object_store, source_id):
    from services.evidence.intake.scanner import ClamAVScanner

    record = _received(repository, source_id, original_filename="invoice.pdf", reported_mime_type="application/pdf")
    real_scanner = ClamAVScanner(host="127.0.0.1", port=33100, connect_timeout=5.0, scan_timeout=30.0)

    result = run_intake_validation(
        intake_id=record.intake_id,
        stream=io.BytesIO(SYNTHETIC_PDF),
        repository=repository,
        object_store=object_store,
        scanner=real_scanner,
    )
    assert result.status == "ACCEPTED"
