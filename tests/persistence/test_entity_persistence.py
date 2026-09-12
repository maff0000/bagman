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


def test_unknown_entity_id_raises_not_found():
    repo = PostgresEntityRepository()
    with pytest.raises(NotFoundError):
        repo.get_entity(identity.generate_id())


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
