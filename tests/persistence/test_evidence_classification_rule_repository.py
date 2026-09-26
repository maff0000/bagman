"""CD-6 Slice 5 WI-1 PostgreSQL persistence proofs —
`persistence/postgres/evidence_classification_rule_repository.py`
against a REAL, disposable PostgreSQL container. Mirrors
`tests/persistence/test_mailbox_domain_rule_repository.py`'s own style.
"""
from __future__ import annotations

import pytest
from sqlalchemy.exc import IntegrityError

from core import identity
from core.contract_validation import validate_against_contract
from core.errors import ConflictError, InvalidStateTransitionError, NotFoundError
from core.timestamps import utc_now
from persistence.postgres.evidence_classification_rule_models import EvidenceClassificationRuleRow
from persistence.postgres.evidence_classification_rule_repository import PostgresEvidenceClassificationRuleRepository
from persistence.postgres.session import get_engine, session_scope
from services.evidence.classification_rule import (
    RULE_SOURCE_OPERATOR,
    RULE_STATUS_ACTIVE,
    RULE_STATUS_RETIRED,
    SENDER_SCOPE_EXACT_SENDER_DOMAIN,
    SUBJECT_PREDICATE_EXACT,
)

_SCHEMA = "evidence/bagman.evidence_classification_rule.v1.schema.json"


def _create(repo, **overrides):
    kwargs = dict(
        sender_scope_type=SENDER_SCOPE_EXACT_SENDER_DOMAIN, sender_scope_value="vendor.com",
        subject_predicate_type=SUBJECT_PREDICATE_EXACT, subject_predicate_value="monthly statement",
        document_type="SUPPLIER_INVOICE", source=RULE_SOURCE_OPERATOR,
    )
    kwargs.update(overrides)
    return repo.create_rule(**kwargs)


# ---------------------------------------------------------------------
# round-trip / create-ACTIVE / exact-replay-idempotent
# ---------------------------------------------------------------------


def test_rule_persists_and_is_retrievable_via_a_fresh_repository_instance(fresh_engine):
    repo = PostgresEvidenceClassificationRuleRepository()
    created = _create(repo, sender_scope_value="Vendor.COM")
    assert created.sender_scope_value == "vendor.com"  # normalised
    assert created.status == RULE_STATUS_ACTIVE

    fresh_repo = PostgresEvidenceClassificationRuleRepository(engine=fresh_engine)
    fetched = fresh_repo.get_rule(created.rule_id)
    assert fetched.document_type == "SUPPLIER_INVOICE"


def test_get_unknown_rule_id_raises_not_found():
    repo = PostgresEvidenceClassificationRuleRepository()
    with pytest.raises(NotFoundError):
        repo.get_rule(identity.generate_id())


def test_exact_semantic_replay_returns_the_same_row_not_a_second_one():
    repo = PostgresEvidenceClassificationRuleRepository()
    first = _create(repo, sender_scope_value="replay.example")
    second = _create(repo, sender_scope_value="replay.example")
    assert second.rule_id == first.rule_id
    with session_scope(get_engine()) as session:
        count = (
            session.query(EvidenceClassificationRuleRow)
            .filter_by(sender_scope_value="replay.example", status=RULE_STATUS_ACTIVE)
            .count()
        )
        assert count == 1


def test_duplicate_active_identity_different_document_type_rejected():
    repo = PostgresEvidenceClassificationRuleRepository()
    _create(repo, sender_scope_value="conflict.example", document_type="SUPPLIER_INVOICE")
    with pytest.raises(ConflictError):
        _create(repo, sender_scope_value="conflict.example", document_type="RECEIPT")


def test_database_level_active_identity_unique_constraint_exists():
    """The real, authoritative backstop — bypassing the repository's own
    resolve-or-create logic with a raw duplicate insert must be rejected
    by the database itself."""
    now = utc_now()
    with pytest.raises(IntegrityError):
        with session_scope(get_engine()) as session:
            session.add(
                EvidenceClassificationRuleRow(
                    rule_id=identity.generate_id(), sender_scope_type=SENDER_SCOPE_EXACT_SENDER_DOMAIN,
                    sender_scope_value="dupe.example", subject_predicate_type=SUBJECT_PREDICATE_EXACT,
                    subject_predicate_value="dupe subject", document_type="SUPPLIER_INVOICE",
                    status=RULE_STATUS_ACTIVE, source=RULE_SOURCE_OPERATOR, supersedes_rule_id=None,
                    created_at=now, approved_at=now, retired_at=None,
                )
            )
            session.flush()
            session.add(
                EvidenceClassificationRuleRow(
                    rule_id=identity.generate_id(), sender_scope_type=SENDER_SCOPE_EXACT_SENDER_DOMAIN,
                    sender_scope_value="dupe.example", subject_predicate_type=SUBJECT_PREDICATE_EXACT,
                    subject_predicate_value="dupe subject", document_type="RECEIPT",
                    status=RULE_STATUS_ACTIVE, source=RULE_SOURCE_OPERATOR, supersedes_rule_id=None,
                    created_at=now, approved_at=now, retired_at=None,
                )
            )
            session.flush()


# ---------------------------------------------------------------------
# retire / replacement-rule-after-retirement / supersedes_rule_id
# ---------------------------------------------------------------------


def test_retire_transitions_active_to_retired():
    repo = PostgresEvidenceClassificationRuleRepository()
    rule = _create(repo, sender_scope_value="retire-me.example")
    retired = repo.retire_rule(rule.rule_id)
    assert retired.status == RULE_STATUS_RETIRED
    assert retired.retired_at is not None


def test_retire_an_already_retired_rule_raises_invalid_state_transition():
    repo = PostgresEvidenceClassificationRuleRepository()
    rule = _create(repo, sender_scope_value="retire-twice.example")
    repo.retire_rule(rule.rule_id)
    with pytest.raises(InvalidStateTransitionError):
        repo.retire_rule(rule.rule_id)


def test_replacement_rule_after_retirement_frees_the_identity():
    repo = PostgresEvidenceClassificationRuleRepository()
    original = _create(repo, sender_scope_value="replace-me.example", document_type="SUPPLIER_INVOICE")
    repo.retire_rule(original.rule_id)
    replacement = _create(
        repo, sender_scope_value="replace-me.example", document_type="RECEIPT", supersedes_rule_id=original.rule_id
    )
    assert replacement.rule_id != original.rule_id
    assert replacement.supersedes_rule_id == original.rule_id
    assert replacement.status == RULE_STATUS_ACTIVE


def test_supersedes_rule_id_must_reference_a_real_rule():
    repo = PostgresEvidenceClassificationRuleRepository()
    with pytest.raises(NotFoundError):
        _create(repo, sender_scope_value="orphan-supersedes.example", supersedes_rule_id=identity.generate_id())


# ---------------------------------------------------------------------
# find_active_rule_at_identity / list_rules
# ---------------------------------------------------------------------


def test_find_active_rule_at_identity_returns_none_after_retirement():
    repo = PostgresEvidenceClassificationRuleRepository()
    rule = _create(repo, sender_scope_value="find-me.example")
    repo.retire_rule(rule.rule_id)
    assert repo.find_active_rule_at_identity(
        rule.sender_scope_type, rule.sender_scope_value, rule.subject_predicate_type, rule.subject_predicate_value
    ) is None


def test_list_rules_filters_by_status():
    repo = PostgresEvidenceClassificationRuleRepository()
    active = _create(repo, sender_scope_value="list-active.example")
    retired_source = _create(repo, sender_scope_value="list-retired.example")
    repo.retire_rule(retired_source.rule_id)
    active_rules = repo.list_rules(status=RULE_STATUS_ACTIVE)
    assert {r.rule_id for r in active_rules if r.rule_id in (active.rule_id, retired_source.rule_id)} == {active.rule_id}


# ---------------------------------------------------------------------
# Contract-layer conformance — the REAL code path, not just the schema
# in isolation.
# ---------------------------------------------------------------------


def test_postgres_repository_create_rule_produces_a_schema_valid_snapshot():
    repo = PostgresEvidenceClassificationRuleRepository()
    rule = _create(repo, sender_scope_value="contract-check.example")
    validate_against_contract(rule.to_dict(), _SCHEMA)
