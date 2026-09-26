"""CD-6 §103 Inference Architecture Ruling —
`scripts/process_background_job_overflow.py` proofs against in-memory
repositories and `ai.providers.litellm.fake.FakeLiteLLMClient` (never a
real/live model call — PID §61). Mirrors
`tests/integration/test_reprocess_evidence_classification.py`'s own
structure: exercises this script's dependency-injected core functions
(`run_submit`/`run_process`/`load_manifest`) directly, never
`main()`/`get_composition()` (no env vars needed)."""
from __future__ import annotations

import email.message
import hashlib
import json
from datetime import datetime, timezone

import pytest

from ai.invocation import InMemoryAIInvocationRepository
from ai.jobs import InMemoryBackgroundJobRepository
from ai.providers.litellm.client import LiteLLMOutcomeStatus
from ai.providers.litellm.fake import FakeLiteLLMClient
from core import identity
from core.audit import InMemoryAuditRepository
from core.external_reference import InMemoryExternalReferenceRepository
from persistence.objects.memory_store import InMemoryObjectStore
from services.evidence.evidence import InMemoryEvidenceRepository

import scripts.process_background_job_overflow as overflow

ACTOR_TYPE = "SYSTEM"
ACTOR_ID = "wi-overflow-tests"


@pytest.fixture
def evidence_repository():
    return InMemoryEvidenceRepository(InMemoryExternalReferenceRepository())


@pytest.fixture
def object_store():
    return InMemoryObjectStore()


@pytest.fixture
def background_job_repository(audit_repository):
    return InMemoryBackgroundJobRepository(audit_repository=audit_repository)


@pytest.fixture
def ai_invocation_repository(audit_repository):
    return InMemoryAIInvocationRepository(audit_repository=audit_repository)


@pytest.fixture
def audit_repository():
    return InMemoryAuditRepository()


@pytest.fixture
def litellm_client():
    return FakeLiteLLMClient()


def _rfc822_email(*, sender: str, subject: str, body: str) -> bytes:
    msg = email.message.EmailMessage()
    msg["From"] = sender
    msg["Subject"] = subject
    msg.set_content(body)
    return bytes(msg)


def _register_email_evidence(evidence_repository, object_store, *, content: bytes) -> str:
    content_hash = {"algorithm": "SHA-256", "value": hashlib.sha256(content).hexdigest()}
    storage_reference = object_store.put(identity.generate_id(), content_hash, content)
    now = datetime.now(timezone.utc)
    item = evidence_repository.register_evidence(
        entity_id=None, evidence_type="EMAIL", source_id=identity.generate_id(),
        observed_at=now, received_at=now, content_hash=content_hash, mime_type="message/rfc822",
        size_bytes=len(content), storage_reference=storage_reference, metadata={},
    )
    return item.evidence_id


def _manifest_file(tmp_path, entries):
    path = tmp_path / "manifest.json"
    path.write_text(json.dumps(entries), encoding="utf-8")
    return str(path)


# ---------------------------------------------------------------------
# load_manifest
# ---------------------------------------------------------------------


def test_load_manifest_parses_valid_entries(tmp_path):
    path = _manifest_file(tmp_path, [{"evidence_id": "e1", "task_id": "DOCUMENT_SUMMARY", "task_version": 1}])
    items = overflow.load_manifest(path)
    assert items == [{"evidence_id": "e1", "task_id": "DOCUMENT_SUMMARY", "task_version": 1}]


def test_load_manifest_rejects_non_json(tmp_path):
    path = tmp_path / "bad.json"
    path.write_text("not json", encoding="utf-8")
    with pytest.raises(SystemExit):
        overflow.load_manifest(str(path))


def test_load_manifest_rejects_empty_array(tmp_path):
    path = _manifest_file(tmp_path, [])
    with pytest.raises(SystemExit):
        overflow.load_manifest(path)


def test_load_manifest_rejects_entry_missing_required_key(tmp_path):
    path = _manifest_file(tmp_path, [{"evidence_id": "e1", "task_id": "DOCUMENT_SUMMARY"}])
    with pytest.raises(SystemExit):
        overflow.load_manifest(path)


# ---------------------------------------------------------------------
# run_submit
# ---------------------------------------------------------------------


def test_run_submit_creates_a_pending_job_for_a_valid_entry(evidence_repository, object_store, background_job_repository):
    evidence_id = _register_email_evidence(
        evidence_repository, object_store,
        content=_rfc822_email(sender="a@example.com", subject="hi", body="hello world"),
    )
    manifest_items = [{"evidence_id": evidence_id, "task_id": "DOCUMENT_SUMMARY", "task_version": 1}]

    result = overflow.run_submit(
        manifest_items=manifest_items, evidence_repository=evidence_repository, object_store=object_store,
        background_job_repository=background_job_repository, actor_type=ACTOR_TYPE, actor_id=ACTOR_ID,
    )
    assert result["per_outcome_counts"] == {"SUBMITTED": 1}
    [record] = result["records"]
    assert record["status"] == "PENDING"

    jobs = background_job_repository.list_jobs()
    assert len(jobs) == 1
    assert jobs[0].inference_backend == "TRINITY_CORE_OVERFLOW"
    assert jobs[0].capability_alias == "trinity-core"
    assert jobs[0].input_references == {"evidence_id": evidence_id}


def test_run_submit_is_idempotent_across_reruns(evidence_repository, object_store, background_job_repository):
    evidence_id = _register_email_evidence(
        evidence_repository, object_store,
        content=_rfc822_email(sender="a@example.com", subject="hi", body="hello world"),
    )
    manifest_items = [{"evidence_id": evidence_id, "task_id": "DOCUMENT_SUMMARY", "task_version": 1}]

    overflow.run_submit(
        manifest_items=manifest_items, evidence_repository=evidence_repository, object_store=object_store,
        background_job_repository=background_job_repository, actor_type=ACTOR_TYPE, actor_id=ACTOR_ID,
    )
    overflow.run_submit(
        manifest_items=manifest_items, evidence_repository=evidence_repository, object_store=object_store,
        background_job_repository=background_job_repository, actor_type=ACTOR_TYPE, actor_id=ACTOR_ID,
    )
    assert len(background_job_repository.list_jobs()) == 1


def test_run_submit_records_unknown_evidence_and_does_not_abort_batch(evidence_repository, object_store, background_job_repository):
    good_evidence_id = _register_email_evidence(
        evidence_repository, object_store,
        content=_rfc822_email(sender="a@example.com", subject="hi", body="hello world"),
    )
    manifest_items = [
        {"evidence_id": "does-not-exist", "task_id": "DOCUMENT_SUMMARY", "task_version": 1},
        {"evidence_id": good_evidence_id, "task_id": "DOCUMENT_SUMMARY", "task_version": 1},
    ]
    result = overflow.run_submit(
        manifest_items=manifest_items, evidence_repository=evidence_repository, object_store=object_store,
        background_job_repository=background_job_repository, actor_type=ACTOR_TYPE, actor_id=ACTOR_ID,
    )
    assert result["per_outcome_counts"] == {"UNKNOWN_EVIDENCE": 1, "SUBMITTED": 1}
    assert len(background_job_repository.list_jobs()) == 1


def test_run_submit_records_unknown_task(evidence_repository, object_store, background_job_repository):
    evidence_id = _register_email_evidence(
        evidence_repository, object_store,
        content=_rfc822_email(sender="a@example.com", subject="hi", body="hello world"),
    )
    manifest_items = [{"evidence_id": evidence_id, "task_id": "NOT_A_REAL_TASK", "task_version": 1}]
    result = overflow.run_submit(
        manifest_items=manifest_items, evidence_repository=evidence_repository, object_store=object_store,
        background_job_repository=background_job_repository, actor_type=ACTOR_TYPE, actor_id=ACTOR_ID,
    )
    assert result["per_outcome_counts"] == {"UNKNOWN_TASK": 1}
    assert background_job_repository.list_jobs() == []


# ---------------------------------------------------------------------
# run_process
# ---------------------------------------------------------------------


def _submit_one_job(evidence_repository, object_store, background_job_repository, *, task_id="DOCUMENT_SUMMARY", task_version=1):
    evidence_id = _register_email_evidence(
        evidence_repository, object_store,
        content=_rfc822_email(sender="a@example.com", subject="hi", body="hello world"),
    )
    manifest_items = [{"evidence_id": evidence_id, "task_id": task_id, "task_version": task_version}]
    overflow.run_submit(
        manifest_items=manifest_items, evidence_repository=evidence_repository, object_store=object_store,
        background_job_repository=background_job_repository, actor_type=ACTOR_TYPE, actor_id=ACTOR_ID,
    )
    return evidence_id


def test_run_process_succeeds_and_marks_job_succeeded(
    evidence_repository, object_store, background_job_repository, ai_invocation_repository, litellm_client, audit_repository
):
    _submit_one_job(evidence_repository, object_store, background_job_repository)
    litellm_client.queue_success(
        capability_alias="trinity-core",
        content=json.dumps({"summary": "a short summary", "confidence": 0.8, "signals": [], "warnings": []}),
    )

    result = overflow.run_process(
        limit=10, worker_id="test-worker",
        background_job_repository=background_job_repository, ai_invocation_repository=ai_invocation_repository,
        litellm_client=litellm_client, record_audit_event=audit_repository.record_audit_event,
    )
    assert result["claimed_count"] == 1
    assert result["per_outcome_counts"] == {"SUCCEEDED": 1}
    [record] = result["records"]
    assert record["job_status"] == "SUCCEEDED"

    job = background_job_repository.get_job(record["job_id"])
    assert job.status == "SUCCEEDED"
    assert job.ai_invocation_id == record["ai_invocation_id"]

    invocation = ai_invocation_repository.get_invocation(record["ai_invocation_id"])
    assert invocation.capability_alias == "trinity-core"
    assert invocation.inference_backend == "TRINITY_CORE_OVERFLOW"
    assert invocation.status == "SUCCEEDED"


def test_run_process_retryable_failure_marks_job_failed_retryable(
    evidence_repository, object_store, background_job_repository, ai_invocation_repository, litellm_client, audit_repository
):
    _submit_one_job(evidence_repository, object_store, background_job_repository)
    litellm_client.queue_failure(capability_alias="trinity-core", status=LiteLLMOutcomeStatus.TIMEOUT, error_detail="slow")

    result = overflow.run_process(
        limit=10, worker_id="test-worker",
        background_job_repository=background_job_repository, ai_invocation_repository=ai_invocation_repository,
        litellm_client=litellm_client, record_audit_event=audit_repository.record_audit_event,
    )
    assert result["per_outcome_counts"] == {"RETRYABLE_FAILURE": 1}
    [record] = result["records"]
    assert record["job_status"] == "FAILED_RETRYABLE"


def test_run_process_non_retryable_schema_failure_marks_job_failed_terminal(
    evidence_repository, object_store, background_job_repository, ai_invocation_repository, litellm_client, audit_repository
):
    _submit_one_job(evidence_repository, object_store, background_job_repository)
    litellm_client.queue_success(capability_alias="trinity-core", content="not valid json at all")

    result = overflow.run_process(
        limit=10, worker_id="test-worker",
        background_job_repository=background_job_repository, ai_invocation_repository=ai_invocation_repository,
        litellm_client=litellm_client, record_audit_event=audit_repository.record_audit_event,
    )
    assert result["per_outcome_counts"] == {"TERMINAL_FAILURE": 1}
    [record] = result["records"]
    assert record["job_status"] == "FAILED_TERMINAL"
    assert record["error_code"] == "OUTPUT_NOT_JSON"


def test_run_process_claims_nothing_when_no_jobs_pending(ai_invocation_repository, background_job_repository, litellm_client, audit_repository):
    result = overflow.run_process(
        limit=10, worker_id="test-worker",
        background_job_repository=background_job_repository, ai_invocation_repository=ai_invocation_repository,
        litellm_client=litellm_client, record_audit_event=audit_repository.record_audit_event,
    )
    assert result["claimed_count"] == 0
    assert result["records"] == []


def test_run_process_respects_limit(
    evidence_repository, object_store, background_job_repository, ai_invocation_repository, litellm_client, audit_repository
):
    _submit_one_job(evidence_repository, object_store, background_job_repository)
    _submit_one_job(evidence_repository, object_store, background_job_repository)
    litellm_client.queue_success(
        capability_alias="trinity-core",
        content=json.dumps({"summary": "s", "confidence": 0.5, "signals": [], "warnings": []}),
    )

    result = overflow.run_process(
        limit=1, worker_id="test-worker",
        background_job_repository=background_job_repository, ai_invocation_repository=ai_invocation_repository,
        litellm_client=litellm_client, record_audit_event=audit_repository.record_audit_event,
    )
    assert result["claimed_count"] == 1
    assert len(background_job_repository.list_jobs(status="PENDING")) == 1


# ---------------------------------------------------------------------
# CLI argument validation
# ---------------------------------------------------------------------


def test_parse_args_requires_manifest_file_with_submit():
    with pytest.raises(SystemExit):
        overflow._parse_args(["--submit", "--runtime-dir", "/tmp/x"])


def test_parse_args_requires_limit_with_process():
    with pytest.raises(SystemExit):
        overflow._parse_args(["--process", "--runtime-dir", "/tmp/x"])


def test_parse_args_requires_runtime_dir(monkeypatch):
    monkeypatch.delenv("BAGMAN_BACKGROUND_JOB_OVERFLOW_RUNTIME_DIR", raising=False)
    with pytest.raises(SystemExit):
        overflow._parse_args(["--process", "--limit", "5"])


def test_parse_args_rejects_limit_out_of_bounds():
    with pytest.raises(SystemExit):
        overflow._parse_args(["--process", "--limit", "0", "--runtime-dir", "/tmp/x"])
