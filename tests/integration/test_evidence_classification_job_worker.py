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
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

from ai.invocation import InMemoryAIInvocationRepository
from ai.providers.litellm.client import LiteLLMOutcomeStatus
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
from services.evidence.classification_job import InMemoryEvidenceClassificationJobRepository
from services.evidence.classification_orchestrator import (
    OUTCOME_AI_IN_PROGRESS,
    OUTCOME_AI_INVOCATION_FAILED,
    OUTCOME_AI_PRIOR_FAILURE,
    OUTCOME_AI_PROPOSAL_REVIEW_REQUIRED,
    OUTCOME_CONTEXT_UNSUPPORTED,
    OUTCOME_CURRENT_CLASSIFICATION_EXISTS,
    OUTCOME_DETERMINISTIC_CLASSIFIED,
    classify_evidence,
)
from services.evidence.classification_rule import InMemoryEvidenceClassificationRuleRepository
from services.evidence.evidence import InMemoryEvidenceRepository
from services.needs_you.needs_you import InMemoryNeedsYouRepository

import scripts.process_evidence_classification_jobs as worker

ACTOR_ID = "evidence-classification-job-worker-tests"

#: This module's own tests exercise job/worker PROCESSING — never
#: discovery (`create_missing_classification_jobs`), which has its own
#: dedicated tests (see
#: tests/persistence/test_evidence_classification_job_discovery.py). A
#: boundary far enough in the past that every evidence item this module
#: creates always satisfies it, for the tests below that DO exercise
#: `_run_worker`'s own discovery dispatch.
_ALWAYS_ACTIVE_BOUNDARY = datetime(2000, 1, 1, tzinfo=timezone.utc)


def _submit_job(
    evidence_id: str, *, classification_job_repository, actor_type=actor.SYSTEM,
    actor_id=ACTOR_ID, correlation_id=None,
) -> None:
    """Thin wrapper directly submitting a job for `evidence_id` — this
    module's own tests exercise job/worker PROCESSING; evidence
    ingestion no longer triggers a classification job at all (see
    `services.evidence.classification_job`'s own module docstring,
    "Simplified design" section), so there is nothing left to wrap
    other than `submit_job` itself."""
    classification_job_repository.submit_job(
        evidence_id=evidence_id, actor_type=actor_type, actor_id=actor_id, correlation_id=correlation_id,
    )


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
def classification_job_repository(audit_repository, evidence_repository, classification_repository):
    # evidence_repository/classification_repository (evidence/
    # classification-simplification WO) back
    # `list_missing_classification_candidates`'s own in-memory
    # implementation — required by the `_run_worker` discovery tests
    # below.
    return InMemoryEvidenceClassificationJobRepository(
        audit_repository=audit_repository, evidence_repository=evidence_repository,
        classification_repository=classification_repository,
    )


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
# submit_job creates exactly one PENDING job, idempotently — this is
# the real safety net discovery (`create_missing_classification_jobs`)
# relies on; see services.evidence.classification_job's own module
# docstring.
# ---------------------------------------------------------------------


def test_submit_job_creates_exactly_one_pending_job(evidence_repository, object_store, classification_job_repository):
    evidence_id = _register_email_evidence(
        evidence_repository, object_store,
        content=_rfc822_email(sender="billing@vendor.com", subject="Invoice #1", body="pay up"),
    )
    _submit_job(
        evidence_id, classification_job_repository=classification_job_repository,
    )
    job = classification_job_repository.get_by_evidence(evidence_id)
    assert job is not None
    assert job.status == "PENDING"


def test_second_submit_job_for_the_same_evidence_is_idempotent(evidence_repository, object_store, classification_job_repository):
    evidence_id = _register_email_evidence(
        evidence_repository, object_store,
        content=_rfc822_email(sender="billing@vendor.com", subject="Invoice #2", body="pay up"),
    )
    _submit_job(
        evidence_id, classification_job_repository=classification_job_repository,
    )
    first = classification_job_repository.get_by_evidence(evidence_id)
    _submit_job(
        evidence_id, classification_job_repository=classification_job_repository,
        actor_id="a-different-caller",
    )
    second = classification_job_repository.get_by_evidence(evidence_id)
    assert second.job_id == first.job_id
    assert second.actor_id == first.actor_id  # unchanged — the original row


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
    _submit_job(
        evidence_id, classification_job_repository=classification_job_repository,
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
    assert record["classification_outcome"] == OUTCOME_AI_PROPOSAL_REVIEW_REQUIRED

    job = classification_job_repository.get_by_evidence(evidence_id)
    assert job.status == "SUCCEEDED"
    assert job.classification_outcome == OUTCOME_AI_PROPOSAL_REVIEW_REQUIRED

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
    _submit_job(
        evidence_id, classification_job_repository=classification_job_repository,
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
    assert record["classification_outcome"] == OUTCOME_DETERMINISTIC_CLASSIFIED
    assert litellm_client.calls == []  # zero AI calls made

    job = classification_job_repository.get_by_evidence(evidence_id)
    assert job.classification_outcome == OUTCOME_DETERMINISTIC_CLASSIFIED


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
    _submit_job(
        evidence_id, classification_job_repository=classification_job_repository,
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
    assert record["classification_outcome"] == OUTCOME_CURRENT_CLASSIFICATION_EXISTS

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
    # Real, pre-existing evidence with no job created for it — never
    # submitted via `submit_job`/discovery.
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
    # existence for evidence nothing ever submitted one for.
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
        # evidence/classification-simplification WO — the new discovery
        # query lives here; must remain just as isolated from Xero as
        # every other file in this delivery's own call path.
        "persistence/postgres/evidence_classification_job_repository.py",
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


# ---------------------------------------------------------------------
# Architect delta (2026-09-29 review, WO item 3) — the corrected
# outcome-to-job-status mapping. One test per outcome category from
# `scripts.process_evidence_classification_jobs`'s own
# `_COMPLETE_OUTCOMES`/`_COMPLETE_BUT_UNRESOLVED_OUTCOMES`/
# `_AI_FAILURE_OUTCOMES`/`_RETRY_DEFER_OUTCOMES` sets.
# ---------------------------------------------------------------------


def _register_document_evidence_no_storage_reference(evidence_repository) -> str:
    """CONTEXT_UNSUPPORTED's own real trigger — an EvidenceItem with NO
    stored content at all (see `classify_evidence`'s own `if not
    evidence.storage_reference:` guard, step 8 of its docstring's
    numbered sequence)."""
    now = datetime.now(timezone.utc)
    item = evidence_repository.register_evidence(
        entity_id=None, evidence_type="DOCUMENT", source_id=identity.generate_id(),
        observed_at=now, received_at=now,
        content_hash={"algorithm": "SHA-256", "value": hashlib.sha256(b"no content stored").hexdigest()},
        mime_type="application/octet-stream", size_bytes=0, storage_reference=None,
    )
    return item.evidence_id


def test_outcome_context_unsupported_maps_to_succeeded_no_real_classification(
    evidence_repository, rule_repository, classification_repository, ai_invocation_repository, litellm_client,
    audit_repository, needs_you_repository, classification_job_repository,
):
    evidence_id = _register_document_evidence_no_storage_reference(evidence_repository)
    _submit_job(
        evidence_id, classification_job_repository=classification_job_repository,
    )
    # Deliberately no object_store passed through — CONTEXT_UNSUPPORTED
    # is reached before object_store.get is ever called (no
    # storage_reference at all), so an InMemoryObjectStore with nothing
    # in it is fine here.
    result = _run_process(
        limit=5, classification_job_repository=classification_job_repository, evidence_repository=evidence_repository,
        rule_repository=rule_repository, classification_repository=classification_repository,
        ai_invocation_repository=ai_invocation_repository, litellm_client=litellm_client,
        object_store=InMemoryObjectStore(), audit_repository=audit_repository, needs_you_repository=needs_you_repository,
    )
    [record] = result["records"]
    assert record["outcome"] == "SUCCEEDED"
    assert record["classify_evidence_outcome"] == OUTCOME_CONTEXT_UNSUPPORTED
    assert record["classification_outcome"] == OUTCOME_CONTEXT_UNSUPPORTED

    job = classification_job_repository.get_by_evidence(evidence_id)
    assert job.status == "SUCCEEDED"
    assert job.classification_outcome == OUTCOME_CONTEXT_UNSUPPORTED
    # Not confusable with a genuine classification — no row was ever created.
    assert classification_repository.get_current_classification(evidence_id, CLASSIFICATION_TYPE_DOCUMENT_TYPE) is None
    assert litellm_client.calls == []


def test_outcome_ai_invocation_failed_maps_to_failed_terminal(
    evidence_repository, rule_repository, classification_repository, ai_invocation_repository, litellm_client,
    object_store, audit_repository, needs_you_repository, classification_job_repository,
):
    evidence_id = _register_email_evidence(
        evidence_repository, object_store,
        content=_rfc822_email(sender="a@b.com", subject="Will fail", body="body"),
    )
    _submit_job(
        evidence_id, classification_job_repository=classification_job_repository,
    )
    litellm_client.queue_failure(capability_alias="bagman-core", status=LiteLLMOutcomeStatus.TIMEOUT, error_detail="boom")

    result = _run_process(
        limit=5, classification_job_repository=classification_job_repository, evidence_repository=evidence_repository,
        rule_repository=rule_repository, classification_repository=classification_repository,
        ai_invocation_repository=ai_invocation_repository, litellm_client=litellm_client, object_store=object_store,
        audit_repository=audit_repository, needs_you_repository=needs_you_repository,
    )
    [record] = result["records"]
    assert record["outcome"] == "TERMINAL_FAILURE"
    assert record["job_status"] == "FAILED_TERMINAL"
    assert record["classify_evidence_outcome"] == OUTCOME_AI_INVOCATION_FAILED
    assert record["classification_outcome"] == OUTCOME_AI_INVOCATION_FAILED

    job = classification_job_repository.get_by_evidence(evidence_id)
    assert job.status == "FAILED_TERMINAL"
    assert job.classification_outcome == OUTCOME_AI_INVOCATION_FAILED
    assert job.attempt_count < job.max_attempts, (
        "FAILED_TERMINAL here comes from retryable=False (the prior-failure-guard reasoning), never from "
        "attempt_count exhaustion"
    )


def test_outcome_ai_prior_failure_maps_to_failed_terminal(
    evidence_repository, rule_repository, classification_repository, ai_invocation_repository, litellm_client,
    object_store, audit_repository, needs_you_repository, classification_job_repository,
):
    """Mirrors `tests/integration/test_classification_orchestrator.py
    ::test_prior_failed_invocation_is_not_silently_retried`'s own exact
    fixture shape for reaching AI_PRIOR_FAILURE: a first, DIRECT
    `classify_evidence(persist=True)` call (simulating an earlier,
    already-completed dispatch attempt — not going through this WO's
    own job/worker machinery at all) establishes a real FAILED
    `AIInvocation` for this evidence's exact classifier fingerprint;
    THEN a job is submitted and processed by the worker, which must
    reach AI_PRIOR_FAILURE (not attempt a second model call — the
    FakeLiteLLMClient's queue is empty by then, so a retry would raise
    an AssertionError instead of silently succeeding)."""
    evidence_id = _register_email_evidence(
        evidence_repository, object_store,
        content=_rfc822_email(sender="a@b.com", subject="Prior failure", body="body"),
    )
    litellm_client.queue_failure(capability_alias="bagman-core", status=LiteLLMOutcomeStatus.TIMEOUT)
    first = classify_evidence(
        evidence_id=evidence_id, persist=True, evidence_repository=evidence_repository,
        rule_repository=rule_repository, classification_repository=classification_repository,
        ai_invocation_repository=ai_invocation_repository, litellm_client=litellm_client, object_store=object_store,
        audit_repository=audit_repository, record_audit_event=audit_repository.record_audit_event,
        needs_you_repository=needs_you_repository, actor_type=actor.SYSTEM, actor_id="pre-existing-dispatch",
    )
    assert first.outcome == OUTCOME_AI_INVOCATION_FAILED
    assert len(litellm_client.calls) == 1

    _submit_job(
        evidence_id, classification_job_repository=classification_job_repository,
    )
    result = _run_process(
        limit=5, classification_job_repository=classification_job_repository, evidence_repository=evidence_repository,
        rule_repository=rule_repository, classification_repository=classification_repository,
        ai_invocation_repository=ai_invocation_repository, litellm_client=litellm_client, object_store=object_store,
        audit_repository=audit_repository, needs_you_repository=needs_you_repository,
    )
    [record] = result["records"]
    assert record["outcome"] == "TERMINAL_FAILURE"
    assert record["job_status"] == "FAILED_TERMINAL"
    assert record["classify_evidence_outcome"] == OUTCOME_AI_PRIOR_FAILURE
    assert record["classification_outcome"] == OUTCOME_AI_PRIOR_FAILURE
    assert len(litellm_client.calls) == 1  # no second model call was made by the worker's own attempt

    job = classification_job_repository.get_by_evidence(evidence_id)
    assert job.status == "FAILED_TERMINAL"
    assert job.classification_outcome == OUTCOME_AI_PRIOR_FAILURE


def test_outcome_ai_in_progress_maps_to_deferred_and_is_reclaimable(
    evidence_repository, rule_repository, classification_repository, ai_invocation_repository, litellm_client,
    object_store, audit_repository, needs_you_repository, classification_job_repository,
):
    """Renamed/rewritten from
    `test_outcome_ai_in_progress_maps_to_failed_retryable_and_is_reclaimable`
    (architect review, 2026-09-29, WO item 3): the destination status is
    now `DEFERRED`, not `FAILED_RETRYABLE` (see
    `services.evidence.classification_job`'s own module docstring,
    "DEFERRED" section) — a `FAILED_RETRYABLE` mapping DOES consume a
    real `attempt_count` slot even though nothing about `AI_IN_PROGRESS`
    is a genuine failed attempt; `DEFERRED`'s own `mark_deferred`
    transition compensates for `mark_in_progress`'s increment instead,
    so `attempt_count` is back to 0 (a wash), never consumed.

    Mirrors `tests/integration/test_classification_orchestrator.py
    ::test_active_invocation_guard_returns_ai_in_progress_never_launches_duplicate`'s
    own exact fixture shape: a genuinely non-terminal (freshly-created,
    still `REQUESTED`) `AIInvocation` for the same
    `(task_id, task_version, evidence_id)` subject already exists before
    the worker ever runs, simulating a concurrent in-flight call
    (another worker/reconciliation race, or a manual HTTP request)."""
    evidence_id = _register_email_evidence(
        evidence_repository, object_store,
        content=_rfc822_email(sender="a@b.com", subject="In progress", body="body"),
    )
    in_flight = ai_invocation_repository.create_invocation(
        task_id="DOCUMENT_TYPE_PROPOSAL", task_version=2, role="BACKGROUND", provider="LITELLM",
        capability_alias="bagman-core", input_references={"evidence_id": evidence_id},
        actor_type=actor.SYSTEM, actor_id="a-concurrent-caller",
    )
    _submit_job(
        evidence_id, classification_job_repository=classification_job_repository,
    )
    result = _run_process(
        limit=5, classification_job_repository=classification_job_repository, evidence_repository=evidence_repository,
        rule_repository=rule_repository, classification_repository=classification_repository,
        ai_invocation_repository=ai_invocation_repository, litellm_client=litellm_client, object_store=object_store,
        audit_repository=audit_repository, needs_you_repository=needs_you_repository,
    )
    [record] = result["records"]
    assert record["outcome"] == "DEFERRED"
    assert record["job_status"] == "DEFERRED"
    assert record["classify_evidence_outcome"] == OUTCOME_AI_IN_PROGRESS
    assert record["classification_outcome"] == OUTCOME_AI_IN_PROGRESS
    assert litellm_client.calls == []  # never a duplicate model call

    job = classification_job_repository.get_by_evidence(evidence_id)
    assert job.status == "DEFERRED"
    assert job.classification_outcome == OUTCOME_AI_IN_PROGRESS
    assert job.attempt_count == 0, "mark_in_progress's +1 and mark_deferred's -1 must be a wash"
    assert job.claimed_by is None
    assert job.claimed_at is None

    # IS reclaimable — not stuck.
    reclaimed = classification_job_repository.claim_next_pending(limit=5, claimed_by="later-worker")
    assert [j.job_id for j in reclaimed] == [job.job_id]


def test_in_memory_mark_deferred_round_trip(classification_job_repository, evidence_repository, object_store):
    """Test plan item 9: `mark_deferred` is a real, working repository
    method — round-trip proof on the `InMemoryEvidenceClassificationJobRepository`
    implementation (the Postgres implementation's own equivalent lives
    in `tests/persistence/test_evidence_classification_job_repository.py
    ::test_mark_deferred_round_trip`)."""
    evidence_id = _register_email_evidence(
        evidence_repository, object_store,
        content=_rfc822_email(sender="a@b.com", subject="Deferred round trip", body="body"),
    )
    _submit_job(
        evidence_id, classification_job_repository=classification_job_repository,
    )
    job = classification_job_repository.get_by_evidence(evidence_id)
    [claimed] = classification_job_repository.claim_next_pending(limit=1, claimed_by="worker-1")
    in_progress = classification_job_repository.mark_in_progress(claimed.job_id)
    assert in_progress.attempt_count == 1

    deferred = classification_job_repository.mark_deferred(job.job_id, classification_outcome=OUTCOME_AI_IN_PROGRESS)
    assert deferred.status == "DEFERRED"
    assert deferred.classification_outcome == OUTCOME_AI_IN_PROGRESS
    assert deferred.attempt_count == 0
    assert deferred.claimed_by is None
    assert deferred.claimed_at is None

    fetched = classification_job_repository.get_job(job.job_id)
    assert fetched.status == "DEFERRED"
    assert fetched.attempt_count == 0

    [reclaimed] = classification_job_repository.claim_next_pending(limit=5, claimed_by="worker-2")
    assert reclaimed.job_id == job.job_id


def test_no_ai_failure_outcome_is_ever_reported_as_a_job_succeeded(
    evidence_repository, rule_repository, classification_repository, ai_invocation_repository, litellm_client,
    object_store, audit_repository, needs_you_repository, classification_job_repository,
):
    """Structural proof (test plan item 7): for every real job produced
    by an outcome in `worker._AI_FAILURE_OUTCOMES`
    (`AI_INVOCATION_FAILED`/`AI_PRIOR_FAILURE`), the job's own `status`
    is NEVER `SUCCEEDED` — never a coincidence of the two scenario
    tests above, a genuine parametrized scan over both."""
    # Scenario A: AI_INVOCATION_FAILED.
    evidence_id_a = _register_email_evidence(
        evidence_repository, object_store,
        content=_rfc822_email(sender="a@b.com", subject="Fails A", body="body"),
    )
    litellm_client.queue_failure(capability_alias="bagman-core", status=LiteLLMOutcomeStatus.TIMEOUT)
    _submit_job(
        evidence_id_a, classification_job_repository=classification_job_repository,
    )

    # Scenario B: AI_PRIOR_FAILURE (a distinct evidence item, its own
    # pre-established FAILED AIInvocation).
    evidence_id_b = _register_email_evidence(
        evidence_repository, object_store,
        content=_rfc822_email(sender="a@b.com", subject="Fails B", body="body"),
    )
    litellm_client.queue_failure(capability_alias="bagman-core", status=LiteLLMOutcomeStatus.TIMEOUT)
    classify_evidence(
        evidence_id=evidence_id_b, persist=True, evidence_repository=evidence_repository,
        rule_repository=rule_repository, classification_repository=classification_repository,
        ai_invocation_repository=ai_invocation_repository, litellm_client=litellm_client, object_store=object_store,
        audit_repository=audit_repository, record_audit_event=audit_repository.record_audit_event,
        needs_you_repository=needs_you_repository, actor_type=actor.SYSTEM, actor_id="pre-existing-dispatch",
    )
    _submit_job(
        evidence_id_b, classification_job_repository=classification_job_repository,
    )

    result = _run_process(
        limit=5, classification_job_repository=classification_job_repository, evidence_repository=evidence_repository,
        rule_repository=rule_repository, classification_repository=classification_repository,
        ai_invocation_repository=ai_invocation_repository, litellm_client=litellm_client, object_store=object_store,
        audit_repository=audit_repository, needs_you_repository=needs_you_repository,
    )
    assert result["claimed_count"] == 2
    scanned = 0
    for record in result["records"]:
        if record["classification_outcome"] in worker._AI_FAILURE_OUTCOMES:
            scanned += 1
            assert record["job_status"] != "SUCCEEDED", (
                f"an AI-failure classification_outcome ({record['classification_outcome']!r}) was recorded "
                f"on a SUCCEEDED job — this must never happen: {record}"
            )
    assert scanned == 2, "both scripted AI-failure scenarios must actually have been scanned"


# ---------------------------------------------------------------------
# The activation-boundary env var: unset, malformed, and valid
# resolution, plus main()'s own extracted dispatch helper (`_run_worker`)
# never letting a skipped discovery pass block ordinary claim/process
# work.
# ---------------------------------------------------------------------


def test_resolve_activation_boundary_unset_skips_with_a_clear_reason():
    boundary, skip_reason = worker.resolve_evidence_classification_activation_boundary(None)
    assert boundary is None
    assert skip_reason is not None
    assert worker.EVIDENCE_CLASSIFICATION_ACTIVATION_BOUNDARY_ENV_VAR in skip_reason
    assert "not set" in skip_reason


def test_resolve_activation_boundary_empty_string_skips_with_a_clear_reason():
    boundary, skip_reason = worker.resolve_evidence_classification_activation_boundary("")
    assert boundary is None
    assert skip_reason is not None


def test_resolve_activation_boundary_malformed_skips_never_raises():
    boundary, skip_reason = worker.resolve_evidence_classification_activation_boundary("not-a-real-timestamp")
    assert boundary is None
    assert skip_reason is not None
    assert "not-a-real-timestamp" in skip_reason


def test_resolve_activation_boundary_naive_value_with_no_timezone_fails_closed():
    """Independent-audit finding (third re-audit round):
    `datetime.fromisoformat` happily accepts a value with no
    timezone/offset at all and silently returns a NAIVE datetime — a
    genuine silent-correctness risk against `received_at`'s own real,
    timezone-aware column. A naive value must be treated as malformed,
    never silently accepted, per this function's own "fail loudly,
    never guess" contract."""
    boundary, skip_reason = worker.resolve_evidence_classification_activation_boundary("2026-10-15T00:00:00")
    assert boundary is None
    assert skip_reason is not None
    assert "timezone" in skip_reason.lower()


def test_resolve_activation_boundary_valid_value_parses_correctly():
    boundary, skip_reason = worker.resolve_evidence_classification_activation_boundary("2026-10-15T00:00:00Z")
    assert skip_reason is None
    assert boundary == datetime(2026, 10, 15, 0, 0, 0, tzinfo=timezone.utc)


def test_resolve_activation_boundary_valid_value_without_trailing_z():
    # datetime.fromisoformat's own native "+HH:MM" form — the same
    # helper handles both, mirroring services/xero/client.py's own
    # `_parse_xero_wire_datetime` doctrine exactly.
    boundary, skip_reason = worker.resolve_evidence_classification_activation_boundary("2026-10-15T00:00:00+00:00")
    assert skip_reason is None
    assert boundary == datetime(2026, 10, 15, 0, 0, 0, tzinfo=timezone.utc)


def _fake_composition(
    *, classification_job_repository, evidence_repository, rule_repository, classification_repository,
    ai_invocation_repository, litellm_client, object_store, audit_repository, needs_you_repository,
):
    """A minimal stand-in for `app.api.composition.Composition`,
    exposing only the attributes `_run_worker` actually reads — lets
    `_run_worker`'s own dispatch logic (resolve boundary, maybe
    discover, always process) be exercised directly with real
    in-memory repositories and no env vars/database at all (see
    `scripts/process_evidence_classification_jobs.py`'s own module
    docstring, "The activation boundary is operator-set Layer-2
    configuration" section, for why this extraction exists)."""
    api = SimpleNamespace(
        evidence_repository=evidence_repository, audit_repository=audit_repository,
        record_audit_event=audit_repository.record_audit_event,
    )
    return SimpleNamespace(
        api=api,
        classification_job_repository=classification_job_repository,
        classification_repository=classification_repository,
        classification_rule_repository=rule_repository,
        ai_invocation_repository=ai_invocation_repository,
        litellm_client=litellm_client,
        object_store=object_store,
        needs_you_repository=needs_you_repository,
    )


def test_run_worker_skips_discovery_when_env_var_unset_but_still_processes_existing_jobs(
    evidence_repository, rule_repository, classification_repository, ai_invocation_repository, litellm_client,
    object_store, audit_repository, needs_you_repository, classification_job_repository,
):
    """env var unset -> `_run_worker` (main()'s own dispatch logic)
    skips discovery, reports why, but ordinary claim/process work for
    an ALREADY-submitted job proceeds completely normally."""
    # An evidence item with a job ALREADY submitted — this must still
    # be claimed/processed even though discovery itself will be
    # skipped.
    evidence_id = _register_email_evidence(
        evidence_repository, object_store,
        content=_rfc822_email(sender="new-vendor@example.com", subject="Needs processing", body="pay up"),
        sender_address="new-vendor@example.com", subject="Needs processing",
    )
    _submit_job(
        evidence_id, classification_job_repository=classification_job_repository,
    )
    _queue_proposal(litellm_client, proposed_type=DOCUMENT_TYPE_SUPPLIER_INVOICE, confidence=0.8)

    # A SEPARATE evidence item with NO job at all — proves discovery
    # genuinely did not run (it would otherwise have created one).
    orphan_evidence_id = _register_email_evidence(
        evidence_repository, object_store,
        content=_rfc822_email(sender="a@b.com", subject="Orphan", body="body"),
    )
    assert classification_job_repository.get_by_evidence(orphan_evidence_id) is None

    composition = _fake_composition(
        classification_job_repository=classification_job_repository, evidence_repository=evidence_repository,
        rule_repository=rule_repository, classification_repository=classification_repository,
        ai_invocation_repository=ai_invocation_repository, litellm_client=litellm_client, object_store=object_store,
        audit_repository=audit_repository, needs_you_repository=needs_you_repository,
    )
    args = SimpleNamespace(limit=5, discovery_limit=200)

    outcome = worker._run_worker(
        args=args, composition=composition, run_id="test-run-1", started_at=datetime.now(timezone.utc),
        worker_id="test-worker-1", env={},
    )
    report = outcome["report"]

    assert report["created_count"] == 0
    assert report["discovery_skipped_reason"] is not None
    assert worker.EVIDENCE_CLASSIFICATION_ACTIVATION_BOUNDARY_ENV_VAR in report["discovery_skipped_reason"]

    # Ordinary claim/process still happened normally.
    assert report["claimed_count"] == 1
    [record] = report["records"]
    assert record["evidence_id"] == evidence_id
    assert record["outcome"] == "SUCCEEDED"

    # And discovery genuinely did not run — the orphan is still unjobbed.
    assert classification_job_repository.get_by_evidence(orphan_evidence_id) is None


def test_run_worker_runs_discovery_with_the_exact_env_supplied_boundary(
    evidence_repository, rule_repository, classification_repository, ai_invocation_repository, litellm_client,
    object_store, audit_repository, needs_you_repository, classification_job_repository,
):
    """env var set to a valid value -> discovery genuinely runs, with
    that exact boundary (parse-and-use correctness, not merely parse
    correctness)."""
    # Historical evidence — registered (so its own server-assigned
    # created_at is stamped) BEFORE `boundary` is captured below. Its
    # own received_at is deliberately set to a time AFTER `boundary`:
    # eligibility is governed SOLELY by created_at, so this proves
    # received_at has zero bearing — a received_at-based filter would
    # have wrongly treated this as prospective.
    historical_evidence = evidence_repository.register_evidence(
        entity_id=None, evidence_type="EMAIL", source_id=identity.generate_id(),
        observed_at=datetime.now(timezone.utc), received_at=datetime.now(timezone.utc) + timedelta(days=1),
        content_hash={"algorithm": "SHA-256", "value": hashlib.sha256(b"historical").hexdigest()},
        mime_type="message/rfc822", size_bytes=10, storage_reference=None,
    )

    boundary = datetime.now(timezone.utc)

    # Prospective orphan — registered AFTER `boundary` was captured, so
    # its own created_at is >= boundary (the inclusive filter).
    prospective_id = _register_email_evidence(
        evidence_repository, object_store, content=_rfc822_email(sender="a@b.com", subject="Prospective", body="body"),
    )

    composition = _fake_composition(
        classification_job_repository=classification_job_repository, evidence_repository=evidence_repository,
        rule_repository=rule_repository, classification_repository=classification_repository,
        ai_invocation_repository=ai_invocation_repository, litellm_client=litellm_client, object_store=object_store,
        audit_repository=audit_repository, needs_you_repository=needs_you_repository,
    )
    args = SimpleNamespace(limit=5, discovery_limit=200)
    env = {worker.EVIDENCE_CLASSIFICATION_ACTIVATION_BOUNDARY_ENV_VAR: boundary.strftime("%Y-%m-%dT%H:%M:%S.%fZ")}

    outcome = worker._run_worker(
        args=args, composition=composition, run_id="test-run-2", started_at=boundary,
        worker_id="test-worker-2", env=env,
    )
    report = outcome["report"]

    assert report["discovery_skipped_reason"] is None
    assert report["created_count"] == 1
    assert classification_job_repository.get_by_evidence(prospective_id) is not None
    assert classification_job_repository.get_by_evidence(historical_evidence.evidence_id) is None
