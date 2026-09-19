"""CD-6 architect amendment PostgreSQL persistence proofs (two-stage
mail processing, Stage B domain gate) —
``persistence/postgres/mailbox_domain_rule_repository.py`` against a
REAL, disposable PostgreSQL container. Mirrors
``tests/persistence/test_mailbox_repository.py``'s own style/rigor.
"""
from __future__ import annotations

import pytest
from sqlalchemy.exc import IntegrityError

from core import identity
from core.errors import NotFoundError, ValidationError
from core.timestamps import utc_now
from persistence.postgres.mailbox_domain_rule_models import MailboxDomainRuleRow
from persistence.postgres.mailbox_domain_rule_repository import PostgresMailboxDomainRuleRepository
from persistence.postgres.mailbox_repository import PostgresMailboxSourceRepository
from persistence.postgres.session import get_engine, session_scope
from services.mailbox.domain_rule import (
    DESTINATION_MODE_FIXED,
    DESTINATION_MODE_REVIEW_REQUIRED,
    MATCH_MODE_EXACT,
    MATCH_MODE_EXACT_ADDRESS,
    MATCH_MODE_INCLUDE_SUBDOMAINS,
    POLICY_MUST_READ,
    POLICY_BLACKLIST,
    SOURCE_OPERATOR,
)
from services.mailbox.mailbox import PROVIDER_MICROSOFT_GRAPH


def _mailbox_id() -> str:
    repo = PostgresMailboxSourceRepository()
    created = repo.create_mailbox(
        display_name="Matt", email_address=f"matt-{identity.generate_id()}@infosecurs.com",
        provider_kind=PROVIDER_MICROSOFT_GRAPH,
    )
    return created.mailbox_id


# ---------------------------------------------------------------------
# Round-trip create/read
# ---------------------------------------------------------------------


def test_rule_persists_and_is_retrievable_via_a_fresh_repository_instance(fresh_engine):
    repo = PostgresMailboxDomainRuleRepository()
    mailbox_id = _mailbox_id()
    created = repo.upsert_rule(
        mailbox_id=mailbox_id, sender_domain="Vendor.COM", match_mode=MATCH_MODE_EXACT, policy=POLICY_BLACKLIST,
        destination_entity_id=None, destination_mode=None, source=SOURCE_OPERATOR,
    )
    assert created.sender_domain == "vendor.com"  # normalised lowercase

    fresh_repo = PostgresMailboxDomainRuleRepository(engine=fresh_engine)
    fetched = fresh_repo.get_rule(created.rule_id)
    assert fetched.policy == POLICY_BLACKLIST
    assert fetched.mailbox_id == mailbox_id


def test_get_unknown_rule_id_raises_not_found():
    repo = PostgresMailboxDomainRuleRepository()
    with pytest.raises(NotFoundError):
        repo.get_rule(identity.generate_id())


# ---------------------------------------------------------------------
# Uniqueness — one rule per (mailbox_id, sender_domain), never a second row
# ---------------------------------------------------------------------


def test_upsert_for_an_existing_domain_updates_the_same_row_never_a_second_one():
    repo = PostgresMailboxDomainRuleRepository()
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
    assert second.policy == POLICY_MUST_READ

    with session_scope(get_engine()) as session:
        count = (
            session.query(MailboxDomainRuleRow)
            .filter_by(mailbox_id=mailbox_id, sender_domain="vendor.com")
            .count()
        )
        assert count == 1


def test_same_domain_in_two_different_mailboxes_are_independent_rows():
    """Architect spec §8: rules are mailbox-specific, never global."""
    repo = PostgresMailboxDomainRuleRepository()
    mailbox_a = _mailbox_id()
    mailbox_b = _mailbox_id()
    rule_a = repo.upsert_rule(
        mailbox_id=mailbox_a, sender_domain="vendor.com", match_mode=MATCH_MODE_EXACT, policy=POLICY_MUST_READ,
        destination_entity_id=None, destination_mode=DESTINATION_MODE_REVIEW_REQUIRED, source=SOURCE_OPERATOR,
    )
    rule_b = repo.upsert_rule(
        mailbox_id=mailbox_b, sender_domain="vendor.com", match_mode=MATCH_MODE_EXACT, policy=POLICY_BLACKLIST,
        destination_entity_id=None, destination_mode=None, source=SOURCE_OPERATOR,
    )
    assert rule_a.rule_id != rule_b.rule_id
    assert repo.find_for_sender(mailbox_id=mailbox_a, sender_domain="vendor.com").policy == POLICY_MUST_READ
    assert repo.find_for_sender(mailbox_id=mailbox_b, sender_domain="vendor.com").policy == POLICY_BLACKLIST


def test_database_level_unique_constraint_exists():
    """The real, authoritative backstop — bypassing the repository's
    own resolve-or-update logic with a raw duplicate insert must be
    rejected by the database itself."""
    mailbox_id = _mailbox_id()
    now = utc_now()
    with pytest.raises(IntegrityError):
        with session_scope(get_engine()) as session:
            session.add(
                MailboxDomainRuleRow(
                    rule_id=identity.generate_id(), mailbox_id=mailbox_id, sender_domain="dupe.com",
                    match_mode=MATCH_MODE_EXACT, policy=POLICY_BLACKLIST, destination_entity_id=None,
                    destination_mode=None, source=SOURCE_OPERATOR, processor_hint=None, approved_at=None,
                    created_at=now, updated_at=now, last_seen_at=now,
                )
            )
            session.flush()
            session.add(
                MailboxDomainRuleRow(
                    rule_id=identity.generate_id(), mailbox_id=mailbox_id, sender_domain="dupe.com",
                    match_mode=MATCH_MODE_EXACT, policy=POLICY_MUST_READ, destination_entity_id=None,
                    destination_mode=DESTINATION_MODE_REVIEW_REQUIRED, source=SOURCE_OPERATOR, processor_hint=None,
                    approved_at=None, created_at=now, updated_at=now, last_seen_at=now,
                )
            )
            session.flush()


# ---------------------------------------------------------------------
# find_for_sender — EXACT vs INCLUDE_SUBDOMAINS matching
# ---------------------------------------------------------------------


def test_exact_match_mode_does_not_match_a_subdomain():
    repo = PostgresMailboxDomainRuleRepository()
    mailbox_id = _mailbox_id()
    repo.upsert_rule(
        mailbox_id=mailbox_id, sender_domain="vendor.com", match_mode=MATCH_MODE_EXACT, policy=POLICY_BLACKLIST,
        destination_entity_id=None, destination_mode=None, source=SOURCE_OPERATOR,
    )
    assert repo.find_for_sender(mailbox_id=mailbox_id, sender_domain="mail.vendor.com") is None


def test_include_subdomains_match_mode_matches_a_subdomain():
    repo = PostgresMailboxDomainRuleRepository()
    mailbox_id = _mailbox_id()
    repo.upsert_rule(
        mailbox_id=mailbox_id, sender_domain="vendor.com", match_mode=MATCH_MODE_INCLUDE_SUBDOMAINS,
        policy=POLICY_BLACKLIST, destination_entity_id=None, destination_mode=None, source=SOURCE_OPERATOR,
    )
    found = repo.find_for_sender(mailbox_id=mailbox_id, sender_domain="mail.vendor.com")
    assert found is not None
    assert found.sender_domain == "vendor.com"


def test_unknown_domain_resolves_to_none_not_an_error():
    repo = PostgresMailboxDomainRuleRepository()
    mailbox_id = _mailbox_id()
    assert repo.find_for_sender(mailbox_id=mailbox_id, sender_domain="never-seen.example") is None


# ---------------------------------------------------------------------
# Policy validation
# ---------------------------------------------------------------------


def test_allowed_with_fixed_destination_requires_a_real_entity_id():
    repo = PostgresMailboxDomainRuleRepository()
    mailbox_id = _mailbox_id()
    with pytest.raises(ValidationError):
        repo.upsert_rule(
            mailbox_id=mailbox_id, sender_domain="vendor.com", match_mode=MATCH_MODE_EXACT, policy=POLICY_MUST_READ,
            destination_entity_id=None, destination_mode=DESTINATION_MODE_FIXED, source=SOURCE_OPERATOR,
        )


def test_ignored_rule_must_never_carry_a_destination():
    repo = PostgresMailboxDomainRuleRepository()
    mailbox_id = _mailbox_id()
    with pytest.raises(ValidationError):
        repo.upsert_rule(
            mailbox_id=mailbox_id, sender_domain="vendor.com", match_mode=MATCH_MODE_EXACT, policy=POLICY_BLACKLIST,
            destination_entity_id=identity.generate_id(), destination_mode=DESTINATION_MODE_FIXED,
            source=SOURCE_OPERATOR,
        )


# ---------------------------------------------------------------------
# Lifecycle — IGNORED -> ALLOWED -> IGNORED (a real, tested transition)
# ---------------------------------------------------------------------


def test_ignored_domain_can_be_changed_back_to_allowed():
    repo = PostgresMailboxDomainRuleRepository()
    mailbox_id = _mailbox_id()
    repo.upsert_rule(
        mailbox_id=mailbox_id, sender_domain="vendor.com", match_mode=MATCH_MODE_EXACT, policy=POLICY_BLACKLIST,
        destination_entity_id=None, destination_mode=None, source=SOURCE_OPERATOR,
    )
    updated = repo.upsert_rule(
        mailbox_id=mailbox_id, sender_domain="vendor.com", match_mode=MATCH_MODE_EXACT, policy=POLICY_MUST_READ,
        destination_entity_id=None, destination_mode=DESTINATION_MODE_REVIEW_REQUIRED, source=SOURCE_OPERATOR,
    )
    assert updated.policy == POLICY_MUST_READ

    reverted = repo.upsert_rule(
        mailbox_id=mailbox_id, sender_domain="vendor.com", match_mode=MATCH_MODE_EXACT, policy=POLICY_BLACKLIST,
        destination_entity_id=None, destination_mode=None, source=SOURCE_OPERATOR,
    )
    assert reverted.policy == POLICY_BLACKLIST
    assert reverted.rule_id == updated.rule_id


# ---------------------------------------------------------------------
# touch_last_seen / list_rules
# ---------------------------------------------------------------------


def test_touch_last_seen_updates_the_matched_rule():
    repo = PostgresMailboxDomainRuleRepository()
    mailbox_id = _mailbox_id()
    created = repo.upsert_rule(
        mailbox_id=mailbox_id, sender_domain="vendor.com", match_mode=MATCH_MODE_EXACT, policy=POLICY_BLACKLIST,
        destination_entity_id=None, destination_mode=None, source=SOURCE_OPERATOR,
    )
    later = utc_now()
    touched = repo.touch_last_seen(mailbox_id=mailbox_id, sender_domain="vendor.com", seen_at=later)
    assert touched.last_seen_at == later
    assert touched.rule_id == created.rule_id


def test_touch_last_seen_for_an_ungoverned_domain_raises_not_found():
    repo = PostgresMailboxDomainRuleRepository()
    mailbox_id = _mailbox_id()
    with pytest.raises(NotFoundError):
        repo.touch_last_seen(mailbox_id=mailbox_id, sender_domain="never-seen.example", seen_at=utc_now())


# ---------------------------------------------------------------------
# find_exact — CD-6 policy-rules-endpoint WO: the "previous state at
# THIS identity" lookup, never find_for_sender's most-specific-wins
# resolution.
# ---------------------------------------------------------------------


def test_find_exact_returns_none_for_a_never_governed_identity():
    repo = PostgresMailboxDomainRuleRepository()
    mailbox_id = _mailbox_id()
    assert repo.find_exact(mailbox_id=mailbox_id, sender_domain="never-seen.example", match_mode=MATCH_MODE_EXACT) is None
    assert (
        repo.find_exact(
            mailbox_id=mailbox_id, sender_domain="never-seen.example", match_mode=MATCH_MODE_EXACT_ADDRESS,
            sender_address="nobody@never-seen.example",
        )
        is None
    )


def test_find_exact_returns_the_domain_level_rule_at_that_identity():
    repo = PostgresMailboxDomainRuleRepository()
    mailbox_id = _mailbox_id()
    created = repo.upsert_rule(
        mailbox_id=mailbox_id, sender_domain="vendor.com", match_mode=MATCH_MODE_EXACT, policy=POLICY_BLACKLIST,
        destination_entity_id=None, destination_mode=None, source=SOURCE_OPERATOR,
    )
    found = repo.find_exact(mailbox_id=mailbox_id, sender_domain="vendor.com", match_mode=MATCH_MODE_EXACT)
    assert found is not None
    assert found.rule_id == created.rule_id


def test_find_exact_address_level_is_a_separate_identity_space_from_domain_level():
    """The core reason this method exists: a brand-new EXACT_ADDRESS
    rule's own 'previous state' must be None even when a broader
    domain-level rule already governs the same domain —
    find_for_sender's most-specific-wins resolution would incorrectly
    surface that broader rule instead."""
    repo = PostgresMailboxDomainRuleRepository()
    mailbox_id = _mailbox_id()
    repo.upsert_rule(
        mailbox_id=mailbox_id, sender_domain="send.xero.com", match_mode=MATCH_MODE_EXACT, policy=POLICY_MUST_READ,
        destination_entity_id=None, destination_mode=DESTINATION_MODE_REVIEW_REQUIRED, source=SOURCE_OPERATOR,
    )
    # find_for_sender correctly falls back to the broader domain rule...
    resolved = repo.find_for_sender(
        mailbox_id=mailbox_id, sender_domain="send.xero.com", sender_address="noreply@send.xero.com"
    )
    assert resolved is not None
    assert resolved.match_mode == MATCH_MODE_EXACT

    # ...but find_exact at the EXACT_ADDRESS identity must see nothing yet.
    exact = repo.find_exact(
        mailbox_id=mailbox_id, sender_domain="send.xero.com", match_mode=MATCH_MODE_EXACT_ADDRESS,
        sender_address="noreply@send.xero.com",
    )
    assert exact is None

    created = repo.upsert_rule(
        mailbox_id=mailbox_id, sender_domain="send.xero.com", sender_address="noreply@send.xero.com",
        match_mode=MATCH_MODE_EXACT_ADDRESS, policy=POLICY_BLACKLIST, destination_entity_id=None,
        destination_mode=None, source=SOURCE_OPERATOR,
    )
    exact_after = repo.find_exact(
        mailbox_id=mailbox_id, sender_domain="send.xero.com", match_mode=MATCH_MODE_EXACT_ADDRESS,
        sender_address="noreply@send.xero.com",
    )
    assert exact_after is not None
    assert exact_after.rule_id == created.rule_id


def test_find_exact_address_mode_requires_sender_address():
    repo = PostgresMailboxDomainRuleRepository()
    mailbox_id = _mailbox_id()
    with pytest.raises(ValidationError):
        repo.find_exact(mailbox_id=mailbox_id, sender_domain="vendor.com", match_mode=MATCH_MODE_EXACT_ADDRESS)


def test_list_rules_scoped_to_one_mailbox():
    repo = PostgresMailboxDomainRuleRepository()
    mailbox_a = _mailbox_id()
    mailbox_b = _mailbox_id()
    repo.upsert_rule(
        mailbox_id=mailbox_a, sender_domain="one.com", match_mode=MATCH_MODE_EXACT, policy=POLICY_BLACKLIST,
        destination_entity_id=None, destination_mode=None, source=SOURCE_OPERATOR,
    )
    repo.upsert_rule(
        mailbox_id=mailbox_a, sender_domain="two.com", match_mode=MATCH_MODE_EXACT, policy=POLICY_BLACKLIST,
        destination_entity_id=None, destination_mode=None, source=SOURCE_OPERATOR,
    )
    repo.upsert_rule(
        mailbox_id=mailbox_b, sender_domain="three.com", match_mode=MATCH_MODE_EXACT, policy=POLICY_BLACKLIST,
        destination_entity_id=None, destination_mode=None, source=SOURCE_OPERATOR,
    )
    rules = repo.list_rules(mailbox_id=mailbox_a)
    assert {r.sender_domain for r in rules} == {"one.com", "two.com"}
