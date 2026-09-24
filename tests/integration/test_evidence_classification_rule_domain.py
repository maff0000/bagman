"""CD-6 Slice 5 WI-1 tests for `services.evidence.classification_rule`
— the in-memory reference implementation. Mirrors
`tests/integration/test_mailbox_domain_rule_domain.py`'s own style.

Covers: create-ACTIVE, exact-semantic-replay-idempotent,
duplicate-active-identity-with-different-document_type-rejected,
retire, retired-rule-semantic-fields-unchanged,
replacement-rule-after-retirement, supersedes_rule_id linkage,
no-two-active-identities-collide, no-semantic-patch-possible.
"""
from __future__ import annotations

import dataclasses

import pytest

from core import identity
from core.errors import ConflictError, InvalidStateTransitionError, NotFoundError, ValidationError
from services.evidence.classification_rule import (
    RULE_SOURCE_OPERATOR,
    RULE_STATUS_ACTIVE,
    RULE_STATUS_RETIRED,
    SENDER_SCOPE_EXACT_SENDER_ADDRESS,
    SENDER_SCOPE_EXACT_SENDER_DOMAIN,
    SUBJECT_PREDICATE_EXACT,
    SUBJECT_PREDICATE_STARTS_WITH,
    InMemoryEvidenceClassificationRuleRepository,
    normalize_sender_scope_value,
    validate_rule_fields_or_raise,
)


@pytest.fixture
def repo() -> InMemoryEvidenceClassificationRuleRepository:
    return InMemoryEvidenceClassificationRuleRepository()


def _create(repo, **overrides):
    kwargs = dict(
        sender_scope_type=SENDER_SCOPE_EXACT_SENDER_DOMAIN, sender_scope_value="vendor.com",
        subject_predicate_type=SUBJECT_PREDICATE_EXACT, subject_predicate_value="monthly statement",
        document_type="SUPPLIER_INVOICE", source=RULE_SOURCE_OPERATOR,
    )
    kwargs.update(overrides)
    return repo.create_rule(**kwargs)


# ---------------------------------------------------------------------
# normalize_sender_scope_value
# ---------------------------------------------------------------------


def test_normalize_sender_scope_value_domain_lowercases_and_strips():
    assert normalize_sender_scope_value(SENDER_SCOPE_EXACT_SENDER_DOMAIN, "  Vendor.COM  ") == "vendor.com"


def test_normalize_sender_scope_value_address_lowercases_and_strips():
    assert normalize_sender_scope_value(SENDER_SCOPE_EXACT_SENDER_ADDRESS, "  Alice@Vendor.COM  ") == "alice@vendor.com"


# ---------------------------------------------------------------------
# validate_rule_fields_or_raise
# ---------------------------------------------------------------------


def test_no_attachment_filename_sender_scope_type():
    with pytest.raises(ValidationError):
        validate_rule_fields_or_raise(
            sender_scope_type="ATTACHMENT_FILENAME", sender_scope_value="invoice.pdf",
            subject_predicate_type=SUBJECT_PREDICATE_EXACT, subject_predicate_value="x",
            document_type="RECEIPT", status=RULE_STATUS_ACTIVE, source=RULE_SOURCE_OPERATOR, retired_at=None,
        )


def test_no_contains_subject_predicate_type():
    with pytest.raises(ValidationError):
        validate_rule_fields_or_raise(
            sender_scope_type=SENDER_SCOPE_EXACT_SENDER_DOMAIN, sender_scope_value="vendor.com",
            subject_predicate_type="CONTAINS", subject_predicate_value="x",
            document_type="RECEIPT", status=RULE_STATUS_ACTIVE, source=RULE_SOURCE_OPERATOR, retired_at=None,
        )


def test_active_status_must_not_carry_retired_at():
    from datetime import datetime, timezone

    with pytest.raises(ValidationError):
        validate_rule_fields_or_raise(
            sender_scope_type=SENDER_SCOPE_EXACT_SENDER_DOMAIN, sender_scope_value="vendor.com",
            subject_predicate_type=SUBJECT_PREDICATE_EXACT, subject_predicate_value="x",
            document_type="RECEIPT", status=RULE_STATUS_ACTIVE, source=RULE_SOURCE_OPERATOR,
            retired_at=datetime.now(timezone.utc),
        )


def test_retired_status_requires_retired_at():
    with pytest.raises(ValidationError):
        validate_rule_fields_or_raise(
            sender_scope_type=SENDER_SCOPE_EXACT_SENDER_DOMAIN, sender_scope_value="vendor.com",
            subject_predicate_type=SUBJECT_PREDICATE_EXACT, subject_predicate_value="x",
            document_type="RECEIPT", status=RULE_STATUS_RETIRED, source=RULE_SOURCE_OPERATOR, retired_at=None,
        )


# ---------------------------------------------------------------------
# create-ACTIVE / exact-semantic-replay-idempotent
# ---------------------------------------------------------------------


def test_create_rule_is_active(repo):
    rule = _create(repo)
    assert rule.status == RULE_STATUS_ACTIVE
    assert rule.retired_at is None
    assert rule.created_at == rule.approved_at


def test_exact_semantic_replay_is_idempotent(repo):
    first = _create(repo)
    second = _create(repo)
    assert second.rule_id == first.rule_id


def test_case_and_whitespace_equivalent_replay_is_idempotent(repo):
    first = _create(repo, sender_scope_value="Vendor.com", subject_predicate_value="Monthly   Statement")
    second = _create(repo, sender_scope_value="vendor.com", subject_predicate_value="monthly statement")
    assert second.rule_id == first.rule_id


def test_duplicate_active_identity_different_document_type_rejected(repo):
    _create(repo, document_type="SUPPLIER_INVOICE")
    with pytest.raises(ConflictError):
        _create(repo, document_type="RECEIPT")


def test_no_two_active_identities_collide(repo):
    a = _create(repo, sender_scope_value="vendor.com")
    b = _create(repo, sender_scope_value="othervendor.com")
    assert a.rule_id != b.rule_id
    assert {r.rule_id for r in repo.list_rules(status=RULE_STATUS_ACTIVE)} == {a.rule_id, b.rule_id}


# ---------------------------------------------------------------------
# retire / retired-rule-semantic-fields-unchanged / no-semantic-patch
# ---------------------------------------------------------------------


def test_retire_transitions_active_to_retired(repo):
    rule = _create(repo)
    retired = repo.retire_rule(rule.rule_id)
    assert retired.status == RULE_STATUS_RETIRED
    assert retired.retired_at is not None


def test_retired_rule_semantic_fields_unchanged(repo):
    rule = _create(repo)
    retired = repo.retire_rule(rule.rule_id)
    assert retired.sender_scope_type == rule.sender_scope_type
    assert retired.sender_scope_value == rule.sender_scope_value
    assert retired.subject_predicate_type == rule.subject_predicate_type
    assert retired.subject_predicate_value == rule.subject_predicate_value
    assert retired.document_type == rule.document_type


def test_retire_an_already_retired_rule_raises_invalid_state_transition(repo):
    rule = _create(repo)
    repo.retire_rule(rule.rule_id)
    with pytest.raises(InvalidStateTransitionError):
        repo.retire_rule(rule.rule_id)


def test_retire_unknown_rule_raises_not_found(repo):
    with pytest.raises(NotFoundError):
        repo.retire_rule(identity.generate_id())


def test_no_semantic_patch_possible():
    """EvidenceClassificationRule is a frozen dataclass — the repository
    has no update method for semantic fields at all; the only mutation
    path is retire_rule (status/retired_at only)."""
    rule = InMemoryEvidenceClassificationRuleRepository()
    created = _create(rule)
    with pytest.raises(dataclasses.FrozenInstanceError):
        created.document_type = "RECEIPT"


# ---------------------------------------------------------------------
# replacement-rule-after-retirement / supersedes_rule_id linkage
# ---------------------------------------------------------------------


def test_replacement_rule_after_retirement(repo):
    original = _create(repo, document_type="SUPPLIER_INVOICE")
    repo.retire_rule(original.rule_id)
    replacement = _create(repo, document_type="RECEIPT", supersedes_rule_id=original.rule_id)
    assert replacement.rule_id != original.rule_id
    assert replacement.supersedes_rule_id == original.rule_id
    assert replacement.status == RULE_STATUS_ACTIVE


def test_supersedes_rule_id_must_reference_a_real_rule(repo):
    with pytest.raises(NotFoundError):
        _create(repo, supersedes_rule_id=identity.generate_id())


# ---------------------------------------------------------------------
# find_active_rule_at_identity / list_rules
# ---------------------------------------------------------------------


def test_find_active_rule_at_identity_returns_none_when_never_governed(repo):
    assert repo.find_active_rule_at_identity(
        SENDER_SCOPE_EXACT_SENDER_DOMAIN, "never-seen.example", SUBJECT_PREDICATE_EXACT, "x"
    ) is None


def test_find_active_rule_at_identity_returns_none_after_retirement(repo):
    rule = _create(repo)
    repo.retire_rule(rule.rule_id)
    assert repo.find_active_rule_at_identity(
        rule.sender_scope_type, rule.sender_scope_value, rule.subject_predicate_type, rule.subject_predicate_value
    ) is None


def test_find_active_rule_at_identity_returns_the_active_rule(repo):
    rule = _create(repo)
    found = repo.find_active_rule_at_identity(
        rule.sender_scope_type, rule.sender_scope_value, rule.subject_predicate_type, rule.subject_predicate_value
    )
    assert found.rule_id == rule.rule_id


def test_list_rules_filters_by_status(repo):
    active = _create(repo, sender_scope_value="active.com")
    retired_source = _create(repo, sender_scope_value="retired.com")
    repo.retire_rule(retired_source.rule_id)
    active_rules = repo.list_rules(status=RULE_STATUS_ACTIVE)
    retired_rules = repo.list_rules(status=RULE_STATUS_RETIRED)
    assert {r.rule_id for r in active_rules} == {active.rule_id}
    assert {r.rule_id for r in retired_rules} == {retired_source.rule_id}


def test_list_rules_filters_by_sender_scope_type(repo):
    _create(repo, sender_scope_value="domain-rule.com")
    address_rule = _create(
        repo, sender_scope_type=SENDER_SCOPE_EXACT_SENDER_ADDRESS, sender_scope_value="ap@address-rule.com",
        subject_predicate_type=SUBJECT_PREDICATE_STARTS_WITH, subject_predicate_value="invoice",
    )
    found = repo.list_rules(sender_scope_type=SENDER_SCOPE_EXACT_SENDER_ADDRESS)
    assert {r.rule_id for r in found} == {address_rule.rule_id}
