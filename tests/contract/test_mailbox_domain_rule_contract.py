"""Contract tests for
`contracts/mailbox/bagman.mailbox_domain_rule.v1.schema.json` (CD-6
architect amendment; extended by the CD-6 GUI-operations-foundation
follow-on WO's three-state policy model + `EXACT_ADDRESS`/
`EXACT_DOMAIN_SUBJECT` match modes; hardened by this delivery's own
match_mode-conditional `allOf`/`if`/`then` schema rules — item 1 of the
CD-6 GUI-operations-foundation follow-on hardening delta).

Before this delivery, the schema accepted structurally-impossible
combinations (e.g. `match_mode=EXACT` with a populated
`subject_predicate_type`) — the Python domain layer
(`services.mailbox.domain_rule.validate_match_fields_or_raise`) already
rejected these, but the CONTRACT itself did not, so a malformed dict
reaching contract validation from any source other than the two
governed repository implementations would have passed. These tests
validate raw, hand-built dicts DIRECTLY against
`core.contract_validation.validate_against_contract` — bypassing
`MailboxDomainRuleRepository` entirely — proving the schema itself now
enforces the same match_mode/sender_address/subject_predicate_*
correspondence, independent of any Python-layer guard. This is
defense-in-depth, not a replacement: `validate_match_fields_or_raise`
and every other Python-layer validation in
`services/mailbox/domain_rule.py` are untouched by this delta.
"""
from __future__ import annotations

from typing import Any

import pytest

from core import identity
from core.contract_validation import validate_against_contract
from core.errors import ValidationError
from services.mailbox.domain_rule import InMemoryMailboxDomainRuleRepository

SCHEMA = "mailbox/bagman.mailbox_domain_rule.v1.schema.json"


# ---------------------------------------------------------------------
# The 7 structurally-impossible combinations the hardened schema must
# now reject at the SCHEMA layer itself (item 1, first bullet list).
# ---------------------------------------------------------------------


def test_exact_with_populated_subject_predicate_type_and_value_is_rejected(make_mailbox_domain_rule):
    instance = make_mailbox_domain_rule(
        match_mode="EXACT", subject_predicate_type="EXACT", subject_predicate_value="monthly statement"
    )
    with pytest.raises(ValidationError):
        validate_against_contract(instance, SCHEMA)


def test_include_subdomains_with_populated_subject_predicate_type_and_value_is_rejected(make_mailbox_domain_rule):
    instance = make_mailbox_domain_rule(
        match_mode="INCLUDE_SUBDOMAINS", subject_predicate_type="STARTS_WITH", subject_predicate_value="order"
    )
    with pytest.raises(ValidationError):
        validate_against_contract(instance, SCHEMA)


def test_exact_address_with_populated_subject_predicate_type_and_value_is_rejected(make_mailbox_domain_rule):
    instance = make_mailbox_domain_rule(
        match_mode="EXACT_ADDRESS",
        sender_address="ap@vendor.com",
        policy="MUST_READ",
        destination_mode="REVIEW_REQUIRED",
        subject_predicate_type="EXACT",
        subject_predicate_value="monthly statement",
    )
    with pytest.raises(ValidationError):
        validate_against_contract(instance, SCHEMA)


def test_exact_domain_subject_with_populated_sender_address_is_rejected(make_mailbox_domain_rule):
    instance = make_mailbox_domain_rule(
        match_mode="EXACT_DOMAIN_SUBJECT",
        sender_address="ap@vendor.com",
        subject_predicate_type="EXACT",
        subject_predicate_value="monthly statement",
    )
    with pytest.raises(ValidationError):
        validate_against_contract(instance, SCHEMA)


def test_exact_domain_subject_missing_subject_predicate_type_is_rejected(make_mailbox_domain_rule):
    instance = make_mailbox_domain_rule(
        match_mode="EXACT_DOMAIN_SUBJECT", subject_predicate_type=None, subject_predicate_value="monthly statement"
    )
    with pytest.raises(ValidationError):
        validate_against_contract(instance, SCHEMA)


def test_exact_domain_subject_missing_subject_predicate_value_is_rejected(make_mailbox_domain_rule):
    instance = make_mailbox_domain_rule(
        match_mode="EXACT_DOMAIN_SUBJECT", subject_predicate_type="EXACT", subject_predicate_value=None
    )
    with pytest.raises(ValidationError):
        validate_against_contract(instance, SCHEMA)


def test_exact_domain_subject_with_both_subject_predicate_fields_null_is_rejected(make_mailbox_domain_rule):
    instance = make_mailbox_domain_rule(
        match_mode="EXACT_DOMAIN_SUBJECT", subject_predicate_type=None, subject_predicate_value=None
    )
    with pytest.raises(ValidationError):
        validate_against_contract(instance, SCHEMA)


# ---------------------------------------------------------------------
# The 5 structurally-valid shapes — one per match_mode, plus both
# subject-predicate-type variants — must still be ACCEPTED.
# ---------------------------------------------------------------------


def test_exact_valid_shape_is_accepted(make_mailbox_domain_rule):
    instance = make_mailbox_domain_rule(match_mode="EXACT", policy="BLACKLIST")
    validate_against_contract(instance, SCHEMA)


def test_include_subdomains_valid_shape_is_accepted(make_mailbox_domain_rule):
    instance = make_mailbox_domain_rule(match_mode="INCLUDE_SUBDOMAINS", policy="BLACKLIST")
    validate_against_contract(instance, SCHEMA)


def test_exact_address_valid_shape_is_accepted(make_mailbox_domain_rule):
    instance = make_mailbox_domain_rule(
        match_mode="EXACT_ADDRESS", sender_address="ap@vendor.com", policy="MUST_READ",
        destination_mode="REVIEW_REQUIRED",
    )
    validate_against_contract(instance, SCHEMA)


def test_exact_domain_subject_exact_predicate_valid_shape_is_accepted(make_mailbox_domain_rule):
    instance = make_mailbox_domain_rule(
        match_mode="EXACT_DOMAIN_SUBJECT", subject_predicate_type="EXACT", subject_predicate_value="monthly statement",
        policy="MUST_READ", destination_mode="REVIEW_REQUIRED",
    )
    validate_against_contract(instance, SCHEMA)


def test_exact_domain_subject_starts_with_predicate_valid_shape_is_accepted(make_mailbox_domain_rule):
    instance = make_mailbox_domain_rule(
        match_mode="EXACT_DOMAIN_SUBJECT", subject_predicate_type="STARTS_WITH",
        subject_predicate_value="order confirmed:", policy="BLACKLIST",
    )
    validate_against_contract(instance, SCHEMA)


# ---------------------------------------------------------------------
# All 19 current production rule *shapes* (Gmail blacklist EXACT, IBKR
# root EXACT/FIXED, IBKR subdomain INCLUDE_SUBDOMAINS/FIXED, Stripe
# EXACT/REVIEW_REQUIRED, Microsoft rules, EXACT_ADDRESS) must still
# validate under the hardened schema UNCHANGED — each family below is a
# representative `.to_dict()`-shaped dict for the real, currently-
# governed rule population this delivery must never regress.
# ---------------------------------------------------------------------


def _family_case(**overrides: Any) -> dict:
    return overrides


PRODUCTION_RULE_FAMILIES: list[tuple[str, dict]] = [
    (
        "gmail_blacklist_exact",
        _family_case(
            sender_domain="mailer.gmail-noise.example", match_mode="EXACT", policy="BLACKLIST",
            destination_entity_id=None, destination_mode=None, processor_hint="GMAIL",
        ),
    ),
    (
        "ibkr_root_exact_fixed",
        _family_case(
            sender_domain="interactivebrokers.com", match_mode="EXACT", policy="MUST_READ",
            destination_mode="FIXED", processor_hint="IBKR",
        ),
    ),
    (
        "ibkr_subdomain_include_subdomains_fixed",
        _family_case(
            sender_domain="interactivebrokers.com", match_mode="INCLUDE_SUBDOMAINS", policy="MUST_READ",
            destination_mode="FIXED", processor_hint="IBKR",
        ),
    ),
    (
        "stripe_exact_review_required",
        _family_case(
            sender_domain="stripe.com", match_mode="EXACT", policy="MUST_READ",
            destination_mode="REVIEW_REQUIRED", destination_entity_id=None, processor_hint="STRIPE",
        ),
    ),
    (
        "microsoft_exact_fixed",
        _family_case(
            sender_domain="microsoft.com", match_mode="EXACT", policy="MUST_READ",
            destination_mode="FIXED", processor_hint="MICROSOFT",
        ),
    ),
    (
        "microsoft_include_subdomains_graylist",
        _family_case(
            sender_domain="microsoft.com", match_mode="INCLUDE_SUBDOMAINS", policy="GRAYLIST",
            destination_entity_id=None, destination_mode=None, processor_hint="MICROSOFT",
        ),
    ),
    (
        "exact_address_ap_must_read_fixed",
        _family_case(
            sender_domain="vendor.com", match_mode="EXACT_ADDRESS", sender_address="ap@vendor.com",
            policy="MUST_READ", destination_mode="FIXED", processor_hint=None,
        ),
    ),
    (
        "exact_address_spam_blacklist",
        _family_case(
            sender_domain="vendor.com", match_mode="EXACT_ADDRESS", sender_address="spam@vendor.com",
            policy="BLACKLIST", destination_entity_id=None, destination_mode=None, processor_hint=None,
        ),
    ),
]


@pytest.mark.parametrize("family_name, family_overrides", PRODUCTION_RULE_FAMILIES, ids=[f[0] for f in PRODUCTION_RULE_FAMILIES])
def test_every_production_rule_family_shape_still_validates_unchanged(
    make_mailbox_domain_rule, family_name, family_overrides
):
    destination_entity_id = family_overrides.get("destination_entity_id", identity.generate_id())
    overrides = dict(family_overrides)
    overrides.setdefault("destination_entity_id", destination_entity_id)
    instance = make_mailbox_domain_rule(**overrides)
    validate_against_contract(instance, SCHEMA)


# ---------------------------------------------------------------------
# Both real code paths — never just the schema in isolation — must
# actually PRODUCE a schema-valid EXACT_DOMAIN_SUBJECT dict. The
# in-memory repository is proven here (no Docker dependency); the
# PostgreSQL repository's own equivalent proof lives in
# `tests/persistence/test_mailbox_domain_rule_repository.py` (which
# already runs against a real, disposable PostgreSQL container).
# ---------------------------------------------------------------------


def test_in_memory_repository_upsert_produces_a_schema_valid_exact_domain_subject_snapshot():
    repo = InMemoryMailboxDomainRuleRepository()
    mailbox_id = identity.generate_id()
    rule = repo.upsert_rule(
        mailbox_id=mailbox_id, sender_domain="vendor.com", match_mode="EXACT_DOMAIN_SUBJECT", policy="MUST_READ",
        destination_entity_id=None, destination_mode="REVIEW_REQUIRED", source="OPERATOR",
        subject_predicate_type="EXACT", subject_predicate_value="Monthly Statement",
    )
    # A second, independent, direct validation of the produced dict —
    # never merely trusting that `upsert_rule`'s own internal
    # `validate_against_contract` call (which already ran once, inside
    # the repository, before this line) happened to pass.
    validate_against_contract(rule.to_dict(), SCHEMA)
