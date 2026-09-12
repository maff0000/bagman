"""Contract tests for `contracts/source/bagman.source.v1.schema.json`
(PID §31, part of "External references" — source identity)."""
from __future__ import annotations

import pytest

from core.contract_validation import validate_against_contract
from core.errors import ValidationError

SCHEMA = "source/bagman.source.v1.schema.json"


def test_valid_source_is_accepted(make_source):
    validate_against_contract(make_source(), SCHEMA)


def test_source_missing_required_field_is_rejected(make_source):
    instance = make_source()
    del instance["provider"]  # required per the schema
    with pytest.raises(ValidationError):
        validate_against_contract(instance, SCHEMA)


def test_source_wrong_type_is_rejected(make_source):
    instance = make_source(status=123)  # must be a string, not an int
    with pytest.raises(ValidationError):
        validate_against_contract(instance, SCHEMA)


def test_source_type_bad_pattern_is_rejected(make_source):
    # source_type must match ^[A-Z][A-Z_]*$ — lowercase is rejected.
    instance = make_source(source_type="mailbox")
    with pytest.raises(ValidationError):
        validate_against_contract(instance, SCHEMA)


def test_governed_entity_hint_null_is_accepted(make_source):
    """PID §9: `governed_entity_hint` is a HINT only, and `null` means
    "no hint available" — a legitimate, explicit state."""
    instance = make_source(governed_entity_hint=None)
    validate_against_contract(instance, SCHEMA)
