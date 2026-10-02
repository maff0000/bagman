"""CD-6 Slice 4 tests for
`services.mailbox.microsoft.evidence_ingest.ingest_email_evidence` —
reuse of CD-4's bounded-streaming + scanner + EvidenceRepository
architecture, idempotent replay via `external_reference`, and the
oversize/quarantine terminal outcomes.
"""
from __future__ import annotations

from datetime import datetime, timezone

import pytest

from core.api import BagmanCanonicalAPI
from core import identity
from persistence.objects.memory_store import InMemoryObjectStore
from services.evidence.intake.scanner import EvidenceSafetyScanner, ScanResult, ScanVerdict
from services.evidence.intake.policy import EMAIL_MESSAGE_MIME_TYPE
from services.mailbox.microsoft.evidence_ingest import (
    INGEST_STATUS_FAILED,
    INGEST_STATUS_INGESTED,
    INGEST_STATUS_QUARANTINED,
    ingest_email_evidence,
)


class ScriptedScanner(EvidenceSafetyScanner):
    def __init__(self, verdict: ScanVerdict):
        self._verdict = verdict

    def scan(self, content):
        return ScanResult(self._verdict, detail="scripted")

    def is_available(self):
        return True


@pytest.fixture
def api() -> BagmanCanonicalAPI:
    return BagmanCanonicalAPI()


@pytest.fixture
def object_store() -> InMemoryObjectStore:
    return InMemoryObjectStore()


@pytest.fixture
def source_id(api) -> str:
    source = api.register_source(
        source_type="EMAIL_MAILBOX", provider="test-mailbox", status="ACTIVE", actor_type="SYSTEM", actor_id="test"
    )
    return source.source_id


def _ingest(api, object_store, scanner, *, source_id, message_id="msg-1", raw=b"From: a@b.com\r\nSubject: Hi\r\n\r\nBody"):
    now = datetime.now(timezone.utc)
    return ingest_email_evidence(
        raw_mime_bytes=raw,
        mailbox_id=identity.generate_id(),
        mailbox_source_id=source_id,
        immutable_provider_message_id=message_id,
        observed_at=now,
        received_at=now,
        sender_address="a@b.com",
        subject="Hi",
        api=api,
        object_store=object_store,
        scanner=scanner,
        actor_type="SYSTEM",
        actor_id="test",
    )


def test_clean_scan_registers_a_real_evidence_item(api, object_store, source_id):
    scanner = ScriptedScanner(ScanVerdict.CLEAN)
    outcome = _ingest(api, object_store, scanner, source_id=source_id)
    assert outcome.status == INGEST_STATUS_INGESTED
    assert outcome.evidence is not None
    assert outcome.evidence.mime_type == EMAIL_MESSAGE_MIME_TYPE
    assert outcome.evidence.evidence_type == "EMAIL"


def test_malicious_scan_quarantines_never_registers_evidence(api, object_store, source_id):
    scanner = ScriptedScanner(ScanVerdict.MALICIOUS)
    outcome = _ingest(api, object_store, scanner, source_id=source_id, message_id="msg-mal")
    assert outcome.status == INGEST_STATUS_QUARANTINED
    assert outcome.evidence is None


def test_suspicious_scan_also_quarantines(api, object_store, source_id):
    scanner = ScriptedScanner(ScanVerdict.SUSPICIOUS)
    outcome = _ingest(api, object_store, scanner, source_id=source_id, message_id="msg-susp")
    assert outcome.status == INGEST_STATUS_QUARANTINED


def test_oversized_message_is_an_explicit_failed_outcome_never_truncated(api, object_store, source_id):
    scanner = ScriptedScanner(ScanVerdict.CLEAN)
    now = datetime.now(timezone.utc)
    outcome = ingest_email_evidence(
        raw_mime_bytes=b"x" * 1000,
        mailbox_id=identity.generate_id(),
        mailbox_source_id=source_id,
        immutable_provider_message_id="msg-big",
        observed_at=now,
        received_at=now,
        sender_address="a@b.com",
        subject="Hi",
        api=api,
        object_store=object_store,
        scanner=scanner,
        actor_type="SYSTEM",
        actor_id="test",
        max_size_bytes=500,
    )
    assert outcome.status == INGEST_STATUS_FAILED
    assert outcome.evidence is None


def test_idempotent_replay_of_the_same_immutable_id_resolves_to_the_same_evidence(api, object_store, source_id):
    scanner = ScriptedScanner(ScanVerdict.CLEAN)
    first = _ingest(api, object_store, scanner, source_id=source_id, message_id="msg-replay")
    second = _ingest(api, object_store, scanner, source_id=source_id, message_id="msg-replay")
    assert first.evidence.evidence_id == second.evidence.evidence_id


def test_scanner_raising_is_treated_as_a_failed_infrastructure_outcome_fail_closed(api, object_store, source_id):
    class RaisingScanner(EvidenceSafetyScanner):
        def scan(self, content):
            raise RuntimeError("scanner down")

        def is_available(self):
            return False

    outcome = _ingest(api, object_store, RaisingScanner(), source_id=source_id, message_id="msg-scanfail")
    assert outcome.status == INGEST_STATUS_FAILED
    assert outcome.evidence is None


def test_never_stores_raw_mime_in_the_evidence_metadata(api, object_store, source_id):
    scanner = ScriptedScanner(ScanVerdict.CLEAN)
    outcome = _ingest(api, object_store, scanner, source_id=source_id, message_id="msg-nolog")
    # metadata carries only non-body context — never the raw bytes/body text.
    assert "Body" not in str(outcome.evidence.metadata)


# ---------------------------------------------------------------------
# evidence/classification-simplification WO — the mailbox call site no
# longer enqueues a classification job of any kind at all
# (classification-job discovery now happens entirely on the worker
# side — see services/evidence/classification_job.py's own module
# docstring, "Simplified design" section). See
# tests/app_api/test_intake_endpoint.py for the manual-upload call
# site's equivalent proof.
# ---------------------------------------------------------------------


def test_clean_ingest_creates_zero_classification_jobs(api, object_store, source_id):
    """`classification_job_repository` is accepted-and-ignored (see
    `ingest_email_evidence`'s own docstring and
    `_ClassificationJobRepository`'s own docstring for why the
    parameter is kept but never called) — proves zero
    `EvidenceClassificationJob` rows are ever created by this call
    site, even when a real repository IS supplied."""
    from services.evidence.classification_job import InMemoryEvidenceClassificationJobRepository

    classification_job_repository = InMemoryEvidenceClassificationJobRepository()
    scanner = ScriptedScanner(ScanVerdict.CLEAN)
    outcome = ingest_email_evidence(
        raw_mime_bytes=b"From: a@b.com\r\nSubject: Hi\r\n\r\nBody",
        mailbox_id=identity.generate_id(),
        mailbox_source_id=source_id,
        immutable_provider_message_id="msg-no-auto-classify",
        observed_at=datetime.now(timezone.utc),
        received_at=datetime.now(timezone.utc),
        sender_address="a@b.com",
        subject="Hi",
        api=api,
        object_store=object_store,
        scanner=scanner,
        actor_type="SYSTEM",
        actor_id="test",
        classification_job_repository=classification_job_repository,
    )
    assert outcome.status == INGEST_STATUS_INGESTED

    job = classification_job_repository.get_by_evidence(outcome.evidence.evidence_id)
    assert job is None


def test_ingest_with_no_classification_job_repository_supplied_never_raises(api, object_store, source_id):
    """`classification_job_repository` defaults to `None` — proving
    ingestion succeeds even when no classification-job
    repository/worker is reachable at all, with no accidental coupling
    remaining (required proof: ingestion decoupling)."""
    scanner = ScriptedScanner(ScanVerdict.CLEAN)
    outcome = _ingest(api, object_store, scanner, source_id=source_id, message_id="msg-no-job-repo")
    assert outcome.status == INGEST_STATUS_INGESTED


def test_quarantined_ingest_creates_zero_classification_jobs(api, object_store, source_id):
    from services.evidence.classification_job import InMemoryEvidenceClassificationJobRepository

    classification_job_repository = InMemoryEvidenceClassificationJobRepository()
    scanner = ScriptedScanner(ScanVerdict.MALICIOUS)
    outcome = ingest_email_evidence(
        raw_mime_bytes=b"From: a@b.com\r\nSubject: Hi\r\n\r\nBody",
        mailbox_id=identity.generate_id(),
        mailbox_source_id=source_id,
        immutable_provider_message_id="msg-quarantine-no-job",
        observed_at=datetime.now(timezone.utc),
        received_at=datetime.now(timezone.utc),
        sender_address="a@b.com",
        subject="Hi",
        api=api,
        object_store=object_store,
        scanner=scanner,
        actor_type="SYSTEM",
        actor_id="test",
        classification_job_repository=classification_job_repository,
    )
    assert outcome.status == INGEST_STATUS_QUARANTINED
    assert outcome.evidence is None
    # No evidence_id exists to create a job against at all — structurally
    # zero jobs, proven by an empty repository.
    assert classification_job_repository._by_id == {}  # noqa: SLF001 - direct internal-state proof, test-only
