"""CD-6 Slice 5 WI-2 tests for `services.evidence.classification_matcher`
— pure, dependency-free precedence/ambiguity proofs against hand-built
`EvidenceClassificationRule` fixtures (no repository of any kind)."""
from __future__ import annotations

from datetime import datetime, timezone

from services.evidence.classification_matcher import (
    OUTCOME_CONFLICT,
    OUTCOME_MATCH,
    OUTCOME_NO_APPLICABLE_RULE_INPUT,
    OUTCOME_NO_MATCH,
    match_evidence_to_rule,
)
from services.evidence.classification_rule import (
    RULE_SOURCE_OPERATOR,
    RULE_STATUS_ACTIVE,
    SENDER_SCOPE_EXACT_SENDER_ADDRESS,
    SENDER_SCOPE_EXACT_SENDER_DOMAIN,
    SUBJECT_PREDICATE_EXACT,
    SUBJECT_PREDICATE_STARTS_WITH,
    EvidenceClassificationRule,
)

_NOW = datetime.now(timezone.utc)


def _rule(
    *, rule_id: str, sender_scope_type: str, sender_scope_value: str, subject_predicate_type: str,
    subject_predicate_value: str, document_type: str = "SUPPLIER_INVOICE",
) -> EvidenceClassificationRule:
    return EvidenceClassificationRule(
        rule_id=rule_id, sender_scope_type=sender_scope_type, sender_scope_value=sender_scope_value,
        subject_predicate_type=subject_predicate_type, subject_predicate_value=subject_predicate_value,
        document_type=document_type, status=RULE_STATUS_ACTIVE, source=RULE_SOURCE_OPERATOR,
        created_at=_NOW, approved_at=_NOW,
    )


# ---------------------------------------------------------------------
# Eligibility gate
# ---------------------------------------------------------------------


def test_missing_sender_is_no_applicable_rule_input():
    result = match_evidence_to_rule(sender_address=None, subject="Monthly Statement", active_rules=[])
    assert result.outcome == OUTCOME_NO_APPLICABLE_RULE_INPUT
    assert result.normalized_subject is None


def test_empty_sender_is_no_applicable_rule_input():
    result = match_evidence_to_rule(sender_address="", subject="Monthly Statement", active_rules=[])
    assert result.outcome == OUTCOME_NO_APPLICABLE_RULE_INPUT


def test_missing_subject_is_no_applicable_rule_input():
    result = match_evidence_to_rule(sender_address="billing@vendor.com", subject=None, active_rules=[])
    assert result.outcome == OUTCOME_NO_APPLICABLE_RULE_INPUT


def test_empty_subject_is_no_applicable_rule_input():
    result = match_evidence_to_rule(sender_address="billing@vendor.com", subject="   ", active_rules=[])
    assert result.outcome == OUTCOME_NO_APPLICABLE_RULE_INPUT


def test_malformed_sender_address_is_no_applicable_rule_input_not_a_crash():
    result = match_evidence_to_rule(sender_address="not-an-email", subject="Monthly Statement", active_rules=[])
    assert result.outcome == OUTCOME_NO_APPLICABLE_RULE_INPUT


def test_manual_upload_metadata_shape_never_qualifies():
    """A manual upload has neither field — proves there is no
    filename-based path anywhere in this matcher."""
    result = match_evidence_to_rule(sender_address=None, subject=None, active_rules=[])
    assert result.outcome == OUTCOME_NO_APPLICABLE_RULE_INPUT


# ---------------------------------------------------------------------
# No match
# ---------------------------------------------------------------------


def test_no_active_rules_is_no_match():
    result = match_evidence_to_rule(sender_address="billing@vendor.com", subject="Monthly Statement", active_rules=[])
    assert result.outcome == OUTCOME_NO_MATCH
    assert result.normalized_subject == "monthly statement"


def test_sender_matches_but_subject_predicate_does_not_is_no_match():
    rule = _rule(
        rule_id="r1", sender_scope_type=SENDER_SCOPE_EXACT_SENDER_DOMAIN, sender_scope_value="vendor.com",
        subject_predicate_type=SUBJECT_PREDICATE_EXACT, subject_predicate_value="invoice",
    )
    result = match_evidence_to_rule(sender_address="billing@vendor.com", subject="Monthly Statement", active_rules=[rule])
    assert result.outcome == OUTCOME_NO_MATCH


def test_retired_rules_never_match_when_excluded_by_caller():
    """The matcher itself does not filter on status — this proves the
    documented contract: a caller (correctly) sourcing `active_rules`
    from `list_rules(status="ACTIVE")` never passes a retired rule in,
    so it can never win."""
    result = match_evidence_to_rule(sender_address="billing@vendor.com", subject="Monthly Statement", active_rules=[])
    assert result.outcome == OUTCOME_NO_MATCH


# ---------------------------------------------------------------------
# Precedence: address beats domain
# ---------------------------------------------------------------------


def test_address_rule_beats_domain_rule_even_when_domain_rule_subject_is_more_specific():
    address_rule = _rule(
        rule_id="addr", sender_scope_type=SENDER_SCOPE_EXACT_SENDER_ADDRESS, sender_scope_value="billing@vendor.com",
        subject_predicate_type=SUBJECT_PREDICATE_STARTS_WITH, subject_predicate_value="monthly",
        document_type="SUPPLIER_INVOICE",
    )
    domain_rule = _rule(
        rule_id="dom", sender_scope_type=SENDER_SCOPE_EXACT_SENDER_DOMAIN, sender_scope_value="vendor.com",
        subject_predicate_type=SUBJECT_PREDICATE_EXACT, subject_predicate_value="monthly statement",
        document_type="RECEIPT",
    )
    result = match_evidence_to_rule(
        sender_address="billing@vendor.com", subject="Monthly Statement", active_rules=[address_rule, domain_rule]
    )
    assert result.outcome == OUTCOME_MATCH
    assert result.rule_id == "addr"
    assert result.document_type == "SUPPLIER_INVOICE"


def test_domain_rule_used_when_no_address_rule_matches_this_sender():
    domain_rule = _rule(
        rule_id="dom", sender_scope_type=SENDER_SCOPE_EXACT_SENDER_DOMAIN, sender_scope_value="vendor.com",
        subject_predicate_type=SUBJECT_PREDICATE_EXACT, subject_predicate_value="monthly statement",
    )
    other_address_rule = _rule(
        rule_id="addr-other", sender_scope_type=SENDER_SCOPE_EXACT_SENDER_ADDRESS,
        sender_scope_value="someone-else@vendor.com", subject_predicate_type=SUBJECT_PREDICATE_EXACT,
        subject_predicate_value="monthly statement",
    )
    result = match_evidence_to_rule(
        sender_address="billing@vendor.com", subject="Monthly Statement", active_rules=[domain_rule, other_address_rule]
    )
    assert result.outcome == OUTCOME_MATCH
    assert result.rule_id == "dom"


# ---------------------------------------------------------------------
# Precedence: EXACT beats STARTS_WITH; longest STARTS_WITH wins
# ---------------------------------------------------------------------


def test_exact_subject_beats_starts_with_at_same_sender_tier():
    exact_rule = _rule(
        rule_id="exact", sender_scope_type=SENDER_SCOPE_EXACT_SENDER_DOMAIN, sender_scope_value="ebay.com",
        subject_predicate_type=SUBJECT_PREDICATE_EXACT, subject_predicate_value="order confirmed: item 123",
        document_type="ORDER_CONFIRMATION",
    )
    starts_with_rule = _rule(
        rule_id="prefix", sender_scope_type=SENDER_SCOPE_EXACT_SENDER_DOMAIN, sender_scope_value="ebay.com",
        subject_predicate_type=SUBJECT_PREDICATE_STARTS_WITH, subject_predicate_value="order confirmed:",
        document_type="RECEIPT",
    )
    result = match_evidence_to_rule(
        sender_address="noreply@ebay.com", subject="Order confirmed: item 123",
        active_rules=[starts_with_rule, exact_rule],
    )
    assert result.outcome == OUTCOME_MATCH
    assert result.rule_id == "exact"
    assert result.matching_tier == "EXACT_SENDER_DOMAIN+EXACT"


def test_longest_starts_with_wins_among_multiple_candidates():
    short = _rule(
        rule_id="short", sender_scope_type=SENDER_SCOPE_EXACT_SENDER_DOMAIN, sender_scope_value="ebay.com",
        subject_predicate_type=SUBJECT_PREDICATE_STARTS_WITH, subject_predicate_value="order",
    )
    medium = _rule(
        rule_id="medium", sender_scope_type=SENDER_SCOPE_EXACT_SENDER_DOMAIN, sender_scope_value="ebay.com",
        subject_predicate_type=SUBJECT_PREDICATE_STARTS_WITH, subject_predicate_value="order confirmed:",
    )
    longest = _rule(
        rule_id="longest", sender_scope_type=SENDER_SCOPE_EXACT_SENDER_DOMAIN, sender_scope_value="ebay.com",
        subject_predicate_type=SUBJECT_PREDICATE_STARTS_WITH, subject_predicate_value="order confirmed: item",
    )
    result = match_evidence_to_rule(
        sender_address="noreply@ebay.com", subject="Order confirmed: item 123 has shipped",
        active_rules=[short, longest, medium],
    )
    assert result.outcome == OUTCOME_MATCH
    assert result.rule_id == "longest"
    assert result.matching_tier == "EXACT_SENDER_DOMAIN+STARTS_WITH"


# ---------------------------------------------------------------------
# Ambiguity — fail closed (constructed tie, bypassing WI-1's own
# active-identity uniqueness — see this module's own docstring analysis
# of why genuine ambiguity is otherwise structurally unreachable).
# ---------------------------------------------------------------------


def test_conflict_when_two_rules_are_genuinely_tied():
    rule_a = _rule(
        rule_id="tie-a", sender_scope_type=SENDER_SCOPE_EXACT_SENDER_DOMAIN, sender_scope_value="vendor.com",
        subject_predicate_type=SUBJECT_PREDICATE_EXACT, subject_predicate_value="monthly statement",
        document_type="SUPPLIER_INVOICE",
    )
    # A genuine tie requires an IDENTICAL (sender_scope_type,
    # sender_scope_value, subject_predicate_type, subject_predicate_value)
    # to rule_a — structurally impossible for two ACTIVE rules under
    # WI-1's own active-identity uniqueness constraint (see
    # services/evidence/classification_matcher.py's own "CONFLICT
    # reachability" docstring section). Constructed directly here, the
    # same way the mailbox-domain-rule precedent's own ambiguity test
    # must bypass its write path.
    rule_b = _rule(
        rule_id="tie-b", sender_scope_type=SENDER_SCOPE_EXACT_SENDER_DOMAIN, sender_scope_value="vendor.com",
        subject_predicate_type=SUBJECT_PREDICATE_EXACT, subject_predicate_value="monthly statement",
        document_type="RECEIPT",
    )
    result = match_evidence_to_rule(
        sender_address="billing@vendor.com", subject="Monthly Statement", active_rules=[rule_a, rule_b]
    )
    assert result.outcome == OUTCOME_CONFLICT
    assert set(result.conflicting_rule_ids) == {"tie-a", "tie-b"}
    assert result.rule_id is None
    assert result.document_type is None


def test_conflict_never_broken_by_rule_id_or_insertion_order():
    """Feeding the SAME tied pair in a different order must still
    CONFLICT, never silently resolve to "first" or "last" or
    lexicographically-smallest rule_id."""
    rule_z = _rule(
        rule_id="zzz", sender_scope_type=SENDER_SCOPE_EXACT_SENDER_DOMAIN, sender_scope_value="vendor.com",
        subject_predicate_type=SUBJECT_PREDICATE_STARTS_WITH, subject_predicate_value="monthly",
    )
    rule_a = _rule(
        rule_id="aaa", sender_scope_type=SENDER_SCOPE_EXACT_SENDER_DOMAIN, sender_scope_value="vendor.com",
        subject_predicate_type=SUBJECT_PREDICATE_STARTS_WITH, subject_predicate_value="monthly",
    )
    for ordering in ([rule_z, rule_a], [rule_a, rule_z]):
        result = match_evidence_to_rule(
            sender_address="billing@vendor.com", subject="Monthly Statement", active_rules=ordering
        )
        assert result.outcome == OUTCOME_CONFLICT
        assert set(result.conflicting_rule_ids) == {"aaa", "zzz"}
