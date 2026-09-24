"""Contract tests for
`contracts/evidence/bagman.evidence_classification_rule.v1.schema.json`
(CD-6 Slice 5 WI-1). Mirrors
`tests/contract/test_evidence_classification_contract.py`'s own style.
"""
from __future__ import annotations

import pytest

from core.contract_validation import validate_against_contract
from core.errors import ValidationError

SCHEMA = "evidence/bagman.evidence_classification_rule.v1.schema.json"


# ---------------------------------------------------------------------
# status / retired_at cross-field invariants
# ---------------------------------------------------------------------


def test_active_with_null_retired_at_is_accepted(make_evidence_classification_rule):
    instance = make_evidence_classification_rule(status="ACTIVE", retired_at=None)
    validate_against_contract(instance, SCHEMA)


def test_active_with_populated_retired_at_is_rejected(make_evidence_classification_rule, now_str):
    instance = make_evidence_classification_rule(status="ACTIVE", retired_at=now_str())
    with pytest.raises(ValidationError):
        validate_against_contract(instance, SCHEMA)


def test_retired_with_populated_retired_at_is_accepted(make_evidence_classification_rule, now_str):
    instance = make_evidence_classification_rule(status="RETIRED", retired_at=now_str())
    validate_against_contract(instance, SCHEMA)


def test_retired_with_null_retired_at_is_rejected(make_evidence_classification_rule):
    instance = make_evidence_classification_rule(status="RETIRED", retired_at=None)
    with pytest.raises(ValidationError):
        validate_against_contract(instance, SCHEMA)


# ---------------------------------------------------------------------
# sender_scope_type-conditional sender_scope_value format
# ---------------------------------------------------------------------


def test_exact_sender_domain_with_a_bare_domain_is_accepted(make_evidence_classification_rule):
    instance = make_evidence_classification_rule(sender_scope_type="EXACT_SENDER_DOMAIN", sender_scope_value="vendor.com")
    validate_against_contract(instance, SCHEMA)


def test_exact_sender_domain_with_an_email_address_is_rejected(make_evidence_classification_rule):
    instance = make_evidence_classification_rule(sender_scope_type="EXACT_SENDER_DOMAIN", sender_scope_value="ap@vendor.com")
    with pytest.raises(ValidationError):
        validate_against_contract(instance, SCHEMA)


def test_exact_sender_address_with_an_email_address_is_accepted(make_evidence_classification_rule):
    instance = make_evidence_classification_rule(sender_scope_type="EXACT_SENDER_ADDRESS", sender_scope_value="ap@vendor.com")
    validate_against_contract(instance, SCHEMA)


def test_exact_sender_address_with_a_bare_domain_is_rejected(make_evidence_classification_rule):
    instance = make_evidence_classification_rule(sender_scope_type="EXACT_SENDER_ADDRESS", sender_scope_value="vendor.com")
    with pytest.raises(ValidationError):
        validate_against_contract(instance, SCHEMA)


# ---------------------------------------------------------------------
# closed-enum membership / additionalProperties / schema_version
# ---------------------------------------------------------------------


def test_attachment_filename_sender_scope_type_is_rejected(make_evidence_classification_rule):
    instance = make_evidence_classification_rule(sender_scope_type="ATTACHMENT_FILENAME", sender_scope_value="invoice.pdf")
    with pytest.raises(ValidationError):
        validate_against_contract(instance, SCHEMA)


def test_contains_subject_predicate_type_is_rejected(make_evidence_classification_rule):
    instance = make_evidence_classification_rule(subject_predicate_type="CONTAINS")
    with pytest.raises(ValidationError):
        validate_against_contract(instance, SCHEMA)


def test_starts_with_predicate_valid_shape_is_accepted(make_evidence_classification_rule):
    instance = make_evidence_classification_rule(subject_predicate_type="STARTS_WITH", subject_predicate_value="order")
    validate_against_contract(instance, SCHEMA)


def test_superseded_status_value_is_rejected(make_evidence_classification_rule):
    instance = make_evidence_classification_rule(status="SUPERSEDED")
    with pytest.raises(ValidationError):
        validate_against_contract(instance, SCHEMA)


def test_bagman_proposed_source_is_accepted(make_evidence_classification_rule):
    instance = make_evidence_classification_rule(source="BAGMAN_PROPOSED")
    validate_against_contract(instance, SCHEMA)


def test_additional_property_is_rejected(make_evidence_classification_rule):
    instance = make_evidence_classification_rule()
    instance["not_a_real_field"] = "x"
    with pytest.raises(ValidationError):
        validate_against_contract(instance, SCHEMA)


def test_wrong_schema_version_is_rejected(make_evidence_classification_rule):
    instance = make_evidence_classification_rule(schema_version="bagman.evidence_classification_rule.v2")
    with pytest.raises(ValidationError):
        validate_against_contract(instance, SCHEMA)


def test_supersedes_rule_id_populated_is_accepted(make_evidence_classification_rule, new_id):
    instance = make_evidence_classification_rule(supersedes_rule_id=new_id())
    validate_against_contract(instance, SCHEMA)
