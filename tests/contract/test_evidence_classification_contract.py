"""Contract tests for
`contracts/evidence/bagman.evidence_classification.v1.schema.json` (CD-6
Slice 5 WI-1). Validates raw, hand-built dicts DIRECTLY against
`core.contract_validation.validate_against_contract` — bypassing
`EvidenceClassificationRepository` entirely — proving the schema itself
enforces the status/document_type and source/reference cross-field
invariants independent of any Python-layer guard (defense-in-depth,
mirrors `tests/contract/test_mailbox_domain_rule_contract.py`'s own
style).
"""
from __future__ import annotations

import pytest

from core.contract_validation import validate_against_contract
from core.errors import ValidationError

SCHEMA = "evidence/bagman.evidence_classification.v1.schema.json"


# ---------------------------------------------------------------------
# status / document_type cross-field invariants
# ---------------------------------------------------------------------


def test_classified_requires_document_type_not_unknown(make_evidence_classification):
    instance = make_evidence_classification(status="CLASSIFIED", document_type="UNKNOWN")
    with pytest.raises(ValidationError):
        validate_against_contract(instance, SCHEMA)


def test_classified_with_a_concrete_document_type_is_accepted(make_evidence_classification):
    instance = make_evidence_classification(status="CLASSIFIED", document_type="RECEIPT")
    validate_against_contract(instance, SCHEMA)


def test_unclassifiable_requires_document_type_unknown(make_evidence_classification):
    instance = make_evidence_classification(status="UNCLASSIFIABLE", document_type="RECEIPT")
    with pytest.raises(ValidationError):
        validate_against_contract(instance, SCHEMA)


def test_unclassifiable_with_unknown_document_type_is_accepted(make_evidence_classification):
    instance = make_evidence_classification(status="UNCLASSIFIABLE", document_type="UNKNOWN")
    validate_against_contract(instance, SCHEMA)


def test_review_required_allows_a_concrete_document_type(make_evidence_classification):
    instance = make_evidence_classification(status="REVIEW_REQUIRED", document_type="ORDER_CONFIRMATION")
    validate_against_contract(instance, SCHEMA)


def test_review_required_allows_unknown_document_type(make_evidence_classification):
    instance = make_evidence_classification(status="REVIEW_REQUIRED", document_type="UNKNOWN")
    validate_against_contract(instance, SCHEMA)


# ---------------------------------------------------------------------
# source / reference cross-field invariants — valid shapes
# ---------------------------------------------------------------------


def test_deterministic_rule_valid_shape_is_accepted(make_evidence_classification, new_id):
    instance = make_evidence_classification(
        source="DETERMINISTIC_RULE", rule_id=new_id(), ai_invocation_id=None, operator_action_id=None, confidence=None,
    )
    validate_against_contract(instance, SCHEMA)


def test_ai_proposal_valid_shape_is_accepted(make_evidence_classification, new_id):
    instance = make_evidence_classification(
        source="AI_PROPOSAL", rule_id=None, ai_invocation_id=new_id(), operator_action_id=None, confidence=0.87,
    )
    validate_against_contract(instance, SCHEMA)


def test_operator_assigned_valid_shape_is_accepted(make_evidence_classification):
    instance = make_evidence_classification(
        source="OPERATOR_ASSIGNED", rule_id=None, ai_invocation_id=None, operator_action_id="op-action-1", confidence=None,
    )
    validate_against_contract(instance, SCHEMA)


# ---------------------------------------------------------------------
# source / reference cross-field invariants — invalid shapes
# ---------------------------------------------------------------------


def test_deterministic_rule_missing_rule_id_is_rejected(make_evidence_classification):
    instance = make_evidence_classification(source="DETERMINISTIC_RULE", rule_id=None)
    with pytest.raises(ValidationError):
        validate_against_contract(instance, SCHEMA)


def test_deterministic_rule_with_confidence_is_rejected(make_evidence_classification, new_id):
    instance = make_evidence_classification(source="DETERMINISTIC_RULE", rule_id=new_id(), confidence=1.0)
    with pytest.raises(ValidationError):
        validate_against_contract(instance, SCHEMA)


def test_deterministic_rule_with_ai_invocation_id_is_rejected(make_evidence_classification, new_id):
    instance = make_evidence_classification(source="DETERMINISTIC_RULE", rule_id=new_id(), ai_invocation_id=new_id())
    with pytest.raises(ValidationError):
        validate_against_contract(instance, SCHEMA)


def test_deterministic_rule_with_operator_action_id_is_rejected(make_evidence_classification, new_id):
    instance = make_evidence_classification(source="DETERMINISTIC_RULE", rule_id=new_id(), operator_action_id="x")
    with pytest.raises(ValidationError):
        validate_against_contract(instance, SCHEMA)


def test_ai_proposal_missing_ai_invocation_id_is_rejected(make_evidence_classification):
    instance = make_evidence_classification(source="AI_PROPOSAL", rule_id=None, ai_invocation_id=None, confidence=0.5)
    with pytest.raises(ValidationError):
        validate_against_contract(instance, SCHEMA)


def test_ai_proposal_missing_confidence_is_rejected(make_evidence_classification, new_id):
    instance = make_evidence_classification(source="AI_PROPOSAL", rule_id=None, ai_invocation_id=new_id(), confidence=None)
    with pytest.raises(ValidationError):
        validate_against_contract(instance, SCHEMA)


def test_ai_proposal_confidence_out_of_range_is_rejected(make_evidence_classification, new_id):
    instance = make_evidence_classification(source="AI_PROPOSAL", rule_id=None, ai_invocation_id=new_id(), confidence=1.5)
    with pytest.raises(ValidationError):
        validate_against_contract(instance, SCHEMA)


def test_ai_proposal_with_rule_id_is_rejected(make_evidence_classification, new_id):
    instance = make_evidence_classification(
        source="AI_PROPOSAL", rule_id=new_id(), ai_invocation_id=new_id(), confidence=0.5
    )
    with pytest.raises(ValidationError):
        validate_against_contract(instance, SCHEMA)


def test_operator_assigned_missing_operator_action_id_is_rejected(make_evidence_classification):
    instance = make_evidence_classification(source="OPERATOR_ASSIGNED", rule_id=None, operator_action_id=None)
    with pytest.raises(ValidationError):
        validate_against_contract(instance, SCHEMA)


def test_operator_assigned_with_confidence_is_rejected(make_evidence_classification):
    instance = make_evidence_classification(
        source="OPERATOR_ASSIGNED", rule_id=None, operator_action_id="op-1", confidence=0.9
    )
    with pytest.raises(ValidationError):
        validate_against_contract(instance, SCHEMA)


def test_operator_assigned_with_rule_id_is_rejected(make_evidence_classification, new_id):
    instance = make_evidence_classification(source="OPERATOR_ASSIGNED", rule_id=new_id(), operator_action_id="op-1")
    with pytest.raises(ValidationError):
        validate_against_contract(instance, SCHEMA)


# ---------------------------------------------------------------------
# reason_codes / additionalProperties / schema_version
# ---------------------------------------------------------------------


def test_empty_reason_codes_is_accepted(make_evidence_classification):
    instance = make_evidence_classification(reason_codes=[])
    validate_against_contract(instance, SCHEMA)


def test_populated_reason_codes_is_accepted(make_evidence_classification):
    instance = make_evidence_classification(reason_codes=["LOW_TEXT_CONFIDENCE", "AMBIGUOUS_SENDER"])
    validate_against_contract(instance, SCHEMA)


def test_additional_property_is_rejected(make_evidence_classification):
    instance = make_evidence_classification()
    instance["not_a_real_field"] = "x"
    with pytest.raises(ValidationError):
        validate_against_contract(instance, SCHEMA)


def test_wrong_schema_version_is_rejected(make_evidence_classification):
    instance = make_evidence_classification(schema_version="bagman.evidence_classification.v2")
    with pytest.raises(ValidationError):
        validate_against_contract(instance, SCHEMA)


def test_unknown_document_type_value_is_rejected(make_evidence_classification):
    instance = make_evidence_classification(status="REVIEW_REQUIRED", document_type="INVOICE_MAYBE")
    with pytest.raises(ValidationError):
        validate_against_contract(instance, SCHEMA)
