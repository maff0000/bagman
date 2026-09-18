"""PID §42 'Entity persistence' proof, plus the CD-3 §63 requirement
that entity_id PK uniqueness is enforced by the real database (not
just application logic like CD-2's InMemoryEntityRepository)."""
from __future__ import annotations

import pytest

from core import identity
from core.errors import ImmutabilityViolationError, NotFoundError
from persistence.postgres.entity_repository import PostgresEntityRepository


def test_entity_persists_and_is_retrievable_via_a_fresh_repository_instance(fresh_engine):
    repo = PostgresEntityRepository()
    entity = repo.register_entity(
        entity_type="COMPANY",
        canonical_name="SYNTHETIC_PERSISTENCE_LTD",
        display_name="Synthetic Persistence Ltd",
        status="ACTIVE",
    )

    # A brand-new repository instance, on its own fresh connection pool
    # (not the same Python object, not even the same engine) — this is
    # what actually proves durability, not in-process object identity.
    fresh_repo = PostgresEntityRepository(engine=fresh_engine)
    fetched = fresh_repo.get_entity(entity.entity_id)

    assert fetched == entity
    assert fetched.canonical_name == "SYNTHETIC_PERSISTENCE_LTD"
    assert fetched.created_at.tzinfo is not None


def test_fiscal_year_and_email_bootstrap_floor_round_trip_through_postgres(fresh_engine):
    """CD-6 architect amendment (email historical-ingestion boundary) —
    ``fiscal_year_start_month_day``/``historical_floor_override_at`` must
    survive a real Postgres round trip via a BRAND-NEW repository
    instance, exactly like every other field this module already
    proves durability for. ``historical_floor_override_at`` was renamed
    from ``email_bootstrap_floor_at`` after a real conceptual-conflation
    bug fix (see ``core.entity.GovernedEntity`` own docstring) — this
    test proves the RENAMED column round-trips, not the old name."""
    import datetime as _dt

    repo = PostgresEntityRepository()
    floor = _dt.datetime(2025, 11, 1, tzinfo=_dt.timezone.utc)
    entity = repo.register_entity(
        entity_type="COMPANY",
        canonical_name="SYNTHETIC_FISCAL_LTD",
        display_name="Synthetic Fiscal Ltd",
        status="ACTIVE",
        fiscal_year_start_month_day="11-01",
        historical_floor_override_at=floor,
    )
    assert entity.fiscal_year_start_month_day == "11-01"
    assert entity.historical_floor_override_at == floor

    fresh_repo = PostgresEntityRepository(engine=fresh_engine)
    fetched = fresh_repo.get_entity(entity.entity_id)
    assert fetched.fiscal_year_start_month_day == "11-01"
    assert fetched.historical_floor_override_at == floor


def test_fiscal_year_and_email_bootstrap_floor_default_to_null_when_omitted(fresh_engine):
    """A caller that omits the two new fields (e.g. an entity registered
    before this amendment, or a genuinely not-yet-configured future
    entity) gets real, honest `None` — never an invented default."""
    repo = PostgresEntityRepository()
    entity = repo.register_entity(
        entity_type="COMPANY", canonical_name="SYNTHETIC_UNCONFIGURED_LTD", display_name="X", status="ACTIVE"
    )
    assert entity.fiscal_year_start_month_day is None
    assert entity.historical_floor_override_at is None

    fresh_repo = PostgresEntityRepository(engine=fresh_engine)
    fetched = fresh_repo.get_entity(entity.entity_id)
    assert fetched.fiscal_year_start_month_day is None
    assert fetched.historical_floor_override_at is None


def test_set_accounting_period_configuration_backfills_an_existing_entity(fresh_engine):
    """PL-review finding, CD-6 architect amendment: an entity registered
    BEFORE fiscal_year_start_month_day/historical_floor_override_at existed
    (every entity from CD-6 Slice 1, including the three already live
    on the production appliance) has both fields permanently None
    unless a real update path exists -- register_entity only ever sets
    them at creation time, and GovernedEntity is otherwise immutable.
    Without this, the architect's own verified accounting-period dates
    could never reach an already-existing entity, and
    compute_bootstrap_floor would refuse the real historical sweep
    forever. Proven against a real Postgres round trip via a brand-new
    repository instance."""
    import datetime as _dt

    repo = PostgresEntityRepository()
    entity = repo.register_entity(
        entity_type="COMPANY", canonical_name="SYNTHETIC_BACKFILL_LTD", display_name="X", status="ACTIVE"
    )
    assert entity.historical_floor_override_at is None

    floor = _dt.datetime(2025, 11, 1, tzinfo=_dt.timezone.utc)
    updated = repo.set_accounting_period_configuration(
        entity.entity_id, fiscal_year_start_month_day="11-01", historical_floor_override_at=floor
    )
    assert updated.fiscal_year_start_month_day == "11-01"
    assert updated.historical_floor_override_at == floor
    # Every other field is untouched by this narrow update.
    assert updated.display_name == entity.display_name
    assert updated.status == entity.status
    assert updated.canonical_name == entity.canonical_name

    fresh_repo = PostgresEntityRepository(engine=fresh_engine)
    fetched = fresh_repo.get_entity(entity.entity_id)
    assert fetched.fiscal_year_start_month_day == "11-01"
    assert fetched.historical_floor_override_at == floor


def test_set_accounting_period_configuration_unknown_entity_raises_not_found():
    import datetime as _dt

    repo = PostgresEntityRepository()
    with pytest.raises(NotFoundError):
        repo.set_accounting_period_configuration(
            identity.generate_id(),
            fiscal_year_start_month_day="11-01",
            historical_floor_override_at=_dt.datetime(2025, 11, 1, tzinfo=_dt.timezone.utc),
        )


def test_unknown_entity_id_raises_not_found():
    repo = PostgresEntityRepository()
    with pytest.raises(NotFoundError):
        repo.get_entity(identity.generate_id())


def test_malformed_entity_id_raises_not_found_not_persistence_error():
    """PL bug fix (CD-3 WI-3): a syntactically-invalid (non-UUID-shaped)
    id string must resolve to NotFoundError (-> HTTP 404), not
    PersistenceError (-> HTTP 503) — a malformed id can never
    correspond to an existing row, so "not found" is the honest
    answer, not "the persistence layer is broken"."""
    repo = PostgresEntityRepository()
    with pytest.raises(NotFoundError):
        repo.get_entity("not-a-valid-uuid")


def test_re_registering_an_existing_entity_id_is_rejected_by_the_real_primary_key():
    repo = PostgresEntityRepository()
    entity = repo.register_entity(
        entity_type="COMPANY",
        canonical_name="SYNTHETIC_DUPLICATE_LTD",
        display_name="Synthetic Duplicate Ltd",
        status="ACTIVE",
    )

    with pytest.raises(ImmutabilityViolationError):
        repo.register_entity(
            entity_type="COMPANY",
            canonical_name="SYNTHETIC_DUPLICATE_LTD_AGAIN",
            display_name="Synthetic Duplicate Ltd Again",
            status="ACTIVE",
            entity_id=entity.entity_id,
        )

    # The repository must remain usable after catching the constraint
    # violation and rolling back — not left holding a broken
    # transaction/session.
    still_there = repo.get_entity(entity.entity_id)
    assert still_there.canonical_name == "SYNTHETIC_DUPLICATE_LTD"


def test_list_entities_returns_every_registered_entity():
    repo = PostgresEntityRepository()
    a = repo.register_entity(
        entity_type="COMPANY", canonical_name="SYNTHETIC_A_LTD", display_name="A", status="ACTIVE"
    )
    b = repo.register_entity(
        entity_type="COMPANY", canonical_name="SYNTHETIC_B_LTD", display_name="B", status="ACTIVE"
    )

    ids = {e.entity_id for e in repo.list_entities()}
    assert {a.entity_id, b.entity_id} == ids
