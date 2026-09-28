"""`xero/account-suggestion-producer` WO —
`services.xero.account_suggestion.produce_account_suggestion`
orchestration proofs, against in-memory repositories and
`ai.providers.litellm.fake.FakeLiteLLMClient` (never a real/live model
call, PID §61). Mirrors
`tests/integration/test_classification_orchestrator.py`'s own style.
"""
from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone

import pytest

from ai.invocation import InMemoryAIInvocationRepository
from ai.providers.litellm.client import LiteLLMOutcomeStatus
from ai.providers.litellm.fake import FakeLiteLLMClient
from core import actor, identity
from core.audit import InMemoryAuditRepository
from core.entity import InMemoryEntityRepository
from core.external_reference import InMemoryExternalReferenceRepository
from persistence.objects.memory_store import InMemoryObjectStore
from services.evidence.classification import (
    CLASSIFICATION_TYPE_DOCUMENT_TYPE,
    DOCUMENT_TYPE_NON_ACCOUNTING_DOCUMENT,
    DOCUMENT_TYPE_SUPPLIER_INVOICE,
    SOURCE_OPERATOR_ASSIGNED,
    STATUS_CLASSIFIED,
    InMemoryEvidenceClassificationRepository,
)
from services.evidence.classification_rule import InMemoryEvidenceClassificationRuleRepository
from services.evidence.evidence import InMemoryEvidenceRepository
from services.needs_you.needs_you import ITEM_TYPE_XERO_ACCOUNT_REQUIRED, InMemoryNeedsYouRepository
from services.xero.account import RawXeroAccount, InMemoryXeroAccountRepository
from services.xero.account_assignment import InMemoryXeroAccountAssignmentRepository
from services.xero.account_suggestion import (
    OUTCOME_AI_INVOCATION_FAILED,
    OUTCOME_CONTEXT_UNSUPPORTED,
    OUTCOME_ENTITY_NOT_RESOLVED,
    OUTCOME_NOT_ELIGIBLE_FOR_ACCOUNT_SUGGESTION,
    OUTCOME_NO_ELIGIBLE_ACCOUNTS,
    OUTCOME_REFERENCE_DATA_STALE,
    OUTCOME_SUGGESTED_ACCOUNT_REJECTED,
    OUTCOME_SUGGESTION_ALREADY_EXISTS,
    OUTCOME_SUGGESTION_PRODUCED,
    OUTCOME_XERO_NOT_CONNECTED,
    InMemoryXeroAccountSuggestionRepository,
    compute_account_suggestion_fingerprint,
    produce_account_suggestion,
)
from services.xero.account_suggestion_context import build_account_suggestion_context
from services.xero.connection import InMemoryXeroConnectionRepository
from services.xero.eligibility import list_eligible_accounts
from ai.prompts.loader import resolve_prompt_contract_version

ACTOR_ID = "xero-account-suggestion-tests"


@pytest.fixture
def entity_repository():
    return InMemoryEntityRepository()


@pytest.fixture
def evidence_repository():
    return InMemoryEvidenceRepository(InMemoryExternalReferenceRepository())


@pytest.fixture
def classification_repository(evidence_repository):
    return InMemoryEvidenceClassificationRepository(
        evidence_repository=evidence_repository,
        rule_repository=InMemoryEvidenceClassificationRuleRepository(),
        ai_invocation_repository=InMemoryAIInvocationRepository(),
    )


@pytest.fixture
def xero_connection_repository():
    return InMemoryXeroConnectionRepository()


@pytest.fixture
def xero_account_repository():
    return InMemoryXeroAccountRepository()


@pytest.fixture
def suggestion_repository():
    return InMemoryXeroAccountSuggestionRepository()


@pytest.fixture
def assignment_repository():
    return InMemoryXeroAccountAssignmentRepository()


@pytest.fixture
def ai_invocation_repository():
    return InMemoryAIInvocationRepository()


@pytest.fixture
def litellm_client():
    return FakeLiteLLMClient()


@pytest.fixture
def object_store():
    return InMemoryObjectStore()


@pytest.fixture
def audit_repository():
    return InMemoryAuditRepository()


@pytest.fixture
def needs_you_repository():
    return InMemoryNeedsYouRepository()


def _make_entity(entity_repository, **overrides) -> str:
    kwargs = dict(
        entity_type="COMPANY",
        canonical_name=f"TEST_{identity.generate_id().replace('-', '').upper()}",
        display_name="Test Company Ltd", status="ACTIVE",
    )
    kwargs.update(overrides)
    return entity_repository.register_entity(**kwargs).entity_id


def _register_text_evidence(evidence_repository, object_store, *, entity_id=None, content: bytes = b"Invoice #123 for cloud hosting services, GBP 42.00.") -> str:
    content_hash = {"algorithm": "SHA-256", "value": hashlib.sha256(content).hexdigest()}
    storage_reference = object_store.put(identity.generate_id(), content_hash, content)
    now = datetime.now(timezone.utc)
    item = evidence_repository.register_evidence(
        entity_id=entity_id, evidence_type="INVOICE", source_id=identity.generate_id(),
        observed_at=now, received_at=now, content_hash=content_hash, mime_type="text/plain",
        size_bytes=len(content), storage_reference=storage_reference,
    )
    return item.evidence_id


def _classify_supplier_invoice(classification_repository, evidence_id, *, document_type=DOCUMENT_TYPE_SUPPLIER_INVOICE, status=STATUS_CLASSIFIED):
    return classification_repository.create_classification(
        evidence_id=evidence_id, classification_type=CLASSIFICATION_TYPE_DOCUMENT_TYPE,
        document_type=document_type, status=status, source=SOURCE_OPERATOR_ASSIGNED,
        operator_action_id="test-operator-action",
    )


def _connect_xero(xero_connection_repository, entity_id, *, tenant_id="tenant-1", stale=False):
    connection = xero_connection_repository.begin_connect(entity_id=entity_id)
    connection = xero_connection_repository.complete_connect(
        connection.xero_connection_id, tenant_id=tenant_id, tenant_name="Test Co (Xero)", token_expires_at=None,
    )
    if not stale:
        connection = xero_connection_repository.record_sync_success(
            connection.xero_connection_id, tenant_name="Test Co (Xero)", token_expires_at=None,
        )
    return connection


def _add_account(xero_account_repository, *, entity_id, tenant_id, account_id="ACC-1", name="IT & Hosting", type_="EXPENSE"):
    return xero_account_repository.upsert_account(
        entity_id=entity_id, tenant_id=tenant_id,
        raw=RawXeroAccount(
            account_id=account_id, code="400", name=name, type=type_, account_class="EXPENSE",
            tax_type="NONE", status="ACTIVE", show_in_expense_claims=True,
            reporting_code=None, reporting_code_name=None, updated_date_utc=None,
        ),
        sync_run_id=identity.generate_id(),
    )


def _queue_suggestion(litellm_client, *, account_id: str, confidence: float = 0.8, signals=None, warnings=None):
    litellm_client.queue_success(
        capability_alias="bagman-fast",
        content=json.dumps(
            {
                "suggested_account_id": account_id, "confidence": confidence,
                "signals": signals or ["matches invoice content"], "warnings": warnings or [],
            }
        ),
    )


def _produce(evidence_id, entity_id, *, entity_repository, evidence_repository, classification_repository,
             xero_connection_repository, xero_account_repository, suggestion_repository, assignment_repository,
             ai_invocation_repository, litellm_client, object_store, audit_repository, needs_you_repository,
             correlation_id=None):
    return produce_account_suggestion(
        evidence_id=evidence_id, entity_id=entity_id,
        evidence_repository=evidence_repository, classification_repository=classification_repository,
        entity_repository=entity_repository, xero_connection_repository=xero_connection_repository,
        xero_account_repository=xero_account_repository, suggestion_repository=suggestion_repository,
        assignment_repository=assignment_repository, ai_invocation_repository=ai_invocation_repository,
        litellm_client=litellm_client, object_store=object_store, audit_repository=audit_repository,
        record_audit_event=audit_repository.record_audit_event, needs_you_repository=needs_you_repository,
        actor_type=actor.SYSTEM, actor_id=ACTOR_ID, correlation_id=correlation_id,
    )


def _expected_fingerprint(*, evidence_id, entity_id, evidence_repository, entity_repository,
                           classification_repository, xero_account_repository, object_store):
    """Independently replicates the EXACT fingerprint
    `produce_account_suggestion` will itself compute right now for this
    evidence_id, under whatever the CURRENT governed context is at call
    time — used to pre-seed a `SUCCEEDED` `AIInvocation` a test wants
    the orchestrator to genuinely reuse (or, with a stale value,
    genuinely refuse to reuse)."""
    evidence = evidence_repository.get_evidence(evidence_id)
    entity = entity_repository.get_entity(entity_id)
    current = classification_repository.get_current_classification(evidence_id, CLASSIFICATION_TYPE_DOCUMENT_TYPE)
    eligible_accounts = list_eligible_accounts(xero_account_repository.list_accounts(entity_id=entity_id))
    raw_content = object_store.get(evidence.storage_reference)
    context = build_account_suggestion_context(
        evidence=evidence, raw_content=raw_content, classification=current,
        eligible_accounts=eligible_accounts, entity_name=entity.display_name,
    ).context
    content_hash = evidence.content_hash
    evidence_content_hash = content_hash.get("value") if isinstance(content_hash, dict) else str(content_hash)
    return compute_account_suggestion_fingerprint(
        task_id="XERO_ACCOUNT_SUGGESTION", task_version=1,
        prompt_contract_version=resolve_prompt_contract_version("XERO_ACCOUNT_SUGGESTION", 1),
        evidence_id=evidence_id, evidence_content_hash=evidence_content_hash,
        context_contract_version=context.context_contract_version, context_sha256=context.context_sha256,
        eligible_account_ids=[a.account_id for a in eligible_accounts],
    )


def _seed_succeeded_invocation(ai_invocation_repository, *, evidence_id, context_fingerprint,
                                account_id="ACC-1", confidence=0.9):
    created = ai_invocation_repository.create_invocation(
        task_id="XERO_ACCOUNT_SUGGESTION", task_version=1, role="BACKGROUND", provider="LITELLM",
        capability_alias="bagman-fast",
        input_references={"evidence_id": evidence_id, "context_fingerprint": context_fingerprint},
        actor_type=actor.SYSTEM, actor_id=ACTOR_ID,
    )
    ai_invocation_repository.transition_status(created.ai_invocation_id, "RUNNING")
    return ai_invocation_repository.transition_status(
        created.ai_invocation_id, "SUCCEEDED",
        output={"suggested_account_id": account_id, "confidence": confidence, "signals": [], "warnings": []},
        confidence=confidence, provider_model="fake-model", prompt_contract_version="v1",
    )


def _full_setup(entity_repository, evidence_repository, classification_repository, xero_connection_repository,
                 xero_account_repository, *, object_store, account_id="ACC-1", stale=False):
    entity_id = _make_entity(entity_repository)
    evidence_id = _register_text_evidence(evidence_repository, object_store, entity_id=entity_id)
    _classify_supplier_invoice(classification_repository, evidence_id)
    connection = _connect_xero(xero_connection_repository, entity_id, stale=stale)
    _add_account(xero_account_repository, entity_id=entity_id, tenant_id=connection.tenant_id, account_id=account_id)
    return entity_id, evidence_id, connection


# ---------------------------------------------------------------------
# 1. Happy path
# ---------------------------------------------------------------------


def test_eligible_evidence_produces_suggestion_and_needs_you_item(
    entity_repository, evidence_repository, classification_repository, xero_connection_repository,
    xero_account_repository, suggestion_repository, assignment_repository, ai_invocation_repository,
    litellm_client, object_store, audit_repository, needs_you_repository,
):
    entity_id, evidence_id, connection = _full_setup(
        entity_repository, evidence_repository, classification_repository, xero_connection_repository,
        xero_account_repository, object_store=object_store,
    )
    _queue_suggestion(litellm_client, account_id="ACC-1")

    result = _produce(
        evidence_id, entity_id, entity_repository=entity_repository, evidence_repository=evidence_repository,
        classification_repository=classification_repository, xero_connection_repository=xero_connection_repository,
        xero_account_repository=xero_account_repository, suggestion_repository=suggestion_repository,
        assignment_repository=assignment_repository, ai_invocation_repository=ai_invocation_repository,
        litellm_client=litellm_client, object_store=object_store, audit_repository=audit_repository,
        needs_you_repository=needs_you_repository,
    )

    assert result.outcome == OUTCOME_SUGGESTION_PRODUCED
    assert result.suggestion is not None
    assert result.suggestion.suggested_account_id == "ACC-1"
    assert result.suggestion.status == "REVIEW_REQUIRED"
    assert result.needs_you_item_id is not None

    item = needs_you_repository.get_needs_you_item(result.needs_you_item_id)
    assert item.item_type == ITEM_TYPE_XERO_ACCOUNT_REQUIRED
    assert item.status == "OPEN"
    assert item.metadata["suggested_account_id"] == "ACC-1"


# ---------------------------------------------------------------------
# 2. Unresolved entity
# ---------------------------------------------------------------------


def test_unresolved_entity_returns_outcome_zero_ai_calls_zero_suggestions(
    entity_repository, evidence_repository, classification_repository, xero_connection_repository,
    xero_account_repository, suggestion_repository, assignment_repository, ai_invocation_repository,
    litellm_client, object_store, audit_repository, needs_you_repository,
):
    entity_id = _make_entity(entity_repository)
    evidence_id = _register_text_evidence(evidence_repository, object_store, entity_id=None)

    result = _produce(
        evidence_id, entity_id, entity_repository=entity_repository, evidence_repository=evidence_repository,
        classification_repository=classification_repository, xero_connection_repository=xero_connection_repository,
        xero_account_repository=xero_account_repository, suggestion_repository=suggestion_repository,
        assignment_repository=assignment_repository, ai_invocation_repository=ai_invocation_repository,
        litellm_client=litellm_client, object_store=object_store, audit_repository=audit_repository,
        needs_you_repository=needs_you_repository,
    )

    assert result.outcome == OUTCOME_ENTITY_NOT_RESOLVED
    assert litellm_client.calls == []
    assert suggestion_repository.get_by_evidence(evidence_id) is None


# ---------------------------------------------------------------------
# 3/4. Hallucinated / out-of-entity account id rejected
# ---------------------------------------------------------------------


def test_hallucinated_account_id_rejected_zero_suggestion_rows(
    entity_repository, evidence_repository, classification_repository, xero_connection_repository,
    xero_account_repository, suggestion_repository, assignment_repository, ai_invocation_repository,
    litellm_client, object_store, audit_repository, needs_you_repository,
):
    entity_id, evidence_id, _connection = _full_setup(
        entity_repository, evidence_repository, classification_repository, xero_connection_repository,
        xero_account_repository, object_store=object_store,
    )
    _queue_suggestion(litellm_client, account_id="TOTALLY-MADE-UP-ACCOUNT-ID")

    result = _produce(
        evidence_id, entity_id, entity_repository=entity_repository, evidence_repository=evidence_repository,
        classification_repository=classification_repository, xero_connection_repository=xero_connection_repository,
        xero_account_repository=xero_account_repository, suggestion_repository=suggestion_repository,
        assignment_repository=assignment_repository, ai_invocation_repository=ai_invocation_repository,
        litellm_client=litellm_client, object_store=object_store, audit_repository=audit_repository,
        needs_you_repository=needs_you_repository,
    )

    assert result.outcome == OUTCOME_SUGGESTED_ACCOUNT_REJECTED
    assert suggestion_repository.get_by_evidence(evidence_id) is None
    assert needs_you_repository.list_needs_you_items(item_type=ITEM_TYPE_XERO_ACCOUNT_REQUIRED) == []


def test_account_id_from_a_different_entity_is_rejected(
    entity_repository, evidence_repository, classification_repository, xero_connection_repository,
    xero_account_repository, suggestion_repository, assignment_repository, ai_invocation_repository,
    litellm_client, object_store, audit_repository, needs_you_repository,
):
    entity_a, evidence_id, _connection_a = _full_setup(
        entity_repository, evidence_repository, classification_repository, xero_connection_repository,
        xero_account_repository, object_store=object_store, account_id="ACC-A1",
    )
    entity_b = _make_entity(entity_repository)
    connection_b = _connect_xero(xero_connection_repository, entity_b, tenant_id="tenant-B")
    _add_account(xero_account_repository, entity_id=entity_b, tenant_id=connection_b.tenant_id, account_id="ACC-B1")

    # The model hallucinates entity B's own real account id.
    _queue_suggestion(litellm_client, account_id="ACC-B1")

    result = _produce(
        evidence_id, entity_a, entity_repository=entity_repository, evidence_repository=evidence_repository,
        classification_repository=classification_repository, xero_connection_repository=xero_connection_repository,
        xero_account_repository=xero_account_repository, suggestion_repository=suggestion_repository,
        assignment_repository=assignment_repository, ai_invocation_repository=ai_invocation_repository,
        litellm_client=litellm_client, object_store=object_store, audit_repository=audit_repository,
        needs_you_repository=needs_you_repository,
    )

    assert result.outcome == OUTCOME_SUGGESTED_ACCOUNT_REJECTED
    assert suggestion_repository.get_by_evidence(evidence_id) is None


# ---------------------------------------------------------------------
# 5. Malformed AI output
# ---------------------------------------------------------------------


def test_malformed_ai_output_returns_ai_invocation_failed_zero_suggestion_rows(
    entity_repository, evidence_repository, classification_repository, xero_connection_repository,
    xero_account_repository, suggestion_repository, assignment_repository, ai_invocation_repository,
    litellm_client, object_store, audit_repository, needs_you_repository,
):
    entity_id, evidence_id, _connection = _full_setup(
        entity_repository, evidence_repository, classification_repository, xero_connection_repository,
        xero_account_repository, object_store=object_store,
    )
    # Missing required `suggested_account_id` key -> schema-invalid.
    litellm_client.queue_success(
        capability_alias="bagman-fast", content=json.dumps({"confidence": 0.5, "signals": [], "warnings": []}),
    )

    result = _produce(
        evidence_id, entity_id, entity_repository=entity_repository, evidence_repository=evidence_repository,
        classification_repository=classification_repository, xero_connection_repository=xero_connection_repository,
        xero_account_repository=xero_account_repository, suggestion_repository=suggestion_repository,
        assignment_repository=assignment_repository, ai_invocation_repository=ai_invocation_repository,
        litellm_client=litellm_client, object_store=object_store, audit_repository=audit_repository,
        needs_you_repository=needs_you_repository,
    )

    assert result.outcome == OUTCOME_AI_INVOCATION_FAILED
    assert suggestion_repository.get_by_evidence(evidence_id) is None
    invocation = ai_invocation_repository.get_invocation(result.ai_invocation_id)
    assert invocation.status == "FAILED"


# ---------------------------------------------------------------------
# 6. Stale reference data
# ---------------------------------------------------------------------


def test_stale_reference_data_returns_outcome_zero_ai_calls(
    entity_repository, evidence_repository, classification_repository, xero_connection_repository,
    xero_account_repository, suggestion_repository, assignment_repository, ai_invocation_repository,
    litellm_client, object_store, audit_repository, needs_you_repository,
):
    entity_id, evidence_id, _connection = _full_setup(
        entity_repository, evidence_repository, classification_repository, xero_connection_repository,
        xero_account_repository, object_store=object_store, stale=True,
    )

    result = _produce(
        evidence_id, entity_id, entity_repository=entity_repository, evidence_repository=evidence_repository,
        classification_repository=classification_repository, xero_connection_repository=xero_connection_repository,
        xero_account_repository=xero_account_repository, suggestion_repository=suggestion_repository,
        assignment_repository=assignment_repository, ai_invocation_repository=ai_invocation_repository,
        litellm_client=litellm_client, object_store=object_store, audit_repository=audit_repository,
        needs_you_repository=needs_you_repository,
    )

    assert result.outcome == OUTCOME_REFERENCE_DATA_STALE
    assert litellm_client.calls == []


# ---------------------------------------------------------------------
# 7. Transport failure, then a later retry can succeed
# ---------------------------------------------------------------------


def test_ai_unavailable_then_a_later_retry_can_succeed(
    entity_repository, evidence_repository, classification_repository, xero_connection_repository,
    xero_account_repository, suggestion_repository, assignment_repository, ai_invocation_repository,
    litellm_client, object_store, audit_repository, needs_you_repository,
):
    entity_id, evidence_id, _connection = _full_setup(
        entity_repository, evidence_repository, classification_repository, xero_connection_repository,
        xero_account_repository, object_store=object_store,
    )
    litellm_client.queue_failure(capability_alias="bagman-fast", status=LiteLLMOutcomeStatus.TRANSPORT_ERROR)

    kwargs = dict(
        entity_repository=entity_repository, evidence_repository=evidence_repository,
        classification_repository=classification_repository, xero_connection_repository=xero_connection_repository,
        xero_account_repository=xero_account_repository, suggestion_repository=suggestion_repository,
        assignment_repository=assignment_repository, ai_invocation_repository=ai_invocation_repository,
        litellm_client=litellm_client, object_store=object_store, audit_repository=audit_repository,
        needs_you_repository=needs_you_repository,
    )

    first = _produce(evidence_id, entity_id, **kwargs)
    assert first.outcome == OUTCOME_AI_INVOCATION_FAILED
    assert suggestion_repository.get_by_evidence(evidence_id) is None

    # No partial writes after the failure.
    assert needs_you_repository.list_needs_you_items(item_type=ITEM_TYPE_XERO_ACCOUNT_REQUIRED) == []

    _queue_suggestion(litellm_client, account_id="ACC-1")
    second = _produce(evidence_id, entity_id, **kwargs)
    assert second.outcome == OUTCOME_SUGGESTION_PRODUCED
    assert second.ai_invocation_id != first.ai_invocation_id


# ---------------------------------------------------------------------
# 8. Full provenance
# ---------------------------------------------------------------------


def test_suggestion_persisted_with_full_provenance(
    entity_repository, evidence_repository, classification_repository, xero_connection_repository,
    xero_account_repository, suggestion_repository, assignment_repository, ai_invocation_repository,
    litellm_client, object_store, audit_repository, needs_you_repository,
):
    entity_id, evidence_id, connection = _full_setup(
        entity_repository, evidence_repository, classification_repository, xero_connection_repository,
        xero_account_repository, object_store=object_store,
    )
    _queue_suggestion(litellm_client, account_id="ACC-1", confidence=0.71)

    result = _produce(
        evidence_id, entity_id, entity_repository=entity_repository, evidence_repository=evidence_repository,
        classification_repository=classification_repository, xero_connection_repository=xero_connection_repository,
        xero_account_repository=xero_account_repository, suggestion_repository=suggestion_repository,
        assignment_repository=assignment_repository, ai_invocation_repository=ai_invocation_repository,
        litellm_client=litellm_client, object_store=object_store, audit_repository=audit_repository,
        needs_you_repository=needs_you_repository,
    )

    suggestion = suggestion_repository.get_by_evidence(evidence_id)
    assert suggestion.entity_id == entity_id
    assert suggestion.tenant_id == connection.tenant_id
    assert suggestion.ai_invocation_id == result.ai_invocation_id
    assert suggestion.confidence == pytest.approx(0.71)


# ---------------------------------------------------------------------
# 9. Duplicate producer invocation is idempotent
# ---------------------------------------------------------------------


def test_duplicate_producer_invocation_is_idempotent_no_second_ai_call_no_second_row(
    entity_repository, evidence_repository, classification_repository, xero_connection_repository,
    xero_account_repository, suggestion_repository, assignment_repository, ai_invocation_repository,
    litellm_client, object_store, audit_repository, needs_you_repository,
):
    entity_id, evidence_id, _connection = _full_setup(
        entity_repository, evidence_repository, classification_repository, xero_connection_repository,
        xero_account_repository, object_store=object_store,
    )
    _queue_suggestion(litellm_client, account_id="ACC-1")  # only ONE queued response

    kwargs = dict(
        entity_repository=entity_repository, evidence_repository=evidence_repository,
        classification_repository=classification_repository, xero_connection_repository=xero_connection_repository,
        xero_account_repository=xero_account_repository, suggestion_repository=suggestion_repository,
        assignment_repository=assignment_repository, ai_invocation_repository=ai_invocation_repository,
        litellm_client=litellm_client, object_store=object_store, audit_repository=audit_repository,
        needs_you_repository=needs_you_repository,
    )

    first = _produce(evidence_id, entity_id, **kwargs)
    second = _produce(evidence_id, entity_id, **kwargs)

    assert first.outcome == OUTCOME_SUGGESTION_PRODUCED
    assert second.outcome == OUTCOME_SUGGESTION_ALREADY_EXISTS
    assert second.suggestion.suggestion_id == first.suggestion.suggestion_id
    assert len(litellm_client.calls) == 1  # no second AI call


# ---------------------------------------------------------------------
# 10. Exactly one Needs You item per evidence
# ---------------------------------------------------------------------


def test_exactly_one_needs_you_item_created_per_evidence_not_per_call(
    entity_repository, evidence_repository, classification_repository, xero_connection_repository,
    xero_account_repository, suggestion_repository, assignment_repository, ai_invocation_repository,
    litellm_client, object_store, audit_repository, needs_you_repository,
):
    entity_id, evidence_id, _connection = _full_setup(
        entity_repository, evidence_repository, classification_repository, xero_connection_repository,
        xero_account_repository, object_store=object_store,
    )
    _queue_suggestion(litellm_client, account_id="ACC-1")

    kwargs = dict(
        entity_repository=entity_repository, evidence_repository=evidence_repository,
        classification_repository=classification_repository, xero_connection_repository=xero_connection_repository,
        xero_account_repository=xero_account_repository, suggestion_repository=suggestion_repository,
        assignment_repository=assignment_repository, ai_invocation_repository=ai_invocation_repository,
        litellm_client=litellm_client, object_store=object_store, audit_repository=audit_repository,
        needs_you_repository=needs_you_repository,
    )
    _produce(evidence_id, entity_id, **kwargs)
    _produce(evidence_id, entity_id, **kwargs)

    items = needs_you_repository.list_needs_you_items(item_type=ITEM_TYPE_XERO_ACCOUNT_REQUIRED)
    assert len(items) == 1


# ---------------------------------------------------------------------
# Additional eligibility-gate coverage
# ---------------------------------------------------------------------


def test_no_eligible_accounts_outcome_zero_ai_calls(
    entity_repository, evidence_repository, classification_repository, xero_connection_repository,
    xero_account_repository, suggestion_repository, assignment_repository, ai_invocation_repository,
    litellm_client, object_store, audit_repository, needs_you_repository,
):
    entity_id = _make_entity(entity_repository)
    evidence_id = _register_text_evidence(evidence_repository, object_store, entity_id=entity_id)
    _classify_supplier_invoice(classification_repository, evidence_id)
    _connect_xero(xero_connection_repository, entity_id)  # no accounts upserted at all

    result = _produce(
        evidence_id, entity_id, entity_repository=entity_repository, evidence_repository=evidence_repository,
        classification_repository=classification_repository, xero_connection_repository=xero_connection_repository,
        xero_account_repository=xero_account_repository, suggestion_repository=suggestion_repository,
        assignment_repository=assignment_repository, ai_invocation_repository=ai_invocation_repository,
        litellm_client=litellm_client, object_store=object_store, audit_repository=audit_repository,
        needs_you_repository=needs_you_repository,
    )
    assert result.outcome == OUTCOME_NO_ELIGIBLE_ACCOUNTS
    assert litellm_client.calls == []


def test_non_accounting_document_is_not_eligible(
    entity_repository, evidence_repository, classification_repository, xero_connection_repository,
    xero_account_repository, suggestion_repository, assignment_repository, ai_invocation_repository,
    litellm_client, object_store, audit_repository, needs_you_repository,
):
    entity_id = _make_entity(entity_repository)
    evidence_id = _register_text_evidence(evidence_repository, object_store, entity_id=entity_id)
    _classify_supplier_invoice(
        classification_repository, evidence_id, document_type=DOCUMENT_TYPE_NON_ACCOUNTING_DOCUMENT,
    )
    connection = _connect_xero(xero_connection_repository, entity_id)
    _add_account(xero_account_repository, entity_id=entity_id, tenant_id=connection.tenant_id)

    result = _produce(
        evidence_id, entity_id, entity_repository=entity_repository, evidence_repository=evidence_repository,
        classification_repository=classification_repository, xero_connection_repository=xero_connection_repository,
        xero_account_repository=xero_account_repository, suggestion_repository=suggestion_repository,
        assignment_repository=assignment_repository, ai_invocation_repository=ai_invocation_repository,
        litellm_client=litellm_client, object_store=object_store, audit_repository=audit_repository,
        needs_you_repository=needs_you_repository,
    )
    assert result.outcome == OUTCOME_NOT_ELIGIBLE_FOR_ACCOUNT_SUGGESTION
    assert litellm_client.calls == []


def test_no_xero_connection_returns_outcome(
    entity_repository, evidence_repository, classification_repository, xero_connection_repository,
    xero_account_repository, suggestion_repository, assignment_repository, ai_invocation_repository,
    litellm_client, object_store, audit_repository, needs_you_repository,
):
    entity_id = _make_entity(entity_repository)
    evidence_id = _register_text_evidence(evidence_repository, object_store, entity_id=entity_id)
    _classify_supplier_invoice(classification_repository, evidence_id)

    result = _produce(
        evidence_id, entity_id, entity_repository=entity_repository, evidence_repository=evidence_repository,
        classification_repository=classification_repository, xero_connection_repository=xero_connection_repository,
        xero_account_repository=xero_account_repository, suggestion_repository=suggestion_repository,
        assignment_repository=assignment_repository, ai_invocation_repository=ai_invocation_repository,
        litellm_client=litellm_client, object_store=object_store, audit_repository=audit_repository,
        needs_you_repository=needs_you_repository,
    )
    assert result.outcome == OUTCOME_XERO_NOT_CONNECTED


def test_no_stored_content_returns_context_unsupported(
    entity_repository, evidence_repository, classification_repository, xero_connection_repository,
    xero_account_repository, suggestion_repository, assignment_repository, ai_invocation_repository,
    litellm_client, object_store, audit_repository, needs_you_repository,
):
    entity_id = _make_entity(entity_repository)
    now = datetime.now(timezone.utc)
    evidence = evidence_repository.register_evidence(
        entity_id=entity_id, evidence_type="INVOICE", source_id=identity.generate_id(),
        observed_at=now, received_at=now, content_hash="ab" * 32, mime_type="text/plain", size_bytes=0,
        storage_reference=None,
    )
    _classify_supplier_invoice(classification_repository, evidence.evidence_id)
    connection = _connect_xero(xero_connection_repository, entity_id)
    _add_account(xero_account_repository, entity_id=entity_id, tenant_id=connection.tenant_id)

    result = _produce(
        evidence.evidence_id, entity_id, entity_repository=entity_repository, evidence_repository=evidence_repository,
        classification_repository=classification_repository, xero_connection_repository=xero_connection_repository,
        xero_account_repository=xero_account_repository, suggestion_repository=suggestion_repository,
        assignment_repository=assignment_repository, ai_invocation_repository=ai_invocation_repository,
        litellm_client=litellm_client, object_store=object_store, audit_repository=audit_repository,
        needs_you_repository=needs_you_repository,
    )
    assert result.outcome == OUTCOME_CONTEXT_UNSUPPORTED
    assert litellm_client.calls == []


# ---------------------------------------------------------------------
# 20. Cross-isolation — entity A never surfaces entity B's accounts
# ---------------------------------------------------------------------


def test_eligible_accounts_never_leak_across_entities(
    entity_repository, evidence_repository, classification_repository, xero_connection_repository,
    xero_account_repository, suggestion_repository, assignment_repository, ai_invocation_repository,
    litellm_client, object_store, audit_repository, needs_you_repository,
):
    entity_a, evidence_id, connection_a = _full_setup(
        entity_repository, evidence_repository, classification_repository, xero_connection_repository,
        xero_account_repository, object_store=object_store, account_id="ACC-A1",
    )
    entity_b = _make_entity(entity_repository)
    connection_b = _connect_xero(xero_connection_repository, entity_b, tenant_id="tenant-B")
    _add_account(xero_account_repository, entity_id=entity_b, tenant_id=connection_b.tenant_id, account_id="ACC-B1", name="Entity B Only Account")

    _queue_suggestion(litellm_client, account_id="ACC-A1")
    result = _produce(
        evidence_id, entity_a, entity_repository=entity_repository, evidence_repository=evidence_repository,
        classification_repository=classification_repository, xero_connection_repository=xero_connection_repository,
        xero_account_repository=xero_account_repository, suggestion_repository=suggestion_repository,
        assignment_repository=assignment_repository, ai_invocation_repository=ai_invocation_repository,
        litellm_client=litellm_client, object_store=object_store, audit_repository=audit_repository,
        needs_you_repository=needs_you_repository,
    )

    assert result.outcome == OUTCOME_SUGGESTION_PRODUCED
    assert result.suggestion.tenant_id == connection_a.tenant_id
    assert result.suggestion.tenant_id != connection_b.tenant_id
    # The context sent to the model never mentions entity B's account.
    sent_context = litellm_client.calls[0].evidence_content
    assert "ACC-B1" not in sent_context
    assert "Entity B Only Account" not in sent_context
    assert "ACC-A1" in sent_context


# ---------------------------------------------------------------------
# 21. Stale prior-SUCCEEDED-invocation reuse (post-merge architect
#     finding) — reuse must be bound to a MATCHING context_fingerprint,
#     never merely to the (task, version, evidence_id) subject.
# ---------------------------------------------------------------------


def test_crash_recovery_reuses_a_prior_succeeded_invocation_when_context_is_unchanged(
    entity_repository, evidence_repository, classification_repository, xero_connection_repository,
    xero_account_repository, suggestion_repository, assignment_repository, ai_invocation_repository,
    litellm_client, object_store, audit_repository, needs_you_repository,
):
    """The legitimate crash-recovery case must still work: a prior
    invocation that SUCCEEDED under the EXACT current context (nothing
    about classification/eligible-accounts has changed since) is reused
    — proven here by never queuing a new litellm response at all; if
    the orchestrator tried to call the model again, the FakeLiteLLMClient
    would raise on the unscripted call."""
    entity_id, evidence_id, connection = _full_setup(
        entity_repository, evidence_repository, classification_repository, xero_connection_repository,
        xero_account_repository, object_store=object_store, account_id="ACC-1",
    )
    fingerprint = _expected_fingerprint(
        evidence_id=evidence_id, entity_id=entity_id, evidence_repository=evidence_repository,
        entity_repository=entity_repository, classification_repository=classification_repository,
        xero_account_repository=xero_account_repository, object_store=object_store,
    )
    _seed_succeeded_invocation(
        ai_invocation_repository, evidence_id=evidence_id, context_fingerprint=fingerprint, account_id="ACC-1",
    )

    result = _produce(
        evidence_id, entity_id, entity_repository=entity_repository, evidence_repository=evidence_repository,
        classification_repository=classification_repository, xero_connection_repository=xero_connection_repository,
        xero_account_repository=xero_account_repository, suggestion_repository=suggestion_repository,
        assignment_repository=assignment_repository, ai_invocation_repository=ai_invocation_repository,
        litellm_client=litellm_client, object_store=object_store, audit_repository=audit_repository,
        needs_you_repository=needs_you_repository,
    )

    assert result.outcome == OUTCOME_SUGGESTION_PRODUCED
    assert result.suggestion.suggested_account_id == "ACC-1"
    assert litellm_client.calls == []  # reused, never called the model


def test_stale_prior_succeeded_invocation_is_not_reused_after_eligible_accounts_change(
    entity_repository, evidence_repository, classification_repository, xero_connection_repository,
    xero_account_repository, suggestion_repository, assignment_repository, ai_invocation_repository,
    litellm_client, object_store, audit_repository, needs_you_repository,
):
    """A prior SUCCEEDED invocation computed BEFORE the eligible-account
    set changed (a new account added) must NOT be reused — its own
    `context_fingerprint` no longer matches. The orchestrator must fall
    through to a genuinely fresh model call reflecting the current
    account universe, never silently trust the stale one."""
    entity_id, evidence_id, connection = _full_setup(
        entity_repository, evidence_repository, classification_repository, xero_connection_repository,
        xero_account_repository, object_store=object_store, account_id="ACC-1",
    )
    stale_fingerprint = _expected_fingerprint(
        evidence_id=evidence_id, entity_id=entity_id, evidence_repository=evidence_repository,
        entity_repository=entity_repository, classification_repository=classification_repository,
        xero_account_repository=xero_account_repository, object_store=object_store,
    )
    _seed_succeeded_invocation(
        ai_invocation_repository, evidence_id=evidence_id, context_fingerprint=stale_fingerprint,
        account_id="ACC-1",
    )

    # The governed context changes: a new eligible account is synced.
    _add_account(xero_account_repository, entity_id=entity_id, tenant_id=connection.tenant_id, account_id="ACC-2", name="New Account")

    # No response is queued for the STALE fingerprint's own account
    # (ACC-1) — the fresh call must reflect the NEW context and propose
    # the newly-added account instead, proving a genuinely new
    # invocation ran rather than the stale one being trusted.
    _queue_suggestion(litellm_client, account_id="ACC-2")

    result = _produce(
        evidence_id, entity_id, entity_repository=entity_repository, evidence_repository=evidence_repository,
        classification_repository=classification_repository, xero_connection_repository=xero_connection_repository,
        xero_account_repository=xero_account_repository, suggestion_repository=suggestion_repository,
        assignment_repository=assignment_repository, ai_invocation_repository=ai_invocation_repository,
        litellm_client=litellm_client, object_store=object_store, audit_repository=audit_repository,
        needs_you_repository=needs_you_repository,
    )

    assert result.outcome == OUTCOME_SUGGESTION_PRODUCED
    assert result.suggestion.suggested_account_id == "ACC-2"
    assert len(litellm_client.calls) == 1  # a genuinely new call was made, the stale invocation was not reused
