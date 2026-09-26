"""CD-6 Slice 5 WI-2 characterization tests for the `core.text_matching`
extraction (§1 of the WO). These tests call the REAL, shared functions
exactly as `services.mailbox.domain_rule` re-exports them (never a
parallel copy), proving the extraction changed ZERO observable
mailbox-policy behaviour.

Every scenario here is also independently exercised by the pre-existing
`tests/integration/test_mailbox_domain_rule_domain.py` (that whole file
is re-run, unmodified, as part of this WI's regression — see the PL
report). This file exists to additionally prove the specific
normalisation edge cases the WO calls out by name, in one place, against
the module boundary that actually matters after the refactor
(`services.mailbox.domain_rule`'s own re-exported names).
"""
from __future__ import annotations

import pytest

from core.errors import ValidationError
from services.mailbox.domain_rule import (
    domain_from_address,
    normalize_address,
    normalize_domain,
    normalize_subject_for_policy,
    subject_matches_predicate,
)


# ---------------------------------------------------------------------
# Vantage's real EXACT-subject-match behaviour
# ---------------------------------------------------------------------


def test_vantage_exact_subject_match():
    predicate_value = normalize_subject_for_policy("Monthly Statement")
    assert subject_matches_predicate("Monthly Statement", predicate_type="EXACT", predicate_value=predicate_value)
    assert subject_matches_predicate("monthly STATEMENT", predicate_type="EXACT", predicate_value=predicate_value)
    assert not subject_matches_predicate("Monthly Statement - March", predicate_type="EXACT", predicate_value=predicate_value)


# ---------------------------------------------------------------------
# eBay's real STARTS_WITH behaviour
# ---------------------------------------------------------------------


def test_ebay_starts_with_match():
    predicate_value = normalize_subject_for_policy("Order confirmed:")
    assert subject_matches_predicate(
        "Order confirmed: your item has shipped", predicate_type="STARTS_WITH", predicate_value=predicate_value
    )
    assert not subject_matches_predicate(
        "Your order confirmed: shipped", predicate_type="STARTS_WITH", predicate_value=predicate_value
    )


# ---------------------------------------------------------------------
# EXACT beats STARTS_WITH at equal specificity (matcher-level precedence
# proof — see also test_classification_matcher.py for the full rule-set
# version of this).
# ---------------------------------------------------------------------


def test_exact_and_starts_with_can_both_structurally_match_one_subject():
    subject = "Monthly Statement"
    exact_value = normalize_subject_for_policy("Monthly Statement")
    starts_with_value = normalize_subject_for_policy("Monthly")
    assert subject_matches_predicate(subject, predicate_type="EXACT", predicate_value=exact_value)
    assert subject_matches_predicate(subject, predicate_type="STARTS_WITH", predicate_value=starts_with_value)
    # The precedence DECISION (EXACT wins) is the matcher's job, not
    # subject_matches_predicate's — this proves both CAN match, which is
    # the situation the matcher must correctly disambiguate.


# ---------------------------------------------------------------------
# Longest STARTS_WITH wins among multiple STARTS_WITH candidates
# ---------------------------------------------------------------------


def test_longest_starts_with_prefix_is_unambiguously_identifiable():
    subject = "Order confirmed: item 12345 has shipped"
    short_prefix = normalize_subject_for_policy("Order confirmed:")
    long_prefix = normalize_subject_for_policy("Order confirmed: item")
    assert subject_matches_predicate(subject, predicate_type="STARTS_WITH", predicate_value=short_prefix)
    assert subject_matches_predicate(subject, predicate_type="STARTS_WITH", predicate_value=long_prefix)
    assert len(long_prefix) > len(short_prefix)


# ---------------------------------------------------------------------
# Unicode NFKC normalisation — full-width vs half-width character pair
# ---------------------------------------------------------------------


def test_nfkc_normalizes_full_width_characters_to_half_width():
    # U+FF29 U+FF2E U+FF36 U+FF2F U+FF29 U+FF23 U+FF25 = fullwidth "INVOICE"
    full_width = "ＩＮＶＯＩＣＥ"
    half_width = "INVOICE"
    assert normalize_subject_for_policy(full_width) == normalize_subject_for_policy(half_width)


# ---------------------------------------------------------------------
# casefold (not just .lower()) — German ß case
# ---------------------------------------------------------------------


def test_casefold_handles_german_sharp_s_that_lower_alone_would_not():
    # "STRASSE" casefolds equal to "straße" (ß -> ss under casefold);
    # .lower() alone does NOT achieve this equivalence.
    assert "straße".casefold() != "straße".lower()
    assert normalize_subject_for_policy("STRASSE") == normalize_subject_for_policy("straße")


# ---------------------------------------------------------------------
# Internal whitespace collapse — the documented eBay double-space defect
# ---------------------------------------------------------------------


def test_internal_whitespace_collapse_ebay_double_space_defect():
    defective = "Order  confirmed:  your item has shipped"
    clean = "Order confirmed: your item has shipped"
    assert normalize_subject_for_policy(defective) == normalize_subject_for_policy(clean)


# ---------------------------------------------------------------------
# Equal-specificity ambiguity remains fail-closed — reproducing
# find_for_sender's own real ambiguity-handling shape (a real
# MailboxDomainRuleRepository, not a hand-rolled duplicate).
# ---------------------------------------------------------------------


def test_equal_specificity_ambiguity_fails_closed_via_find_for_sender():
    """`find_for_sender`'s own Tier-2 ambiguity branch scans EVERY row in
    `_by_id` directly (never the uniqueness index) — so the only way to
    reach it is two rows sharing the IDENTICAL (mailbox_id, sender_domain,
    subject_predicate_type, subject_predicate_value) tuple, which the
    real `upsert_rule` write path structurally prevents (it replaces the
    existing row at that identity rather than creating a second one).
    Reproducing this therefore requires writing directly into the
    repository's internal state — exactly the "data corruption bypassing
    the normal validated write path" scenario `find_for_sender`'s own
    docstring names. This is the REAL ambiguity-handling shape (the
    method under test is unmodified `find_for_sender` itself), not an
    invented parallel implementation."""
    from datetime import datetime, timezone

    from core import identity
    from core.errors import ConflictError
    from services.mailbox.domain_rule import MATCH_MODE_EXACT_DOMAIN_SUBJECT, InMemoryMailboxDomainRuleRepository, MailboxDomainRule

    repo = InMemoryMailboxDomainRuleRepository()
    now = datetime.now(timezone.utc)
    predicate_value = normalize_subject_for_policy("order confirmed:")
    for _ in range(2):
        rule = MailboxDomainRule(
            rule_id=identity.generate_id(), mailbox_id="mbx-1", sender_domain="ebay.com",
            match_mode=MATCH_MODE_EXACT_DOMAIN_SUBJECT, policy="MUST_READ",
            destination_entity_id=None, destination_mode="REVIEW_REQUIRED", source="OPERATOR",
            processor_hint=None, approved_at=now, created_at=now, updated_at=now, last_seen_at=now,
            subject_predicate_type="STARTS_WITH", subject_predicate_value=predicate_value,
        )
        repo._by_id[rule.rule_id] = rule  # noqa: SLF001 - deliberate corruption-scenario setup, see docstring above

    with pytest.raises(ConflictError):
        repo.find_for_sender(mailbox_id="mbx-1", sender_domain="ebay.com", subject="Order confirmed: shipped")


# ---------------------------------------------------------------------
# Malformed address handling (domain_from_address) — reused directly by
# the WI-2 matcher's own eligibility gate.
# ---------------------------------------------------------------------


def test_domain_from_address_rejects_missing_at_sign():
    with pytest.raises(ValidationError):
        domain_from_address("not-an-email")


def test_normalize_domain_and_address_strip_and_lowercase():
    assert normalize_domain("  Example.COM  ") == "example.com"
    assert normalize_address("  User@Example.COM  ") == "user@example.com"
