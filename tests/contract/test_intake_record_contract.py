"""Contract tests for `contracts/intake/bagman.intake_record.v1.schema.json`
(CD-4 WI-1, PID §8/§68).
"""
from __future__ import annotations

import pytest

from core.contract_validation import validate_against_contract
from core.errors import ValidationError

SCHEMA = "intake/bagman.intake_record.v1.schema.json"


def test_valid_intake_record_is_accepted(make_intake_record):
    validate_against_contract(make_intake_record(), SCHEMA)


def test_intake_record_missing_required_field_is_rejected(make_intake_record):
    instance = make_intake_record()
    del instance["status"]  # required per the schema
    with pytest.raises(ValidationError):
        validate_against_contract(instance, SCHEMA)


def test_intake_record_unknown_additional_property_is_rejected(make_intake_record):
    instance = make_intake_record()
    instance["not_a_real_field"] = "nope"
    with pytest.raises(ValidationError):
        validate_against_contract(instance, SCHEMA)


@pytest.mark.parametrize(
    "valid_status",
    ["RECEIVED", "VALIDATING", "QUARANTINED", "REJECTED", "ACCEPTED", "REGISTERED", "FAILED"],
)
def test_every_pid_section_7_status_value_is_accepted(make_intake_record, valid_status):
    instance = make_intake_record(status=valid_status)
    validate_against_contract(instance, SCHEMA)


def test_status_is_a_closed_enum_rejecting_unknown_values(make_intake_record):
    """Unlike `EvidenceItem.status` (deliberately open), `IntakeRecord.status`
    is a closed enum (PID §7's exact, non-extensible state machine)."""
    instance = make_intake_record(status="SOMETHING_MADE_UP")
    with pytest.raises(ValidationError):
        validate_against_contract(instance, SCHEMA)


def test_entity_hint_null_unresolved_is_accepted(make_intake_record):
    instance = make_intake_record(entity_hint=None)
    validate_against_contract(instance, SCHEMA)


def test_entity_hint_missing_key_is_rejected(make_intake_record):
    """`entity_hint` is required-but-nullable — same doctrine as
    `bagman.evidence.v1`'s `entity_id`: the KEY must always be present."""
    instance = make_intake_record()
    del instance["entity_hint"]
    with pytest.raises(ValidationError):
        validate_against_contract(instance, SCHEMA)


def test_completed_at_null_while_non_terminal_is_accepted(make_intake_record):
    instance = make_intake_record(status="RECEIVED", completed_at=None)
    validate_against_contract(instance, SCHEMA)


def test_completed_at_valid_timestamp_is_accepted(make_intake_record, now_str):
    instance = make_intake_record(status="REGISTERED", completed_at=now_str())
    validate_against_contract(instance, SCHEMA)


def test_content_hash_null_is_accepted(make_intake_record):
    instance = make_intake_record(content_hash=None)
    validate_against_contract(instance, SCHEMA)


def test_content_hash_valid_object_is_accepted(make_intake_record):
    instance = make_intake_record(content_hash={"algorithm": "SHA-256", "value": "a" * 64})
    validate_against_contract(instance, SCHEMA)


def test_content_hash_wrong_shape_is_rejected(make_intake_record):
    instance = make_intake_record(content_hash={"algorithm": "SHA-256", "value": "too-short"})
    with pytest.raises(ValidationError):
        validate_against_contract(instance, SCHEMA)


def test_evidence_id_null_before_registration_is_accepted(make_intake_record):
    instance = make_intake_record(status="ACCEPTED", evidence_id=None)
    validate_against_contract(instance, SCHEMA)


def test_evidence_id_set_on_registered_is_accepted(make_intake_record, new_id):
    instance = make_intake_record(status="REGISTERED", evidence_id=new_id(), completed_at=None)
    # completed_at may independently be null or set; only evidence_id's
    # shape is under test here.
    instance["completed_at"] = None
    validate_against_contract(instance, SCHEMA)


def test_schema_version_must_equal_the_pinned_const(make_intake_record):
    instance = make_intake_record(schema_version="bagman.intake_record.v2")
    with pytest.raises(ValidationError):
        validate_against_contract(instance, SCHEMA)


def test_idempotency_key_null_is_accepted(make_intake_record):
    instance = make_intake_record(idempotency_key=None)
    validate_against_contract(instance, SCHEMA)


def test_idempotency_key_string_is_accepted(make_intake_record):
    instance = make_intake_record(idempotency_key="synthetic-idempotency-key-0001")  # gitleaks:allow
    validate_against_contract(instance, SCHEMA)


def test_size_bytes_null_is_accepted(make_intake_record):
    instance = make_intake_record(size_bytes=None)
    validate_against_contract(instance, SCHEMA)


def test_size_bytes_negative_is_rejected(make_intake_record):
    instance = make_intake_record(size_bytes=-1)
    with pytest.raises(ValidationError):
        validate_against_contract(instance, SCHEMA)
