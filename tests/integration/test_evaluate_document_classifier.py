"""CD-6 Slice 5 WI-6 — `scripts/evaluate_document_classifier.py` proofs
against in-memory repositories + `ai.providers.litellm.fake
.FakeLiteLLMClient` (never a real/live model call — PID §61). Mirrors
`tests/integration/test_classification_orchestrator.py`'s own fixture
shape. Genuine real-database concurrency proofs live under
`tests/persistence/` instead (see that directory's own WI-6 file)."""
from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone

import pytest

from ai.invocation import InMemoryAIInvocationRepository
from ai.providers.litellm.fake import FakeLiteLLMClient
from core import identity
from core.audit import InMemoryAuditRepository
from core.external_reference import InMemoryExternalReferenceRepository
from persistence.objects.memory_store import InMemoryObjectStore
from services.evidence.classification import InMemoryEvidenceClassificationRepository
from services.evidence.classification_rule import (
    RULE_SOURCE_OPERATOR,
    SENDER_SCOPE_EXACT_SENDER_DOMAIN,
    SUBJECT_PREDICATE_EXACT,
    InMemoryEvidenceClassificationRuleRepository,
)
from services.evidence.evidence import InMemoryEvidenceRepository
from services.needs_you.needs_you import InMemoryNeedsYouRepository

import scripts.evaluate_document_classifier as evl

ACTOR_ID = "wi6-evaluator-tests"


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


def _deps(evidence_repository, rule_repository, classification_repository, ai_invocation_repository,
          litellm_client, object_store, audit_repository, needs_you_repository):
    return dict(
        evidence_repository=evidence_repository, rule_repository=rule_repository,
        classification_repository=classification_repository, ai_invocation_repository=ai_invocation_repository,
        litellm_client=litellm_client, object_store=object_store, audit_repository=audit_repository,
        record_audit_event=audit_repository.record_audit_event, needs_you_repository=needs_you_repository,
        actor_type="SYSTEM", actor_id=ACTOR_ID, correlation_id=None,
    )


def _rfc822_email(*, sender: str, subject: str, body: str) -> bytes:
    import email.message

    msg = email.message.EmailMessage()
    msg["From"] = sender
    msg["Subject"] = subject
    msg.set_content(body)
    return bytes(msg)


def _register_email(evidence_repository, object_store, *, sender: str, subject: str, body: str = "body"):
    content = _rfc822_email(sender=sender, subject=subject, body=body)
    content_hash = {"algorithm": "SHA-256", "value": hashlib.sha256(content).hexdigest()}
    storage_reference = object_store.put(identity.generate_id(), content_hash, content)
    now = datetime.now(timezone.utc)
    return evidence_repository.register_evidence(
        entity_id=None, evidence_type="EMAIL", source_id=identity.generate_id(),
        observed_at=now, received_at=now, content_hash=content_hash, mime_type="message/rfc822",
        size_bytes=len(content), storage_reference=storage_reference,
        metadata={"sender_address": sender, "subject": subject},
    )


def _make_rule(rule_repository, *, sender_domain, document_type="SUPPLIER_INVOICE", subject="monthly statement"):
    return rule_repository.create_rule(
        sender_scope_type=SENDER_SCOPE_EXACT_SENDER_DOMAIN, sender_scope_value=sender_domain,
        subject_predicate_type=SUBJECT_PREDICATE_EXACT, subject_predicate_value=subject,
        document_type=document_type, source=RULE_SOURCE_OPERATOR,
    )


def _write_manifest(tmp_path, entries: list[dict]) -> str:
    path = tmp_path / "manifest.json"
    path.write_text(json.dumps(entries), encoding="utf-8")
    return str(path)


def _queue_ai_success(litellm_client, *, proposed_type: str, confidence: float = 0.8):
    litellm_client.queue_success(
        capability_alias="bagman-core",
        content=json.dumps({"proposed_type": proposed_type, "confidence": confidence, "signals": [], "warnings": []}),
    )


# ---------------------------------------------------------------------
# Zero-mutation proof
# ---------------------------------------------------------------------


def test_evaluator_never_creates_classification_rule_or_needs_you_rows(
    evidence_repository, rule_repository, classification_repository, ai_invocation_repository, litellm_client,
    object_store, audit_repository, needs_you_repository, tmp_path,
):
    rule = _make_rule(rule_repository, sender_domain="vendor.example")
    det_item = _register_email(evidence_repository, object_store, sender="billing@vendor.example", subject="monthly statement")
    ai_item = _register_email(evidence_repository, object_store, sender="other@random.example", subject="a receipt maybe")
    _queue_ai_success(litellm_client, proposed_type="RECEIPT")

    manifest_file = _write_manifest(tmp_path, [
        {"evidence_id": det_item.evidence_id, "expected_document_type": "SUPPLIER_INVOICE", "label_family": "det"},
        {"evidence_id": ai_item.evidence_id, "expected_document_type": "RECEIPT", "label_family": "ai"},
    ])
    items = evl.load_manifest(manifest_file)

    rule_count_before = len(rule_repository.list_rules())

    report = evl.run_evaluation(
        items=items, manifest_file=manifest_file,
        **_deps(evidence_repository, rule_repository, classification_repository, ai_invocation_repository,
                 litellm_client, object_store, audit_repository, needs_you_repository),
    )

    assert classification_repository.get_current_classification(det_item.evidence_id, "DOCUMENT_TYPE") is None
    assert classification_repository.get_current_classification(ai_item.evidence_id, "DOCUMENT_TYPE") is None
    assert len(rule_repository.list_rules()) == rule_count_before
    assert needs_you_repository.list_needs_you_items() == []
    assert report["overall"]["total"] == 2
    assert report["overall"]["correct"] == 2
    assert rule.rule_id  # sanity: the rule exists and was used for the deterministic short-circuit


# ---------------------------------------------------------------------
# Invocation reuse
# ---------------------------------------------------------------------


def test_second_run_against_the_identical_item_reuses_the_invocation(
    evidence_repository, rule_repository, classification_repository, ai_invocation_repository, litellm_client,
    object_store, audit_repository, needs_you_repository, tmp_path,
):
    ai_item = _register_email(evidence_repository, object_store, sender="other@random.example", subject="unmatched subject")
    _queue_ai_success(litellm_client, proposed_type="RECEIPT")

    manifest_file = _write_manifest(tmp_path, [
        {"evidence_id": ai_item.evidence_id, "expected_document_type": "RECEIPT", "label_family": "ai"},
    ])
    items = evl.load_manifest(manifest_file)
    deps = _deps(evidence_repository, rule_repository, classification_repository, ai_invocation_repository,
                 litellm_client, object_store, audit_repository, needs_you_repository)

    first = evl.run_evaluation(items=items, manifest_file=manifest_file, **deps)
    invocation_count_after_first = len(ai_invocation_repository.list_invocations())
    assert first["invocation_reuse"]["new_invocation_count"] == 1
    assert first["invocation_reuse"]["reused_invocation_count"] == 0

    second = evl.run_evaluation(items=items, manifest_file=manifest_file, **deps)
    assert len(ai_invocation_repository.list_invocations()) == invocation_count_after_first  # no growth
    assert second["invocation_reuse"]["new_invocation_count"] == 0
    assert second["invocation_reuse"]["reused_invocation_count"] == 1


# ---------------------------------------------------------------------
# Metrics correctness
# ---------------------------------------------------------------------


def test_metrics_are_arithmetically_correct_with_one_deliberate_mismatch(
    evidence_repository, rule_repository, classification_repository, ai_invocation_repository, litellm_client,
    object_store, audit_repository, needs_you_repository, tmp_path,
):
    rule = _make_rule(rule_repository, sender_domain="vendor.example")
    correct_item = _register_email(evidence_repository, object_store, sender="billing@vendor.example", subject="monthly statement")
    wrong_item = _register_email(evidence_repository, object_store, sender="other@random.example", subject="totally different")
    _queue_ai_success(litellm_client, proposed_type="RECEIPT")

    manifest_file = _write_manifest(tmp_path, [
        {"evidence_id": correct_item.evidence_id, "expected_document_type": "SUPPLIER_INVOICE", "label_family": "det"},
        # Deliberately WRONG expected label — the AI will say RECEIPT.
        {"evidence_id": wrong_item.evidence_id, "expected_document_type": "ORDER_CONFIRMATION", "label_family": "ai"},
    ])
    items = evl.load_manifest(manifest_file)

    report = evl.run_evaluation(
        items=items, manifest_file=manifest_file,
        **_deps(evidence_repository, rule_repository, classification_repository, ai_invocation_repository,
                 litellm_client, object_store, audit_repository, needs_you_repository),
    )

    assert report["overall"]["total"] == 2
    assert report["overall"]["correct"] == 1
    assert report["overall"]["incorrect"] == 1
    assert report["confusion_pairs"] == {"ORDER_CONFIRMATION->RECEIPT": 1}
    assert report["deterministic_metrics"]["rule_covered_labelled_items"] == 1
    assert report["deterministic_metrics"]["incorrect"] == 0
    assert report["ai_metrics"]["total"] == 1
    assert report["ai_metrics"]["incorrect"] == 1
    assert report["blocking_findings"] == []  # AI mismatch is not a STOP-class deterministic defect
    assert rule.document_type == "SUPPLIER_INVOICE"


def test_deterministic_misclassification_is_surfaced_as_a_blocking_finding(
    evidence_repository, rule_repository, classification_repository, ai_invocation_repository, litellm_client,
    object_store, audit_repository, needs_you_repository, tmp_path,
):
    _make_rule(rule_repository, sender_domain="vendor.example", document_type="SUPPLIER_INVOICE")
    item = _register_email(evidence_repository, object_store, sender="billing@vendor.example", subject="monthly statement")

    # Deliberately wrong expected label for a DETERMINISTIC item — this
    # must be loudly flagged, never buried (WI-6 §29).
    manifest_file = _write_manifest(tmp_path, [
        {"evidence_id": item.evidence_id, "expected_document_type": "RECEIPT", "label_family": "det"},
    ])
    items = evl.load_manifest(manifest_file)

    report = evl.run_evaluation(
        items=items, manifest_file=manifest_file,
        **_deps(evidence_repository, rule_repository, classification_repository, ai_invocation_repository,
                 litellm_client, object_store, audit_repository, needs_you_repository),
    )
    assert report["deterministic_metrics"]["incorrect"] == 1
    assert len(report["blocking_findings"]) == 1
    assert "DETERMINISTIC MISCLASSIFICATION" in report["blocking_findings"][0]


# ---------------------------------------------------------------------
# AI invocation failures / context-unsupported are visible, never crash
# ---------------------------------------------------------------------


def test_ai_invocation_failure_is_reported_not_dropped_and_does_not_crash(
    evidence_repository, rule_repository, classification_repository, ai_invocation_repository, litellm_client,
    object_store, audit_repository, needs_you_repository, tmp_path,
):
    from ai.providers.litellm.client import LiteLLMOutcomeStatus

    item = _register_email(evidence_repository, object_store, sender="other@random.example", subject="whatever")
    litellm_client.queue_failure(capability_alias="bagman-core", status=LiteLLMOutcomeStatus.PROVIDER_ERROR, error_detail="boom")

    manifest_file = _write_manifest(tmp_path, [
        {"evidence_id": item.evidence_id, "expected_document_type": "RECEIPT", "label_family": "ai"},
    ])
    items = evl.load_manifest(manifest_file)

    report = evl.run_evaluation(
        items=items, manifest_file=manifest_file,
        **_deps(evidence_repository, rule_repository, classification_repository, ai_invocation_repository,
                 litellm_client, object_store, audit_repository, needs_you_repository),
    )
    assert len(report["ai_invocation_failures"]) == 1
    assert report["ai_invocation_failures"][0]["evidence_id"] == item.evidence_id
    assert report["overall"]["total"] == 0  # a failure is never counted as correct/incorrect


def test_context_unsupported_evidence_never_crashes_and_is_reported(
    evidence_repository, rule_repository, classification_repository, ai_invocation_repository, litellm_client,
    object_store, audit_repository, needs_you_repository, tmp_path,
):
    # No storage_reference at all -> CONTEXT_UNSUPPORTED, per the
    # orchestrator's own documented behaviour.
    now = datetime.now(timezone.utc)
    item = evidence_repository.register_evidence(
        entity_id=None, evidence_type="EMAIL", source_id=identity.generate_id(),
        observed_at=now, received_at=now, content_hash=hashlib.sha256(b"x").hexdigest(), mime_type="message/rfc822",
        size_bytes=1, metadata={"sender_address": "other@random.example", "subject": "no body at all"},
    )

    manifest_file = _write_manifest(tmp_path, [
        {"evidence_id": item.evidence_id, "expected_document_type": "RECEIPT", "label_family": "ai"},
    ])
    items = evl.load_manifest(manifest_file)

    report = evl.run_evaluation(
        items=items, manifest_file=manifest_file,
        **_deps(evidence_repository, rule_repository, classification_repository, ai_invocation_repository,
                 litellm_client, object_store, audit_repository, needs_you_repository),
    )
    assert len(report["context_unsupported"]) == 1
    assert report["overall"]["total"] == 0


# ---------------------------------------------------------------------
# Manifest loading validation
# ---------------------------------------------------------------------


def test_load_manifest_rejects_empty_array(tmp_path):
    path = tmp_path / "empty.json"
    path.write_text("[]", encoding="utf-8")
    with pytest.raises(SystemExit):
        evl.load_manifest(str(path))


def test_load_manifest_rejects_entries_missing_required_fields(tmp_path):
    path = tmp_path / "bad.json"
    path.write_text(json.dumps([{"evidence_id": "e1"}]), encoding="utf-8")
    with pytest.raises(SystemExit):
        evl.load_manifest(str(path))


# ---------------------------------------------------------------------
# Sequential load — 20 calls, zero failures/crashes
# ---------------------------------------------------------------------


def test_twenty_sequential_evaluator_calls_against_distinct_items_have_zero_failures(
    evidence_repository, rule_repository, classification_repository, ai_invocation_repository, litellm_client,
    object_store, audit_repository, needs_you_repository, tmp_path,
):
    entries = []
    for i in range(20):
        item = _register_email(evidence_repository, object_store, sender=f"vendor{i}@random.example", subject=f"subject {i}")
        _queue_ai_success(litellm_client, proposed_type="RECEIPT")
        entries.append({"evidence_id": item.evidence_id, "expected_document_type": "RECEIPT", "label_family": "load"})

    manifest_file = _write_manifest(tmp_path, entries)
    items = evl.load_manifest(manifest_file)

    report = evl.run_evaluation(
        items=items, manifest_file=manifest_file,
        **_deps(evidence_repository, rule_repository, classification_repository, ai_invocation_repository,
                 litellm_client, object_store, audit_repository, needs_you_repository),
    )
    assert len(report["items"]) == 20
    assert report["overall"]["total"] == 20
    assert report["overall"]["correct"] == 20
    assert report["ai_invocation_failures"] == []
