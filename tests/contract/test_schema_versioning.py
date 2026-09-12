"""Cross-cutting contract tests: schema identity/versioning (PID §31
"Versioning" — "contracts carry schema version" / "incompatible
structures fail validation").

Unlike the per-domain files in this directory, these tests read the
schema JSON files directly (to check their own self-declared identity)
in addition to using `validate_against_contract` (to prove
`additionalProperties: false` actually bites for every domain schema).
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from core.contract_validation import validate_against_contract
from core.errors import ValidationError

_CONTRACTS_ROOT = Path(__file__).resolve().parent.parent.parent / "contracts"

_ALL_SCHEMA_FILES = sorted(_CONTRACTS_ROOT.rglob("*.schema.json"))

_DOMAIN_SCHEMAS = [
    ("entity/bagman.entity.v1.schema.json", "make_entity"),
    ("evidence/bagman.evidence.v1.schema.json", "make_evidence"),
    ("source/bagman.source.v1.schema.json", "make_source"),
    ("source/bagman.external_reference.v1.schema.json", "make_external_reference"),
    ("provenance/bagman.provenance.v1.schema.json", "make_provenance"),
    ("audit/bagman.audit_event.v1.schema.json", "make_audit_event"),
]


def test_at_least_the_six_domain_schemas_plus_three_common_primitives_exist():
    assert len(_ALL_SCHEMA_FILES) >= 9, (
        f"expected >= 9 *.schema.json files under contracts/, found "
        f"{len(_ALL_SCHEMA_FILES)}: {[p.name for p in _ALL_SCHEMA_FILES]}"
    )


def test_every_schema_files_own_id_reflects_its_v1_filename():
    """PID §17/§31: contracts carry explicit schema/version identity.
    Every `*.schema.json` file's filename carries a `.v1.` version
    segment, and that file's own `$id` ends with exactly that
    filename — proving the file's declared identity and its on-disk
    location/version agree."""
    for path in _ALL_SCHEMA_FILES:
        assert ".v1." in path.name, f"{path.name}: filename has no 'v1' version segment"
        data = json.loads(path.read_text(encoding="utf-8"))
        assert data["$id"].endswith(path.name), (
            f"{path.name}: $id {data['$id']!r} does not end with this file's own name"
        )


def test_audit_event_schema_version_const_pins_v1():
    path = _CONTRACTS_ROOT / "audit" / "bagman.audit_event.v1.schema.json"
    data = json.loads(path.read_text(encoding="utf-8"))
    const = data["properties"]["schema_version"]["const"]
    assert const == "bagman.audit_event.v1"


@pytest.mark.parametrize("schema, fixture_name", _DOMAIN_SCHEMAS)
def test_additional_properties_false_actually_bites(request, schema, fixture_name):
    """Simulates a hypothetical incompatible/newer shape: an instance
    that is otherwise fully valid except for one extra top-level field
    the schema doesn't know about. Every CD-2 domain schema declares
    `additionalProperties: false` at its root, so this must be
    REJECTED, not silently accepted — proving a structurally
    incompatible instance genuinely fails validation rather than being
    tolerated by an overly-permissive schema."""
    make = request.getfixturevalue(fixture_name)
    instance = make(unexpected_v2_only_field="simulates an incompatible newer shape")
    with pytest.raises(ValidationError):
        validate_against_contract(instance, schema)


@pytest.mark.parametrize("schema, fixture_name", _DOMAIN_SCHEMAS)
def test_baseline_valid_instance_still_accepted(request, schema, fixture_name):
    """Sanity check for the parametrization itself: the same six
    factories, with no extra field, must still validate — otherwise
    the rejection above could be a false positive caused by some other
    unrelated defect in the fixture."""
    make = request.getfixturevalue(fixture_name)
    validate_against_contract(make(), schema)
