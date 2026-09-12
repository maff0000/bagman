"""Contract tests for
`contracts/provenance/bagman.provenance.v1.schema.json` (PID §31
"Provenance")."""
from __future__ import annotations

import pytest

from core.contract_validation import validate_against_contract
from core.errors import ValidationError

SCHEMA = "provenance/bagman.provenance.v1.schema.json"


def test_valid_provenance_is_accepted(make_provenance):
    validate_against_contract(make_provenance(), SCHEMA)


def test_invalid_relationship_enum_value_is_rejected(make_provenance):
    instance = make_provenance(relationship="FRIENDS_WITH")  # not in the closed enum
    with pytest.raises(ValidationError):
        validate_against_contract(instance, SCHEMA)


def test_provenance_missing_required_field_is_rejected(make_provenance):
    instance = make_provenance()
    del instance["evidence_id"]  # required per the schema
    with pytest.raises(ValidationError):
        validate_against_contract(instance, SCHEMA)


def test_orphan_provenance_is_a_domain_concern_not_a_schema_concern(
    make_provenance, new_id
):
    """PID §31 lists "invalid/orphan provenance rejected" as a required
    proof — but a raw JSON Schema instance validator has no concept of
    "which `evidence_id`s currently exist in the repository". It can
    only check that `evidence_id` has the SHAPE of a canonical BAGMAN
    identifier, not that the identifier resolves to a real record.

    A provenance instance that references an `evidence_id` nobody has
    ever registered is still a perfectly well-formed instance, so the
    raw schema ACCEPTS it here. Rejecting a genuine orphan reference is
    `core/provenance.py`'s `InMemoryProvenanceRepository` job
    (`InvalidProvenanceError`, by looking the id up in the
    `EvidenceRepository` it was constructed with) — proven at the
    domain layer in
    `tests/integration/test_domain_and_lineage.py::test_orphan_provenance_is_rejected_at_the_domain_layer`,
    not here.
    """
    nonexistent_evidence_id = new_id()  # well-formed, but never registered anywhere
    instance = make_provenance(evidence_id=nonexistent_evidence_id)
    validate_against_contract(instance, SCHEMA)  # accepted: shape-valid, orphan-ness is undetectable here


def test_multiple_evidence_sources_are_multiple_provenance_records(
    make_provenance, new_id
):
    """PID §11/§31: where a subject has multiple evidence sources, that
    is represented as multiple `Provenance` records (one per
    contributing evidence item), never as an array of evidence ids on
    one record. Prove two independently-valid records can share a
    `subject_id` while pointing at two different `evidence_id`s."""
    shared_subject_id = new_id()
    first = make_provenance(subject_id=shared_subject_id, evidence_id=new_id())
    second = make_provenance(subject_id=shared_subject_id, evidence_id=new_id())

    assert first["evidence_id"] != second["evidence_id"]
    validate_against_contract(first, SCHEMA)
    validate_against_contract(second, SCHEMA)
