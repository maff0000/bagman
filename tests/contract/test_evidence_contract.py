"""Contract tests for `contracts/evidence/bagman.evidence.v1.schema.json`
(PID §31 "Evidence").
"""
from __future__ import annotations

import pytest

from core.contract_validation import validate_against_contract
from core.errors import ValidationError

SCHEMA = "evidence/bagman.evidence.v1.schema.json"


def test_valid_evidence_is_accepted(make_evidence):
    validate_against_contract(make_evidence(), SCHEMA)


def test_evidence_missing_required_field_is_rejected(make_evidence):
    instance = make_evidence()
    del instance["mime_type"]  # required per the schema
    with pytest.raises(ValidationError):
        validate_against_contract(instance, SCHEMA)


def test_evidence_wrong_type_is_rejected(make_evidence):
    instance = make_evidence(size_bytes="not-an-integer")  # must be an integer
    with pytest.raises(ValidationError):
        validate_against_contract(instance, SCHEMA)


def test_evidence_entity_id_null_unresolved_is_accepted(make_evidence):
    """PID §4's entity isolation invariant: unresolved ownership is a
    first-class, explicit state. `entity_id: null` must be ACCEPTED."""
    instance = make_evidence(entity_id=None)
    validate_against_contract(instance, SCHEMA)


def test_evidence_missing_entity_id_key_is_rejected(make_evidence):
    """`entity_id` is required-but-nullable: the KEY must always be
    present (explicit `null` for "unresolved"), precisely so a caller
    cannot silently omit entity resolution. Omitting the key entirely
    must be REJECTED, distinct from supplying an explicit `null`."""
    instance = make_evidence()
    del instance["entity_id"]
    with pytest.raises(ValidationError):
        validate_against_contract(instance, SCHEMA)


def test_valid_content_hash_is_accepted(make_evidence):
    instance = make_evidence(
        content_hash={"algorithm": "SHA-256", "value": "f" * 64}
    )
    validate_against_contract(instance, SCHEMA)


def test_content_hash_wrong_length_is_rejected(make_evidence):
    instance = make_evidence(
        content_hash={"algorithm": "SHA-256", "value": "abc123"}  # far too short
    )
    with pytest.raises(ValidationError):
        validate_against_contract(instance, SCHEMA)


def test_content_hash_non_hex_is_rejected(make_evidence):
    instance = make_evidence(
        content_hash={"algorithm": "SHA-256", "value": "g" * 64}  # 'g' is not hex
    )
    with pytest.raises(ValidationError):
        validate_against_contract(instance, SCHEMA)


def test_identical_content_hash_across_distinct_evidence_instances_both_validate(
    make_evidence, new_id
):
    """PID §8: content identity is not evidence identity. Two
    EvidenceItem instances may legitimately share the same
    `content_hash` (e.g. the same PDF invoice received via two
    different mailboxes) while remaining two distinct observations.
    Prove the raw schema does not conflate the two by validating two
    instances that share a `content_hash` but have different
    `evidence_id`/`source_id` — both must independently validate; the
    schema has no uniqueness constraint tying `content_hash` to a
    single `evidence_id`.
    """
    shared_hash = {"algorithm": "SHA-256", "value": "c" * 64}
    first = make_evidence(content_hash=shared_hash, source_id=new_id())
    second = make_evidence(content_hash=shared_hash, source_id=new_id())

    assert first["evidence_id"] != second["evidence_id"]
    assert first["source_id"] != second["source_id"]
    assert first["content_hash"] == second["content_hash"]

    validate_against_contract(first, SCHEMA)
    validate_against_contract(second, SCHEMA)
