"""CD-6 architect amendment PostgreSQL persistence proofs (two-stage
mail processing, Stage B domain gate) —
``persistence/postgres/mailbox_domain_rule_repository.py`` against a
REAL, disposable PostgreSQL container. Mirrors
``tests/persistence/test_mailbox_repository.py``'s own style/rigor.
"""
from __future__ import annotations

import pytest
import sqlalchemy as sa
from sqlalchemy.exc import IntegrityError

from core import identity
from core.errors import ConflictError, NotFoundError, ValidationError
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
    MATCH_MODE_EXACT_DOMAIN_SUBJECT,
    MATCH_MODE_INCLUDE_SUBDOMAINS,
    POLICY_MUST_READ,
    POLICY_BLACKLIST,
    POLICY_GRAYLIST,
    SOURCE_OPERATOR,
    SUBJECT_PREDICATE_EXACT,
    SUBJECT_PREDICATE_STARTS_WITH,
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


# ---------------------------------------------------------------------
# Deterministic subject-aware mailbox domain policy (CD-6 GUI-operations-
# foundation follow-on WO) — EXACT_DOMAIN_SUBJECT match mode
# ---------------------------------------------------------------------


def test_postgres_upsert_creates_and_updates_a_subject_rule_at_its_own_identity():
    repo = PostgresMailboxDomainRuleRepository()
    mailbox_id = _mailbox_id()
    first = repo.upsert_rule(
        mailbox_id=mailbox_id, sender_domain="vendor.com", match_mode=MATCH_MODE_EXACT_DOMAIN_SUBJECT,
        policy=POLICY_BLACKLIST, destination_entity_id=None, destination_mode=None, source=SOURCE_OPERATOR,
        subject_predicate_type=SUBJECT_PREDICATE_EXACT, subject_predicate_value="Monthly Statement",
    )
    assert first.subject_predicate_value == "monthly statement"  # stored normalised

    second = repo.upsert_rule(
        mailbox_id=mailbox_id, sender_domain="vendor.com", match_mode=MATCH_MODE_EXACT_DOMAIN_SUBJECT,
        policy=POLICY_MUST_READ, destination_entity_id=None, destination_mode=DESTINATION_MODE_REVIEW_REQUIRED,
        source=SOURCE_OPERATOR, subject_predicate_type=SUBJECT_PREDICATE_EXACT,
        subject_predicate_value="monthly statement",  # already-normalised-equal -> same identity
    )
    assert second.rule_id == first.rule_id
    assert second.policy == POLICY_MUST_READ


def test_postgres_one_domain_holds_fallback_plus_multiple_subject_rules_plus_multiple_address_rules():
    """Migration acceptance (item 34) — the corrected index scheme lets
    one domain simultaneously hold one EXACT fallback, several
    EXACT_DOMAIN_SUBJECT rules, and several EXACT_ADDRESS rules, all
    coexisting without collision."""
    repo = PostgresMailboxDomainRuleRepository()
    mailbox_id = _mailbox_id()
    fallback = repo.upsert_rule(
        mailbox_id=mailbox_id, sender_domain="vendor.com", match_mode=MATCH_MODE_EXACT, policy=POLICY_GRAYLIST,
        destination_entity_id=None, destination_mode=None, source=SOURCE_OPERATOR,
    )
    subject_1 = repo.upsert_rule(
        mailbox_id=mailbox_id, sender_domain="vendor.com", match_mode=MATCH_MODE_EXACT_DOMAIN_SUBJECT,
        policy=POLICY_MUST_READ, destination_entity_id=None, destination_mode=DESTINATION_MODE_REVIEW_REQUIRED,
        source=SOURCE_OPERATOR, subject_predicate_type=SUBJECT_PREDICATE_EXACT, subject_predicate_value="monthly statement",
    )
    subject_2 = repo.upsert_rule(
        mailbox_id=mailbox_id, sender_domain="vendor.com", match_mode=MATCH_MODE_EXACT_DOMAIN_SUBJECT,
        policy=POLICY_BLACKLIST, destination_entity_id=None, destination_mode=None, source=SOURCE_OPERATOR,
        subject_predicate_type=SUBJECT_PREDICATE_STARTS_WITH, subject_predicate_value="marketing",
    )
    address_1 = repo.upsert_rule(
        mailbox_id=mailbox_id, sender_domain="vendor.com", match_mode=MATCH_MODE_EXACT_ADDRESS,
        policy=POLICY_MUST_READ, destination_entity_id=None, destination_mode=DESTINATION_MODE_REVIEW_REQUIRED,
        source=SOURCE_OPERATOR, sender_address="ap@vendor.com",
    )
    address_2 = repo.upsert_rule(
        mailbox_id=mailbox_id, sender_domain="vendor.com", match_mode=MATCH_MODE_EXACT_ADDRESS,
        policy=POLICY_BLACKLIST, destination_entity_id=None, destination_mode=None, source=SOURCE_OPERATOR,
        sender_address="spam@vendor.com",
    )
    rule_ids = {r.rule_id for r in (fallback, subject_1, subject_2, address_1, address_2)}
    assert len(rule_ids) == 5
    assert {r.rule_id for r in repo.list_rules(mailbox_id=mailbox_id)} == rule_ids


def test_postgres_subject_unique_index_rejects_canonical_duplicate():
    """Case/whitespace-equivalent predicate values normalise to the same
    identity and collide correctly — a genuinely new upsert at the SAME
    normalised identity updates the SAME row, never creates a second
    one, verified directly against the database."""
    repo = PostgresMailboxDomainRuleRepository()
    mailbox_id = _mailbox_id()
    repo.upsert_rule(
        mailbox_id=mailbox_id, sender_domain="vendor.com", match_mode=MATCH_MODE_EXACT_DOMAIN_SUBJECT,
        policy=POLICY_BLACKLIST, destination_entity_id=None, destination_mode=None, source=SOURCE_OPERATOR,
        subject_predicate_type=SUBJECT_PREDICATE_EXACT, subject_predicate_value="  Monthly   Statement  ",
    )
    repo.upsert_rule(
        mailbox_id=mailbox_id, sender_domain="vendor.com", match_mode=MATCH_MODE_EXACT_DOMAIN_SUBJECT,
        policy=POLICY_MUST_READ, destination_entity_id=None, destination_mode=DESTINATION_MODE_REVIEW_REQUIRED,
        source=SOURCE_OPERATOR, subject_predicate_type=SUBJECT_PREDICATE_EXACT, subject_predicate_value="MONTHLY STATEMENT",
    )
    with session_scope(get_engine()) as session:
        count = (
            session.query(MailboxDomainRuleRow)
            .filter_by(mailbox_id=mailbox_id, match_mode=MATCH_MODE_EXACT_DOMAIN_SUBJECT)
            .count()
        )
        assert count == 1


def test_postgres_database_level_subject_unique_index_exists():
    """The real, authoritative backstop — a raw duplicate insert at the
    exact same subject identity must be rejected by the database
    itself, mirroring `test_database_level_unique_constraint_exists`."""
    mailbox_id = _mailbox_id()
    now = utc_now()
    with pytest.raises(IntegrityError):
        with session_scope(get_engine()) as session:
            session.add(
                MailboxDomainRuleRow(
                    rule_id=identity.generate_id(), mailbox_id=mailbox_id, sender_domain="dupe.com",
                    match_mode=MATCH_MODE_EXACT_DOMAIN_SUBJECT, subject_predicate_type=SUBJECT_PREDICATE_EXACT,
                    subject_predicate_value="dupe subject", policy=POLICY_BLACKLIST, destination_entity_id=None,
                    destination_mode=None, source=SOURCE_OPERATOR, processor_hint=None, approved_at=None,
                    created_at=now, updated_at=now, last_seen_at=now,
                )
            )
            session.flush()
            session.add(
                MailboxDomainRuleRow(
                    rule_id=identity.generate_id(), mailbox_id=mailbox_id, sender_domain="dupe.com",
                    match_mode=MATCH_MODE_EXACT_DOMAIN_SUBJECT, subject_predicate_type=SUBJECT_PREDICATE_EXACT,
                    subject_predicate_value="dupe subject", policy=POLICY_MUST_READ, destination_entity_id=None,
                    destination_mode=DESTINATION_MODE_REVIEW_REQUIRED, source=SOURCE_OPERATOR, processor_hint=None,
                    approved_at=None, created_at=now, updated_at=now, last_seen_at=now,
                )
            )
            session.flush()


def test_postgres_old_domain_index_no_longer_blocks_subject_row_coexistence():
    """The critical index correction (item 5.2/5.3): a domain-level
    fallback rule and an EXACT_DOMAIN_SUBJECT row at the SAME domain
    must both be able to exist — the OLD `!= 'EXACT_ADDRESS'` scope
    would have wrongly collided them."""
    repo = PostgresMailboxDomainRuleRepository()
    mailbox_id = _mailbox_id()
    fallback = repo.upsert_rule(
        mailbox_id=mailbox_id, sender_domain="coexist.com", match_mode=MATCH_MODE_EXACT, policy=POLICY_GRAYLIST,
        destination_entity_id=None, destination_mode=None, source=SOURCE_OPERATOR,
    )
    subject = repo.upsert_rule(
        mailbox_id=mailbox_id, sender_domain="coexist.com", match_mode=MATCH_MODE_EXACT_DOMAIN_SUBJECT,
        policy=POLICY_BLACKLIST, destination_entity_id=None, destination_mode=None, source=SOURCE_OPERATOR,
        subject_predicate_type=SUBJECT_PREDICATE_EXACT, subject_predicate_value="noise",
    )
    assert fallback.rule_id != subject.rule_id


def test_postgres_plain_domain_uniqueness_remains_enforced_after_the_index_correction():
    repo = PostgresMailboxDomainRuleRepository()
    mailbox_id = _mailbox_id()
    first = repo.upsert_rule(
        mailbox_id=mailbox_id, sender_domain="still-unique.com", match_mode=MATCH_MODE_EXACT, policy=POLICY_BLACKLIST,
        destination_entity_id=None, destination_mode=None, source=SOURCE_OPERATOR,
    )
    second = repo.upsert_rule(
        mailbox_id=mailbox_id, sender_domain="still-unique.com", match_mode=MATCH_MODE_EXACT, policy=POLICY_MUST_READ,
        destination_entity_id=None, destination_mode=DESTINATION_MODE_REVIEW_REQUIRED, source=SOURCE_OPERATOR,
    )
    assert second.rule_id == first.rule_id
    with session_scope(get_engine()) as session:
        count = (
            session.query(MailboxDomainRuleRow)
            .filter_by(mailbox_id=mailbox_id, sender_domain="still-unique.com")
            .filter(MailboxDomainRuleRow.match_mode.in_(["EXACT", "INCLUDE_SUBDOMAINS"]))
            .count()
        )
        assert count == 1


def test_postgres_find_for_sender_subject_precedence_matches_in_memory():
    """Both implementations must agree exactly — exact-address beats
    subject, EXACT predicate beats STARTS_WITH, longest STARTS_WITH wins."""
    repo = PostgresMailboxDomainRuleRepository()
    mailbox_id = _mailbox_id()
    long_rule = repo.upsert_rule(
        mailbox_id=mailbox_id, sender_domain="ebay.example", match_mode=MATCH_MODE_EXACT_DOMAIN_SUBJECT,
        policy=POLICY_MUST_READ, destination_entity_id=None, destination_mode=DESTINATION_MODE_REVIEW_REQUIRED,
        source=SOURCE_OPERATOR, subject_predicate_type=SUBJECT_PREDICATE_STARTS_WITH,
        subject_predicate_value="order confirmed:",
    )
    repo.upsert_rule(
        mailbox_id=mailbox_id, sender_domain="ebay.example", match_mode=MATCH_MODE_EXACT_DOMAIN_SUBJECT,
        policy=POLICY_BLACKLIST, destination_entity_id=None, destination_mode=None, source=SOURCE_OPERATOR,
        subject_predicate_type=SUBJECT_PREDICATE_STARTS_WITH, subject_predicate_value="order",
    )
    found = repo.find_for_sender(mailbox_id=mailbox_id, sender_domain="ebay.example", subject="Order confirmed: iPad")
    assert found.rule_id == long_rule.rule_id
    assert found.policy == POLICY_MUST_READ

    no_match = repo.find_for_sender(mailbox_id=mailbox_id, sender_domain="ebay.example", subject="porsche targa, Porsche: 2 NEW!")
    assert no_match is None


def test_postgres_find_for_sender_ambiguous_exact_predicates_raise_conflict_error():
    """Fail-closed corruption test hook (item 30) — Postgres side.

    Two ambiguous EXACT_DOMAIN_SUBJECT rows at the identical identity
    can NEVER be committed while `uq_mailbox_domain_rules_mailbox_subject`
    is in force (proven by
    `test_postgres_database_level_subject_unique_index_exists` above) —
    that partial unique index is EXACTLY what makes this corruption
    structurally impossible in ordinary operation. To construct the
    genuine corruption scenario this test proves `find_for_sender` fails
    closed against, the index itself is dropped first (simulating a
    real-world corruption vector this delivery cannot otherwise reach —
    e.g. a botched manual DB repair, a migration race, or replication
    divergence — never something the application's own write path could
    ever produce), the two colliding rows are inserted directly, and the
    index is restored again in a `finally` so this test never leaves the
    schema itself corrupted for any test that runs after it."""
    repo = PostgresMailboxDomainRuleRepository()
    mailbox_id = _mailbox_id()
    now = utc_now()
    engine = get_engine()
    with engine.begin() as conn:
        conn.execute(sa.text("DROP INDEX uq_mailbox_domain_rules_mailbox_subject"))
    try:
        with session_scope(engine) as session:
            for policy, destination_mode in (
                (POLICY_MUST_READ, DESTINATION_MODE_REVIEW_REQUIRED),
                (POLICY_BLACKLIST, None),
            ):
                session.add(
                    MailboxDomainRuleRow(
                        rule_id=identity.generate_id(), mailbox_id=mailbox_id, sender_domain="corrupt.example",
                        match_mode=MATCH_MODE_EXACT_DOMAIN_SUBJECT, subject_predicate_type=SUBJECT_PREDICATE_EXACT,
                        subject_predicate_value="monthly statement", policy=policy, destination_entity_id=None,
                        destination_mode=destination_mode, source=SOURCE_OPERATOR, processor_hint=None,
                        approved_at=None, created_at=now, updated_at=now, last_seen_at=now,
                    )
                )
            session.flush()

        with pytest.raises(ConflictError):
            repo.find_for_sender(mailbox_id=mailbox_id, sender_domain="corrupt.example", subject="Monthly Statement")

        # Never silently picked one, never returned None, never fell
        # through to a broader tier.
    finally:
        # Remove the corrupt rows FIRST — recreating the unique index
        # while they still exist would itself fail with the exact same
        # UniqueViolation this test just proved `find_for_sender` fails
        # closed against.
        with engine.begin() as conn:
            conn.execute(
                sa.text("DELETE FROM mailbox_domain_rules WHERE mailbox_id = :mailbox_id"),
                {"mailbox_id": mailbox_id},
            )
            conn.execute(
                sa.text(
                    "CREATE UNIQUE INDEX uq_mailbox_domain_rules_mailbox_subject ON mailbox_domain_rules "
                    "(mailbox_id, sender_domain, subject_predicate_type, subject_predicate_value) "
                    "WHERE match_mode = 'EXACT_DOMAIN_SUBJECT'"
                )
            )
