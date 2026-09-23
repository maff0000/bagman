"""CD-6 architect amendment tests for ``services.mailbox.domain_rule``
(the MAILBOX-SPECIFIC domain-policy registry driving Stage B of the
two-stage mail-processing gate). In-memory reference implementation —
mirrors ``tests/integration/test_mailbox_domain.py``'s own style for
``MailboxSource``.
"""
from __future__ import annotations

import dataclasses

import pytest

from core import identity
from core.errors import ConflictError, InvalidStateTransitionError, NotFoundError, ValidationError
from services.mailbox.domain_rule import (
    DESTINATION_MODE_FIXED,
    DESTINATION_MODE_REVIEW_REQUIRED,
    MATCH_MODE_EXACT,
    MATCH_MODE_EXACT_ADDRESS,
    MATCH_MODE_EXACT_DOMAIN_SUBJECT,
    MATCH_MODE_INCLUDE_SUBDOMAINS,
    POLICY_MUST_READ,
    POLICY_BLACKLIST,
    POLICY_GRAYLIST,
    SOURCE_OPERATOR,
    SUBJECT_PREDICATE_EXACT,
    SUBJECT_PREDICATE_STARTS_WITH,
    InMemoryMailboxDomainRuleRepository,
    normalize_address,
    normalize_domain,
    normalize_subject_for_policy,
    subject_matches_predicate,
    transition_policy,
)


@pytest.fixture
def repo() -> InMemoryMailboxDomainRuleRepository:
    return InMemoryMailboxDomainRuleRepository()


def _mailbox_id() -> str:
    return identity.generate_id()


# ---------------------------------------------------------------------
# normalize_domain
# ---------------------------------------------------------------------


def test_normalize_domain_lowercases_and_strips():
    assert normalize_domain("  Vendor.COM  ") == "vendor.com"


# ---------------------------------------------------------------------
# Create / uniqueness — one rule per (mailbox_id, sender_domain)
# ---------------------------------------------------------------------


def test_upsert_creates_a_new_rule(repo):
    mailbox_id = _mailbox_id()
    rule = repo.upsert_rule(
        mailbox_id=mailbox_id, sender_domain="Vendor.com", match_mode=MATCH_MODE_EXACT, policy=POLICY_BLACKLIST,
        destination_entity_id=None, destination_mode=None, source=SOURCE_OPERATOR,
    )
    assert rule.sender_domain == "vendor.com"
    assert rule.policy == POLICY_BLACKLIST
    assert rule.created_at == rule.updated_at == rule.last_seen_at


def test_upsert_for_an_existing_domain_updates_the_same_row(repo):
    mailbox_id = _mailbox_id()
    first = repo.upsert_rule(
        mailbox_id=mailbox_id, sender_domain="vendor.com", match_mode=MATCH_MODE_EXACT, policy=POLICY_BLACKLIST,
        destination_entity_id=None, destination_mode=None, source=SOURCE_OPERATOR,
    )
    second = repo.upsert_rule(
        mailbox_id=mailbox_id, sender_domain="vendor.com", match_mode=MATCH_MODE_EXACT, policy=POLICY_MUST_READ,
        destination_entity_id=None, destination_mode=DESTINATION_MODE_REVIEW_REQUIRED, source=SOURCE_OPERATOR,
    )
    assert second.rule_id == first.rule_id
    assert repo.list_rules(mailbox_id=mailbox_id) == [second]


def test_two_different_mailboxes_never_share_a_rule_row(repo):
    mailbox_a, mailbox_b = _mailbox_id(), _mailbox_id()
    rule_a = repo.upsert_rule(
        mailbox_id=mailbox_a, sender_domain="vendor.com", match_mode=MATCH_MODE_EXACT, policy=POLICY_MUST_READ,
        destination_entity_id=None, destination_mode=DESTINATION_MODE_REVIEW_REQUIRED, source=SOURCE_OPERATOR,
    )
    rule_b = repo.upsert_rule(
        mailbox_id=mailbox_b, sender_domain="vendor.com", match_mode=MATCH_MODE_EXACT, policy=POLICY_BLACKLIST,
        destination_entity_id=None, destination_mode=None, source=SOURCE_OPERATOR,
    )
    assert rule_a.rule_id != rule_b.rule_id


# ---------------------------------------------------------------------
# Policy field validation
# ---------------------------------------------------------------------


def test_allowed_fixed_requires_a_destination_entity_id(repo):
    with pytest.raises(ValidationError):
        repo.upsert_rule(
            mailbox_id=_mailbox_id(), sender_domain="vendor.com", match_mode=MATCH_MODE_EXACT, policy=POLICY_MUST_READ,
            destination_entity_id=None, destination_mode=DESTINATION_MODE_FIXED, source=SOURCE_OPERATOR,
        )


def test_allowed_review_required_permits_a_null_destination(repo):
    rule = repo.upsert_rule(
        mailbox_id=_mailbox_id(), sender_domain="vendor.com", match_mode=MATCH_MODE_EXACT, policy=POLICY_MUST_READ,
        destination_entity_id=None, destination_mode=DESTINATION_MODE_REVIEW_REQUIRED, source=SOURCE_OPERATOR,
    )
    assert rule.destination_entity_id is None


def test_allowed_requires_a_destination_mode(repo):
    with pytest.raises(ValidationError):
        repo.upsert_rule(
            mailbox_id=_mailbox_id(), sender_domain="vendor.com", match_mode=MATCH_MODE_EXACT, policy=POLICY_MUST_READ,
            destination_entity_id=None, destination_mode=None, source=SOURCE_OPERATOR,
        )


def test_ignored_rejects_any_destination_field(repo):
    with pytest.raises(ValidationError):
        repo.upsert_rule(
            mailbox_id=_mailbox_id(), sender_domain="vendor.com", match_mode=MATCH_MODE_EXACT, policy=POLICY_BLACKLIST,
            destination_entity_id=identity.generate_id(), destination_mode=None, source=SOURCE_OPERATOR,
        )


def test_ungoverned_match_mode_rejected(repo):
    with pytest.raises(ValidationError):
        repo.upsert_rule(
            mailbox_id=_mailbox_id(), sender_domain="vendor.com", match_mode="SUBSTRING", policy=POLICY_BLACKLIST,
            destination_entity_id=None, destination_mode=None, source=SOURCE_OPERATOR,
        )


# ---------------------------------------------------------------------
# find_for_sender
# ---------------------------------------------------------------------


def test_exact_rule_matches_only_the_exact_domain(repo):
    mailbox_id = _mailbox_id()
    repo.upsert_rule(
        mailbox_id=mailbox_id, sender_domain="vendor.com", match_mode=MATCH_MODE_EXACT, policy=POLICY_BLACKLIST,
        destination_entity_id=None, destination_mode=None, source=SOURCE_OPERATOR,
    )
    assert repo.find_for_sender(mailbox_id=mailbox_id, sender_domain="vendor.com") is not None
    assert repo.find_for_sender(mailbox_id=mailbox_id, sender_domain="mail.vendor.com") is None


def test_include_subdomains_rule_matches_any_subdomain(repo):
    mailbox_id = _mailbox_id()
    repo.upsert_rule(
        mailbox_id=mailbox_id, sender_domain="vendor.com", match_mode=MATCH_MODE_INCLUDE_SUBDOMAINS,
        policy=POLICY_BLACKLIST, destination_entity_id=None, destination_mode=None, source=SOURCE_OPERATOR,
    )
    assert repo.find_for_sender(mailbox_id=mailbox_id, sender_domain="vendor.com") is not None
    assert repo.find_for_sender(mailbox_id=mailbox_id, sender_domain="billing.mail.vendor.com") is not None
    assert repo.find_for_sender(mailbox_id=mailbox_id, sender_domain="notvendor.com") is None


def test_no_rule_resolves_to_none(repo):
    assert repo.find_for_sender(mailbox_id=_mailbox_id(), sender_domain="unknown.example") is None


# ---------------------------------------------------------------------
# touch_last_seen
# ---------------------------------------------------------------------


def test_touch_last_seen_updates_timestamp_without_changing_policy(repo):
    mailbox_id = _mailbox_id()
    repo.upsert_rule(
        mailbox_id=mailbox_id, sender_domain="vendor.com", match_mode=MATCH_MODE_EXACT, policy=POLICY_MUST_READ,
        destination_entity_id=None, destination_mode=DESTINATION_MODE_REVIEW_REQUIRED, source=SOURCE_OPERATOR,
    )
    from core.timestamps import utc_now

    later = utc_now()
    touched = repo.touch_last_seen(mailbox_id=mailbox_id, sender_domain="vendor.com", seen_at=later)
    assert touched.last_seen_at == later
    assert touched.policy == POLICY_MUST_READ


def test_touch_last_seen_for_an_ungoverned_domain_raises_not_found(repo):
    with pytest.raises(NotFoundError):
        repo.touch_last_seen(mailbox_id=_mailbox_id(), sender_domain="unknown.example", seen_at=None)


# ---------------------------------------------------------------------
# transition_policy — the real, tested IGNORED<->ALLOWED lifecycle
# ---------------------------------------------------------------------


def test_transition_policy_ignored_to_allowed_requires_destination_fields():
    mailbox_id = _mailbox_id()
    repo = InMemoryMailboxDomainRuleRepository()
    rule = repo.upsert_rule(
        mailbox_id=mailbox_id, sender_domain="vendor.com", match_mode=MATCH_MODE_EXACT, policy=POLICY_BLACKLIST,
        destination_entity_id=None, destination_mode=None, source=SOURCE_OPERATOR,
    )
    updated = transition_policy(
        rule, POLICY_MUST_READ, destination_entity_id=None, destination_mode=DESTINATION_MODE_REVIEW_REQUIRED
    )
    assert updated.policy == POLICY_MUST_READ


def test_transition_policy_same_value_is_rejected_not_a_no_op():
    """ALLOWED_POLICY_TRANSITIONS only names ALLOWED<->IGNORED — a
    same-value 'transition' is not itself in that table (upsert_rule's
    own idempotent-update semantics handle the ordinary "operator
    resubmits the same decision" case at a higher layer; this function
    is the raw state-machine primitive)."""
    mailbox_id = _mailbox_id()
    repo = InMemoryMailboxDomainRuleRepository()
    rule = repo.upsert_rule(
        mailbox_id=mailbox_id, sender_domain="vendor.com", match_mode=MATCH_MODE_EXACT, policy=POLICY_BLACKLIST,
        destination_entity_id=None, destination_mode=None, source=SOURCE_OPERATOR,
    )
    with pytest.raises(InvalidStateTransitionError):
        transition_policy(rule, POLICY_BLACKLIST)


def test_upsert_rule_lifecycle_ignored_then_allowed_then_ignored_again(repo):
    """Architect spec §6: an operator must later be able to change an
    ignored domain back to allowed — and, symmetrically, back again."""
    mailbox_id = _mailbox_id()
    repo.upsert_rule(
        mailbox_id=mailbox_id, sender_domain="vendor.com", match_mode=MATCH_MODE_EXACT, policy=POLICY_BLACKLIST,
        destination_entity_id=None, destination_mode=None, source=SOURCE_OPERATOR,
    )
    allowed = repo.upsert_rule(
        mailbox_id=mailbox_id, sender_domain="vendor.com", match_mode=MATCH_MODE_EXACT, policy=POLICY_MUST_READ,
        destination_entity_id=None, destination_mode=DESTINATION_MODE_REVIEW_REQUIRED, source=SOURCE_OPERATOR,
    )
    assert allowed.policy == POLICY_MUST_READ
    ignored_again = repo.upsert_rule(
        mailbox_id=mailbox_id, sender_domain="vendor.com", match_mode=MATCH_MODE_EXACT, policy=POLICY_BLACKLIST,
        destination_entity_id=None, destination_mode=None, source=SOURCE_OPERATOR,
    )
    assert ignored_again.policy == POLICY_BLACKLIST
    assert ignored_again.rule_id == allowed.rule_id


# ---------------------------------------------------------------------
# CD-6 GUI-operations-foundation follow-on WO — three-state policy
# model: GRAYLIST as a real, persistable state, and full reversibility
# across all three states.
# ---------------------------------------------------------------------


def test_graylist_is_a_real_persistable_policy(repo):
    mailbox_id = _mailbox_id()
    rule = repo.upsert_rule(
        mailbox_id=mailbox_id, sender_domain="vendor.com", match_mode=MATCH_MODE_EXACT, policy=POLICY_GRAYLIST,
        destination_entity_id=None, destination_mode=None, source=SOURCE_OPERATOR,
    )
    assert rule.policy == POLICY_GRAYLIST
    assert repo.find_for_sender(mailbox_id=mailbox_id, sender_domain="vendor.com").policy == POLICY_GRAYLIST


def test_graylist_rejects_a_destination():
    with pytest.raises(ValidationError):
        InMemoryMailboxDomainRuleRepository().upsert_rule(
            mailbox_id=_mailbox_id(), sender_domain="vendor.com", match_mode=MATCH_MODE_EXACT, policy=POLICY_GRAYLIST,
            destination_entity_id=identity.generate_id(), destination_mode=None, source=SOURCE_OPERATOR,
        )


@pytest.mark.parametrize(
    "start,end",
    [
        (POLICY_MUST_READ, POLICY_GRAYLIST),
        (POLICY_MUST_READ, POLICY_BLACKLIST),
        (POLICY_GRAYLIST, POLICY_MUST_READ),
        (POLICY_GRAYLIST, POLICY_BLACKLIST),
        (POLICY_BLACKLIST, POLICY_MUST_READ),
        (POLICY_BLACKLIST, POLICY_GRAYLIST),
    ],
)
def test_every_policy_can_transition_to_either_other_policy(start, end):
    """The architect's own explicit 'reversible' requirement, generalised
    to all three states: there is no state a confirmed decision cannot
    later be changed away from."""
    destination_kwargs = (
        {"destination_entity_id": None, "destination_mode": DESTINATION_MODE_REVIEW_REQUIRED}
        if start == POLICY_MUST_READ
        else {"destination_entity_id": None, "destination_mode": None}
    )
    rule = InMemoryMailboxDomainRuleRepository().upsert_rule(
        mailbox_id=_mailbox_id(), sender_domain="vendor.com", match_mode=MATCH_MODE_EXACT, policy=start,
        source=SOURCE_OPERATOR, **destination_kwargs,
    )
    end_kwargs = (
        {"destination_entity_id": None, "destination_mode": DESTINATION_MODE_REVIEW_REQUIRED}
        if end == POLICY_MUST_READ
        else {"destination_entity_id": None, "destination_mode": None}
    )
    updated = transition_policy(rule, end, **end_kwargs)
    assert updated.policy == end


# ---------------------------------------------------------------------
# CD-6 GUI-operations-foundation follow-on WO — MATCH_MODE_EXACT_ADDRESS:
# a genuinely new, more-specific match tier, plus most-specific-rule-
# wins resolution (WO required test #9).
# ---------------------------------------------------------------------


def test_normalize_address_lowercases_and_strips():
    assert normalize_address("  Alice@Vendor.COM  ") == "alice@vendor.com"


def test_exact_address_rule_requires_a_sender_address(repo):
    with pytest.raises(ValidationError):
        repo.upsert_rule(
            mailbox_id=_mailbox_id(), sender_domain="vendor.com", match_mode=MATCH_MODE_EXACT_ADDRESS,
            policy=POLICY_BLACKLIST, destination_entity_id=None, destination_mode=None, source=SOURCE_OPERATOR,
        )


def test_domain_level_rule_rejects_a_sender_address(repo):
    with pytest.raises(ValidationError):
        repo.upsert_rule(
            mailbox_id=_mailbox_id(), sender_domain="vendor.com", match_mode=MATCH_MODE_EXACT,
            policy=POLICY_BLACKLIST, destination_entity_id=None, destination_mode=None, source=SOURCE_OPERATOR,
            sender_address="alice@vendor.com",
        )


def test_exact_address_rule_derives_its_own_sender_domain(repo):
    rule = repo.upsert_rule(
        mailbox_id=_mailbox_id(), sender_domain="", match_mode=MATCH_MODE_EXACT_ADDRESS, policy=POLICY_MUST_READ,
        destination_entity_id=None, destination_mode=DESTINATION_MODE_REVIEW_REQUIRED, source=SOURCE_OPERATOR,
        sender_address="Alice@Vendor.com",
    )
    assert rule.sender_address == "alice@vendor.com"
    assert rule.sender_domain == "vendor.com"


def test_exact_address_rule_overrides_a_broader_domain_rule_for_the_same_domain(repo):
    """WO required test #9, verbatim: an EXACT_ADDRESS rule for one
    specific sender correctly overrides a broader EXACT(domain) rule
    that would otherwise apply to that same domain — the MORE specific
    rule wins."""
    mailbox_id = _mailbox_id()
    repo.upsert_rule(
        mailbox_id=mailbox_id, sender_domain="vendor.com", match_mode=MATCH_MODE_EXACT, policy=POLICY_GRAYLIST,
        destination_entity_id=None, destination_mode=None, source=SOURCE_OPERATOR,
    )
    entity_id = identity.generate_id()
    repo.upsert_rule(
        mailbox_id=mailbox_id, sender_domain="vendor.com", match_mode=MATCH_MODE_EXACT_ADDRESS, policy=POLICY_MUST_READ,
        destination_entity_id=entity_id, destination_mode=DESTINATION_MODE_FIXED, source=SOURCE_OPERATOR,
        sender_address="ap@vendor.com",
    )

    # The specific address's own rule wins...
    specific = repo.find_for_sender(mailbox_id=mailbox_id, sender_domain="vendor.com", sender_address="ap@vendor.com")
    assert specific.policy == POLICY_MUST_READ
    assert specific.destination_entity_id == entity_id

    # ...while every OTHER address at the same domain still falls
    # through to the broader domain-level rule.
    other = repo.find_for_sender(mailbox_id=mailbox_id, sender_domain="vendor.com", sender_address="billing@vendor.com")
    assert other.policy == POLICY_GRAYLIST


def test_two_different_addresses_at_the_same_domain_get_independent_rows(repo):
    mailbox_id = _mailbox_id()
    entity_a, entity_b = identity.generate_id(), identity.generate_id()
    rule_a = repo.upsert_rule(
        mailbox_id=mailbox_id, sender_domain="vendor.com", match_mode=MATCH_MODE_EXACT_ADDRESS, policy=POLICY_MUST_READ,
        destination_entity_id=entity_a, destination_mode=DESTINATION_MODE_FIXED, source=SOURCE_OPERATOR,
        sender_address="alice@vendor.com",
    )
    rule_b = repo.upsert_rule(
        mailbox_id=mailbox_id, sender_domain="vendor.com", match_mode=MATCH_MODE_EXACT_ADDRESS, policy=POLICY_MUST_READ,
        destination_entity_id=entity_b, destination_mode=DESTINATION_MODE_FIXED, source=SOURCE_OPERATOR,
        sender_address="bob@vendor.com",
    )
    assert rule_a.rule_id != rule_b.rule_id
    assert repo.find_for_sender(mailbox_id=mailbox_id, sender_domain="vendor.com", sender_address="alice@vendor.com").destination_entity_id == entity_a
    assert repo.find_for_sender(mailbox_id=mailbox_id, sender_domain="vendor.com", sender_address="bob@vendor.com").destination_entity_id == entity_b


def test_no_sender_address_supplied_skips_straight_to_domain_level_resolution(repo):
    """A caller (e.g. the aggregate `touch_last_seen`/bootstrap-floor
    call sites) that never supplies `sender_address` at all still
    resolves the domain-level rule exactly as before — the new
    EXACT_ADDRESS tier is purely additive."""
    mailbox_id = _mailbox_id()
    repo.upsert_rule(
        mailbox_id=mailbox_id, sender_domain="vendor.com", match_mode=MATCH_MODE_EXACT, policy=POLICY_BLACKLIST,
        destination_entity_id=None, destination_mode=None, source=SOURCE_OPERATOR,
    )
    found = repo.find_for_sender(mailbox_id=mailbox_id, sender_domain="vendor.com")
    assert found is not None
    assert found.policy == POLICY_BLACKLIST


# ---------------------------------------------------------------------
# Deterministic subject-aware mailbox domain policy (CD-6 GUI-operations-
# foundation follow-on WO) — normalize_subject_for_policy
# ---------------------------------------------------------------------


def test_normalize_subject_for_policy_none_in_none_out():
    assert normalize_subject_for_policy(None) is None


def test_normalize_subject_for_policy_is_idempotent():
    subject = "  Order Confirmed:   iPad  "
    once = normalize_subject_for_policy(subject)
    twice = normalize_subject_for_policy(once)
    assert once == twice


def test_normalize_subject_for_policy_casefolds():
    assert normalize_subject_for_policy("ORDER CONFIRMED") == normalize_subject_for_policy("order confirmed")


def test_normalize_subject_for_policy_strips_leading_and_trailing_whitespace():
    assert normalize_subject_for_policy("   Invoice   ") == "invoice"


def test_normalize_subject_for_policy_collapses_repeated_internal_whitespace():
    """Real, observed eBay defect — a literal double space inside a
    subject line must not silently defeat an otherwise-correct
    predicate."""
    assert normalize_subject_for_policy("Order  confirmed:  Apple iPad  (9th generation)") == \
        "order confirmed: apple ipad (9th generation)"


def test_normalize_subject_for_policy_preserves_punctuation_and_digits():
    normalized = normalize_subject_for_policy("Invoice #12345 - Due 2026-09-23!")
    assert "12345" in normalized
    assert "2026-09-23" in normalized
    assert "#" in normalized
    assert "!" in normalized


def test_normalize_subject_for_policy_does_not_strip_re_or_fwd_prefixes():
    assert normalize_subject_for_policy("Re: Order confirmed") == "re: order confirmed"
    assert normalize_subject_for_policy("Fwd: Order confirmed") == "fwd: order confirmed"


def test_normalize_subject_for_policy_nfkc_canonicalizes_full_width_forms():
    # Full-width "Order" (U+FF2F etc.) vs. ordinary ASCII "Order" —
    # NFKC canonicalises full-width Latin forms to their ASCII
    # equivalents before casefolding.
    full_width = "Ｏｒｄｅｒ"  # "Order" in full-width forms
    assert normalize_subject_for_policy(full_width) == "order"


# ---------------------------------------------------------------------
# subject_matches_predicate
# ---------------------------------------------------------------------


def test_subject_matches_predicate_exact():
    assert subject_matches_predicate("Monthly Statement", predicate_type=SUBJECT_PREDICATE_EXACT, predicate_value="monthly statement")
    assert not subject_matches_predicate("Monthly Statement 2", predicate_type=SUBJECT_PREDICATE_EXACT, predicate_value="monthly statement")


def test_subject_matches_predicate_starts_with():
    assert subject_matches_predicate(
        "Order confirmed: iPad", predicate_type=SUBJECT_PREDICATE_STARTS_WITH, predicate_value="order confirmed:"
    )
    assert not subject_matches_predicate(
        "Order pending: iPad", predicate_type=SUBJECT_PREDICATE_STARTS_WITH, predicate_value="order confirmed:"
    )


def test_subject_matches_predicate_null_subject_never_matches():
    assert not subject_matches_predicate(None, predicate_type=SUBJECT_PREDICATE_EXACT, predicate_value="anything")


# ---------------------------------------------------------------------
# EXACT_DOMAIN_SUBJECT — validation
# ---------------------------------------------------------------------


def test_exact_domain_subject_requires_a_predicate_type(repo):
    with pytest.raises(ValidationError):
        repo.upsert_rule(
            mailbox_id=_mailbox_id(), sender_domain="vendor.com", match_mode=MATCH_MODE_EXACT_DOMAIN_SUBJECT,
            policy=POLICY_BLACKLIST, destination_entity_id=None, destination_mode=None, source=SOURCE_OPERATOR,
            subject_predicate_value="monthly statement",
        )


def test_exact_domain_subject_requires_a_non_empty_normalized_predicate_value(repo):
    with pytest.raises(ValidationError):
        repo.upsert_rule(
            mailbox_id=_mailbox_id(), sender_domain="vendor.com", match_mode=MATCH_MODE_EXACT_DOMAIN_SUBJECT,
            policy=POLICY_BLACKLIST, destination_entity_id=None, destination_mode=None, source=SOURCE_OPERATOR,
            subject_predicate_type=SUBJECT_PREDICATE_EXACT, subject_predicate_value="   ",
        )


def test_exact_domain_subject_rejects_a_sender_address(repo):
    with pytest.raises(ValidationError):
        repo.upsert_rule(
            mailbox_id=_mailbox_id(), sender_domain="vendor.com", match_mode=MATCH_MODE_EXACT_DOMAIN_SUBJECT,
            policy=POLICY_BLACKLIST, destination_entity_id=None, destination_mode=None, source=SOURCE_OPERATOR,
            subject_predicate_type=SUBJECT_PREDICATE_EXACT, subject_predicate_value="monthly statement",
            sender_address="ap@vendor.com",
        )


def test_every_other_match_mode_rejects_subject_predicate_fields(repo):
    with pytest.raises(ValidationError):
        repo.upsert_rule(
            mailbox_id=_mailbox_id(), sender_domain="vendor.com", match_mode=MATCH_MODE_EXACT,
            policy=POLICY_BLACKLIST, destination_entity_id=None, destination_mode=None, source=SOURCE_OPERATOR,
            subject_predicate_type=SUBJECT_PREDICATE_EXACT, subject_predicate_value="monthly statement",
        )


def test_exact_domain_subject_rejects_graylist(repo):
    """Item 24 — subject-scoped GRAYLIST is explicitly unsupported in V1."""
    with pytest.raises(ValidationError):
        repo.upsert_rule(
            mailbox_id=_mailbox_id(), sender_domain="vendor.com", match_mode=MATCH_MODE_EXACT_DOMAIN_SUBJECT,
            policy=POLICY_GRAYLIST, destination_entity_id=None, destination_mode=None, source=SOURCE_OPERATOR,
            subject_predicate_type=SUBJECT_PREDICATE_EXACT, subject_predicate_value="monthly statement",
        )


def test_exact_domain_subject_supports_blacklist(repo):
    rule = repo.upsert_rule(
        mailbox_id=_mailbox_id(), sender_domain="vendor.com", match_mode=MATCH_MODE_EXACT_DOMAIN_SUBJECT,
        policy=POLICY_BLACKLIST, destination_entity_id=None, destination_mode=None, source=SOURCE_OPERATOR,
        subject_predicate_type=SUBJECT_PREDICATE_EXACT, subject_predicate_value="Marketing Blast",
    )
    assert rule.policy == POLICY_BLACKLIST
    assert rule.subject_predicate_value == "marketing blast"  # stored normalised


def test_exact_domain_subject_supports_must_read():
    entity_id = identity.generate_id()
    rule = InMemoryMailboxDomainRuleRepository().upsert_rule(
        mailbox_id=_mailbox_id(), sender_domain="vendor.com", match_mode=MATCH_MODE_EXACT_DOMAIN_SUBJECT,
        policy=POLICY_MUST_READ, destination_entity_id=entity_id, destination_mode=DESTINATION_MODE_FIXED,
        source=SOURCE_OPERATOR, subject_predicate_type=SUBJECT_PREDICATE_STARTS_WITH,
        subject_predicate_value="Monthly Statement",
    )
    assert rule.policy == POLICY_MUST_READ
    assert rule.destination_entity_id == entity_id


def test_exact_domain_subject_value_stored_in_canonical_normalized_form(repo):
    rule = repo.upsert_rule(
        mailbox_id=_mailbox_id(), sender_domain="vendor.com", match_mode=MATCH_MODE_EXACT_DOMAIN_SUBJECT,
        policy=POLICY_BLACKLIST, destination_entity_id=None, destination_mode=None, source=SOURCE_OPERATOR,
        subject_predicate_type=SUBJECT_PREDICATE_STARTS_WITH, subject_predicate_value="  Order   Confirmed:  ",
    )
    assert rule.subject_predicate_value == "order confirmed:"


def test_exact_domain_subject_identity_is_a_third_separate_space_from_domain_and_address(repo):
    """One domain can hold a domain-level fallback rule, an EXACT_ADDRESS
    override, and an EXACT_DOMAIN_SUBJECT rule simultaneously, without
    the old (pre-correction) index incorrectly colliding them."""
    mailbox_id = _mailbox_id()
    domain_rule = repo.upsert_rule(
        mailbox_id=mailbox_id, sender_domain="vendor.com", match_mode=MATCH_MODE_EXACT, policy=POLICY_GRAYLIST,
        destination_entity_id=None, destination_mode=None, source=SOURCE_OPERATOR,
    )
    address_rule = repo.upsert_rule(
        mailbox_id=mailbox_id, sender_domain="vendor.com", match_mode=MATCH_MODE_EXACT_ADDRESS,
        policy=POLICY_BLACKLIST, destination_entity_id=None, destination_mode=None, source=SOURCE_OPERATOR,
        sender_address="spam@vendor.com",
    )
    subject_rule = repo.upsert_rule(
        mailbox_id=mailbox_id, sender_domain="vendor.com", match_mode=MATCH_MODE_EXACT_DOMAIN_SUBJECT,
        policy=POLICY_BLACKLIST, destination_entity_id=None, destination_mode=None, source=SOURCE_OPERATOR,
        subject_predicate_type=SUBJECT_PREDICATE_EXACT, subject_predicate_value="marketing",
    )
    assert len({domain_rule.rule_id, address_rule.rule_id, subject_rule.rule_id}) == 3
    assert len(repo.list_rules(mailbox_id=mailbox_id)) == 3


def test_exact_domain_subject_upsert_is_resolve_or_create_or_update_at_its_own_identity(repo):
    mailbox_id = _mailbox_id()
    first = repo.upsert_rule(
        mailbox_id=mailbox_id, sender_domain="vendor.com", match_mode=MATCH_MODE_EXACT_DOMAIN_SUBJECT,
        policy=POLICY_BLACKLIST, destination_entity_id=None, destination_mode=None, source=SOURCE_OPERATOR,
        subject_predicate_type=SUBJECT_PREDICATE_EXACT, subject_predicate_value="monthly statement",
    )
    second = repo.upsert_rule(
        mailbox_id=mailbox_id, sender_domain="vendor.com", match_mode=MATCH_MODE_EXACT_DOMAIN_SUBJECT,
        policy=POLICY_MUST_READ, destination_entity_id=None, destination_mode=DESTINATION_MODE_REVIEW_REQUIRED,
        source=SOURCE_OPERATOR, subject_predicate_type=SUBJECT_PREDICATE_EXACT,
        subject_predicate_value="Monthly Statement",  # case/whitespace-equivalent -> same identity
    )
    assert second.rule_id == first.rule_id
    assert second.policy == POLICY_MUST_READ

    # A genuinely DIFFERENT predicate value creates a fresh row.
    different = repo.upsert_rule(
        mailbox_id=mailbox_id, sender_domain="vendor.com", match_mode=MATCH_MODE_EXACT_DOMAIN_SUBJECT,
        policy=POLICY_BLACKLIST, destination_entity_id=None, destination_mode=None, source=SOURCE_OPERATOR,
        subject_predicate_type=SUBJECT_PREDICATE_EXACT, subject_predicate_value="marketing blast",
    )
    assert different.rule_id != first.rule_id


# ---------------------------------------------------------------------
# find_exact — EXACT_DOMAIN_SUBJECT identity
# ---------------------------------------------------------------------


def test_find_exact_subject_requires_both_predicate_fields(repo):
    with pytest.raises(ValidationError):
        repo.find_exact(
            mailbox_id=_mailbox_id(), sender_domain="vendor.com", match_mode=MATCH_MODE_EXACT_DOMAIN_SUBJECT,
            subject_predicate_type=SUBJECT_PREDICATE_EXACT,
        )


def test_find_exact_subject_returns_none_for_a_never_governed_predicate(repo):
    assert repo.find_exact(
        mailbox_id=_mailbox_id(), sender_domain="vendor.com", match_mode=MATCH_MODE_EXACT_DOMAIN_SUBJECT,
        subject_predicate_type=SUBJECT_PREDICATE_EXACT, subject_predicate_value="monthly statement",
    ) is None


def test_find_exact_subject_never_falls_back_to_the_domain_rule(repo):
    mailbox_id = _mailbox_id()
    repo.upsert_rule(
        mailbox_id=mailbox_id, sender_domain="vendor.com", match_mode=MATCH_MODE_EXACT, policy=POLICY_MUST_READ,
        destination_entity_id=None, destination_mode=DESTINATION_MODE_REVIEW_REQUIRED, source=SOURCE_OPERATOR,
    )
    assert repo.find_exact(
        mailbox_id=mailbox_id, sender_domain="vendor.com", match_mode=MATCH_MODE_EXACT_DOMAIN_SUBJECT,
        subject_predicate_type=SUBJECT_PREDICATE_EXACT, subject_predicate_value="monthly statement",
    ) is None


# ---------------------------------------------------------------------
# find_for_sender — 4-tier precedence (subject-aware)
# ---------------------------------------------------------------------


def test_exact_address_beats_subject_rule(repo):
    mailbox_id = _mailbox_id()
    repo.upsert_rule(
        mailbox_id=mailbox_id, sender_domain="vendor.com", match_mode=MATCH_MODE_EXACT_DOMAIN_SUBJECT,
        policy=POLICY_MUST_READ, destination_entity_id=None, destination_mode=DESTINATION_MODE_REVIEW_REQUIRED,
        source=SOURCE_OPERATOR, subject_predicate_type=SUBJECT_PREDICATE_EXACT, subject_predicate_value="monthly statement",
    )
    repo.upsert_rule(
        mailbox_id=mailbox_id, sender_domain="vendor.com", match_mode=MATCH_MODE_EXACT_ADDRESS,
        policy=POLICY_BLACKLIST, destination_entity_id=None, destination_mode=None, source=SOURCE_OPERATOR,
        sender_address="ap@vendor.com",
    )
    found = repo.find_for_sender(
        mailbox_id=mailbox_id, sender_domain="vendor.com", sender_address="ap@vendor.com", subject="Monthly Statement"
    )
    assert found.policy == POLICY_BLACKLIST
    assert found.match_mode == MATCH_MODE_EXACT_ADDRESS


def test_exact_predicate_beats_starts_with_predicate(repo):
    mailbox_id = _mailbox_id()
    exact_rule = repo.upsert_rule(
        mailbox_id=mailbox_id, sender_domain="vendor.com", match_mode=MATCH_MODE_EXACT_DOMAIN_SUBJECT,
        policy=POLICY_MUST_READ, destination_entity_id=None, destination_mode=DESTINATION_MODE_REVIEW_REQUIRED,
        source=SOURCE_OPERATOR, subject_predicate_type=SUBJECT_PREDICATE_EXACT, subject_predicate_value="order confirmed: ipad",
    )
    repo.upsert_rule(
        mailbox_id=mailbox_id, sender_domain="vendor.com", match_mode=MATCH_MODE_EXACT_DOMAIN_SUBJECT,
        policy=POLICY_BLACKLIST, destination_entity_id=None, destination_mode=None, source=SOURCE_OPERATOR,
        subject_predicate_type=SUBJECT_PREDICATE_STARTS_WITH, subject_predicate_value="order",
    )
    found = repo.find_for_sender(mailbox_id=mailbox_id, sender_domain="vendor.com", subject="Order confirmed: iPad")
    assert found.rule_id == exact_rule.rule_id
    assert found.policy == POLICY_MUST_READ


def test_longest_starts_with_prefix_wins():
    """WO's own literal example: STARTS_WITH 'order' vs. STARTS_WITH
    'order confirmed:' against subject 'Order confirmed: iPad' — the
    longer, more specific prefix wins."""
    repo = InMemoryMailboxDomainRuleRepository()
    mailbox_id = _mailbox_id()
    short_rule = repo.upsert_rule(
        mailbox_id=mailbox_id, sender_domain="vendor.com", match_mode=MATCH_MODE_EXACT_DOMAIN_SUBJECT,
        policy=POLICY_BLACKLIST, destination_entity_id=None, destination_mode=None, source=SOURCE_OPERATOR,
        subject_predicate_type=SUBJECT_PREDICATE_STARTS_WITH, subject_predicate_value="order",
    )
    long_rule = repo.upsert_rule(
        mailbox_id=mailbox_id, sender_domain="vendor.com", match_mode=MATCH_MODE_EXACT_DOMAIN_SUBJECT,
        policy=POLICY_MUST_READ, destination_entity_id=None, destination_mode=DESTINATION_MODE_REVIEW_REQUIRED,
        source=SOURCE_OPERATOR, subject_predicate_type=SUBJECT_PREDICATE_STARTS_WITH,
        subject_predicate_value="order confirmed:",
    )
    assert short_rule.rule_id != long_rule.rule_id
    found = repo.find_for_sender(mailbox_id=mailbox_id, sender_domain="vendor.com", subject="Order confirmed: iPad")
    assert found.rule_id == long_rule.rule_id
    assert found.policy == POLICY_MUST_READ


def test_subject_rule_only_matches_the_exact_domain_never_a_subdomain(repo):
    mailbox_id = _mailbox_id()
    repo.upsert_rule(
        mailbox_id=mailbox_id, sender_domain="vendor.com", match_mode=MATCH_MODE_EXACT_DOMAIN_SUBJECT,
        policy=POLICY_MUST_READ, destination_entity_id=None, destination_mode=DESTINATION_MODE_REVIEW_REQUIRED,
        source=SOURCE_OPERATOR, subject_predicate_type=SUBJECT_PREDICATE_EXACT, subject_predicate_value="monthly statement",
    )
    found = repo.find_for_sender(mailbox_id=mailbox_id, sender_domain="mail.vendor.com", subject="Monthly Statement")
    assert found is None


def test_domain_fallback_used_when_no_subject_rule_matches(repo):
    mailbox_id = _mailbox_id()
    repo.upsert_rule(
        mailbox_id=mailbox_id, sender_domain="vendor.com", match_mode=MATCH_MODE_EXACT_DOMAIN_SUBJECT,
        policy=POLICY_MUST_READ, destination_entity_id=None, destination_mode=DESTINATION_MODE_REVIEW_REQUIRED,
        source=SOURCE_OPERATOR, subject_predicate_type=SUBJECT_PREDICATE_EXACT, subject_predicate_value="monthly statement",
    )
    repo.upsert_rule(
        mailbox_id=mailbox_id, sender_domain="vendor.com", match_mode=MATCH_MODE_EXACT, policy=POLICY_GRAYLIST,
        destination_entity_id=None, destination_mode=None, source=SOURCE_OPERATOR,
    )
    found = repo.find_for_sender(mailbox_id=mailbox_id, sender_domain="vendor.com", subject="Marketing blast")
    assert found.policy == POLICY_GRAYLIST
    assert found.match_mode == MATCH_MODE_EXACT


def test_same_domain_include_subdomains_rule_is_the_direct_fallback_not_parent(repo):
    mailbox_id = _mailbox_id()
    repo.upsert_rule(
        mailbox_id=mailbox_id, sender_domain="vendor.com", match_mode=MATCH_MODE_INCLUDE_SUBDOMAINS,
        policy=POLICY_BLACKLIST, destination_entity_id=None, destination_mode=None, source=SOURCE_OPERATOR,
    )
    found = repo.find_for_sender(mailbox_id=mailbox_id, sender_domain="vendor.com", subject="anything")
    assert found is not None
    assert found.policy == POLICY_BLACKLIST


def test_parent_include_subdomains_rule_used_when_no_direct_or_subject_rule_matches(repo):
    mailbox_id = _mailbox_id()
    repo.upsert_rule(
        mailbox_id=mailbox_id, sender_domain="vendor.com", match_mode=MATCH_MODE_INCLUDE_SUBDOMAINS,
        policy=POLICY_BLACKLIST, destination_entity_id=None, destination_mode=None, source=SOURCE_OPERATOR,
    )
    found = repo.find_for_sender(mailbox_id=mailbox_id, sender_domain="billing.vendor.com", subject="anything")
    assert found is not None
    assert found.policy == POLICY_BLACKLIST
    assert found.match_mode == MATCH_MODE_INCLUDE_SUBDOMAINS


def test_no_rule_matches_at_all_resolves_to_none_with_subject_supplied(repo):
    assert repo.find_for_sender(mailbox_id=_mailbox_id(), sender_domain="unknown.example", subject="anything") is None


def test_null_subject_skips_tier_2_entirely_and_falls_through_to_domain_fallback(repo):
    mailbox_id = _mailbox_id()
    repo.upsert_rule(
        mailbox_id=mailbox_id, sender_domain="vendor.com", match_mode=MATCH_MODE_EXACT_DOMAIN_SUBJECT,
        policy=POLICY_MUST_READ, destination_entity_id=None, destination_mode=DESTINATION_MODE_REVIEW_REQUIRED,
        source=SOURCE_OPERATOR, subject_predicate_type=SUBJECT_PREDICATE_EXACT, subject_predicate_value="monthly statement",
    )
    repo.upsert_rule(
        mailbox_id=mailbox_id, sender_domain="vendor.com", match_mode=MATCH_MODE_EXACT, policy=POLICY_GRAYLIST,
        destination_entity_id=None, destination_mode=None, source=SOURCE_OPERATOR,
    )
    found = repo.find_for_sender(mailbox_id=mailbox_id, sender_domain="vendor.com", subject=None)
    assert found.policy == POLICY_GRAYLIST


# ---------------------------------------------------------------------
# Fail-closed corruption test hook — item 9/30
# ---------------------------------------------------------------------


def test_two_ambiguous_exact_predicates_raise_conflict_error_never_none_never_silent_pick(repo):
    """Constructed by going straight at the repository's internal
    identity dicts, bypassing the normal validated `upsert_rule` write
    path entirely — the only way two colliding EXACT predicate
    identities could ever coexist (the real uniqueness index makes this
    structurally impossible via the normal path)."""
    mailbox_id = _mailbox_id()
    repo.upsert_rule(
        mailbox_id=mailbox_id, sender_domain="vendor.com", match_mode=MATCH_MODE_EXACT, policy=POLICY_GRAYLIST,
        destination_entity_id=None, destination_mode=None, source=SOURCE_OPERATOR,
    )
    rule_a = repo.upsert_rule(
        mailbox_id=mailbox_id, sender_domain="vendor.com", match_mode=MATCH_MODE_EXACT_DOMAIN_SUBJECT,
        policy=POLICY_MUST_READ, destination_entity_id=None, destination_mode=DESTINATION_MODE_REVIEW_REQUIRED,
        source=SOURCE_OPERATOR, subject_predicate_type=SUBJECT_PREDICATE_EXACT, subject_predicate_value="monthly statement",
    )
    # Directly inject a SECOND row at the SAME (mailbox_id, domain,
    # EXACT, "monthly statement") identity, bypassing upsert_rule's own
    # uniqueness dict entirely — pure corruption simulation.
    corrupt = dataclasses.replace(rule_a, rule_id=identity.generate_id(), policy=POLICY_BLACKLIST)
    repo._by_id[corrupt.rule_id] = corrupt  # noqa: SLF001 - deliberate corruption-test bypass

    with pytest.raises(ConflictError):
        repo.find_for_sender(mailbox_id=mailbox_id, sender_domain="vendor.com", subject="Monthly Statement")


def test_two_ambiguous_equal_length_starts_with_predicates_raise_conflict_error(repo):
    """Two DIFFERENT STARTS_WITH values of the same length can never
    BOTH genuinely match the same subject (mathematically: if they did,
    both would have to equal the subject's own same-length prefix,
    making them identical) — so the only way to reach the genuinely
    ambiguous, tied-longest-length branch at all is two rows sharing the
    literal SAME predicate value, which the real uniqueness index
    already prevents in ordinary operation. Reached here only by
    injecting a second row directly, bypassing `upsert_rule` entirely."""
    mailbox_id = _mailbox_id()
    rule_a = repo.upsert_rule(
        mailbox_id=mailbox_id, sender_domain="vendor.com", match_mode=MATCH_MODE_EXACT_DOMAIN_SUBJECT,
        policy=POLICY_MUST_READ, destination_entity_id=None, destination_mode=DESTINATION_MODE_REVIEW_REQUIRED,
        source=SOURCE_OPERATOR, subject_predicate_type=SUBJECT_PREDICATE_STARTS_WITH, subject_predicate_value="order",
    )
    corrupt = dataclasses.replace(rule_a, rule_id=identity.generate_id(), policy=POLICY_BLACKLIST)
    repo._by_id[corrupt.rule_id] = corrupt  # noqa: SLF001 - deliberate corruption-test bypass

    with pytest.raises(ConflictError):
        repo.find_for_sender(mailbox_id=mailbox_id, sender_domain="vendor.com", subject="Order confirmed: iPad")
