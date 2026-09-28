"""Real, disposable-PostgreSQL concurrency proof for the suspected
`xero_account_suggestions` duplicate-row race (Architect finding,
`xero/account-suggestion-producer` WO post-fix follow-up).

The suspected race: `xero_account_suggestions.evidence_id` was created
with no uniqueness constraint (PK-only). Two concurrent callers of
`services.xero.account_suggestion.produce_account_suggestion` for the
SAME `evidence_id` could both:

1. pass `suggestion_repository.get_by_evidence(evidence_id)` (see
   `None`);
2. both find/reuse the same terminal `SUCCEEDED` `AIInvocation`
   (`ai_invocation_repository.find_active_invocation`'s own guard only
   blocks a NON-terminal invocation — it does nothing once the
   invocation has already reached a terminal state);
3. both call `suggestion_repository.create_suggestion(...)`.

Sequential/rapid-fire calls are NOT sufficient to prove or disprove
this — Python's GIL and this codebase's own fresh-`Session`-per-call
discipline make sequential calls misleadingly "safe" even when the
underlying table has no constraint. This test forces GENUINE
concurrency: two real OS threads, each holding its own SQLAlchemy
`Session`/database connection, synchronised with a `threading.Barrier`
to arrive at the `create_suggestion` INSERT at (as close to) the exact
same instant as is achievable — the actual TOCTOU window the architect
described.
"""
from __future__ import annotations

import hashlib
import threading
from datetime import datetime, timezone
from typing import Optional, Sequence

import pytest

from core import identity
from persistence.objects.memory_store import InMemoryObjectStore
from persistence.postgres.ai_invocation_repository import PostgresAIInvocationRepository
from persistence.postgres.audit_repository import PostgresAuditRepository
from persistence.postgres.entity_repository import PostgresEntityRepository
from persistence.postgres.evidence_classification_repository import PostgresEvidenceClassificationRepository
from persistence.postgres.evidence_classification_rule_repository import PostgresEvidenceClassificationRuleRepository
from persistence.postgres.evidence_repository import PostgresEvidenceRepository
from persistence.postgres.external_reference_repository import PostgresExternalReferenceRepository
from persistence.postgres.needs_you_repository import PostgresNeedsYouRepository
from persistence.postgres.session import get_engine, session_scope
from persistence.postgres.source_repository import PostgresSourceRepository
from persistence.postgres.xero_account_suggestion_models import XeroAccountSuggestionRow
from persistence.postgres.xero_account_suggestion_repository import (
    PostgresXeroAccountAssignmentRepository,
    PostgresXeroAccountSuggestionRepository,
)
from persistence.postgres.xero_repository import PostgresXeroAccountRepository, PostgresXeroConnectionRepository
from services.evidence.classification import (
    CLASSIFICATION_TYPE_DOCUMENT_TYPE,
    DOCUMENT_TYPE_SUPPLIER_INVOICE,
    SOURCE_OPERATOR_ASSIGNED,
    STATUS_CLASSIFIED,
)
from services.xero.account import RawXeroAccount
from services.xero.account_suggestion import (
    compute_account_suggestion_fingerprint,
    produce_account_suggestion,
)
from services.xero.account_suggestion_context import build_account_suggestion_context
from services.xero.eligibility import list_eligible_accounts
from ai.prompts.loader import resolve_prompt_contract_version

ACTOR_ID = "xero-suggestion-concurrency-test"


class _NeverCalledLiteLLMClient:
    """Proves both threads genuinely reused the pre-seeded SUCCEEDED
    invocation rather than each making a real (and therefore
    non-deterministically-timed) model call."""

    def complete(self, **kwargs):  # pragma: no cover - must never run
        raise AssertionError("litellm_client.complete() must never be called — both threads must reuse the "
                              "pre-seeded SUCCEEDED AIInvocation, never invoke the model directly")


class _BarrierGatedSuggestionRepository(PostgresXeroAccountSuggestionRepository):
    """Wraps the real Postgres repository, forcing every caller to
    reach `create_suggestion`'s own INSERT at (as close to) the exact
    same instant — the real TOCTOU window under test."""

    def __init__(self, barrier: threading.Barrier) -> None:
        super().__init__()
        self._barrier = barrier

    def create_suggestion(self, **kwargs):
        self._barrier.wait(timeout=10)
        return super().create_suggestion(**kwargs)


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _classification_repository() -> PostgresEvidenceClassificationRepository:
    return PostgresEvidenceClassificationRepository(
        evidence_repository=PostgresEvidenceRepository(PostgresExternalReferenceRepository()),
        rule_repository=PostgresEvidenceClassificationRuleRepository(),
        ai_invocation_repository=PostgresAIInvocationRepository(),
    )


def _setup_eligible_evidence(object_store: InMemoryObjectStore) -> tuple[str, str]:
    """Returns (entity_id, evidence_id) for one real, fully-eligible
    piece of evidence: classified SUPPLIER_INVOICE, entity resolved,
    Xero connection CONNECTED with one synced eligible account,
    reference data fresh."""
    entity = PostgresEntityRepository().register_entity(
        entity_type="COMPANY", canonical_name=f"CONCURRENCY_TEST_{identity.generate_id().replace('-', '').upper()}",
        display_name="Concurrency Test Co", status="ACTIVE",
    )
    entity_id = entity.entity_id

    content = b"Invoice #999 for cloud hosting, GBP 42."
    content_hash = {"algorithm": "SHA-256", "value": hashlib.sha256(content).hexdigest()}
    storage_reference = object_store.put(identity.generate_id(), content_hash, content)

    source = PostgresSourceRepository().register_source(
        source_type="MANUAL_UPLOAD", provider="INTERNAL", status="ACTIVE"
    )
    now = _utc_now()
    evidence = PostgresEvidenceRepository(PostgresExternalReferenceRepository()).register_evidence(
        entity_id=entity_id, evidence_type="INVOICE", source_id=source.source_id, observed_at=now,
        received_at=now, content_hash=content_hash, mime_type="text/plain", size_bytes=len(content),
        storage_reference=storage_reference,
    )
    evidence_id = evidence.evidence_id

    _classification_repository().create_classification(
        evidence_id=evidence_id, classification_type=CLASSIFICATION_TYPE_DOCUMENT_TYPE,
        document_type=DOCUMENT_TYPE_SUPPLIER_INVOICE, status=STATUS_CLASSIFIED, source=SOURCE_OPERATOR_ASSIGNED,
        operator_action_id="concurrency-test-operator-action",
    )

    connection_repo = PostgresXeroConnectionRepository()
    connection = connection_repo.begin_connect(entity_id=entity_id)
    connection = connection_repo.complete_connect(
        connection.xero_connection_id, tenant_id=f"tenant-{identity.generate_id()}",
        tenant_name="Concurrency Test Co (Xero)", token_expires_at=None,
    )
    connection = connection_repo.record_sync_success(
        connection.xero_connection_id, tenant_name="Concurrency Test Co (Xero)", token_expires_at=None,
    )
    PostgresXeroAccountRepository().upsert_account(
        entity_id=entity_id, tenant_id=connection.tenant_id,
        raw=RawXeroAccount(
            account_id="ACC-RACE-1", code="400", name="IT & Hosting", type="EXPENSE", account_class="EXPENSE",
            tax_type="NONE", status="ACTIVE", show_in_expense_claims=True,
            reporting_code=None, reporting_code_name=None, updated_date_utc=None,
        ),
        sync_run_id=identity.generate_id(),
    )
    return entity_id, evidence_id


def _expected_context_fingerprint(entity_id: str, evidence_id: str, object_store: InMemoryObjectStore) -> str:
    """Independently replicates EXACTLY the fingerprint
    `produce_account_suggestion` will itself compute for this
    evidence_id under the CURRENT (unchanged, in this test) governed
    context — so the pre-seeded invocation below is reusable by both
    threads for a genuine reason (an identical context), not merely
    because the test bypassed the fingerprint check."""
    evidence = PostgresEvidenceRepository(PostgresExternalReferenceRepository()).get_evidence(evidence_id)
    entity = PostgresEntityRepository().get_entity(entity_id)
    current = _classification_repository().get_current_classification(evidence_id, CLASSIFICATION_TYPE_DOCUMENT_TYPE)
    eligible_accounts = list_eligible_accounts(
        PostgresXeroAccountRepository().list_accounts(entity_id=entity_id)
    )
    raw_content = object_store.get(evidence.storage_reference)
    context_result = build_account_suggestion_context(
        evidence=evidence, raw_content=raw_content, classification=current,
        eligible_accounts=eligible_accounts, entity_name=entity.display_name,
    )
    context = context_result.context
    prompt_contract_version = resolve_prompt_contract_version("XERO_ACCOUNT_SUGGESTION", 1)
    content_hash = evidence.content_hash
    evidence_content_hash = content_hash.get("value") if isinstance(content_hash, dict) else str(content_hash)
    return compute_account_suggestion_fingerprint(
        task_id="XERO_ACCOUNT_SUGGESTION", task_version=1, prompt_contract_version=prompt_contract_version,
        evidence_id=evidence_id, evidence_content_hash=evidence_content_hash,
        context_contract_version=context.context_contract_version, context_sha256=context.context_sha256,
        eligible_account_ids=[a.account_id for a in eligible_accounts],
    )


def _seed_succeeded_invocation(
    entity_id: str, evidence_id: str, object_store: InMemoryObjectStore, *, account_id: str = "ACC-RACE-1",
) -> str:
    fingerprint = _expected_context_fingerprint(entity_id, evidence_id, object_store)
    repo = PostgresAIInvocationRepository()
    created = repo.create_invocation(
        task_id="XERO_ACCOUNT_SUGGESTION", task_version=1, role="BACKGROUND", provider="LITELLM",
        capability_alias="bagman-fast",
        input_references={"evidence_id": evidence_id, "context_fingerprint": fingerprint},
        actor_type="SYSTEM", actor_id=ACTOR_ID,
    )
    repo.transition_status(created.ai_invocation_id, "RUNNING")
    repo.transition_status(
        created.ai_invocation_id, "SUCCEEDED",
        output={"suggested_account_id": account_id, "confidence": 0.9, "signals": ["race test"], "warnings": []},
        confidence=0.9, provider_model="fake-model", prompt_contract_version="v1",
    )
    return created.ai_invocation_id


def _run_producer(*, entity_id: str, evidence_id: str, object_store: InMemoryObjectStore,
                   suggestion_repository, results: list, errors: list) -> None:
    try:
        result = produce_account_suggestion(
            evidence_id=evidence_id, entity_id=entity_id,
            evidence_repository=PostgresEvidenceRepository(PostgresExternalReferenceRepository()),
            classification_repository=_classification_repository(),
            entity_repository=PostgresEntityRepository(),
            xero_connection_repository=PostgresXeroConnectionRepository(),
            xero_account_repository=PostgresXeroAccountRepository(),
            suggestion_repository=suggestion_repository,
            assignment_repository=PostgresXeroAccountAssignmentRepository(),
            ai_invocation_repository=PostgresAIInvocationRepository(),
            litellm_client=_NeverCalledLiteLLMClient(),
            object_store=object_store,
            audit_repository=PostgresAuditRepository(),
            record_audit_event=PostgresAuditRepository().record_audit_event,
            needs_you_repository=PostgresNeedsYouRepository(),
            actor_type="SYSTEM", actor_id=ACTOR_ID,
        )
        results.append(result)
    except Exception as exc:  # noqa: BLE001 - captured for the assertions below, not swallowed silently
        errors.append(exc)


@pytest.mark.usefixtures("fresh_engine")
def test_two_genuinely_concurrent_producer_calls_for_the_same_evidence(fresh_engine):
    """Forces two real threads through the exact post-invocation/
    pre-suggestion-persist window at the same instant and reports what
    actually happens against real PostgreSQL — the deterministic
    reproduction the architect required."""
    object_store = InMemoryObjectStore()
    entity_id, evidence_id = _setup_eligible_evidence(object_store)
    _seed_succeeded_invocation(entity_id, evidence_id, object_store)

    barrier = threading.Barrier(2)
    shared_gated_repo = _BarrierGatedSuggestionRepository(barrier)

    results: list = []
    errors: list = []
    threads = [
        threading.Thread(
            target=_run_producer,
            kwargs=dict(
                entity_id=entity_id, evidence_id=evidence_id, object_store=object_store,
                suggestion_repository=shared_gated_repo, results=results, errors=errors,
            ),
        )
        for _ in range(2)
    ]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=15)
        assert not t.is_alive(), "a producer thread deadlocked/timed out"

    # Report exactly what the real table now holds for this evidence_id
    # — the ground truth, independent of what either thread believed
    # its own outcome was.
    with session_scope(get_engine()) as session:
        rows = (
            session.query(XeroAccountSuggestionRow)
            .filter_by(evidence_id=evidence_id)
            .all()
        )
        row_count = len(rows)

    needs_you_items = [
        item for item in PostgresNeedsYouRepository().list_needs_you_items(item_type="XERO_ACCOUNT_REQUIRED")
        if item.source_object_reference == evidence_id
    ]

    print(f"\n[CONCURRENCY REPRODUCTION] suggestion rows for evidence_id={evidence_id}: {row_count}")
    print(f"[CONCURRENCY REPRODUCTION] thread results: {[r.outcome for r in results]}")
    print(f"[CONCURRENCY REPRODUCTION] thread errors: {[type(e).__name__ for e in errors]}")
    print(f"[CONCURRENCY REPRODUCTION] NeedsYou XERO_ACCOUNT_REQUIRED items for this evidence: {len(needs_you_items)}")

    # The durable invariant this test exists to prove or disprove:
    # exactly ONE suggestion row may ever exist per evidence_id, even
    # under genuine concurrency — never two.
    assert row_count == 1, (
        f"DUPLICATE SUGGESTION ROWS: {row_count} XeroAccountSuggestion rows exist for one evidence_id "
        f"after two genuinely concurrent producer calls — the suspected race is real."
    )
    # And Needs You must never end up with two open items for the same
    # question either, regardless of which thread "won".
    assert len(needs_you_items) == 1, (
        f"DUPLICATE NEEDS YOU ITEMS: {len(needs_you_items)} XERO_ACCOUNT_REQUIRED items exist for one "
        f"evidence_id after two genuinely concurrent producer calls."
    )
