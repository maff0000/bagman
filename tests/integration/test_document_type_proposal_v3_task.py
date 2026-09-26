"""CD-6 follow-up ("AI classifier boundary correction") — task-contract
and prompt-resolution proofs for `DOCUMENT_TYPE_PROPOSAL` v3, mirroring
`tests/integration/test_document_type_proposal_v2_task.py`'s own v1/v2
proofs exactly: v3 is registered ADDITIVELY (v1/v2 stay registered and
unchanged), reuses v2's own input/output schema shapes VERBATIM
(including the identical closed eight-value canonical vocabulary — no
ninth value, no rename), and resolves its own, distinct prompt asset."""
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
# v3 registered additively; v1/v2 stay registered and unchanged
# ---------------------------------------------------------------------


def test_document_type_proposal_v1_and_v2_still_registered_and_unchanged():
    v1 = get_task_contract("DOCUMENT_TYPE_PROPOSAL", 1)
    assert v1.preferred_capability == "bagman-fast"
    assert "enum" not in v1.output_schema["properties"]["proposed_type"]

    v2 = get_task_contract("DOCUMENT_TYPE_PROPOSAL", 2)
    assert v2.preferred_capability == "bagman-core"
    assert v2.timeout_seconds == 30
    assert v2.data_policy == "LOCAL_OK"


def test_document_type_proposal_v3_registered_separately():
    contract = get_task_contract("DOCUMENT_TYPE_PROPOSAL", 3)
    assert contract.task_version == 3
    assert contract.task_id == "DOCUMENT_TYPE_PROPOSAL"
    assert contract.role == "BACKGROUND"
    assert contract.preferred_capability == "bagman-core"
    assert contract.data_policy == "LOCAL_OK"
    assert contract.timeout_seconds == 30  # explicitly not touched by this delivery


def test_all_three_document_type_proposal_versions_coexist_in_registry():
    assert ("DOCUMENT_TYPE_PROPOSAL", 1) in TASK_REGISTRY
    assert ("DOCUMENT_TYPE_PROPOSAL", 2) in TASK_REGISTRY
    assert ("DOCUMENT_TYPE_PROPOSAL", 3) in TASK_REGISTRY
    v1 = TASK_REGISTRY[("DOCUMENT_TYPE_PROPOSAL", 1)]
    v2 = TASK_REGISTRY[("DOCUMENT_TYPE_PROPOSAL", 2)]
    v3 = TASK_REGISTRY[("DOCUMENT_TYPE_PROPOSAL", 3)]
    assert v1 is not v2 is not v3
    assert v1 is not v3


# ---------------------------------------------------------------------
# v3 reuses v2's input/output schema shapes verbatim — same closed
# eight-value canonical vocabulary, no ninth value, no rename
# ---------------------------------------------------------------------


def test_v3_output_schema_is_identical_to_v2s_own():
    v2 = get_task_contract("DOCUMENT_TYPE_PROPOSAL", 2)
    v3 = get_task_contract("DOCUMENT_TYPE_PROPOSAL", 3)
    assert v3.output_schema == v2.output_schema


def test_v3_input_schema_is_identical_to_v2s_own():
    v2 = get_task_contract("DOCUMENT_TYPE_PROPOSAL", 2)
    v3 = get_task_contract("DOCUMENT_TYPE_PROPOSAL", 3)
    assert v3.input_schema == v2.input_schema


def test_v3_canonical_vocabulary_still_matches_slice5_v1_document_types_exactly():
    assert set(DOCUMENT_TYPE_PROPOSAL_V2_CANONICAL_TYPES) == set(DOCUMENT_TYPES)
    v3 = get_task_contract("DOCUMENT_TYPE_PROPOSAL", 3)
    assert set(v3.output_schema["properties"]["proposed_type"]["enum"]) == set(DOCUMENT_TYPE_PROPOSAL_V2_CANONICAL_TYPES)
    assert len(v3.output_schema["properties"]["proposed_type"]["enum"]) == 8


@pytest.mark.parametrize(
    "proposed_type",
    [
        "SUPPLIER_INVOICE", "RECEIPT", "ORDER_CONFIRMATION", "REFUND_CONFIRMATION",
        "BROKER_STATEMENT", "BROKER_ACTIVITY_NOTICE", "NON_ACCOUNTING_DOCUMENT", "UNKNOWN",
    ],
)
def test_v3_output_accepts_every_canonical_type(proposed_type):
    contract = get_task_contract("DOCUMENT_TYPE_PROPOSAL", 3)
    result = validate_task_output(
        contract, {"proposed_type": proposed_type, "confidence": 0.7, "signals": [], "warnings": []}
    )
    assert result.valid is True


@pytest.mark.parametrize("generic_value", ["INVOICE", "STATEMENT", "CONTRACT", "BILL", "OTHER", "A_NINTH_TYPE"])
def test_v3_output_rejects_generic_or_non_canonical_values(generic_value):
    contract = get_task_contract("DOCUMENT_TYPE_PROPOSAL", 3)
    result = validate_task_output(
        contract, {"proposed_type": generic_value, "confidence": 0.7, "signals": [], "warnings": []}
    )
    assert result.valid is False


# ---------------------------------------------------------------------
# task-version-aware prompt resolution
# ---------------------------------------------------------------------


def test_v3_resolves_prompt_v3():
    assert resolve_prompt_contract_version("DOCUMENT_TYPE_PROPOSAL", 3) == "v3"


def test_v2_and_v3_resolve_to_different_prompt_assets():
    v2_text = load_system_prompt("DOCUMENT_TYPE_PROPOSAL", resolve_prompt_contract_version("DOCUMENT_TYPE_PROPOSAL", 2))
    v3_text = load_system_prompt("DOCUMENT_TYPE_PROPOSAL", resolve_prompt_contract_version("DOCUMENT_TYPE_PROPOSAL", 3))
    assert v2_text != v3_text
    assert "BROKER_ACTIVITY_NOTICE" in v3_text
    assert "NON_ACCOUNTING_DOCUMENT" in v3_text


def test_v3_prompt_contains_the_already_occurred_correction():
    """The whole point of v3: BROKER_ACTIVITY_NOTICE must now explicitly
    require the event to have already happened, and an account
    reference alone must never be sufficient."""
    v3_text = load_system_prompt("DOCUMENT_TYPE_PROPOSAL", "v3")
    assert "ALREADY OCCURRED" in v3_text
    assert "Status: ANNOUNCED" in v3_text
    assert "account number" in v3_text.lower()
    assert "never" in v3_text.lower() and "sufficient" in v3_text.lower()


def test_v3_prompt_preserves_v2s_prompt_injection_defense_paragraph_verbatim():
    """The prompt-injection defense paragraph is untouched — proves this
    is a targeted, surgical correction, not a rewrite."""
    v2_text = load_system_prompt("DOCUMENT_TYPE_PROPOSAL", "v2")
    v3_text = load_system_prompt("DOCUMENT_TYPE_PROPOSAL", "v3")
    injection_paragraph = v2_text.splitlines()[2]  # the "You will receive..." untrusted-data paragraph
    assert injection_paragraph in v3_text.splitlines()


def test_v3_prompt_preserves_every_other_category_definition_verbatim():
    v2_text = load_system_prompt("DOCUMENT_TYPE_PROPOSAL", "v2")
    v3_text = load_system_prompt("DOCUMENT_TYPE_PROPOSAL", "v3")
    for unchanged_category in (
        "### SUPPLIER_INVOICE", "### RECEIPT", "### ORDER_CONFIRMATION",
        "### REFUND_CONFIRMATION", "### BROKER_STATEMENT", "### UNKNOWN",
    ):
        v2_section = v2_text.split(unchanged_category, 1)[1].split("\n\n### ", 1)[0]
        v3_section = v3_text.split(unchanged_category, 1)[1].split("\n\n### ", 1)[0]
        assert v2_section == v3_section, f"{unchanged_category} section changed between v2 and v3"


def test_v3_prompt_preserves_v2s_output_shape_section_verbatim():
    v2_text = load_system_prompt("DOCUMENT_TYPE_PROPOSAL", "v2")
    v3_text = load_system_prompt("DOCUMENT_TYPE_PROPOSAL", "v3")
    v2_output_section = v2_text.split("## Output shape", 1)[1]
    v3_output_section = v3_text.split("## Output shape", 1)[1]
    assert v2_output_section == v3_output_section


def test_unregistered_document_type_proposal_v4_raises_not_found():
    with pytest.raises(NotFoundError):
        resolve_prompt_contract_version("DOCUMENT_TYPE_PROPOSAL", 4)
    with pytest.raises(NotFoundError):
        get_task_contract("DOCUMENT_TYPE_PROPOSAL", 4)
