"""PL-review finding, CD-6 architect amendment (email historical-
ingestion boundary): ``ensure_seed_entities`` (app/api/composition.py)
originally only set ``fiscal_year_start_month_day``/
``historical_floor_override_at`` (renamed from ``email_bootstrap_floor_at``
— see ``core.entity.GovernedEntity``'s own docstring for the second
conceptual-conflation bug fix that rename encodes) when CREATING a
brand-new canonical entity — an entity that already existed (every
entity registered during CD-6 Slice 1, including all three already
live on the production appliance, long before these two fields
existed) was returned unchanged, with both fields permanently ``None``.
Without a real backfill path, the architect's own verified accounting-
period configuration could never actually reach the real, already-
existing entities, and ``services.mailbox.sweep.compute_bootstrap_floor``
would refuse the real historical sweep forever — a real, live-blocking
gap, not a theoretical one, since the exact scenario it describes is
the current state of the production Mac appliance.

These tests prove the fix directly against ``ensure_seed_entities``
itself (not just the lower-level repository primitive it now calls —
see ``tests/persistence/test_entity_persistence.py`` for that proof).
"""
from __future__ import annotations

from datetime import datetime, timezone

import pytest

from app.api.composition import SEED_ENTITIES, ensure_seed_entities, get_composition, reset_composition_for_tests
from services.mailbox.domain_rule import InMemoryMailboxDomainRuleRepository
from services.mailbox.mailbox import PROVIDER_MICROSOFT_GRAPH, InMemoryMailboxSourceRepository
from services.mailbox.sweep import compute_bootstrap_floor


@pytest.fixture
def dev_composition(monkeypatch):
    monkeypatch.setenv("BAGMAN_RUNTIME_ENV", "development")
    reset_composition_for_tests()
    yield get_composition()
    reset_composition_for_tests()


def test_ensure_seed_entities_backfills_a_pre_existing_entity_missing_the_new_fields(dev_composition):
    """Simulates the real production scenario directly: an entity that
    already exists (registered the way CD-6 Slice 1's own seed code
    always did, with no fiscal_year_start_month_day/
    historical_floor_override_at at all) must have
    fiscal_year_start_month_day backfilled the next time
    ensure_seed_entities runs — not left permanently None forever.
    Uses SEED_ENTITIES[1] (NoustAI Limited) deliberately, not [0]
    (Infosecurs) — NoustAI is the one canonical entity whose seed
    override is genuinely non-None (see SEED_ENTITIES' own docstring),
    so this test also proves the override itself gets backfilled, not
    only the rule."""
    canonical_name, display_name, entity_type, fiscal_year_start_month_day, historical_floor_override_at = (
        SEED_ENTITIES[1]
    )
    assert historical_floor_override_at is not None  # NoustAI — a real, non-None override

    pre_existing = dev_composition.api.entity_repository.register_entity(
        entity_type=entity_type,
        canonical_name=canonical_name,
        display_name=display_name,
        status="ACTIVE",
        # Deliberately omitted, exactly like every real CD-6 Slice 1
        # seed call was — this is the actual shape of the bug.
    )
    assert pre_existing.fiscal_year_start_month_day is None
    assert pre_existing.historical_floor_override_at is None

    resolved = ensure_seed_entities(dev_composition)
    assert resolved[canonical_name] == pre_existing.entity_id  # same row, never re-created

    backfilled = dev_composition.api.entity_repository.get_entity(pre_existing.entity_id)
    assert backfilled.fiscal_year_start_month_day == fiscal_year_start_month_day
    assert backfilled.historical_floor_override_at == historical_floor_override_at
    assert backfilled.display_name == pre_existing.display_name  # untouched


def test_ensure_seed_entities_backfills_fiscal_year_start_even_when_the_seed_override_is_none(dev_composition):
    """SECOND-CORRECTION-specific proof: Infosecurs' own SEED_ENTITIES
    row now carries historical_floor_override_at=None (the correct,
    common case — see SEED_ENTITIES' own docstring). The backfill gate
    must trigger on fiscal_year_start_month_day being None, NOT on
    historical_floor_override_at being None — otherwise Infosecurs'
    (and Matthew Scott Personal's) real, required
    fiscal_year_start_month_day rule could never reach an
    already-existing row, since a gate keyed on the override would read
    "already configured" from the moment of creation."""
    canonical_name, display_name, entity_type, fiscal_year_start_month_day, historical_floor_override_at = (
        SEED_ENTITIES[0]
    )
    assert historical_floor_override_at is None  # Infosecurs — the correct, common case

    pre_existing = dev_composition.api.entity_repository.register_entity(
        entity_type=entity_type, canonical_name=canonical_name, display_name=display_name, status="ACTIVE",
    )
    assert pre_existing.fiscal_year_start_month_day is None

    ensure_seed_entities(dev_composition)

    backfilled = dev_composition.api.entity_repository.get_entity(pre_existing.entity_id)
    assert backfilled.fiscal_year_start_month_day == fiscal_year_start_month_day  # "11-01" — really backfilled
    assert backfilled.historical_floor_override_at is None  # unchanged: None was already the correct seed value


def test_ensure_seed_entities_never_overwrites_an_already_configured_entity(dev_composition):
    """Backfill-only: if an entity's accounting-period configuration is
    already set (a real future operator correction, or simply because
    it was created fresh with the current seed code), a later
    ensure_seed_entities call must never silently overwrite it."""
    canonical_name, display_name, entity_type, _, _ = SEED_ENTITIES[0]
    deliberately_different_floor = datetime(2099, 1, 1, tzinfo=timezone.utc)

    pre_existing = dev_composition.api.entity_repository.register_entity(
        entity_type=entity_type,
        canonical_name=canonical_name,
        display_name=display_name,
        status="ACTIVE",
        fiscal_year_start_month_day="12-25",
        historical_floor_override_at=deliberately_different_floor,
    )

    ensure_seed_entities(dev_composition)

    unchanged = dev_composition.api.entity_repository.get_entity(pre_existing.entity_id)
    assert unchanged.fiscal_year_start_month_day == "12-25"
    assert unchanged.historical_floor_override_at == deliberately_different_floor


def test_end_to_end_the_exact_live_production_scenario_this_fix_closes(dev_composition):
    """The precise, real scenario found during PL review: simulates the
    production Mac appliance's actual current state (all three
    canonical entities already registered, from CD-6 Slice 1, with no
    accounting-period configuration at all) and proves the full real
    call sequence the sweep router actually performs
    (ensure_seed_entities, then compute_bootstrap_floor) now succeeds.

    RE-DERIVED for the mailbox-scoped compute_bootstrap_floor signature
    (second correction): matt@infosecurs.com has no default_entity_id
    hint and no MailboxDomainRule rows, so it falls back to every
    seeded entity and returns the minimum of their DERIVED historical-
    bootstrap values — 2024-11-01 (Infosecurs), the earliest of the
    three as of "now" = 2026-09-18 — never the old, wrong, stored-
    literal answer this test asserted before the rename (2025-04-06,
    which was merely Matthew Scott Personal's coincidentally-equal
    OLD email_bootstrap_floor_at literal, not a real derivation)."""
    for canonical_name, display_name, entity_type, _, _ in SEED_ENTITIES:
        dev_composition.api.entity_repository.register_entity(
            entity_type=entity_type, canonical_name=canonical_name, display_name=display_name, status="ACTIVE"
        )

    # Before the fix: compute_bootstrap_floor would raise ConflictError
    # here forever, since these rows can never be re-created with the
    # new fields (entity_id/canonical_name uniqueness already claimed).
    ensure_seed_entities(dev_composition)

    mailbox_repo = InMemoryMailboxSourceRepository()
    mailbox = mailbox_repo.create_mailbox(
        display_name="Matt", email_address="matt@infosecurs.com", provider_kind=PROVIDER_MICROSOFT_GRAPH
    )
    domain_rule_repo = InMemoryMailboxDomainRuleRepository()

    floor = compute_bootstrap_floor(
        mailbox=mailbox,
        entity_repository=dev_composition.api.entity_repository,
        domain_rule_repository=domain_rule_repo,
        now=datetime(2026, 9, 18, tzinfo=timezone.utc),
    )
    assert floor == datetime(2024, 11, 1, tzinfo=timezone.utc)
