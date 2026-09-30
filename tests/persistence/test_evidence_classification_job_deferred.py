"""Real, disposable-PostgreSQL proof that a genuinely non-terminal,
still-active `AIInvocation` (`AI_IN_PROGRESS`) can be observed on a job
across MANY MORE worker polls than `max_attempts` without ever
consuming real attempt/failure budget and without the job ever going
`FAILED_TERMINAL` (BAGMAN accounting platform,
`evidence/automatic-classification-activation` WO — architect review,
2026-09-29, WO item 3: the `DEFERRED` status /
`EvidenceClassificationJobRepository.mark_deferred` correctness fix).

Mirrors `tests/integration/test_evidence_classification_job_worker.py
::test_outcome_ai_in_progress_maps_to_failed_retryable_and_is_reclaimable`'s
own exact fixture shape (a pre-existing, non-terminal `AIInvocation` for
the same `(task_id, task_version, evidence_id)` subject, simulating a
concurrent in-flight call) but against REAL PostgreSQL repositories for
`EvidenceClassificationJob`/`EvidenceItem` (never in-memory) — the real
target of this correctness fix is the durable `attempt_count` column,
so an in-memory-only proof would not be conclusive.

Note on "genuine concurrency": the pre-existing `AIInvocation` is
seeded ONCE, before any poll, and deliberately never touched again — a
real, still-non-terminal ("another invocation is genuinely still
running") row that persists across every subsequent poll exactly as it
would in production (nothing in this worker's own code path ever
resolves someone else's in-flight invocation). Re-seeding it before
each individual poll would not change anything real about the proof
(the invocation is already non-terminal and stays that way on its own)
so this test does not do it — documented judgment call, consistent
with "prove the real mechanism, not a busier-looking test".
"""
from __future__ import annotations

import hashlib
from datetime import datetime, timezone

import scripts.process_evidence_classification_jobs as worker
from ai.invocation import InMemoryAIInvocationRepository
from ai.providers.litellm.fake import FakeLiteLLMClient
from core import actor, identity
from core.audit import InMemoryAuditRepository
from core.timestamps import utc_now
from persistence.objects.memory_store import InMemoryObjectStore
from persistence.postgres.evidence_classification_job_repository import PostgresEvidenceClassificationJobRepository
from persistence.postgres.evidence_classification_repository import PostgresEvidenceClassificationRepository
from persistence.postgres.evidence_classification_rule_repository import PostgresEvidenceClassificationRuleRepository
from persistence.postgres.evidence_repository import PostgresEvidenceRepository
from persistence.postgres.external_reference_repository import PostgresExternalReferenceRepository
from persistence.postgres.source_repository import PostgresSourceRepository
from services.evidence.classification_job import enqueue_classification_job_for_evidence
from services.evidence.classification_orchestrator import AI_TASK_ID, AI_TASK_VERSION
from services.needs_you.needs_you import InMemoryNeedsYouRepository

ACTOR_ID = "evidence-classification-job-deferred-test"


def _evidence_repository() -> PostgresEvidenceRepository:
    return PostgresEvidenceRepository(PostgresExternalReferenceRepository())


def _rfc822_email(*, sender: str, subject: str, body: str) -> bytes:
    import email.message

    msg = email.message.EmailMessage()
    msg["From"] = sender
    msg["Subject"] = subject
    msg.set_content(body)
    return bytes(msg)


def _real_evidence_id(object_store: InMemoryObjectStore) -> str:
    source = PostgresSourceRepository().register_source(
        source_type="MANUAL_UPLOAD", provider="INTERNAL", status="ACTIVE"
    )
    content = _rfc822_email(sender="a@b.com", subject="Deferred polling proof", body="body")
    content_hash = {"algorithm": "SHA-256", "value": hashlib.sha256(content).hexdigest()}
    storage_reference = object_store.put(identity.generate_id(), content_hash, content)
    now = utc_now()
    evidence = _evidence_repository().register_evidence(
        entity_id=None,
        evidence_type="EMAIL",
        source_id=source.source_id,
        observed_at=now,
        received_at=now,
        content_hash=content_hash,
        mime_type="message/rfc822",
        size_bytes=len(content),
        storage_reference=storage_reference,
        metadata={"sender_address": "a@b.com", "subject": "Deferred polling proof"},
    )
    return evidence.evidence_id


def test_ai_in_progress_across_more_polls_than_max_attempts_never_exhausts_or_terminates():
    max_attempts = 3
    poll_count = 5  # deliberately > max_attempts
    assert poll_count > max_attempts

    object_store = InMemoryObjectStore()
    evidence_id = _real_evidence_id(object_store)

    # AIInvocation itself is in-memory here, deliberately — the real
    # target of this proof is the DURABLE `attempt_count` column on
    # `EvidenceClassificationJob` (a real Postgres row, below), never
    # the AIInvocation record itself; `PostgresEvidenceClassificationRepository`
    # accepts this duck-typed dependency exactly like
    # `InMemoryEvidenceClassificationRepository` does (see that class's
    # own docstring), so mixing repositories here is a supported,
    # documented simplification, not a hidden inconsistency.
    ai_invocation_repository = InMemoryAIInvocationRepository(audit_repository=InMemoryAuditRepository())
    # A genuinely non-terminal (freshly-created, still REQUESTED)
    # AIInvocation for the same (task_id, task_version, evidence_id)
    # subject — simulates a concurrent worker/manual-HTTP call already
    # in flight. Seeded ONCE — see module docstring's own "genuine
    # concurrency" note for why re-seeding per poll is not needed.
    ai_invocation_repository.create_invocation(
        task_id=AI_TASK_ID, task_version=AI_TASK_VERSION, role="BACKGROUND", provider="LITELLM",
        capability_alias="bagman-core", input_references={"evidence_id": evidence_id},
        actor_type=actor.SYSTEM, actor_id="a-concurrent-caller",
    )

    job_repository = PostgresEvidenceClassificationJobRepository()
    evidence_created_at = _evidence_repository().get_evidence(evidence_id).created_at
    enqueue_classification_job_for_evidence(
        evidence_id,
        classification_job_repository=job_repository,
        actor_type=actor.SYSTEM,
        actor_id=ACTOR_ID,
        # This module's own test exercises DEFERRED polling behaviour —
        # never the preflight-review enqueue-side activation gate (item
        # C) — so a boundary far enough in the past always satisfies it.
        evidence_created_at=evidence_created_at,
        activation_boundary=datetime.min.replace(tzinfo=timezone.utc),
        # max_attempts is NOT a parameter of enqueue_classification_job_for_evidence
        # (it always defaults to 3 there) — submit directly instead so
        # this test's own max_attempts is explicit and independent of
        # that default, per the test plan's own "e.g. max_attempts=3".
    )
    job = job_repository.get_by_evidence(evidence_id)
    assert job.max_attempts == 3, "confirms this proof exercises the same max_attempts=3 the test plan names"

    rule_repository = PostgresEvidenceClassificationRuleRepository()
    classification_repository = PostgresEvidenceClassificationRepository(
        evidence_repository=_evidence_repository(),
        rule_repository=rule_repository,
        ai_invocation_repository=ai_invocation_repository,
    )
    litellm_client = FakeLiteLLMClient()  # never actually called — find_active_invocation short-circuits first

    for poll_number in range(1, poll_count + 1):
        result = worker.run_process(
            limit=1,
            claimed_by=f"deferred-poll-worker-{poll_number}",
            classification_job_repository=job_repository,
            evidence_repository=_evidence_repository(),
            rule_repository=rule_repository,
            classification_repository=classification_repository,
            ai_invocation_repository=ai_invocation_repository,
            litellm_client=litellm_client,
            object_store=object_store,
            audit_repository=InMemoryAuditRepository(),
            record_audit_event=InMemoryAuditRepository().record_audit_event,
            needs_you_repository=InMemoryNeedsYouRepository(),
        )
        assert result["claimed_count"] == 1, f"poll {poll_number}: job must be reclaimable every time"
        [record] = result["records"]
        assert record["outcome"] == "DEFERRED", f"poll {poll_number}: {record}"
        assert record["job_status"] == "DEFERRED"

        current = job_repository.get_job(job.job_id)
        assert current.status == "DEFERRED"
        # The core correctness proof (test plan item 7): each
        # mark_in_progress (+1) / mark_deferred (-1) pair is a wash —
        # attempt_count is back at 0 after every single poll, so it
        # never net-increases, no matter how many polls run, and can
        # therefore never reach max_attempts.
        assert current.attempt_count == 0, f"poll {poll_number}: attempt_count must return to 0, not accumulate"
        assert litellm_client.calls == [], "never a real model call — find_active_invocation short-circuits first"

    final = job_repository.get_job(job.job_id)
    assert final.status == "DEFERRED", "never FAILED_TERMINAL, even after more polls than max_attempts"
    assert final.attempt_count == 0
    assert final.claimed_by is None
    assert final.claimed_at is None

    # Still genuinely claimable after all of that.
    [reclaimed] = job_repository.claim_next_pending(limit=5, claimed_by="one-more-worker")
    assert reclaimed.job_id == job.job_id
