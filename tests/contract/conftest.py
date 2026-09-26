"""Shared instance-factory fixtures for `tests/contract/` (PID §31).

Every fixture here builds a plain, contract-conformant *dict* — never a
`core`/`services` domain object (`GovernedEntity`, `EvidenceItem`, ...)
and never goes through a repository's `register_*`/`record_*` method.
That is deliberate: these tests validate the RAW JSON Schema contracts
under `contracts/` directly, via
`core.contract_validation.validate_against_contract` (the same
validation machinery `core`/`services` use internally — Draft 2020-12,
`$ref` cross-references resolved against the real `contracts/` tree,
`FormatChecker` wired so `format: date-time` is actually enforced) —
but never via `core/entity.py`, `services/evidence/evidence.py`, or any
other domain/repository class. Domain-layer behaviour (immutability
enforcement, idempotent replay, orphan-provenance rejection, etc.) is
proven separately in `tests/integration/`.

`core.identity.generate_id()` and `core.timestamps` are used here only
as small, dependency-free *value generators* (a valid UUIDv7 string, a
valid RFC 3339 UTC string) — not as part of the thing under test.
"""
from __future__ import annotations

from typing import Any, Callable

import pytest

from core import identity, timestamps


@pytest.fixture
def new_id() -> Callable[[], str]:
    """Factory returning a fresh, contract-conformant canonical identifier."""
    return identity.generate_id


@pytest.fixture
def now_str() -> Callable[[], str]:
    """Factory returning a fresh, contract-conformant UTC timestamp string."""
    return lambda: timestamps.to_contract_string(timestamps.utc_now())


@pytest.fixture
def make_entity(new_id, now_str) -> Callable[..., dict]:
    """Factory for a minimal valid `bagman.entity.v1` instance dict."""

    def _make(**overrides: Any) -> dict:
        instance = {
            "entity_id": new_id(),
            "entity_type": "COMPANY",
            "canonical_name": "EXAMPLE_SYSTEMS_LTD",
            "display_name": "Example Systems Ltd",
            "status": "ACTIVE",
            "created_at": now_str(),
            "fiscal_year_start_month_day": None,
            "historical_floor_override_at": None,
            "metadata": {},
        }
        instance.update(overrides)
        return instance

    return _make


@pytest.fixture
def make_source(new_id) -> Callable[..., dict]:
    """Factory for a minimal valid `bagman.source.v1` instance dict."""

    def _make(**overrides: Any) -> dict:
        instance = {
            "source_id": new_id(),
            "source_type": "MANUAL_UPLOAD",
            "provider": "INTERNAL",
            "status": "ACTIVE",
            "governed_entity_hint": None,
            "metadata": {},
        }
        instance.update(overrides)
        return instance

    return _make


@pytest.fixture
def make_external_reference(new_id, now_str) -> Callable[..., dict]:
    """Factory for a minimal valid `bagman.external_reference.v1` instance dict."""

    def _make(**overrides: Any) -> dict:
        instance = {
            "external_reference_id": new_id(),
            "provider": "MICROSOFT_GRAPH",
            "resource_type": "EMAIL_MESSAGE",
            "external_id": "AAMkAGI2-synthetic-provider-native-id-0001",
            "canonical_object_type": "EvidenceItem",
            "canonical_object_id": new_id(),
            "source_id": new_id(),
            "first_observed_at": now_str(),
            "metadata": {},
        }
        instance.update(overrides)
        return instance

    return _make


@pytest.fixture
def make_evidence(new_id, now_str) -> Callable[..., dict]:
    """Factory for a minimal valid `bagman.evidence.v1` instance dict."""

    def _make(**overrides: Any) -> dict:
        instance = {
            "evidence_id": new_id(),
            "entity_id": None,
            "evidence_type": "INVOICE",
            "source_id": new_id(),
            "observed_at": now_str(),
            "received_at": now_str(),
            "content_hash": {
                "algorithm": "SHA-256",
                "value": "a" * 64,
            },
            "mime_type": "application/pdf",
            "size_bytes": 1024,
            "status": "OBSERVED",
            "created_at": now_str(),
            "metadata": {},
        }
        instance.update(overrides)
        return instance

    return _make


@pytest.fixture
def make_provenance(new_id, now_str) -> Callable[..., dict]:
    """Factory for a minimal valid `bagman.provenance.v1` instance dict."""

    def _make(**overrides: Any) -> dict:
        instance = {
            "provenance_id": new_id(),
            "subject_type": "EvidenceItem",
            "subject_id": new_id(),
            "evidence_id": new_id(),
            "relationship": "OBSERVED_FROM",
            "transform_id": None,
            "created_at": now_str(),
            "metadata": {},
        }
        instance.update(overrides)
        return instance

    return _make


@pytest.fixture
def make_intake_record(new_id, now_str) -> Callable[..., dict]:
    """Factory for a minimal valid `bagman.intake_record.v1` instance dict
    (CD-4 WI-1)."""

    def _make(**overrides: Any) -> dict:
        instance = {
            "intake_id": new_id(),
            "source_id": new_id(),
            "entity_hint": None,
            "status": "RECEIVED",
            "received_at": now_str(),
            "completed_at": None,
            "original_filename": "synthetic-invoice.pdf",
            "reported_mime_type": "application/pdf",
            "detected_mime_type": None,
            "size_bytes": None,
            "content_hash": None,
            "evidence_id": None,
            "failure_code": None,
            "quarantine_reason": None,
            "correlation_id": new_id(),
            "idempotency_key": None,
            "metadata": {},
            "schema_version": "bagman.intake_record.v1",
        }
        instance.update(overrides)
        return instance

    return _make


@pytest.fixture
def make_ai_invocation(new_id, now_str) -> Callable[..., dict]:
    """Factory for a minimal valid `bagman.ai_invocation.v1` instance
    dict (CD-5 WI-1)."""

    def _make(**overrides: Any) -> dict:
        instance = {
            "ai_invocation_id": new_id(),
            "task_id": "DOCUMENT_TYPE_PROPOSAL",
            "task_version": 1,
            "role": "BACKGROUND",
            "provider": "LITELLM",
            "capability_alias": "bagman-fast",
            "provider_model": None,
            "started_at": now_str(),
            "completed_at": None,
            "status": "REQUESTED",
            "correlation_id": new_id(),
            "actor": {"actor_type": "SYSTEM", "actor_id": "bagman-test-harness"},
            "input_references": {"evidence_id": new_id()},
            "prompt_contract_version": None,
            "output": None,
            "confidence": None,
            "validation_result": None,
            "error_code": None,
            "usage_metadata": {},
            "latency_ms": None,
            "schema_version": "bagman.ai_invocation.v1",
        }
        instance.update(overrides)
        return instance

    return _make


@pytest.fixture
def make_mailbox_domain_rule(new_id, now_str) -> Callable[..., dict]:
    """Factory for a minimal valid `bagman.mailbox_domain_rule.v1`
    instance dict (CD-6 architect amendment; extended by the CD-6
    GUI-operations-foundation follow-on WO's three-state policy model +
    `EXACT_ADDRESS`/`EXACT_DOMAIN_SUBJECT` match modes, and hardened by
    this delivery's own match_mode-conditional `allOf`/`if`/`then`
    schema rules). Defaults to the simplest valid shape — a plain
    domain-level `EXACT`/`BLACKLIST` rule, `sender_address` and both
    subject-predicate fields `None` — callers pass `match_mode`-specific
    overrides (`sender_address`, `subject_predicate_type`,
    `subject_predicate_value`, `policy`, `destination_*`) to build any
    of the other three match-mode shapes."""

    def _make(**overrides: Any) -> dict:
        instance = {
            "rule_id": new_id(),
            "mailbox_id": new_id(),
            "sender_domain": "vendor.com",
            "sender_address": None,
            "match_mode": "EXACT",
            "policy": "BLACKLIST",
            "destination_entity_id": None,
            "destination_mode": None,
            "source": "OPERATOR",
            "processor_hint": None,
            "approved_at": now_str(),
            "created_at": now_str(),
            "updated_at": now_str(),
            "last_seen_at": now_str(),
            "subject_predicate_type": None,
            "subject_predicate_value": None,
            "schema_version": "bagman.mailbox_domain_rule.v1",
        }
        instance.update(overrides)
        return instance

    return _make


@pytest.fixture
def make_evidence_classification(new_id, now_str) -> Callable[..., dict]:
    """Factory for a minimal valid `bagman.evidence_classification.v1`
    instance dict (CD-6 Slice 5 WI-1). Defaults to the simplest valid
    shape — a DETERMINISTIC_RULE-sourced CLASSIFIED SUPPLIER_INVOICE —
    callers pass source-specific overrides to build any of the other
    valid shapes."""

    def _make(**overrides: Any) -> dict:
        instance = {
            "classification_id": new_id(),
            "evidence_id": new_id(),
            "classification_type": "DOCUMENT_TYPE",
            "document_type": "SUPPLIER_INVOICE",
            "status": "CLASSIFIED",
            "source": "DETERMINISTIC_RULE",
            "confidence": None,
            "rule_id": new_id(),
            "ai_invocation_id": None,
            "operator_action_id": None,
            "reason_codes": [],
            "supersedes_classification_id": None,
            "created_at": now_str(),
            "schema_version": "bagman.evidence_classification.v1",
        }
        instance.update(overrides)
        return instance

    return _make


@pytest.fixture
def make_evidence_classification_rule(new_id, now_str) -> Callable[..., dict]:
    """Factory for a minimal valid `bagman.evidence_classification_rule.v1`
    instance dict (CD-6 Slice 5 WI-1)."""

    def _make(**overrides: Any) -> dict:
        instance = {
            "rule_id": new_id(),
            "sender_scope_type": "EXACT_SENDER_DOMAIN",
            "sender_scope_value": "vendor.com",
            "subject_predicate_type": "EXACT",
            "subject_predicate_value": "monthly statement",
            "document_type": "SUPPLIER_INVOICE",
            "status": "ACTIVE",
            "source": "OPERATOR",
            "supersedes_rule_id": None,
            "created_at": now_str(),
            "approved_at": now_str(),
            "retired_at": None,
            "schema_version": "bagman.evidence_classification_rule.v1",
        }
        instance.update(overrides)
        return instance

    return _make


@pytest.fixture
def make_audit_event(new_id, now_str) -> Callable[..., dict]:
    """Factory for a minimal valid `bagman.audit_event.v1` instance dict."""

    def _make(**overrides: Any) -> dict:
        instance = {
            "audit_event_id": new_id(),
            "event_type": "EVIDENCE_OBSERVED",
            "occurred_at": now_str(),
            "actor_type": "SYSTEM",
            "actor_id": "bagman-test-harness",
            "subject_type": "EvidenceItem",
            "subject_id": new_id(),
            "correlation_id": new_id(),
            "causation_id": None,
            "payload": {},
            "schema_version": "bagman.audit_event.v1",
        }
        instance.update(overrides)
        return instance

    return _make
