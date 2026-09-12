"""Contract tests for `contracts/entity/bagman.entity.v1.schema.json`
(PID §31 "Entity identity").
"""
from __future__ import annotations

import pytest

from core.contract_validation import validate_against_contract
from core.errors import ValidationError

SCHEMA = "entity/bagman.entity.v1.schema.json"


def test_valid_entity_is_accepted(make_entity):
    validate_against_contract(make_entity(), SCHEMA)


def test_entity_missing_required_field_is_rejected(make_entity):
    instance = make_entity()
    del instance["status"]  # required per the schema
    with pytest.raises(ValidationError):
        validate_against_contract(instance, SCHEMA)


def test_entity_wrong_type_is_rejected(make_entity):
    instance = make_entity(display_name=12345)  # must be a string, not an int
    with pytest.raises(ValidationError):
        validate_against_contract(instance, SCHEMA)


def test_entity_id_bad_pattern_is_rejected(make_entity):
    # entity_id must match the canonical UUIDv7 shape, not an arbitrary string.
    instance = make_entity(entity_id="not-a-canonical-identifier")
    with pytest.raises(ValidationError):
        validate_against_contract(instance, SCHEMA)


def test_unsupported_entity_type_is_accepted_not_rejected(make_entity):
    """PID §4: `entity_type` is deliberately open (a pattern string, not
    a closed `enum`) so new entity types (e.g. `TRUST`, `PARTNERSHIP`,
    `OTHER`, or anything else not yet anticipated) can be introduced
    without a schema redesign. A well-formed but unfamiliar value —
    neither of the two documented `COMPANY`/`PERSON` values — must be
    ACCEPTED by the raw schema: this is deliberate extensibility, not
    malformed input, and must not be conflated with the "malformed
    entity rejected" tests above.

    The domain layer agrees: `core/entity.py`'s `EntityRepository` does
    not restrict `entity_type` to a closed set either (only
    `EvidenceItem.status` is narrowed to a closed set, at the service
    layer, per WI-2's `services/evidence/evidence.py` `STATUSES`
    constant) — there is no second, stricter gate hiding behind this
    one. See `tests/integration/test_domain_and_lineage.py` for the
    domain-layer proof that entities register successfully end-to-end.
    """
    instance = make_entity(entity_type="SPACE_TRUST")
    validate_against_contract(instance, SCHEMA)  # accepted, not rejected


def test_schema_alone_cannot_enforce_immutability_across_writes(make_entity, new_id):
    """PID §31's "immutable canonical identifier semantics" and PID §7's
    broader immutability doctrine are DOMAIN concerns — enforced by
    `core/entity.py`'s `EntityRepository`, which rejects re-registering
    an already-used `entity_id` with `ImmutabilityViolationError` (see
    `tests/integration/test_domain_and_lineage.py`) — not something a
    single JSON Schema instance validation can express on its own. A
    schema validates one instance in isolation; it has no memory of any
    previously-validated instance, so it has no way to detect that
    `entity_id` X's `canonical_name` "changed" between two hypothetical
    writes.

    Demonstrate this concretely: two structurally-valid entity dicts
    sharing the same `entity_id` but different `canonical_name` /
    `display_name` both independently pass raw-schema validation — the
    schema has no mechanism to flag the second as an illegitimate
    mutation of the first. This is not a bug in the schema; it is the
    documented boundary between what a JSON Schema can prove and what
    the repository layer must.
    """
    shared_id = new_id()
    first = make_entity(entity_id=shared_id, canonical_name="ORIGINAL_NAME")
    second = make_entity(entity_id=shared_id, canonical_name="MUTATED_NAME")

    validate_against_contract(first, SCHEMA)
    validate_against_contract(second, SCHEMA)  # schema alone permits this "mutation"
