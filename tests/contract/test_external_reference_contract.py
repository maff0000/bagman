"""Contract tests for
`contracts/source/bagman.external_reference.v1.schema.json` (PID §31
"External references")."""
from __future__ import annotations

import pytest

from core.contract_validation import validate_against_contract
from core.errors import ValidationError

SCHEMA = "source/bagman.external_reference.v1.schema.json"


def test_valid_external_reference_is_accepted(make_external_reference):
    validate_against_contract(make_external_reference(), SCHEMA)


def test_external_reference_missing_required_field_is_rejected(make_external_reference):
    instance = make_external_reference()
    del instance["external_id"]  # required per the schema
    with pytest.raises(ValidationError):
        validate_against_contract(instance, SCHEMA)


def test_external_reference_wrong_type_is_rejected(make_external_reference):
    instance = make_external_reference(provider=12345)  # must be a string
    with pytest.raises(ValidationError):
        validate_against_contract(instance, SCHEMA)


def test_canonical_and_external_identity_are_separated_with_different_constraints(
    make_external_reference,
):
    """PID §10: canonical BAGMAN identity and external provider identity
    must be represented separately and never conflated. Prove the two
    fields exist and are genuinely different in shape/constraint:

    * `external_id` is an arbitrary, provider-native string (no BAGMAN
      identifier shape constraint at all) — an obviously non-UUID,
      provider-flavoured string is perfectly valid here.
    * `canonical_object_id` MUST be a BAGMAN canonical identifier (the
      opaque UUIDv7 shape via `$ref` to
      `bagman.identifier.v1.schema.json`) — the same arbitrary string
      is rejected there.
    """
    provider_native_value = "urn:starling:transaction:2026-09-12:SYN-000123-not-a-uuid"

    # Valid as `external_id`: no canonical-identifier shape is imposed.
    as_external_id = make_external_reference(external_id=provider_native_value)
    validate_against_contract(as_external_id, SCHEMA)

    # The SAME arbitrary string is rejected as `canonical_object_id`,
    # which requires the opaque BAGMAN identifier shape.
    as_canonical_object_id = make_external_reference(
        canonical_object_id=provider_native_value
    )
    with pytest.raises(ValidationError):
        validate_against_contract(as_canonical_object_id, SCHEMA)


def test_external_reference_requires_both_canonical_object_id_and_external_id(
    make_external_reference,
):
    without_canonical = make_external_reference()
    del without_canonical["canonical_object_id"]
    with pytest.raises(ValidationError):
        validate_against_contract(without_canonical, SCHEMA)

    without_external = make_external_reference()
    del without_external["external_id"]
    with pytest.raises(ValidationError):
        validate_against_contract(without_external, SCHEMA)
