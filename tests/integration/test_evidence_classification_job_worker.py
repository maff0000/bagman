"""`evidence/automatic-classification-activation` WO —
`services.evidence.classification_job` orchestration proofs and
`scripts/process_evidence_classification_jobs.py` proofs, against
in-memory repositories and `ai.providers.litellm.fake.FakeLiteLLMClient`
(never a real/live model call — PID §61). Mirrors
`tests/integration/test_process_background_job_overflow.py`'s own
structure: exercises this script's dependency-injected core function
(`run_process`) directly, never `main()`/`get_composition()` (no env
vars needed), and
`tests/integration/test_classification_orchestrator.py`'s own
evidence/rule/AI-proposal fixture setup.
"""
from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone

import pytest

from ai.invocation import InMemoryAIInvocationRepository
from ai.providers.litellm.fake import FakeLiteLLMClient
from core import actor, identity
from core.audit import InMemoryAuditRepository
from core.errors import PersistenceError
from core.external_reference import InMemoryExternalReferenceRepository
from persistence.objects.memory_store import InMemoryObjectStore
from services.evidence.classification import (
    CLASSIFICATION_TYPE_DOCUMENT_TYPE,
    DOCUMENT_TYPE_SUPPLIER_INVOICE,
    SOURCE_OPERATOR_ASSIGNED,
    STATUS_CLASSIFIED,
    InMemoryEvidenceClassificationRepository,
)
from services.evidence.classification_job import (
    InMemoryEvidenceClassificationJobRepository,
    enqueue_classification_job_for_evidence,
)
from services.evidence.classification_rule import InMemoryEvidenceClassificationRuleRepository
from services.evidence.evidence import InMemoryEvidenceRepository
from services.needs_you.needs_you import InMemoryNeedsYouRepository

import scripts.process_evidence_classification_jobs as worker

ACTOR_ID = "evidence-classification-job-worker-tests"


@pytest.fixture
def evidence_repository():
    return InMemoryEvidenceRepository(InMemoryExternalReferenceRepository())


@pytest.fixture
def rule_repository():
    return InMemoryEvidenceClassificationRuleRepository()


@pytest.fixture
def audit_repository():
    return InMemoryAuditRepository()


@pytest.fixture
def ai_invocation_repository(audit_repository):
    return InMemoryAIInvocationRepository(audit_repository=audit_repository)


@pytest.fixture
def classification_repository(evidence_repository, rule_repository, ai_invocation_repository):
    return InMemoryEvidenceClassificationRepository(
        evidence_repository=evidence_repository, rule_repository=rule_repository,
        ai_invocation_repository=ai_invocation_repository,
    )


@pytest.fixture
def object_store():
    return InMemoryObjectStore()


@pytest.fixture
def litellm_client():
    return FakeLiteLLMClient()


@pytest.fixture
def needs_you_repository():
    return InMemoryNeedsYouRepository()


@pytest.fixture
def classification_job_repository(audit_repository):
    return InMemoryEvidenceClassificationJobRepository(audit_repository=audit_repository)


def _register_email_evidence(
    evidence_repository, object_store, *, content: bytes, sender_address=None, subject=None,
) -> str:
    content_hash = {"algorithm": "SHA-256", "value": hashlib.sha256(content).hexdigest()}
    storage_reference = object_store.put(identity.generate_id(), content_hash, content)
    metadata = {}
    if sender_address is not None:
        metadata["sender_address"] = sender_address
    if subject is not None:
        metadata["subject"] = subject
    now = datetime.now(timezone.utc)
    item = evidence_repository.register_evidence(
        entity_id=None, evidence_type="EMAIL", source_id=identity.generate_id(),
        observed_at=now, received_at=now, content_hash=content_hash, mime_type="message/rfc822",
        size_bytes=len(content), storage_reference=storage_reference, metadata=metadata,
    )
    return item.evidence_id


def _rfc822_email(*, sender: str, subject: str, body: str) -> bytes:
    import email.message

    msg = email.message.EmailMessage()
    msg["From"] = sender
    msg["Subject"] = subject
    msg.set_content(body)
    return bytes(msg)


def _queue_proposal(litellm_client, *, proposed_type: str, confidence: float = 0.8, signals=None, warnings=None):
    litellm_client.queue_success(
        capability_alias="bagman-core",
        content=json.dumps(
            {"proposed_type": proposed_type, "confidence": confidence, "signals": signals or [], "warnings": warnings or []}
        ),
    )


def _run_process(
    *, limit, classification_job_repository, evidence_repository, rule_repository, classification_repository,
    ai_invocation_repository, litellm_client, object_store, audit_repository, needs_you_repository,
):
    return worker.run_process(
        limit=limit,
        claimed_by="test-worker-1",
        classification_job_repository=classification_job_repository,
        evidence_repository=evidence_repository,
        rule_repository=rule_repository,
        classification_repository=classification_repository,
        ai_invocation_repository=ai_invocation_repository,
        litellm_client=litellm_client,
        object_store=object_store,
        audit_repository=audit_repository,
        record_audit_event=audit_repository.record_audit_event,
        needs_you_repository=needs_you_repository,
    )


# ---------------------------------------------------------------------
# Test plan item 1/2 — enqueue creates exactly one PENDING job,
# idempotently.
# ---------------------------------------------------------------------


def test_enqueue_creates_exactly_one_pending_job(evidence_repository, object_store, classification_job_repository):
    evidence_id = _register_email_evidence(
        evidence_repository, object_store,
        content=_rfc822_email(sender="billing@vendor.com", subject="Invoice #1", body="pay up"),
    )
    enqueue_classification_job_for_evidence(
        evidence_id, classification_job_repository=classification_job_repository, actor_type=actor.SYSTEM, actor_id=ACTOR_ID,
    )
    job = classification_job_repository.get_by_evidence(evidence_id)
    assert job is not None
    assert job.status == "PENDING"


def test_second_enqueue_for_the_same_evidence_is_idempotent(evidence_repository, object_store, classification_job_repository):
    evidence_id = _register_email_evidence(
        evidence_repository, object_store,
        content=_rfc822_email(sender="billing@vendor.com", subject="Invoice #2", body="pay up"),
    )
    enqueue_classification_job_for_evidence(
        evidence_id, classification_job_repository=classification_job_repository, actor_type=actor.SYSTEM, actor_id=ACTOR_ID,
    )
    first = classification_job_repository.get_by_evidence(evidence_id)
    enqueue_classification_job_for_evidence(
        evidence_id, classification_job_repository=classification_job_repository, actor_type=actor.SYSTEM, actor_id="a-different-caller",
    )
    second = classification_job_repository.get_by_evidence(evidence_id)
    assert second.job_id == first.job_id
    assert second.actor_id == first.actor_id  # unchanged — the original row


def test_enqueue_never_raises_when_the_repository_call_fails():
    class _AlwaysRaisingRepository:
        def submit_job(self, **kwargs):
            raise RuntimeError("simulated repository failure")

    # Must not raise.
    enqueue_classification_job_for_evidence(
        identity.generate_id(), classification_job_repository=_AlwaysRaisingRepository(),
        actor_type=actor.SYSTEM, actor_id=ACTOR_ID,
    )


# ---------------------------------------------------------------------
# Test plan item 5 — AI succeeds -> job SUCCEEDED, exactly one
# CLASSIFICATION_REVIEW Needs You item, via the UNMODIFIED
# classify_evidence/ensure_classification_review_item path.
# ---------------------------------------------------------------------


def test_worker_processes_pending_job_ai_succeeds_creates_one_review_item(
    evidence_repository, rule_repository, classification_repository, ai_invocation_repository, litellm_client,
    object_store, audit_repository, needs_you_repository, classification_job_repository,
):
    evidence_id = _register_email_evidence(
        evidence_repository, object_store,
        content=_rfc822_email(sender="new-vendor@example.com", subject="Your invoice", body="pay up"),
        sender_address="new-vendor@example.com", subject="Your invoice",
    )
    enqueue_classification_job_for_evidence(
        evidence_id, classification_job_repository=classification_job_repository, actor_type=actor.SYSTEM, actor_id=ACTOR_ID,
    )
    _queue_proposal(litellm_client, proposed_type=DOCUMENT_TYPE_SUPPLIER_INVOICE, confidence=0.8)

    result = _run_process(
        limit=5, classification_job_repository=classification_job_repository, evidence_repository=evidence_repository,
        rule_repository=rule_repository, classification_repository=classification_repository,
        ai_invocation_repository=ai_invocation_repository, litellm_client=litellm_client, object_store=object_store,
        audit_repository=audit_repository, needs_you_repository=needs_you_repository,
    )
    assert result["claimed_count"] == 1
    [record] = result["records"]
    assert record["outcome"] == "SUCCEEDED"
    assert record["job_status"] == "SUCCEEDED"
    assert record["classify_evidence_outcome"] == "AI_PROPOSAL_REVIEW_REQUIRED"

    job = classification_job_repository.get_by_evidence(evidence_id)
    assert job.status == "SUCCEEDED"

    # `ensure_classification_review_item`'s own `source_object_reference`
    # is the `EvidenceClassification.classification_id` (its dedupe
    # anchor), not the evidence_id — filter on `metadata['evidence_id']`
    # instead, which it always carries (see
    # `services.evidence.classification_review.ensure_classification_review_item`).
    review_items = [
        item for item in needs_you_repository.list_needs_you_items(item_type="CLASSIFICATION_REVIEW")
        if item.metadata.get("evidence_id") == evidence_id
    ]
    assert len(review_items) == 1

    current = classification_repository.get_current_classification(evidence_id, CLASSIFICATION_TYPE_DOCUMENT_TYPE)
    assert current is not None
    assert current.document_type == DOCUMENT_TYPE_SUPPLIER_INVOICE


# ---------------------------------------------------------------------
# Test plan item 6 — deterministic rule match -> job SUCCEEDED, ZERO AI
# calls made.
# ---------------------------------------------------------------------


def test_worker_processes_job_deterministic_rule_match_zero_ai_calls(
    evidence_repository, rule_repository, classification_repository, ai_invocation_repository, litellm_client,
    object_store, audit_repository, needs_you_repository, classification_job_repository,
):
    rule_repository.create_rule(
        sender_scope_type="EXACT_SENDER_DOMAIN", sender_scope_value="vendor.com",
        subject_predicate_type="EXACT", subject_predicate_value="Monthly Statement",
        document_type=DOCUMENT_TYPE_SUPPLIER_INVOICE, source="OPERATOR",
    )
    evidence_id = _register_email_evidence(
        evidence_repository, object_store,
        content=_rfc822_email(sender="billing@vendor.com", subject="Monthly Statement", body="pay up"),
        sender_address="billing@vendor.com", subject="Monthly Statement",
    )
    enqueue_classification_job_for_evidence(
        evidence_id, classification_job_repository=classification_job_repository, actor_type=actor.SYSTEM, actor_id=ACTOR_ID,
    )

    result = _run_process(
        limit=5, classification_job_repository=classification_job_repository, evidence_repository=evidence_repository,
        rule_repository=rule_repository, classification_repository=classification_repository,
        ai_invocation_repository=ai_invocation_repository, litellm_client=litellm_client, object_store=object_store,
        audit_repository=audit_repository, needs_you_repository=needs_you_repository,
    )
    [record] = result["records"]
    assert record["outcome"] == "SUCCEEDED"
    assert record["classify_evidence_outcome"] == "DETERMINISTIC_CLASSIFIED"
    assert litellm_client.calls == []  # zero AI calls made


# ---------------------------------------------------------------------
# Test plan item 7 — evidence already has a current classification ->
# job SUCCEEDED (CURRENT_CLASSIFICATION_EXISTS), no duplicate row.
# ---------------------------------------------------------------------


def test_worker_processes_job_for_already_classified_evidence_succeeds_no_duplicate(
    evidence_repository, rule_repository, classification_repository, ai_invocation_repository, litellm_client,
    object_store, audit_repository, needs_you_repository, classification_job_repository,
):
    evidence_id = _register_email_evidence(
        evidence_repository, object_store,
        content=_rfc822_email(sender="a@b.com", subject="Already classified", body="body"),
    )
    # A human classified it out-of-band before the job ran.
    classification_repository.create_classification(
        evidence_id=evidence_id, classification_type=CLASSIFICATION_TYPE_DOCUMENT_TYPE,
        document_type=DOCUMENT_TYPE_SUPPLIER_INVOICE, status=STATUS_CLASSIFIED, source=SOURCE_OPERATOR_ASSIGNED,
        operator_action_id="pre-existing-operator-action",
    )
    enqueue_classification_job_for_evidence(
        evidence_id, classification_job_repository=classification_job_repository, actor_type=actor.SYSTEM, actor_id=ACTOR_ID,
    )
    before_history = classification_repository.list_classification_history(evidence_id, CLASSIFICATION_TYPE_DOCUMENT_TYPE)

    result = _run_process(
        limit=5, classification_job_repository=classification_job_repository, evidence_repository=evidence_repository,
        rule_repository=rule_repository, classification_repository=classification_repository,
        ai_invocation_repository=ai_invocation_repository, litellm_client=litellm_client, object_store=object_store,
        audit_repository=audit_repository, needs_you_repository=needs_you_repository,
    )
    [record] = result["records"]
    assert record["outcome"] == "SUCCEEDED"
    assert record["classify_evidence_outcome"] == "CURRENT_CLASSIFICATION_EXISTS"

    after_history = classification_repository.list_classification_history(evidence_id, CLASSIFICATION_TYPE_DOCUMENT_TYPE)
    assert len(after_history) == len(before_history) == 1  # no duplicate classification row
    assert litellm_client.calls == []


# ---------------------------------------------------------------------
# Test plan item 9 — genuine infrastructure exception -> retryable
# failure, reclaimable; after max_attempts -> terminal, no longer
# claimable.
# ---------------------------------------------------------------------


class _RaisingObjectStore:
    """Simulates a genuine infrastructure fault (object store down) —
    `get` always raises, mirroring PersistenceError-shaped failures."""

    def get(self, storage_reference):
        raise PersistenceError("simulated object store outage")

    def put(self, *args, **kwargs):
        raise PersistenceError("simulated object store outage")


def test_genuine_exception_marks_retryable_then_terminal_after_max_attempts(
    evidence_repository, rule_repository, classification_repository, ai_invocation_repository, litellm_client,
    audit_repository, needs_you_repository,
):
    real_object_store = InMemoryObjectStore()
    evidence_id = _register_email_evidence(
        evidence_repository, real_object_store,
        content=_rfc822_email(sender="a@b.com", subject="Will fail", body="body"),
    )
    classification_job_repository = InMemoryEvidenceClassificationJobRepository(audit_repository=audit_repository)
    job = classification_job_repository.submit_job(evidence_id=evidence_id, actor_type=actor.SYSTEM, actor_id=ACTOR_ID, max_attempts=2)

    raising_object_store = _RaisingObjectStore()

    for attempt in range(1, 3):
        result = _run_process(
            limit=5, classification_job_repository=classification_job_repository, evidence_repository=evidence_repository,
            rule_repository=rule_repository, classification_repository=classification_repository,
            ai_invocation_repository=ai_invocation_repository, litellm_client=litellm_client, object_store=raising_object_store,
            audit_repository=audit_repository, needs_you_repository=needs_you_repository,
        )
        assert result["claimed_count"] == 1
        [record] = result["records"]
        assert record["outcome"] == "RETRYABLE_FAILURE"
        expected_status = "FAILED_RETRYABLE" if attempt < 2 else "FAILED_TERMINAL"
        assert record["job_status"] == expected_status

    fetched = classification_job_repository.get_job(job.job_id)
    assert fetched.status == "FAILED_TERMINAL"
    # No longer claimable — a later claim_next_pending call must not pick it up.
    assert classification_job_repository.claim_next_pending(limit=5, claimed_by="late-worker") == []


# ---------------------------------------------------------------------
# Test plan item 11 — the worker's own --process mode never CREATES
# jobs itself, only claims and processes ones that already exist.
# ---------------------------------------------------------------------


def test_run_process_never_creates_jobs_itself(
    evidence_repository, rule_repository, classification_repository, ai_invocation_repository, litellm_client,
    object_store, audit_repository, needs_you_repository, classification_job_repository,
):
    # Real, pre-existing evidence — created before this feature shipped,
    # never enqueued (no call to enqueue_classification_job_for_evidence
    # was ever made for it).
    historical_evidence_id = _register_email_evidence(
        evidence_repository, object_store,
        content=_rfc822_email(sender="a@b.com", subject="Historical", body="body"),
    )
    assert classification_job_repository.get_by_evidence(historical_evidence_id) is None

    result = _run_process(
        limit=200, classification_job_repository=classification_job_repository, evidence_repository=evidence_repository,
        rule_repository=rule_repository, classification_repository=classification_repository,
        ai_invocation_repository=ai_invocation_repository, litellm_client=litellm_client, object_store=object_store,
        audit_repository=audit_repository, needs_you_repository=needs_you_repository,
    )
    assert result["claimed_count"] == 0

    # Still no job for the historical evidence — --process claims and
    # processes only what already exists, it never conjures a job into
    # existence for evidence nothing ever enqueued.
    assert classification_job_repository.get_by_evidence(historical_evidence_id) is None


# ---------------------------------------------------------------------
# Test plan item 12 — no automatic Xero Account Suggestion invocation
# anywhere in this delivery's own code paths (structural).
# ---------------------------------------------------------------------


def test_no_new_code_imports_the_xero_account_suggestion_producer():
    import ast
    from pathlib import Path

    repo_root = Path(__file__).resolve().parents[2]
    for relative_path in (
        "services/evidence/classification_job.py",
        "scripts/process_evidence_classification_jobs.py",
    ):
        source = (repo_root / relative_path).read_text(encoding="utf-8")
        tree = ast.parse(source, filename=relative_path)
        imported_modules = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                imported_modules.update(alias.name for alias in node.names)
            elif isinstance(node, ast.ImportFrom) and node.module:
                imported_modules.add(node.module)
        assert not any(m.startswith("services.xero") for m in imported_modules), (
            f"{relative_path} must never import services.xero.* (no automatic Xero Account Suggestion "
            f"triggering is part of this delivery) — found: {sorted(m for m in imported_modules if m.startswith('services.xero'))}"
        )
