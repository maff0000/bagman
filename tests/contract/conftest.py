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
