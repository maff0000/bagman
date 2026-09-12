"""Contract tests for
`contracts/audit/bagman.audit_event.v1.schema.json` (PID §31 "Audit")."""
from __future__ import annotations

import pytest

from core.contract_validation import validate_against_contract
from core.errors import ValidationError

SCHEMA = "audit/bagman.audit_event.v1.schema.json"


def test_valid_audit_event_is_accepted(make_audit_event):
    validate_against_contract(make_audit_event(), SCHEMA)


def test_naive_occurred_at_without_utc_offset_is_rejected(make_audit_event):
    """PID §16/§31: UTC-aware timestamps are mandatory. A naive-looking
    timestamp string with no offset/`Z` must fail `format: date-time`
    validation. This requires a `jsonschema.FormatChecker` with
    `rfc3339-validator` importable — matching
    `core/contract_validation.py`'s own setup (see its module
    docstring): a plain `FormatChecker()` only enforces `date-time` if
    `rfc3339-validator` is installed, which `requirements.txt` pins."""
    instance = make_audit_event(occurred_at="2026-09-12T09:41:07")  # no offset/Z
    with pytest.raises(ValidationError):
        validate_against_contract(instance, SCHEMA)


def test_utc_offset_occurred_at_is_accepted(make_audit_event):
    instance = make_audit_event(occurred_at="2026-09-12T09:41:07Z")
    validate_against_contract(instance, SCHEMA)


def test_missing_actor_id_is_rejected(make_audit_event):
    instance = make_audit_event()
    del instance["actor_id"]  # actor required (PID §14/§31)
    with pytest.raises(ValidationError):
        validate_against_contract(instance, SCHEMA)


def test_invalid_actor_type_enum_value_is_rejected(make_audit_event):
    # PID §14's actor_type is a CLOSED enum, unlike entity_type/source_type/etc.
    instance = make_audit_event(actor_type="ROBOT")
    with pytest.raises(ValidationError):
        validate_against_contract(instance, SCHEMA)


def test_correlation_id_is_required(make_audit_event):
    instance = make_audit_event()
    del instance["correlation_id"]
    with pytest.raises(ValidationError):
        validate_against_contract(instance, SCHEMA)


def test_causation_id_null_for_first_event_in_chain_is_accepted(make_audit_event):
    instance = make_audit_event(causation_id=None)
    validate_against_contract(instance, SCHEMA)


def test_missing_causation_id_key_is_rejected(make_audit_event):
    """`causation_id` is required-but-nullable, matching `entity_id` on
    `EvidenceItem`: the key must always be present (explicit `null` for
    "first event, no cause") — a caller cannot omit causation
    silently."""
    instance = make_audit_event()
    del instance["causation_id"]
    with pytest.raises(ValidationError):
        validate_against_contract(instance, SCHEMA)


def test_wrong_schema_version_is_rejected(make_audit_event):
    # The v1 schema pins schema_version via `const` — a "v2"-shaped
    # value must be rejected outright, never silently accepted.
    instance = make_audit_event(schema_version="bagman.audit_event.v2")
    with pytest.raises(ValidationError):
        validate_against_contract(instance, SCHEMA)
