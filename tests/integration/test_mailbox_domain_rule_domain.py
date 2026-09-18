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
    MATCH_MODE_INCLUDE_SUBDOMAINS,
    POLICY_ALLOWED,
    POLICY_IGNORED,
    SOURCE_OPERATOR,
    InMemoryMailboxDomainRuleRepository,
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
        mailbox_id=mailbox_id, sender_domain="Vendor.com", match_mode=MATCH_MODE_EXACT, policy=POLICY_IGNORED,
        destination_entity_id=None, destination_mode=None, source=SOURCE_OPERATOR,
    )
    assert rule.sender_domain == "vendor.com"
    assert rule.policy == POLICY_IGNORED
    assert rule.created_at == rule.updated_at == rule.last_seen_at


def test_upsert_for_an_existing_domain_updates_the_same_row(repo):
    mailbox_id = _mailbox_id()
    first = repo.upsert_rule(
        mailbox_id=mailbox_id, sender_domain="vendor.com", match_mode=MATCH_MODE_EXACT, policy=POLICY_IGNORED,
        destination_entity_id=None, destination_mode=None, source=SOURCE_OPERATOR,
    )
    second = repo.upsert_rule(
        mailbox_id=mailbox_id, sender_domain="vendor.com", match_mode=MATCH_MODE_EXACT, policy=POLICY_ALLOWED,
        destination_entity_id=None, destination_mode=DESTINATION_MODE_REVIEW_REQUIRED, source=SOURCE_OPERATOR,
    )
    assert second.rule_id == first.rule_id
    assert repo.list_rules(mailbox_id=mailbox_id) == [second]


def test_two_different_mailboxes_never_share_a_rule_row(repo):
    mailbox_a, mailbox_b = _mailbox_id(), _mailbox_id()
    rule_a = repo.upsert_rule(
        mailbox_id=mailbox_a, sender_domain="vendor.com", match_mode=MATCH_MODE_EXACT, policy=POLICY_ALLOWED,
        destination_entity_id=None, destination_mode=DESTINATION_MODE_REVIEW_REQUIRED, source=SOURCE_OPERATOR,
    )
    rule_b = repo.upsert_rule(
        mailbox_id=mailbox_b, sender_domain="vendor.com", match_mode=MATCH_MODE_EXACT, policy=POLICY_IGNORED,
        destination_entity_id=None, destination_mode=None, source=SOURCE_OPERATOR,
    )
    assert rule_a.rule_id != rule_b.rule_id


# ---------------------------------------------------------------------
# Policy field validation
# ---------------------------------------------------------------------


def test_allowed_fixed_requires_a_destination_entity_id(repo):
    with pytest.raises(ValidationError):
        repo.upsert_rule(
            mailbox_id=_mailbox_id(), sender_domain="vendor.com", match_mode=MATCH_MODE_EXACT, policy=POLICY_ALLOWED,
            destination_entity_id=None, destination_mode=DESTINATION_MODE_FIXED, source=SOURCE_OPERATOR,
        )


def test_allowed_review_required_permits_a_null_destination(repo):
    rule = repo.upsert_rule(
        mailbox_id=_mailbox_id(), sender_domain="vendor.com", match_mode=MATCH_MODE_EXACT, policy=POLICY_ALLOWED,
        destination_entity_id=None, destination_mode=DESTINATION_MODE_REVIEW_REQUIRED, source=SOURCE_OPERATOR,
    )
    assert rule.destination_entity_id is None


def test_allowed_requires_a_destination_mode(repo):
    with pytest.raises(ValidationError):
        repo.upsert_rule(
            mailbox_id=_mailbox_id(), sender_domain="vendor.com", match_mode=MATCH_MODE_EXACT, policy=POLICY_ALLOWED,
            destination_entity_id=None, destination_mode=None, source=SOURCE_OPERATOR,
        )


def test_ignored_rejects_any_destination_field(repo):
    with pytest.raises(ValidationError):
        repo.upsert_rule(
            mailbox_id=_mailbox_id(), sender_domain="vendor.com", match_mode=MATCH_MODE_EXACT, policy=POLICY_IGNORED,
            destination_entity_id=identity.generate_id(), destination_mode=None, source=SOURCE_OPERATOR,
        )


def test_ungoverned_match_mode_rejected(repo):
    with pytest.raises(ValidationError):
        repo.upsert_rule(
            mailbox_id=_mailbox_id(), sender_domain="vendor.com", match_mode="SUBSTRING", policy=POLICY_IGNORED,
            destination_entity_id=None, destination_mode=None, source=SOURCE_OPERATOR,
        )


# ---------------------------------------------------------------------
# find_for_sender
# ---------------------------------------------------------------------


def test_exact_rule_matches_only_the_exact_domain(repo):
    mailbox_id = _mailbox_id()
    repo.upsert_rule(
        mailbox_id=mailbox_id, sender_domain="vendor.com", match_mode=MATCH_MODE_EXACT, policy=POLICY_IGNORED,
        destination_entity_id=None, destination_mode=None, source=SOURCE_OPERATOR,
    )
    assert repo.find_for_sender(mailbox_id=mailbox_id, sender_domain="vendor.com") is not None
    assert repo.find_for_sender(mailbox_id=mailbox_id, sender_domain="mail.vendor.com") is None


def test_include_subdomains_rule_matches_any_subdomain(repo):
    mailbox_id = _mailbox_id()
    repo.upsert_rule(
        mailbox_id=mailbox_id, sender_domain="vendor.com", match_mode=MATCH_MODE_INCLUDE_SUBDOMAINS,
        policy=POLICY_IGNORED, destination_entity_id=None, destination_mode=None, source=SOURCE_OPERATOR,
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
        mailbox_id=mailbox_id, sender_domain="vendor.com", match_mode=MATCH_MODE_EXACT, policy=POLICY_ALLOWED,
        destination_entity_id=None, destination_mode=DESTINATION_MODE_REVIEW_REQUIRED, source=SOURCE_OPERATOR,
    )
    from core.timestamps import utc_now

    later = utc_now()
    touched = repo.touch_last_seen(mailbox_id=mailbox_id, sender_domain="vendor.com", seen_at=later)
    assert touched.last_seen_at == later
    assert touched.policy == POLICY_ALLOWED


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
        mailbox_id=mailbox_id, sender_domain="vendor.com", match_mode=MATCH_MODE_EXACT, policy=POLICY_IGNORED,
        destination_entity_id=None, destination_mode=None, source=SOURCE_OPERATOR,
    )
    updated = transition_policy(
        rule, POLICY_ALLOWED, destination_entity_id=None, destination_mode=DESTINATION_MODE_REVIEW_REQUIRED
    )
    assert updated.policy == POLICY_ALLOWED


def test_transition_policy_same_value_is_rejected_not_a_no_op():
    """ALLOWED_POLICY_TRANSITIONS only names ALLOWED<->IGNORED — a
    same-value 'transition' is not itself in that table (upsert_rule's
    own idempotent-update semantics handle the ordinary "operator
    resubmits the same decision" case at a higher layer; this function
    is the raw state-machine primitive)."""
    mailbox_id = _mailbox_id()
    repo = InMemoryMailboxDomainRuleRepository()
    rule = repo.upsert_rule(
        mailbox_id=mailbox_id, sender_domain="vendor.com", match_mode=MATCH_MODE_EXACT, policy=POLICY_IGNORED,
        destination_entity_id=None, destination_mode=None, source=SOURCE_OPERATOR,
    )
    with pytest.raises(InvalidStateTransitionError):
        transition_policy(rule, POLICY_IGNORED)


def test_upsert_rule_lifecycle_ignored_then_allowed_then_ignored_again(repo):
    """Architect spec §6: an operator must later be able to change an
    ignored domain back to allowed — and, symmetrically, back again."""
    mailbox_id = _mailbox_id()
    repo.upsert_rule(
        mailbox_id=mailbox_id, sender_domain="vendor.com", match_mode=MATCH_MODE_EXACT, policy=POLICY_IGNORED,
        destination_entity_id=None, destination_mode=None, source=SOURCE_OPERATOR,
    )
    allowed = repo.upsert_rule(
        mailbox_id=mailbox_id, sender_domain="vendor.com", match_mode=MATCH_MODE_EXACT, policy=POLICY_ALLOWED,
        destination_entity_id=None, destination_mode=DESTINATION_MODE_REVIEW_REQUIRED, source=SOURCE_OPERATOR,
    )
    assert allowed.policy == POLICY_ALLOWED
    ignored_again = repo.upsert_rule(
        mailbox_id=mailbox_id, sender_domain="vendor.com", match_mode=MATCH_MODE_EXACT, policy=POLICY_IGNORED,
        destination_entity_id=None, destination_mode=None, source=SOURCE_OPERATOR,
    )
    assert ignored_again.policy == POLICY_IGNORED
    assert ignored_again.rule_id == allowed.rule_id
