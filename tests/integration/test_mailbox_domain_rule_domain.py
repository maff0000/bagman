"""CD-6 architect amendment tests for ``services.mailbox.domain_rule``
(the MAILBOX-SPECIFIC domain-policy registry driving Stage B of the
two-stage mail-processing gate). In-memory reference implementation —
mirrors ``tests/integration/test_mailbox_domain.py``'s own style for
``MailboxSource``.
"""
from __future__ import annotations

import pytest

from core import identity
from core.errors import InvalidStateTransitionError, NotFoundError, ValidationError
from services.mailbox.domain_rule import (
    DESTINATION_MODE_FIXED,
    DESTINATION_MODE_REVIEW_REQUIRED,
    MATCH_MODE_EXACT,
    MATCH_MODE_EXACT_ADDRESS,
    MATCH_MODE_INCLUDE_SUBDOMAINS,
    POLICY_MUST_READ,
    POLICY_BLACKLIST,
    POLICY_GRAYLIST,
    SOURCE_OPERATOR,
    InMemoryMailboxDomainRuleRepository,
    normalize_address,
    normalize_domain,
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
