"""Tests for `ai.tasks` — the task contract/registry framework (CD-5
WI-1, PID §21-24/§55/§76).
"""
from __future__ import annotations

import pytest

from core.errors import NotFoundError
from ai.invocation import BACKGROUND_CAPABILITY_ALIASES
from ai.tasks import (
    DATA_POLICIES,
    TASK_REGISTRY,
    get_task_contract,
    validate_task_output,
)


# ---------------------------------------------------------------------
# Registry resolution (PID §21-22)
# ---------------------------------------------------------------------


@pytest.mark.parametrize(
    "task_id,role,preferred_capability",
    [
        ("DOCUMENT_SUMMARY", "BACKGROUND", "bagman-fast"),
        ("DOCUMENT_TYPE_PROPOSAL", "BACKGROUND", "bagman-fast"),
        ("ENTITY_PROPOSAL", "BACKGROUND", "bagman-core"),
        ("OPERATOR_DOCUMENT_REVIEW", "OPERATOR", None),
    ],
)
def test_every_cd5_initial_task_is_registered_at_version_1(task_id, role, preferred_capability):
    contract = get_task_contract(task_id, 1)
    assert contract.task_id == task_id
    assert contract.role == role
    assert contract.preferred_capability == preferred_capability


def test_task_registry_has_exactly_the_four_pid_section_21_tasks_plus_wi3s_ask_bagman():
    # WI-1 registered the four PID §21 tasks. CD-5 WI-3 additively
    # registers a 5th, ASK_BAGMAN v1 (Ask BAGMAN's general operator
    # chat task — see ai/tasks.py's own docstring for why this is a
    # new task rather than a reuse of OPERATOR_DOCUMENT_REVIEW) — the
    # registry is deliberately OPEN by design (this module's own
    # docstring), so this is an expected, additive registry growth, not
    # a WI-1 regression. CD-6 Slice 5 WI-3 additively registers a 6th
    # entry, DOCUMENT_TYPE_PROPOSAL v2 (the governed AI document
    # classifier) — v1 stays registered and untouched (Slice-5 WI-3
    # §3), the new entry sits alongside it under the SAME task_id at a
    # distinct task_version, exactly the additive growth this test's
    # own name already anticipates. A CD-6 follow-up ("AI classifier
    # boundary correction") additively registers a 7th entry,
    # DOCUMENT_TYPE_PROPOSAL v3 (a targeted BROKER_ACTIVITY_NOTICE/
    # NON_ACCOUNTING_DOCUMENT prompt-wording fix) — v1/v2 stay
    # registered and untouched, same additive pattern.
    assert set(TASK_REGISTRY.keys()) == {
        ("DOCUMENT_SUMMARY", 1),
        ("DOCUMENT_TYPE_PROPOSAL", 1),
        ("DOCUMENT_TYPE_PROPOSAL", 2),
        ("DOCUMENT_TYPE_PROPOSAL", 3),
        ("ENTITY_PROPOSAL", 1),
        ("OPERATOR_DOCUMENT_REVIEW", 1),
        ("ASK_BAGMAN", 1),
    }


def test_unknown_task_id_raises_not_found():
    with pytest.raises(NotFoundError):
        get_task_contract("SOME_MADE_UP_TASK", 1)


def test_known_task_id_wrong_version_raises_not_found():
    with pytest.raises(NotFoundError):
        get_task_contract("DOCUMENT_SUMMARY", 99)


def test_every_background_task_preferred_capability_is_in_the_closed_alias_set():
    for contract in TASK_REGISTRY.values():
        if contract.role == "BACKGROUND":
            assert contract.preferred_capability in BACKGROUND_CAPABILITY_ALIASES


def test_every_operator_task_preferred_capability_is_null():
    for contract in TASK_REGISTRY.values():
        if contract.role == "OPERATOR":
            assert contract.preferred_capability is None


def test_every_task_data_policy_is_in_the_closed_set():
    for contract in TASK_REGISTRY.values():
        assert contract.data_policy in DATA_POLICIES


def test_every_task_has_a_positive_timeout():
    for contract in TASK_REGISTRY.values():
        assert contract.timeout_seconds > 0


# ---------------------------------------------------------------------
# validate_task_output (PID §24/§76)
# ---------------------------------------------------------------------


def test_valid_document_type_proposal_output_is_accepted():
    contract = get_task_contract("DOCUMENT_TYPE_PROPOSAL", 1)
    result = validate_task_output(
        contract,
        {"proposed_type": "INVOICE", "confidence": 0.94, "signals": ["invoice number present"], "warnings": []},
    )
    assert result.valid is True
    assert result.errors == ()


def test_document_type_proposal_output_missing_confidence_is_rejected():
    contract = get_task_contract("DOCUMENT_TYPE_PROPOSAL", 1)
    result = validate_task_output(
        contract,
        {"proposed_type": "INVOICE", "signals": [], "warnings": []},
    )
    assert result.valid is False
    assert len(result.errors) > 0


def test_document_type_proposal_output_confidence_as_string_is_rejected():
    contract = get_task_contract("DOCUMENT_TYPE_PROPOSAL", 1)
    result = validate_task_output(
        contract,
        {"proposed_type": "INVOICE", "confidence": "high", "signals": [], "warnings": []},
    )
    assert result.valid is False


def test_document_type_proposal_output_with_unknown_extra_field_is_rejected():
    contract = get_task_contract("DOCUMENT_TYPE_PROPOSAL", 1)
    result = validate_task_output(
        contract,
        {
            "proposed_type": "INVOICE",
            "confidence": 0.9,
            "signals": [],
            "warnings": [],
            "raw_chain_of_thought": "let me think step by step...",
        },
    )
    assert result.valid is False


def test_validate_task_output_never_raises_on_a_non_object_output():
    """A provider returning something that is not even a JSON object at
    all (PID §76: malformed output) must be reported as data, never
    crash this function."""
    contract = get_task_contract("DOCUMENT_TYPE_PROPOSAL", 1)
    result = validate_task_output(contract, "not even an object")  # type: ignore[arg-type]
    assert result.valid is False
    assert len(result.errors) > 0


def test_valid_entity_proposal_output_is_accepted():
    contract = get_task_contract("ENTITY_PROPOSAL", 1)
    result = validate_task_output(
        contract,
        {"proposed_entity_hint": "NOUSTAI_LIMITED", "confidence": 0.8, "signals": [], "warnings": []},
    )
    assert result.valid is True


def test_entity_proposal_output_allows_null_hint():
    contract = get_task_contract("ENTITY_PROPOSAL", 1)
    result = validate_task_output(
        contract,
        {"proposed_entity_hint": None, "confidence": 0.1, "signals": [], "warnings": ["no confident candidate"]},
    )
    assert result.valid is True


def test_valid_document_summary_output_is_accepted():
    contract = get_task_contract("DOCUMENT_SUMMARY", 1)
    result = validate_task_output(
        contract,
        {"summary": "A one-page invoice from Acme Ltd for consulting services.", "confidence": 0.88, "signals": [], "warnings": []},
    )
    assert result.valid is True


def test_valid_operator_document_review_output_is_accepted():
    contract = get_task_contract("OPERATOR_DOCUMENT_REVIEW", 1)
    result = validate_task_output(
        contract,
        {"decision_summary": "This looks like an invoice from Acme Ltd.", "confidence": 0.9, "signals": [], "warnings": []},
    )
    assert result.valid is True


def test_operator_document_review_input_schema_accepts_null_question():
    contract = get_task_contract("OPERATOR_DOCUMENT_REVIEW", 1)
    from core.contract_validation import describe_schema_errors

    errors = describe_schema_errors(
        {"evidence_id": "abc", "operator_question": None}, dict(contract.input_schema)
    )
    assert errors == []
