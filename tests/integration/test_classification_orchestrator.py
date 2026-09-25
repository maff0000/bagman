"""CD-6 Slice 5 WI-3 §47/§48 —
`services.evidence.classification_orchestrator.classify_evidence`
orchestration and safety proofs, against in-memory repositories and
`ai.providers.litellm.fake.FakeLiteLLMClient` (never a real/live model
call — PID §61)."""
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
from core.errors import NotFoundError
from core.external_reference import InMemoryExternalReferenceRepository
from persistence.objects.memory_store import InMemoryObjectStore
from services.evidence.classification import (
    STATUS_REVIEW_REQUIRED,
    STATUS_UNCLASSIFIABLE,
    InMemoryEvidenceClassificationRepository,
)
from services.evidence.classification_orchestrator import (
    OUTCOME_AI_IN_PROGRESS,
    OUTCOME_AI_INVOCATION_FAILED,
    OUTCOME_AI_PRIOR_FAILURE,
    OUTCOME_AI_PROPOSAL_REVIEW_REQUIRED,
    OUTCOME_AI_PROPOSAL_UNCLASSIFIABLE,
    OUTCOME_CONTEXT_UNSUPPORTED,
    OUTCOME_CURRENT_CLASSIFICATION_EXISTS,
    OUTCOME_DETERMINISTIC_CLASSIFIED,
    OUTCOME_DETERMINISTIC_CONFLICT,
    classify_evidence,
)
from services.evidence.classification_rule import InMemoryEvidenceClassificationRuleRepository
from services.evidence.evidence import InMemoryEvidenceRepository
from services.needs_you.needs_you import InMemoryNeedsYouRepository

ACTOR_ID = "wi3-orchestrator-tests"


@pytest.fixture
def evidence_repository():
    return InMemoryEvidenceRepository(InMemoryExternalReferenceRepository())


@pytest.fixture
def rule_repository():
    return InMemoryEvidenceClassificationRuleRepository()


@pytest.fixture
def ai_invocation_repository():
    return InMemoryAIInvocationRepository()


@pytest.fixture
def classification_repository(evidence_repository, rule_repository, ai_invocation_repository):
    return InMemoryEvidenceClassificationRepository(
        evidence_repository=evidence_repository, rule_repository=rule_repository,
        ai_invocation_repository=ai_invocation_repository,
    )


@pytest.fixture
def audit_repository():
    return InMemoryAuditRepository()


@pytest.fixture
def object_store():
    return InMemoryObjectStore()


@pytest.fixture
def litellm_client():
    return FakeLiteLLMClient()


@pytest.fixture
def needs_you_repository():
    return InMemoryNeedsYouRepository()


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


def _classify(evidence_id, *, persist, evidence_repository, rule_repository, classification_repository,
              ai_invocation_repository, litellm_client, object_store, audit_repository, needs_you_repository,
              correlation_id=None):
    return classify_evidence(
        evidence_id=evidence_id, persist=persist,
        evidence_repository=evidence_repository, rule_repository=rule_repository,
        classification_repository=classification_repository, ai_invocation_repository=ai_invocation_repository,
        litellm_client=litellm_client, object_store=object_store, audit_repository=audit_repository,
        needs_you_repository=needs_you_repository,
        record_audit_event=audit_repository.record_audit_event,
        actor_type=actor.SYSTEM, actor_id=ACTOR_ID, correlation_id=correlation_id,
    )


def _queue_proposal(litellm_client, *, proposed_type: str, confidence: float = 0.8, signals=None, warnings=None):
    litellm_client.queue_success(
        capability_alias="bagman-core",
        content=json.dumps(
            {
                "proposed_type": proposed_type, "confidence": confidence,
                "signals": signals or [], "warnings": warnings or [],
            }
        ),
    )


# ---------------------------------------------------------------------
# Deterministic match -> AI never called (§47)
# ---------------------------------------------------------------------


def test_deterministic_match_short_circuits_ai_never_called(
    evidence_repository, rule_repository, classification_repository, ai_invocation_repository,
    litellm_client, object_store, audit_repository, needs_you_repository,
):
    rule_repository.create_rule(
        sender_scope_type="EXACT_SENDER_DOMAIN", sender_scope_value="vendor.com",
        subject_predicate_type="EXACT", subject_predicate_value="Monthly Statement",
        document_type="SUPPLIER_INVOICE", source="OPERATOR",
    )
    evidence_id = _register_email_evidence(
        evidence_repository, object_store,
        content=_rfc822_email(sender="billing@vendor.com", subject="Monthly Statement", body="pay up"),
        sender_address="billing@vendor.com", subject="Monthly Statement",
    )
    result = _classify(
        evidence_id, persist=True, evidence_repository=evidence_repository, rule_repository=rule_repository,
        classification_repository=classification_repository, ai_invocation_repository=ai_invocation_repository,
        litellm_client=litellm_client, object_store=object_store, audit_repository=audit_repository,
        needs_you_repository=needs_you_repository,
    )
    assert result.outcome == OUTCOME_DETERMINISTIC_CLASSIFIED
    assert result.classification.source == "DETERMINISTIC_RULE"
    assert litellm_client.calls == []


def test_deterministic_conflict_stops_ai_never_called(
    evidence_repository, rule_repository, classification_repository, ai_invocation_repository,
    litellm_client, object_store, audit_repository, needs_you_repository,
):
    # CONFLICT is structurally unreachable through the ordinary,
    # validated `create_rule` write path (see
    # `services.evidence.classification_matcher`'s own "CONFLICT
    # reachability" module-docstring note) — mirror
    # `tests/integration/test_classification_service.py::test_conflict_writes_nothing`'s
    # own technique: insert two identically-tied ACTIVE rows directly.
    from datetime import datetime, timezone as _tz

    from services.evidence.classification_rule import EvidenceClassificationRule

    now = datetime.now(_tz.utc)
    for rule_id, document_type in (("tie-a", "SUPPLIER_INVOICE"), ("tie-b", "RECEIPT")):
        rule = EvidenceClassificationRule(
            rule_id=rule_id, sender_scope_type="EXACT_SENDER_DOMAIN", sender_scope_value="vendor.com",
            subject_predicate_type="EXACT", subject_predicate_value="monthly statement",
            document_type=document_type, status="ACTIVE", source="OPERATOR", created_at=now, approved_at=now,
        )
        rule_repository._by_id[rule_id] = rule  # noqa: SLF001 - deliberate corruption-scenario setup

    evidence_id = _register_email_evidence(
        evidence_repository, object_store,
        content=_rfc822_email(sender="billing@vendor.com", subject="Monthly Statement", body="pay up"),
        sender_address="billing@vendor.com", subject="Monthly Statement",
    )
    result = _classify(
        evidence_id, persist=True, evidence_repository=evidence_repository, rule_repository=rule_repository,
        classification_repository=classification_repository, ai_invocation_repository=ai_invocation_repository,
        litellm_client=litellm_client, object_store=object_store, audit_repository=audit_repository,
        needs_you_repository=needs_you_repository,
    )
    assert result.outcome == OUTCOME_DETERMINISTIC_CONFLICT
    assert len(result.conflicting_rule_ids) == 2
    assert litellm_client.calls == []
    assert classification_repository.get_current_classification(evidence_id, "DOCUMENT_TYPE") is None


def test_existing_current_classification_stops_ai_never_called(
    evidence_repository, rule_repository, classification_repository, ai_invocation_repository,
    litellm_client, object_store, audit_repository, needs_you_repository,
):
    evidence_id = _register_email_evidence(
        evidence_repository, object_store,
        content=_rfc822_email(sender="a@b.com", subject="s", body="body"),
    )
    classification_repository.create_classification(
        evidence_id=evidence_id, classification_type="DOCUMENT_TYPE", document_type="RECEIPT",
        status="CLASSIFIED", source="OPERATOR_ASSIGNED", operator_action_id="op-1",
        expected_current_classification_id=None,
    )
    result = _classify(
        evidence_id, persist=True, evidence_repository=evidence_repository, rule_repository=rule_repository,
        classification_repository=classification_repository, ai_invocation_repository=ai_invocation_repository,
        litellm_client=litellm_client, object_store=object_store, audit_repository=audit_repository,
        needs_you_repository=needs_you_repository,
    )
    assert result.outcome == OUTCOME_CURRENT_CLASSIFICATION_EXISTS
    assert result.existing_source == "OPERATOR_ASSIGNED"
    assert litellm_client.calls == []


# ---------------------------------------------------------------------
# No deterministic match -> AI IS called
# ---------------------------------------------------------------------


def test_no_deterministic_match_calls_ai_v2(
    evidence_repository, rule_repository, classification_repository, ai_invocation_repository,
    litellm_client, object_store, audit_repository, needs_you_repository,
):
    evidence_id = _register_email_evidence(
        evidence_repository, object_store,
        content=_rfc822_email(sender="billing@vendor.com", subject="Invoice 42", body="Please pay $500."),
        sender_address="billing@vendor.com", subject="Invoice 42",
    )
    _queue_proposal(litellm_client, proposed_type="SUPPLIER_INVOICE", confidence=0.9, signals=["requests payment"])
    result = _classify(
        evidence_id, persist=False, evidence_repository=evidence_repository, rule_repository=rule_repository,
        classification_repository=classification_repository, ai_invocation_repository=ai_invocation_repository,
        litellm_client=litellm_client, object_store=object_store, audit_repository=audit_repository,
        needs_you_repository=needs_you_repository,
    )
    assert result.outcome == OUTCOME_AI_PROPOSAL_REVIEW_REQUIRED
    assert result.proposed_type == "SUPPLIER_INVOICE"
    assert len(litellm_client.calls) == 1
    assert litellm_client.calls[0].capability_alias == "bagman-core"


# ---------------------------------------------------------------------
# Context unsupported -> AI never called
# ---------------------------------------------------------------------


def test_context_unsupported_ai_never_called(
    evidence_repository, rule_repository, classification_repository, ai_invocation_repository,
    litellm_client, object_store, audit_repository, needs_you_repository,
):
    content = b"%PDF-1.4 fake pdf bytes"
    content_hash = {"algorithm": "SHA-256", "value": hashlib.sha256(content).hexdigest()}
    storage_reference = object_store.put(identity.generate_id(), content_hash, content)
    now = datetime.now(timezone.utc)
    item = evidence_repository.register_evidence(
        entity_id=None, evidence_type="INVOICE", source_id=identity.generate_id(),
        observed_at=now, received_at=now, content_hash=content_hash, mime_type="application/pdf",
        size_bytes=len(content), storage_reference=storage_reference,
    )
    result = _classify(
        item.evidence_id, persist=True, evidence_repository=evidence_repository, rule_repository=rule_repository,
        classification_repository=classification_repository, ai_invocation_repository=ai_invocation_repository,
        litellm_client=litellm_client, object_store=object_store, audit_repository=audit_repository,
        needs_you_repository=needs_you_repository,
    )
    assert result.outcome == OUTCOME_CONTEXT_UNSUPPORTED
    assert result.unsupported_reason is not None
    assert litellm_client.calls == []
    assert classification_repository.get_current_classification(item.evidence_id, "DOCUMENT_TYPE") is None


def test_evidence_with_no_stored_content_is_context_unsupported(
    evidence_repository, rule_repository, classification_repository, ai_invocation_repository,
    litellm_client, object_store, audit_repository, needs_you_repository,
):
    now = datetime.now(timezone.utc)
    item = evidence_repository.register_evidence(
        entity_id=None, evidence_type="EMAIL", source_id=identity.generate_id(),
        observed_at=now, received_at=now, content_hash="a" * 64, mime_type="message/rfc822",
        size_bytes=0, storage_reference=None,
    )
    result = _classify(
        item.evidence_id, persist=True, evidence_repository=evidence_repository, rule_repository=rule_repository,
        classification_repository=classification_repository, ai_invocation_repository=ai_invocation_repository,
        litellm_client=litellm_client, object_store=object_store, audit_repository=audit_repository,
        needs_you_repository=needs_you_repository,
    )
    assert result.outcome == OUTCOME_CONTEXT_UNSUPPORTED
    assert litellm_client.calls == []


# ---------------------------------------------------------------------
# Successful proposal persistence — REVIEW_REQUIRED / UNCLASSIFIABLE
# ---------------------------------------------------------------------


def test_successful_concrete_proposal_persists_as_review_required(
    evidence_repository, rule_repository, classification_repository, ai_invocation_repository,
    litellm_client, object_store, audit_repository, needs_you_repository,
):
    evidence_id = _register_email_evidence(
        evidence_repository, object_store,
        content=_rfc822_email(sender="billing@vendor.com", subject="Invoice 42", body="Please pay $500."),
    )
    _queue_proposal(litellm_client, proposed_type="SUPPLIER_INVOICE", confidence=0.91)
    result = _classify(
        evidence_id, persist=True, evidence_repository=evidence_repository, rule_repository=rule_repository,
        classification_repository=classification_repository, ai_invocation_repository=ai_invocation_repository,
        litellm_client=litellm_client, object_store=object_store, audit_repository=audit_repository,
        needs_you_repository=needs_you_repository,
    )
    assert result.outcome == OUTCOME_AI_PROPOSAL_REVIEW_REQUIRED
    assert result.was_created is True
    assert result.classification.status == STATUS_REVIEW_REQUIRED
    assert result.classification.source == "AI_PROPOSAL"
    assert result.classification.document_type == "SUPPLIER_INVOICE"
    assert result.classification.confidence == 0.91
    assert result.classification.ai_invocation_id == result.ai_invocation_id

    # CD-6 Slice 5 WI-4 §5/§7/§45 — a genuinely fresh, persisted concrete
    # AI proposal, produced through the REAL `classify_evidence` wiring
    # (not a standalone call to `ensure_classification_review_item`),
    # must result in exactly one OPEN CLASSIFICATION_REVIEW item anchored
    # to this exact classification_id.
    from services.needs_you.needs_you import ITEM_TYPE_CLASSIFICATION_REVIEW

    review_items = needs_you_repository.list_needs_you_items(item_type=ITEM_TYPE_CLASSIFICATION_REVIEW)
    assert len(review_items) == 1
    assert review_items[0].status == "OPEN"
    assert review_items[0].source_object_reference == result.classification.classification_id


def test_unknown_proposal_persists_as_unclassifiable(
    evidence_repository, rule_repository, classification_repository, ai_invocation_repository,
    litellm_client, object_store, audit_repository, needs_you_repository,
):
    evidence_id = _register_email_evidence(
        evidence_repository, object_store,
        content=_rfc822_email(sender="a@b.com", subject="???", body="incomprehensible fragment"),
    )
    _queue_proposal(litellm_client, proposed_type="UNKNOWN", confidence=0.1, warnings=["insufficient evidence"])
    result = _classify(
        evidence_id, persist=True, evidence_repository=evidence_repository, rule_repository=rule_repository,
        classification_repository=classification_repository, ai_invocation_repository=ai_invocation_repository,
        litellm_client=litellm_client, object_store=object_store, audit_repository=audit_repository,
        needs_you_repository=needs_you_repository,
    )
    assert result.outcome == OUTCOME_AI_PROPOSAL_UNCLASSIFIABLE
    assert result.classification.status == STATUS_UNCLASSIFIABLE
    assert result.classification.document_type == "UNKNOWN"
    assert result.classification.source == "AI_PROPOSAL"

    # WI-4 §5 — an UNKNOWN proposal ALSO needs a review item (the
    # operator may know what it is even when the model does not).
    from services.needs_you.needs_you import ITEM_TYPE_CLASSIFICATION_REVIEW

    review_items = needs_you_repository.list_needs_you_items(item_type=ITEM_TYPE_CLASSIFICATION_REVIEW)
    assert len(review_items) == 1
    assert review_items[0].status == "OPEN"
    assert review_items[0].source_object_reference == result.classification.classification_id


def test_preview_mode_never_persists_a_classification(
    evidence_repository, rule_repository, classification_repository, ai_invocation_repository,
    litellm_client, object_store, audit_repository, needs_you_repository,
):
    evidence_id = _register_email_evidence(
        evidence_repository, object_store,
        content=_rfc822_email(sender="billing@vendor.com", subject="Invoice 42", body="Please pay $500."),
    )
    _queue_proposal(litellm_client, proposed_type="SUPPLIER_INVOICE", confidence=0.9)
    result = _classify(
        evidence_id, persist=False, evidence_repository=evidence_repository, rule_repository=rule_repository,
        classification_repository=classification_repository, ai_invocation_repository=ai_invocation_repository,
        litellm_client=litellm_client, object_store=object_store, audit_repository=audit_repository,
        needs_you_repository=needs_you_repository,
    )
    assert result.outcome == OUTCOME_AI_PROPOSAL_REVIEW_REQUIRED
    assert result.classification is None
    assert result.was_created is None
    assert classification_repository.get_current_classification(evidence_id, "DOCUMENT_TYPE") is None
    # A real AIInvocation was still created.
    assert result.ai_invocation_id is not None
    ai_invocation_repository.get_invocation(result.ai_invocation_id)
    # WI-4 §6/§9 — preview mode NEVER performs the review-item producer
    # behaviour (zero EvidenceClassification AND zero NeedsYouItem).
    assert needs_you_repository.list_needs_you_items() == []


# ---------------------------------------------------------------------
# Failed invocation -> no classification
# ---------------------------------------------------------------------


def test_preview_mode_never_persists_even_when_a_real_deterministic_rule_matches(
    evidence_repository, rule_repository, classification_repository, ai_invocation_repository,
    litellm_client, object_store, audit_repository, needs_you_repository,
):
    """PL independent-review regression: an earlier version of the
    orchestrator called WI-2's real, WRITING
    `classify_evidence_deterministically` unconditionally — so the
    `ai-preview` endpoint (`persist=False`, WI-3 §39's explicit "creates
    NO EvidenceClassification row regardless of outcome") could silently
    create a genuine `DETERMINISTIC_CLASSIFIED` row whenever a real
    ACTIVE rule happened to match, exactly contradicting the endpoint's
    fundamental 'preview only, never mutates' safety contract. This is
    the one outcome (`DETERMINISTIC_CLASSIFIED`) where WI-2's own
    function genuinely writes — every other deterministic outcome was
    already a pure read regardless of which variant is used."""
    rule_repository.create_rule(
        sender_scope_type="EXACT_SENDER_DOMAIN", sender_scope_value="vendor.com",
        subject_predicate_type="EXACT", subject_predicate_value="Monthly Statement",
        document_type="SUPPLIER_INVOICE", source="OPERATOR",
    )
    evidence_id = _register_email_evidence(
        evidence_repository, object_store,
        content=_rfc822_email(sender="billing@vendor.com", subject="Monthly Statement", body="pay up"),
        sender_address="billing@vendor.com", subject="Monthly Statement",
    )
    result = _classify(
        evidence_id, persist=False, evidence_repository=evidence_repository, rule_repository=rule_repository,
        classification_repository=classification_repository, ai_invocation_repository=ai_invocation_repository,
        litellm_client=litellm_client, object_store=object_store, audit_repository=audit_repository,
        needs_you_repository=needs_you_repository,
    )
    assert result.outcome == OUTCOME_DETERMINISTIC_CLASSIFIED
    # A real match was found (the caller can see this), but NOTHING was
    # persisted and no AI call was made either.
    assert result.matched_rule_id is not None
    assert result.classification is None
    assert result.was_created is not True
    assert classification_repository.get_current_classification(evidence_id, "DOCUMENT_TYPE") is None
    assert litellm_client.calls == []


def test_litellm_transport_failure_persists_no_classification(
    evidence_repository, rule_repository, classification_repository, ai_invocation_repository,
    litellm_client, object_store, audit_repository, needs_you_repository,
):
    evidence_id = _register_email_evidence(
        evidence_repository, object_store,
        content=_rfc822_email(sender="billing@vendor.com", subject="Invoice 42", body="Please pay $500."),
    )
    litellm_client.queue_failure(capability_alias="bagman-core", status=LiteLLMOutcomeStatus.TIMEOUT, error_detail="boom")
    result = _classify(
        evidence_id, persist=True, evidence_repository=evidence_repository, rule_repository=rule_repository,
        classification_repository=classification_repository, ai_invocation_repository=ai_invocation_repository,
        litellm_client=litellm_client, object_store=object_store, audit_repository=audit_repository,
        needs_you_repository=needs_you_repository,
    )
    assert result.outcome == OUTCOME_AI_INVOCATION_FAILED
    assert result.error_code is not None
    assert classification_repository.get_current_classification(evidence_id, "DOCUMENT_TYPE") is None


def test_malformed_schema_invalid_ai_output_persists_no_classification(
    evidence_repository, rule_repository, classification_repository, ai_invocation_repository,
    litellm_client, object_store, audit_repository, needs_you_repository,
):
    evidence_id = _register_email_evidence(
        evidence_repository, object_store,
        content=_rfc822_email(sender="billing@vendor.com", subject="Invoice 42", body="Please pay $500."),
    )
    # Missing "confidence", and "proposed_type" is not in the closed v2 enum.
    litellm_client.queue_success(capability_alias="bagman-core", content=json.dumps({"proposed_type": "INVOICE", "signals": [], "warnings": []}))
    result = _classify(
        evidence_id, persist=True, evidence_repository=evidence_repository, rule_repository=rule_repository,
        classification_repository=classification_repository, ai_invocation_repository=ai_invocation_repository,
        litellm_client=litellm_client, object_store=object_store, audit_repository=audit_repository,
        needs_you_repository=needs_you_repository,
    )
    assert result.outcome == OUTCOME_AI_INVOCATION_FAILED
    assert result.error_code == "OUTPUT_SCHEMA_INVALID"
    assert classification_repository.get_current_classification(evidence_id, "DOCUMENT_TYPE") is None


def test_malformed_not_json_ai_output_persists_no_classification(
    evidence_repository, rule_repository, classification_repository, ai_invocation_repository,
    litellm_client, object_store, audit_repository, needs_you_repository,
):
    evidence_id = _register_email_evidence(
        evidence_repository, object_store,
        content=_rfc822_email(sender="billing@vendor.com", subject="Invoice 42", body="Please pay $500."),
    )
    litellm_client.queue_success(capability_alias="bagman-core", content="not even json")
    result = _classify(
        evidence_id, persist=True, evidence_repository=evidence_repository, rule_repository=rule_repository,
        classification_repository=classification_repository, ai_invocation_repository=ai_invocation_repository,
        litellm_client=litellm_client, object_store=object_store, audit_repository=audit_repository,
        needs_you_repository=needs_you_repository,
    )
    assert result.outcome == OUTCOME_AI_INVOCATION_FAILED
    assert result.error_code == "OUTPUT_NOT_JSON"


# ---------------------------------------------------------------------
# Idempotency / reuse (§25), crash-recovery (§26), prior-failure (§27),
# active-invocation guard (§28)
# ---------------------------------------------------------------------


def test_second_identical_preview_request_reuses_invocation_no_second_provider_call(
    evidence_repository, rule_repository, classification_repository, ai_invocation_repository,
    litellm_client, object_store, audit_repository, needs_you_repository,
):
    evidence_id = _register_email_evidence(
        evidence_repository, object_store,
        content=_rfc822_email(sender="billing@vendor.com", subject="Invoice 42", body="Please pay $500."),
    )
    _queue_proposal(litellm_client, proposed_type="SUPPLIER_INVOICE", confidence=0.9)
    first = _classify(
        evidence_id, persist=False, evidence_repository=evidence_repository, rule_repository=rule_repository,
        classification_repository=classification_repository, ai_invocation_repository=ai_invocation_repository,
        litellm_client=litellm_client, object_store=object_store, audit_repository=audit_repository,
        needs_you_repository=needs_you_repository,
    )
    second = _classify(
        evidence_id, persist=False, evidence_repository=evidence_repository, rule_repository=rule_repository,
        classification_repository=classification_repository, ai_invocation_repository=ai_invocation_repository,
        litellm_client=litellm_client, object_store=object_store, audit_repository=audit_repository,
        needs_you_repository=needs_you_repository,
    )
    assert len(litellm_client.calls) == 1
    assert first.ai_invocation_id == second.ai_invocation_id
    assert second.proposed_type == "SUPPLIER_INVOICE"


def test_crash_recovery_reuses_succeeded_invocation_to_create_missing_classification(
    evidence_repository, rule_repository, classification_repository, ai_invocation_repository,
    litellm_client, object_store, audit_repository, needs_you_repository,
):
    evidence_id = _register_email_evidence(
        evidence_repository, object_store,
        content=_rfc822_email(sender="billing@vendor.com", subject="Invoice 42", body="Please pay $500."),
    )
    _queue_proposal(litellm_client, proposed_type="SUPPLIER_INVOICE", confidence=0.9)
    # Simulate "AI succeeded, process crashed before EvidenceClassification
    # was persisted" — a preview call runs the model and creates the
    # AIInvocation, but (like preview always does) persists nothing.
    preview = _classify(
        evidence_id, persist=False, evidence_repository=evidence_repository, rule_repository=rule_repository,
        classification_repository=classification_repository, ai_invocation_repository=ai_invocation_repository,
        litellm_client=litellm_client, object_store=object_store, audit_repository=audit_repository,
        needs_you_repository=needs_you_repository,
    )
    assert classification_repository.get_current_classification(evidence_id, "DOCUMENT_TYPE") is None

    recovered = _classify(
        evidence_id, persist=True, evidence_repository=evidence_repository, rule_repository=rule_repository,
        classification_repository=classification_repository, ai_invocation_repository=ai_invocation_repository,
        litellm_client=litellm_client, object_store=object_store, audit_repository=audit_repository,
        needs_you_repository=needs_you_repository,
    )
    assert len(litellm_client.calls) == 1  # no second model call
    assert recovered.ai_invocation_id == preview.ai_invocation_id
    assert recovered.was_created is True
    assert recovered.classification.status == STATUS_REVIEW_REQUIRED


def test_prior_failed_invocation_is_not_silently_retried(
    evidence_repository, rule_repository, classification_repository, ai_invocation_repository,
    litellm_client, object_store, audit_repository, needs_you_repository,
):
    evidence_id = _register_email_evidence(
        evidence_repository, object_store,
        content=_rfc822_email(sender="billing@vendor.com", subject="Invoice 42", body="Please pay $500."),
    )
    litellm_client.queue_failure(capability_alias="bagman-core", status=LiteLLMOutcomeStatus.TIMEOUT)
    first = _classify(
        evidence_id, persist=True, evidence_repository=evidence_repository, rule_repository=rule_repository,
        classification_repository=classification_repository, ai_invocation_repository=ai_invocation_repository,
        litellm_client=litellm_client, object_store=object_store, audit_repository=audit_repository,
        needs_you_repository=needs_you_repository,
    )
    assert first.outcome == OUTCOME_AI_INVOCATION_FAILED
    assert len(litellm_client.calls) == 1

    second = _classify(
        evidence_id, persist=True, evidence_repository=evidence_repository, rule_repository=rule_repository,
        classification_repository=classification_repository, ai_invocation_repository=ai_invocation_repository,
        litellm_client=litellm_client, object_store=object_store, audit_repository=audit_repository,
        needs_you_repository=needs_you_repository,
    )
    assert second.outcome == OUTCOME_AI_PRIOR_FAILURE
    assert second.ai_invocation_id == first.ai_invocation_id
    # No second model call was made — the FakeLiteLLMClient's queue for
    # bagman-core is now empty; if the orchestrator had retried, it
    # would have raised AssertionError (no scripted response queued).
    assert len(litellm_client.calls) == 1


def test_active_invocation_guard_returns_ai_in_progress_never_launches_duplicate(
    evidence_repository, rule_repository, classification_repository, ai_invocation_repository,
    litellm_client, object_store, audit_repository, needs_you_repository,
):
    evidence_id = _register_email_evidence(
        evidence_repository, object_store,
        content=_rfc822_email(sender="billing@vendor.com", subject="Invoice 42", body="Please pay $500."),
    )
    # Manually create a non-terminal (REQUESTED) invocation for the
    # exact same subject, simulating a concurrent in-flight call.
    in_flight = ai_invocation_repository.create_invocation(
        task_id="DOCUMENT_TYPE_PROPOSAL", task_version=2, role="BACKGROUND", provider="LITELLM",
        capability_alias="bagman-core", input_references={"evidence_id": evidence_id},
        actor_type=actor.SYSTEM, actor_id=ACTOR_ID,
    )
    result = _classify(
        evidence_id, persist=True, evidence_repository=evidence_repository, rule_repository=rule_repository,
        classification_repository=classification_repository, ai_invocation_repository=ai_invocation_repository,
        litellm_client=litellm_client, object_store=object_store, audit_repository=audit_repository,
        needs_you_repository=needs_you_repository,
    )
    assert result.outcome == OUTCOME_AI_IN_PROGRESS
    assert result.ai_invocation_id == in_flight.ai_invocation_id
    assert litellm_client.calls == []


# ---------------------------------------------------------------------
# Race after model completion (§38)
# ---------------------------------------------------------------------


def test_race_after_model_completion_never_supersedes_established_current_truth(
    evidence_repository, rule_repository, classification_repository, ai_invocation_repository,
    litellm_client, object_store, audit_repository, needs_you_repository,
):
    evidence_id = _register_email_evidence(
        evidence_repository, object_store,
        content=_rfc822_email(sender="billing@vendor.com", subject="Invoice 42", body="Please pay $500."),
    )
    _queue_proposal(litellm_client, proposed_type="SUPPLIER_INVOICE", confidence=0.9)
    # Simulate the AI already having run (preview), and then another
    # producer establishing current truth for real WHILE that model
    # work is/was in flight — never possible to interleave truly
    # concurrently in a synchronous test, so this proves the FINAL
    # persistence attempt's own concurrency guard instead: by the time
    # `persist=True` is attempted, current truth already exists.
    preview = _classify(
        evidence_id, persist=False, evidence_repository=evidence_repository, rule_repository=rule_repository,
        classification_repository=classification_repository, ai_invocation_repository=ai_invocation_repository,
        litellm_client=litellm_client, object_store=object_store, audit_repository=audit_repository,
        needs_you_repository=needs_you_repository,
    )
    assert preview.outcome == OUTCOME_AI_PROPOSAL_REVIEW_REQUIRED

    classification_repository.create_classification(
        evidence_id=evidence_id, classification_type="DOCUMENT_TYPE", document_type="RECEIPT",
        status="CLASSIFIED", source="OPERATOR_ASSIGNED", operator_action_id="op-race",
        expected_current_classification_id=None,
    )

    result = _classify(
        evidence_id, persist=True, evidence_repository=evidence_repository, rule_repository=rule_repository,
        classification_repository=classification_repository, ai_invocation_repository=ai_invocation_repository,
        litellm_client=litellm_client, object_store=object_store, audit_repository=audit_repository,
        needs_you_repository=needs_you_repository,
    )
    assert result.outcome == OUTCOME_CURRENT_CLASSIFICATION_EXISTS
    assert result.existing_source == "OPERATOR_ASSIGNED"
    current = classification_repository.get_current_classification(evidence_id, "DOCUMENT_TYPE")
    assert current.source == "OPERATOR_ASSIGNED"
    assert current.document_type == "RECEIPT"


# ---------------------------------------------------------------------
# Audit — genuinely new row audits once; replay never double-audits (§37)
# ---------------------------------------------------------------------


def test_genuinely_new_ai_classification_emits_one_bounded_audit_event(
    evidence_repository, rule_repository, classification_repository, ai_invocation_repository,
    litellm_client, object_store, audit_repository, needs_you_repository,
):
    evidence_id = _register_email_evidence(
        evidence_repository, object_store,
        content=_rfc822_email(sender="billing@vendor.com", subject="Invoice 42", body="Please pay $500."),
    )
    _queue_proposal(litellm_client, proposed_type="SUPPLIER_INVOICE", confidence=0.9)
    result = _classify(
        evidence_id, persist=True, evidence_repository=evidence_repository, rule_repository=rule_repository,
        classification_repository=classification_repository, ai_invocation_repository=ai_invocation_repository,
        litellm_client=litellm_client, object_store=object_store, audit_repository=audit_repository,
        needs_you_repository=needs_you_repository,
    )
    events = audit_repository.list_by_subject("EvidenceClassification", result.classification.classification_id)
    classified_events = [e for e in events if e.event_type == "EVIDENCE_CLASSIFIED"]
    assert len(classified_events) == 1
    payload = classified_events[0].payload
    assert payload["evidence_id"] == evidence_id
    assert payload["ai_invocation_id"] == result.ai_invocation_id
    assert payload["document_type"] == "SUPPLIER_INVOICE"
    assert "signals" not in payload
    assert "warnings" not in payload


def test_recovery_replay_never_double_audits(
    evidence_repository, rule_repository, classification_repository, ai_invocation_repository,
    litellm_client, object_store, audit_repository, needs_you_repository,
):
    evidence_id = _register_email_evidence(
        evidence_repository, object_store,
        content=_rfc822_email(sender="billing@vendor.com", subject="Invoice 42", body="Please pay $500."),
    )
    _queue_proposal(litellm_client, proposed_type="SUPPLIER_INVOICE", confidence=0.9)
    _classify(
        evidence_id, persist=False, evidence_repository=evidence_repository, rule_repository=rule_repository,
        classification_repository=classification_repository, ai_invocation_repository=ai_invocation_repository,
        litellm_client=litellm_client, object_store=object_store, audit_repository=audit_repository,
        needs_you_repository=needs_you_repository,
    )
    recovered = _classify(
        evidence_id, persist=True, evidence_repository=evidence_repository, rule_repository=rule_repository,
        classification_repository=classification_repository, ai_invocation_repository=ai_invocation_repository,
        litellm_client=litellm_client, object_store=object_store, audit_repository=audit_repository,
        needs_you_repository=needs_you_repository,
    )
    events = audit_repository.list_by_subject("EvidenceClassification", recovered.classification.classification_id)
    classified_events = [e for e in events if e.event_type == "EVIDENCE_CLASSIFIED"]
    assert len(classified_events) == 1


# ---------------------------------------------------------------------
# WI-4 §5/§7/§8/§9/§45 — the review-item producer, exercised through the
# REAL `classify_evidence` orchestrator wiring (not a standalone call to
# `ensure_classification_review_item` — see
# `tests/integration/test_classification_review.py` for that unit-level
# coverage; these tests instead prove the actual integration point:
# the two exact edit sites inside `classify_evidence` itself).
# ---------------------------------------------------------------------


def test_persistent_orchestrator_replay_reuses_the_same_review_item(
    evidence_repository, rule_repository, classification_repository, ai_invocation_repository,
    litellm_client, object_store, audit_repository, needs_you_repository,
):
    from services.needs_you.needs_you import ITEM_TYPE_CLASSIFICATION_REVIEW

    evidence_id = _register_email_evidence(
        evidence_repository, object_store,
        content=_rfc822_email(sender="billing@vendor.com", subject="Invoice 42", body="Please pay $500."),
    )
    _queue_proposal(litellm_client, proposed_type="SUPPLIER_INVOICE", confidence=0.9)
    first = _classify(
        evidence_id, persist=True, evidence_repository=evidence_repository, rule_repository=rule_repository,
        classification_repository=classification_repository, ai_invocation_repository=ai_invocation_repository,
        litellm_client=litellm_client, object_store=object_store, audit_repository=audit_repository,
        needs_you_repository=needs_you_repository,
    )
    # A second call now hits the top-of-function "current AI_PROPOSAL"
    # guard (WI-4 §9) — it must re-ensure (idempotently) the SAME review
    # item, never a second one.
    second = _classify(
        evidence_id, persist=True, evidence_repository=evidence_repository, rule_repository=rule_repository,
        classification_repository=classification_repository, ai_invocation_repository=ai_invocation_repository,
        litellm_client=litellm_client, object_store=object_store, audit_repository=audit_repository,
        needs_you_repository=needs_you_repository,
    )
    assert second.outcome == OUTCOME_CURRENT_CLASSIFICATION_EXISTS
    assert second.classification.classification_id == first.classification.classification_id

    review_items = needs_you_repository.list_needs_you_items(item_type=ITEM_TYPE_CLASSIFICATION_REVIEW)
    assert len(review_items) == 1
    assert review_items[0].source_object_reference == first.classification.classification_id


def test_orchestrator_recovers_missing_review_item_after_a_simulated_crash(
    evidence_repository, rule_repository, classification_repository, ai_invocation_repository,
    litellm_client, object_store, audit_repository, needs_you_repository,
):
    """WI-4 §8 — the crash-recovery case: an AI_PROPOSAL classification
    already committed (simulated here via a DIRECT
    `classification_repository.create_classification` call, exactly as
    if an earlier `classify_evidence(persist=True)` call had reached
    that point and then the process died before ever reaching the
    review-item producer call) — the NEXT `classify_evidence(persist=True)`
    call for the SAME evidence must detect the current AI_PROPOSAL via
    the top-of-function guard and recover the missing review item,
    never skip recovery merely because 'current already exists'."""
    from services.needs_you.needs_you import ITEM_TYPE_CLASSIFICATION_REVIEW

    evidence_id = _register_email_evidence(
        evidence_repository, object_store,
        content=_rfc822_email(sender="billing@vendor.com", subject="Invoice 42", body="Please pay $500."),
    )
    invocation = ai_invocation_repository.create_invocation(
        task_id="DOCUMENT_TYPE_PROPOSAL", task_version=2, role="BACKGROUND", provider="LITELLM",
        capability_alias="bagman-core", input_references={"evidence_id": evidence_id},
        actor_type=actor.SYSTEM, actor_id=ACTOR_ID,
    )
    ai_invocation_repository.transition_status(invocation.ai_invocation_id, "RUNNING")
    ai_invocation_repository.transition_status(invocation.ai_invocation_id, "SUCCEEDED")
    classification = classification_repository.create_classification(
        evidence_id=evidence_id, classification_type="DOCUMENT_TYPE", document_type="SUPPLIER_INVOICE",
        status=STATUS_REVIEW_REQUIRED, source="AI_PROPOSAL", confidence=0.9,
        ai_invocation_id=invocation.ai_invocation_id, expected_current_classification_id=None,
    )
    assert needs_you_repository.list_needs_you_items(item_type=ITEM_TYPE_CLASSIFICATION_REVIEW) == []

    result = _classify(
        evidence_id, persist=True, evidence_repository=evidence_repository, rule_repository=rule_repository,
        classification_repository=classification_repository, ai_invocation_repository=ai_invocation_repository,
        litellm_client=litellm_client, object_store=object_store, audit_repository=audit_repository,
        needs_you_repository=needs_you_repository,
    )
    assert result.outcome == OUTCOME_CURRENT_CLASSIFICATION_EXISTS
    assert result.classification.classification_id == classification.classification_id

    review_items = needs_you_repository.list_needs_you_items(item_type=ITEM_TYPE_CLASSIFICATION_REVIEW)
    assert len(review_items) == 1
    assert review_items[0].source_object_reference == classification.classification_id


def test_deterministic_classification_via_real_orchestrator_creates_no_review_item(
    evidence_repository, rule_repository, classification_repository, ai_invocation_repository,
    litellm_client, object_store, audit_repository, needs_you_repository,
):
    """WI-4 §40 — a real, genuine DETERMINISTIC_RULE current
    classification, produced through the REAL `classify_evidence`
    orchestrator (not a bypass), must never create a review item — both
    on the fresh-classify call AND on a subsequent replay call that
    hits the top-of-function current-classification guard."""
    from services.needs_you.needs_you import ITEM_TYPE_CLASSIFICATION_REVIEW

    rule_repository.create_rule(
        sender_scope_type="EXACT_SENDER_DOMAIN", sender_scope_value="vendor.com",
        subject_predicate_type="EXACT", subject_predicate_value="Monthly Statement",
        document_type="SUPPLIER_INVOICE", source="OPERATOR",
    )
    evidence_id = _register_email_evidence(
        evidence_repository, object_store,
        content=_rfc822_email(sender="billing@vendor.com", subject="Monthly Statement", body="pay up"),
        sender_address="billing@vendor.com", subject="Monthly Statement",
    )
    first = _classify(
        evidence_id, persist=True, evidence_repository=evidence_repository, rule_repository=rule_repository,
        classification_repository=classification_repository, ai_invocation_repository=ai_invocation_repository,
        litellm_client=litellm_client, object_store=object_store, audit_repository=audit_repository,
        needs_you_repository=needs_you_repository,
    )
    assert first.outcome == OUTCOME_DETERMINISTIC_CLASSIFIED
    second = _classify(
        evidence_id, persist=True, evidence_repository=evidence_repository, rule_repository=rule_repository,
        classification_repository=classification_repository, ai_invocation_repository=ai_invocation_repository,
        litellm_client=litellm_client, object_store=object_store, audit_repository=audit_repository,
        needs_you_repository=needs_you_repository,
    )
    assert second.outcome == OUTCOME_CURRENT_CLASSIFICATION_EXISTS
    assert needs_you_repository.list_needs_you_items(item_type=ITEM_TYPE_CLASSIFICATION_REVIEW) == []


# ---------------------------------------------------------------------
# Safety (§48) — AI cannot mutate EvidenceItem, entity_id, create a
# rule, call mailbox providers, call Xero, invoke Claude.
# ---------------------------------------------------------------------


def test_ai_classification_never_mutates_evidence_item(
    evidence_repository, rule_repository, classification_repository, ai_invocation_repository,
    litellm_client, object_store, audit_repository, needs_you_repository,
):
    evidence_id = _register_email_evidence(
        evidence_repository, object_store,
        content=_rfc822_email(sender="billing@vendor.com", subject="Invoice 42", body="Please pay $500."),
    )
    before = evidence_repository.get_evidence(evidence_id)
    _queue_proposal(litellm_client, proposed_type="SUPPLIER_INVOICE", confidence=0.9)
    _classify(
        evidence_id, persist=True, evidence_repository=evidence_repository, rule_repository=rule_repository,
        classification_repository=classification_repository, ai_invocation_repository=ai_invocation_repository,
        litellm_client=litellm_client, object_store=object_store, audit_repository=audit_repository,
        needs_you_repository=needs_you_repository,
    )
    after = evidence_repository.get_evidence(evidence_id)
    assert after.status == before.status
    assert after.entity_id == before.entity_id
    assert after.entity_id is None
    assert after.mime_type == before.mime_type
    assert after.content_hash == before.content_hash


def test_ai_classification_never_creates_a_classification_rule(
    evidence_repository, rule_repository, classification_repository, ai_invocation_repository,
    litellm_client, object_store, audit_repository, needs_you_repository,
):
    evidence_id = _register_email_evidence(
        evidence_repository, object_store,
        content=_rfc822_email(sender="billing@vendor.com", subject="Invoice 42", body="Please pay $500."),
    )
    _queue_proposal(litellm_client, proposed_type="SUPPLIER_INVOICE", confidence=0.9)
    _classify(
        evidence_id, persist=True, evidence_repository=evidence_repository, rule_repository=rule_repository,
        classification_repository=classification_repository, ai_invocation_repository=ai_invocation_repository,
        litellm_client=litellm_client, object_store=object_store, audit_repository=audit_repository,
        needs_you_repository=needs_you_repository,
    )
    assert rule_repository.list_rules() == []


def test_source_module_never_imports_mailbox_xero_or_claude_providers():
    """Static, source-level proof (complements the behavioural proofs
    above): the orchestrator module's own text never references a
    mailbox provider adapter, Xero, or the Claude operator gateway."""
    import inspect

    import services.evidence.classification_orchestrator as orchestrator_module

    source = inspect.getsource(orchestrator_module)
    for forbidden in ("services.mailbox", "services.xero", "ClaudeCodeOperatorRunner", "agent.claude_code"):
        assert forbidden not in source


def test_not_found_evidence_id_propagates():
    evidence_repository = InMemoryEvidenceRepository(InMemoryExternalReferenceRepository())
    rule_repository = InMemoryEvidenceClassificationRuleRepository()
    ai_invocation_repository = InMemoryAIInvocationRepository()
    classification_repository = InMemoryEvidenceClassificationRepository(
        evidence_repository=evidence_repository, rule_repository=rule_repository,
        ai_invocation_repository=ai_invocation_repository,
    )
    audit_repository = InMemoryAuditRepository()
    with pytest.raises(NotFoundError):
        classify_evidence(
            evidence_id="does-not-exist", persist=True,
            evidence_repository=evidence_repository, rule_repository=rule_repository,
            classification_repository=classification_repository, ai_invocation_repository=ai_invocation_repository,
            litellm_client=FakeLiteLLMClient(), object_store=InMemoryObjectStore(),
            audit_repository=audit_repository, record_audit_event=audit_repository.record_audit_event,
            needs_you_repository=InMemoryNeedsYouRepository(),
            actor_type=actor.SYSTEM, actor_id=ACTOR_ID,
        )
