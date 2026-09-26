"""CD-6 Slice 5 WI-3 §10/§45 — task-versioning proofs: `DOCUMENT_TYPE_PROPOSAL`
v1 stays registered/unchanged, v2 is registered additively, prompt
resolution is `(task_id, task_version)`-aware, and every other
registered background task still resolves its own existing prompt
version correctly."""
from __future__ import annotations

import pytest

from ai.prompts.loader import load_system_prompt, resolve_prompt_contract_version
from ai.tasks import (
    DOCUMENT_TYPE_PROPOSAL_V2_CANONICAL_TYPES,
    TASK_REGISTRY,
    get_task_contract,
    validate_task_output,
)
from core.errors import NotFoundError
from services.evidence.classification import DOCUMENT_TYPES


# ---------------------------------------------------------------------
# §45 — v1 still registered, unchanged; v2 registered separately
# ---------------------------------------------------------------------


def test_document_type_proposal_v1_still_registered_and_unchanged():
    contract = get_task_contract("DOCUMENT_TYPE_PROPOSAL", 1)
    assert contract.task_version == 1
    assert contract.preferred_capability == "bagman-fast"
    assert contract.output_schema["properties"]["proposed_type"]["pattern"] == "^[A-Z][A-Z_]*$"
    assert "enum" not in contract.output_schema["properties"]["proposed_type"]


def test_document_type_proposal_v2_registered_separately():
    contract = get_task_contract("DOCUMENT_TYPE_PROPOSAL", 2)
    assert contract.task_version == 2
    assert contract.task_id == "DOCUMENT_TYPE_PROPOSAL"
    assert contract.role == "BACKGROUND"
    assert contract.preferred_capability == "bagman-core"
    assert contract.data_policy == "LOCAL_OK"


def test_both_document_type_proposal_versions_coexist_in_registry():
    assert ("DOCUMENT_TYPE_PROPOSAL", 1) in TASK_REGISTRY
    assert ("DOCUMENT_TYPE_PROPOSAL", 2) in TASK_REGISTRY
    assert TASK_REGISTRY[("DOCUMENT_TYPE_PROPOSAL", 1)] is not TASK_REGISTRY[("DOCUMENT_TYPE_PROPOSAL", 2)]


# ---------------------------------------------------------------------
# §5/§6 — v2's closed enum vocabulary, exactly Slice-5 V1's own set
# ---------------------------------------------------------------------


def test_v2_canonical_vocabulary_matches_slice5_v1_document_types_exactly():
    assert set(DOCUMENT_TYPE_PROPOSAL_V2_CANONICAL_TYPES) == set(DOCUMENT_TYPES)


def test_v2_output_schema_proposed_type_is_a_closed_enum():
    contract = get_task_contract("DOCUMENT_TYPE_PROPOSAL", 2)
    proposed_type_schema = contract.output_schema["properties"]["proposed_type"]
    assert proposed_type_schema["type"] == "string"
    assert set(proposed_type_schema["enum"]) == set(DOCUMENT_TYPE_PROPOSAL_V2_CANONICAL_TYPES)
    assert "pattern" not in proposed_type_schema


@pytest.mark.parametrize(
    "proposed_type",
    [
        "SUPPLIER_INVOICE", "RECEIPT", "ORDER_CONFIRMATION", "REFUND_CONFIRMATION",
        "BROKER_STATEMENT", "BROKER_ACTIVITY_NOTICE", "NON_ACCOUNTING_DOCUMENT", "UNKNOWN",
    ],
)
def test_v2_output_accepts_every_canonical_type(proposed_type):
    contract = get_task_contract("DOCUMENT_TYPE_PROPOSAL", 2)
    result = validate_task_output(
        contract, {"proposed_type": proposed_type, "confidence": 0.7, "signals": [], "warnings": []}
    )
    assert result.valid is True


@pytest.mark.parametrize("generic_value", ["INVOICE", "STATEMENT", "CONTRACT", "BILL", "OTHER", "RANDOM_MADE_UP_TYPE"])
def test_v2_output_rejects_generic_or_non_canonical_values(generic_value):
    contract = get_task_contract("DOCUMENT_TYPE_PROPOSAL", 2)
    result = validate_task_output(
        contract, {"proposed_type": generic_value, "confidence": 0.7, "signals": [], "warnings": []}
    )
    assert result.valid is False


def test_v2_output_bounds_signals_and_warnings_item_count():
    contract = get_task_contract("DOCUMENT_TYPE_PROPOSAL", 2)
    too_many = [f"signal {i}" for i in range(11)]
    result = validate_task_output(
        contract, {"proposed_type": "RECEIPT", "confidence": 0.5, "signals": too_many, "warnings": []}
    )
    assert result.valid is False


def test_v2_output_bounds_individual_signal_string_length():
    contract = get_task_contract("DOCUMENT_TYPE_PROPOSAL", 2)
    too_long = "x" * 301
    result = validate_task_output(
        contract, {"proposed_type": "RECEIPT", "confidence": 0.5, "signals": [too_long], "warnings": []}
    )
    assert result.valid is False


def test_v2_output_rejects_additional_properties():
    contract = get_task_contract("DOCUMENT_TYPE_PROPOSAL", 2)
    result = validate_task_output(
        contract,
        {
            "proposed_type": "RECEIPT", "confidence": 0.5, "signals": [], "warnings": [],
            "raw_chain_of_thought": "let me think...",
        },
    )
    assert result.valid is False


def test_v2_input_schema_requires_full_provenance_shape():
    contract = get_task_contract("DOCUMENT_TYPE_PROPOSAL", 2)
    assert set(contract.input_schema["required"]) == {
        "evidence_id", "evidence_content_hash", "classification_context_version",
        "classification_context_hash", "classifier_fingerprint",
    }
    assert contract.input_schema["additionalProperties"] is False


# ---------------------------------------------------------------------
# §10/§45 — task-version-aware prompt resolution
# ---------------------------------------------------------------------


def test_v1_resolves_prompt_v1():
    assert resolve_prompt_contract_version("DOCUMENT_TYPE_PROPOSAL", 1) == "v1"


def test_v2_resolves_prompt_v2():
    assert resolve_prompt_contract_version("DOCUMENT_TYPE_PROPOSAL", 2) == "v2"


def test_v1_and_v2_resolve_to_different_prompt_assets():
    v1_text = load_system_prompt("DOCUMENT_TYPE_PROPOSAL", resolve_prompt_contract_version("DOCUMENT_TYPE_PROPOSAL", 1))
    v2_text = load_system_prompt("DOCUMENT_TYPE_PROPOSAL", resolve_prompt_contract_version("DOCUMENT_TYPE_PROPOSAL", 2))
    assert v1_text != v2_text
    assert "SUPPLIER_INVOICE" in v2_text
    assert "BROKER_ACTIVITY_NOTICE" in v2_text


def test_unregistered_task_version_pair_raises_not_found():
    with pytest.raises(NotFoundError):
        resolve_prompt_contract_version("DOCUMENT_TYPE_PROPOSAL", 99)


@pytest.mark.parametrize(
    "task_id,task_version,expected_prompt_version",
    [
        ("DOCUMENT_SUMMARY", 1, "v1"),
        ("ENTITY_PROPOSAL", 1, "v1"),
    ],
)
def test_every_other_background_task_still_resolves_its_existing_prompt_version(task_id, task_version, expected_prompt_version):
    assert resolve_prompt_contract_version(task_id, task_version) == expected_prompt_version
    # And the asset genuinely loads.
    text = load_system_prompt(task_id, expected_prompt_version)
    assert task_id in text
