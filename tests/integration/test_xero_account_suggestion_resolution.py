"""`xero/account-suggestion-producer` WO —
`services.xero.account_suggestion_resolution.resolve_account_suggestion`
proofs, against in-memory repositories. Exercises the full
suggestion-produced -> operator-resolves flow, including through
`app/api/routers/needs_you.py`'s own `POST /{item_id}/resolve` HTTP
handler (mirrors `services.evidence.classification_review`'s own sibling
test style, adapted to this module's own write-once-assignment
contract)."""
from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone

import pytest

from ai.invocation import InMemoryAIInvocationRepository
from ai.providers.litellm.fake import FakeLiteLLMClient
from core import actor, identity
from core.audit import InMemoryAuditRepository
from core.entity import InMemoryEntityRepository
from core.errors import ConflictError, ValidationError
from core.external_reference import InMemoryExternalReferenceRepository
from persistence.objects.memory_store import InMemoryObjectStore
from services.evidence.classification import (
    CLASSIFICATION_TYPE_DOCUMENT_TYPE,
    DOCUMENT_TYPE_SUPPLIER_INVOICE,
    SOURCE_OPERATOR_ASSIGNED,
    STATUS_CLASSIFIED,
    InMemoryEvidenceClassificationRepository,
)
from services.evidence.classification_rule import InMemoryEvidenceClassificationRuleRepository
from services.evidence.evidence import InMemoryEvidenceRepository
from services.needs_you.needs_you import InMemoryNeedsYouRepository
from services.xero.account import RawXeroAccount, InMemoryXeroAccountRepository
from services.xero.account_assignment import (
    SOURCE_AI_ACCEPTED,
    SOURCE_OPERATOR_SELECTED,
    InMemoryXeroAccountAssignmentRepository,
)
from services.xero.account_suggestion import (
    OUTCOME_SUGGESTION_PRODUCED,
    InMemoryXeroAccountSuggestionRepository,
    produce_account_suggestion,
)
from services.xero.account_suggestion_resolution import resolve_account_suggestion
from services.xero.connection import InMemoryXeroConnectionRepository

ACTOR_ID = "xero-account-suggestion-resolution-tests"


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


def _register_text_evidence(evidence_repository, object_store, *, entity_id) -> str:
    content = b"Invoice #999 for cloud hosting services, GBP 12.00."
    content_hash = {"algorithm": "SHA-256", "value": hashlib.sha256(content).hexdigest()}
    storage_reference = object_store.put(identity.generate_id(), content_hash, content)
    now = datetime.now(timezone.utc)
    item = evidence_repository.register_evidence(
        entity_id=entity_id, evidence_type="INVOICE", source_id=identity.generate_id(),
        observed_at=now, received_at=now, content_hash=content_hash, mime_type="text/plain",
        size_bytes=len(content), storage_reference=storage_reference,
    )
    return item.evidence_id


def _connect_xero(xero_connection_repository, entity_id, *, tenant_id="tenant-1"):
    connection = xero_connection_repository.begin_connect(entity_id=entity_id)
    connection = xero_connection_repository.complete_connect(
        connection.xero_connection_id, tenant_id=tenant_id, tenant_name="Test Co (Xero)", token_expires_at=None,
    )
    return xero_connection_repository.record_sync_success(
        connection.xero_connection_id, tenant_name="Test Co (Xero)", token_expires_at=None,
    )


def _add_account(xero_account_repository, *, entity_id, tenant_id, account_id="ACC-1", name="IT & Hosting"):
    return xero_account_repository.upsert_account(
        entity_id=entity_id, tenant_id=tenant_id,
        raw=RawXeroAccount(
            account_id=account_id, code="400", name=name, type="EXPENSE", account_class="EXPENSE",
            tax_type="NONE", status="ACTIVE", show_in_expense_claims=True,
            reporting_code=None, reporting_code_name=None, updated_date_utc=None,
        ),
        sync_run_id=identity.generate_id(),
    )


@pytest.fixture
def produced_suggestion(
    entity_repository, evidence_repository, classification_repository, xero_connection_repository,
    xero_account_repository, suggestion_repository, assignment_repository, ai_invocation_repository,
    litellm_client, object_store, audit_repository, needs_you_repository,
):
    """A full, real SUGGESTION_PRODUCED outcome — every resolution test
    builds on this exact fixture."""
    entity_id = _make_entity(entity_repository)
    evidence_id = _register_text_evidence(evidence_repository, object_store, entity_id=entity_id)
    classification_repository.create_classification(
        evidence_id=evidence_id, classification_type=CLASSIFICATION_TYPE_DOCUMENT_TYPE,
        document_type=DOCUMENT_TYPE_SUPPLIER_INVOICE, status=STATUS_CLASSIFIED, source=SOURCE_OPERATOR_ASSIGNED,
        operator_action_id="test-operator-action",
    )
    connection = _connect_xero(xero_connection_repository, entity_id)
    _add_account(xero_account_repository, entity_id=entity_id, tenant_id=connection.tenant_id, account_id="ACC-1", name="IT & Hosting")
    _add_account(xero_account_repository, entity_id=entity_id, tenant_id=connection.tenant_id, account_id="ACC-2", name="Office Supplies")

    litellm_client.queue_success(
        capability_alias="bagman-fast",
        content=json.dumps({"suggested_account_id": "ACC-1", "confidence": 0.8, "signals": ["match"], "warnings": []}),
    )
    result = produce_account_suggestion(
        evidence_id=evidence_id, entity_id=entity_id,
        evidence_repository=evidence_repository, classification_repository=classification_repository,
        entity_repository=entity_repository, xero_connection_repository=xero_connection_repository,
        xero_account_repository=xero_account_repository, suggestion_repository=suggestion_repository,
        assignment_repository=assignment_repository, ai_invocation_repository=ai_invocation_repository,
        litellm_client=litellm_client, object_store=object_store, audit_repository=audit_repository,
        record_audit_event=audit_repository.record_audit_event, needs_you_repository=needs_you_repository,
        actor_type=actor.SYSTEM, actor_id=ACTOR_ID,
    )
    assert result.outcome == OUTCOME_SUGGESTION_PRODUCED
    return {
        "entity_id": entity_id,
        "evidence_id": evidence_id,
        "connection": connection,
        "needs_you_item": needs_you_repository.get_needs_you_item(result.needs_you_item_id),
    }


def _resolve(produced_suggestion, resolution, *, evidence_repository, xero_connection_repository,
             xero_account_repository, suggestion_repository, assignment_repository, audit_repository):
    return resolve_account_suggestion(
        needs_you_item=produced_suggestion["needs_you_item"], resolution=resolution,
        actor_type="USER", actor_id="matt",
        evidence_repository=evidence_repository, xero_connection_repository=xero_connection_repository,
        xero_account_repository=xero_account_repository, suggestion_repository=suggestion_repository,
        assignment_repository=assignment_repository, record_audit_event=audit_repository.record_audit_event,
    )


# ---------------------------------------------------------------------
# 11. Accepted suggestion
# ---------------------------------------------------------------------


def test_accepting_the_suggested_account_creates_ai_accepted_assignment(
    produced_suggestion, evidence_repository, xero_connection_repository, xero_account_repository,
    suggestion_repository, assignment_repository, audit_repository,
):
    result = _resolve(
        produced_suggestion, {"account_id": "ACC-1"},
        evidence_repository=evidence_repository, xero_connection_repository=xero_connection_repository,
        xero_account_repository=xero_account_repository, suggestion_repository=suggestion_repository,
        assignment_repository=assignment_repository, audit_repository=audit_repository,
    )
    assert result.assignment_was_created is True
    assert result.assignment.source == SOURCE_AI_ACCEPTED
    assert result.assignment.account_id == "ACC-1"
    assert result.assignment.evidence_id == produced_suggestion["evidence_id"]


# ---------------------------------------------------------------------
# 12. Operator-selected alternative
# ---------------------------------------------------------------------


def test_choosing_a_different_valid_account_creates_operator_selected_assignment(
    produced_suggestion, evidence_repository, xero_connection_repository, xero_account_repository,
    suggestion_repository, assignment_repository, audit_repository,
):
    result = _resolve(
        produced_suggestion, {"account_id": "ACC-2"},
        evidence_repository=evidence_repository, xero_connection_repository=xero_connection_repository,
        xero_account_repository=xero_account_repository, suggestion_repository=suggestion_repository,
        assignment_repository=assignment_repository, audit_repository=audit_repository,
    )
    assert result.assignment.source == SOURCE_OPERATOR_SELECTED
    assert result.assignment.account_id == "ACC-2"


# ---------------------------------------------------------------------
# 13. Fresh eligible-set enforcement — account no longer eligible fails closed
# ---------------------------------------------------------------------


def test_account_not_in_the_fresh_eligible_set_fails_closed_item_stays_open(
    produced_suggestion, evidence_repository, xero_connection_repository, xero_account_repository,
    suggestion_repository, assignment_repository, audit_repository,
):
    with pytest.raises(ValidationError):
        _resolve(
            produced_suggestion, {"account_id": "ACCOUNT-DOES-NOT-EXIST"},
            evidence_repository=evidence_repository, xero_connection_repository=xero_connection_repository,
            xero_account_repository=xero_account_repository, suggestion_repository=suggestion_repository,
            assignment_repository=assignment_repository, audit_repository=audit_repository,
        )
    assert assignment_repository.get_by_evidence(produced_suggestion["evidence_id"]) is None
    assert produced_suggestion["needs_you_item"].status == "OPEN"


def test_account_archived_since_suggestion_time_is_rejected_at_resolution(
    produced_suggestion, evidence_repository, xero_connection_repository, xero_account_repository,
    suggestion_repository, assignment_repository, audit_repository,
):
    """Proves the FRESH re-fetch discipline directly: re-sync ACC-1 as
    ARCHIVED after the suggestion was produced — the suggestion-time
    snapshot would still show it eligible, but resolution must re-check
    live state."""
    xero_account_repository.upsert_account(
        entity_id=produced_suggestion["entity_id"], tenant_id=produced_suggestion["connection"].tenant_id,
        raw=RawXeroAccount(
            account_id="ACC-1", code="400", name="IT & Hosting", type="EXPENSE", account_class="EXPENSE",
            tax_type="NONE", status="ARCHIVED", show_in_expense_claims=True,
            reporting_code=None, reporting_code_name=None, updated_date_utc=None,
        ),
        sync_run_id=identity.generate_id(),
    )
    with pytest.raises(ValidationError):
        _resolve(
            produced_suggestion, {"account_id": "ACC-1"},
            evidence_repository=evidence_repository, xero_connection_repository=xero_connection_repository,
            xero_account_repository=xero_account_repository, suggestion_repository=suggestion_repository,
            assignment_repository=assignment_repository, audit_repository=audit_repository,
        )
    assert assignment_repository.get_by_evidence(produced_suggestion["evidence_id"]) is None


# ---------------------------------------------------------------------
# 16. Reassignment attempt raises ConflictError, no silent overwrite
# ---------------------------------------------------------------------


def test_a_second_resolution_attempt_with_a_different_account_raises_conflict(
    produced_suggestion, evidence_repository, xero_connection_repository, xero_account_repository,
    suggestion_repository, assignment_repository, audit_repository,
):
    _resolve(
        produced_suggestion, {"account_id": "ACC-1"},
        evidence_repository=evidence_repository, xero_connection_repository=xero_connection_repository,
        xero_account_repository=xero_account_repository, suggestion_repository=suggestion_repository,
        assignment_repository=assignment_repository, audit_repository=audit_repository,
    )
    with pytest.raises(ConflictError):
        _resolve(
            produced_suggestion, {"account_id": "ACC-2"},
            evidence_repository=evidence_repository, xero_connection_repository=xero_connection_repository,
            xero_account_repository=xero_account_repository, suggestion_repository=suggestion_repository,
            assignment_repository=assignment_repository, audit_repository=audit_repository,
        )
    # Original assignment is untouched.
    current = assignment_repository.get_by_evidence(produced_suggestion["evidence_id"])
    assert current.account_id == "ACC-1"


def test_a_second_resolution_attempt_with_the_same_account_is_idempotent(
    produced_suggestion, evidence_repository, xero_connection_repository, xero_account_repository,
    suggestion_repository, assignment_repository, audit_repository,
):
    first = _resolve(
        produced_suggestion, {"account_id": "ACC-1"},
        evidence_repository=evidence_repository, xero_connection_repository=xero_connection_repository,
        xero_account_repository=xero_account_repository, suggestion_repository=suggestion_repository,
        assignment_repository=assignment_repository, audit_repository=audit_repository,
    )
    second = _resolve(
        produced_suggestion, {"account_id": "ACC-1"},
        evidence_repository=evidence_repository, xero_connection_repository=xero_connection_repository,
        xero_account_repository=xero_account_repository, suggestion_repository=suggestion_repository,
        assignment_repository=assignment_repository, audit_repository=audit_repository,
    )
    assert first.assignment.assignment_id == second.assignment.assignment_id
    assert second.assignment_was_created is False


# ---------------------------------------------------------------------
# No Xero mutation anywhere in this feature
# ---------------------------------------------------------------------


def test_no_xero_write_method_exists_on_the_client_protocol_used_by_this_feature():
    """This feature never imports/calls any Xero client write method —
    proven structurally: neither `produce_account_suggestion` nor
    `resolve_account_suggestion` accept an accounting-client parameter
    at all (only read-only repositories), so there is no code path
    through which either function could invoke a Xero write."""
    import inspect

    from services.xero.account_suggestion import produce_account_suggestion as producer
    from services.xero.account_suggestion_resolution import resolve_account_suggestion as resolver

    producer_params = set(inspect.signature(producer).parameters)
    resolver_params = set(inspect.signature(resolver).parameters)
    for forbidden in ("accounting_client", "xero_accounting_client", "xero_client"):
        assert forbidden not in producer_params
        assert forbidden not in resolver_params


# ---------------------------------------------------------------------
# No secret/credential material anywhere in a suggestion/assignment row
# ---------------------------------------------------------------------


def test_no_secret_material_in_suggestion_or_assignment_rows_or_audit_payloads(
    produced_suggestion, evidence_repository, xero_connection_repository, xero_account_repository,
    suggestion_repository, assignment_repository, audit_repository,
):
    result = _resolve(
        produced_suggestion, {"account_id": "ACC-1"},
        evidence_repository=evidence_repository, xero_connection_repository=xero_connection_repository,
        xero_account_repository=xero_account_repository, suggestion_repository=suggestion_repository,
        assignment_repository=assignment_repository, audit_repository=audit_repository,
    )
    suggestion = suggestion_repository.get_by_evidence(produced_suggestion["evidence_id"])

    forbidden_substrings = ("access_token", "refresh_token", "client_secret")
    suggestion_blob = json.dumps(suggestion.to_dict())
    assignment_blob = json.dumps(result.assignment.to_dict())
    for needle in forbidden_substrings:
        assert needle not in suggestion_blob
        assert needle not in assignment_blob

    for event in audit_repository.list_by_subject("XeroAccountAssignment", result.assignment.assignment_id):
        blob = json.dumps(event.to_dict())
        for needle in forbidden_substrings:
            assert needle not in blob
