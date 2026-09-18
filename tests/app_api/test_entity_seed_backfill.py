"""PL-review finding, CD-6 architect amendment (email historical-
ingestion boundary): ``ensure_seed_entities`` (app/api/composition.py)
originally only set ``fiscal_year_start_month_day``/
``email_bootstrap_floor_at`` when CREATING a brand-new canonical
entity — an entity that already existed (every entity registered
during CD-6 Slice 1, including all three already live on the
production appliance, long before these two fields existed) was
returned unchanged, with both fields permanently ``None``. Without a
real backfill path, the architect's own verified accounting-period
dates could never actually reach the real, already-existing entities,
and ``services.mailbox.sweep.compute_bootstrap_floor`` would refuse
the real historical sweep forever — a real, live-blocking gap, not a
theoretical one, since the exact scenario it describes is the current
state of the production Mac appliance.

These tests prove the fix directly against ``ensure_seed_entities``
itself (not just the lower-level repository primitive it now calls —
see ``tests/persistence/test_entity_persistence.py`` for that proof).
"""
from __future__ import annotations

import pytest

from app.api.composition import SEED_ENTITIES, ensure_seed_entities, get_composition, reset_composition_for_tests
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
    email_bootstrap_floor_at at all) must have those fields backfilled
    the next time ensure_seed_entities runs — not left permanently
    None forever."""
    canonical_name, display_name, entity_type, fiscal_year_start_month_day, email_bootstrap_floor_at = (
        SEED_ENTITIES[0]
    )

    pre_existing = dev_composition.api.entity_repository.register_entity(
        entity_type=entity_type,
        canonical_name=canonical_name,
        display_name=display_name,
        status="ACTIVE",
        # Deliberately omitted, exactly like every real CD-6 Slice 1
        # seed call was — this is the actual shape of the bug.
    )
    assert pre_existing.fiscal_year_start_month_day is None
    assert pre_existing.email_bootstrap_floor_at is None

    resolved = ensure_seed_entities(dev_composition)
    assert resolved[canonical_name] == pre_existing.entity_id  # same row, never re-created

    backfilled = dev_composition.api.entity_repository.get_entity(pre_existing.entity_id)
    assert backfilled.fiscal_year_start_month_day == fiscal_year_start_month_day
    assert backfilled.email_bootstrap_floor_at == email_bootstrap_floor_at
    assert backfilled.display_name == pre_existing.display_name  # untouched


def test_ensure_seed_entities_never_overwrites_an_already_configured_entity(dev_composition):
    """Backfill-only: if an entity's accounting-period configuration is
    already set (a real future operator correction, or simply because
    it was created fresh with the current seed code), a later
    ensure_seed_entities call must never silently overwrite it."""
    import datetime as _dt

    canonical_name, display_name, entity_type, _, _ = SEED_ENTITIES[0]
    deliberately_different_floor = _dt.datetime(2099, 1, 1, tzinfo=_dt.timezone.utc)

    pre_existing = dev_composition.api.entity_repository.register_entity(
        entity_type=entity_type,
        canonical_name=canonical_name,
        display_name=display_name,
        status="ACTIVE",
        fiscal_year_start_month_day="12-25",
        email_bootstrap_floor_at=deliberately_different_floor,
    )

    ensure_seed_entities(dev_composition)

    unchanged = dev_composition.api.entity_repository.get_entity(pre_existing.entity_id)
    assert unchanged.fiscal_year_start_month_day == "12-25"
    assert unchanged.email_bootstrap_floor_at == deliberately_different_floor


def test_end_to_end_the_exact_live_production_scenario_this_fix_closes(dev_composition):
    """The precise, real scenario found during PL review: simulates the
    production Mac appliance's actual current state (all three
    canonical entities already registered, from CD-6 Slice 1, with no
    accounting-period configuration at all) and proves the full real
    call sequence the sweep router actually performs
    (ensure_seed_entities, then compute_bootstrap_floor) now succeeds
    and returns the correct, real, architect-verified global-minimum
    floor (2025-04-06, Matthew Scott Personal's UK tax-year start —
    the earliest of the three) instead of refusing forever."""
    import datetime as _dt

    for canonical_name, display_name, entity_type, _, _ in SEED_ENTITIES:
        dev_composition.api.entity_repository.register_entity(
            entity_type=entity_type, canonical_name=canonical_name, display_name=display_name, status="ACTIVE"
        )

    # Before the fix: compute_bootstrap_floor would raise ConflictError
    # here forever, since these rows can never be re-created with the
    # new fields (entity_id/canonical_name uniqueness already claimed).
    ensure_seed_entities(dev_composition)
    floor = compute_bootstrap_floor(dev_composition.api.entity_repository)
    assert floor == _dt.datetime(2025, 4, 6, tzinfo=_dt.timezone.utc)
