"""PID §42 'Source persistence' proof, plus the CD-3 §63/PID §9
requirement that `governed_entity_hint` is stored as a plain, non-
enforced hint — never a foreign key/ownership assertion."""
from __future__ import annotations

import pytest

from core import identity
from core.errors import NotFoundError
from persistence.postgres.source_repository import PostgresSourceRepository


def test_source_persists_and_is_retained_via_a_fresh_repository_instance(fresh_engine):
    repo = PostgresSourceRepository()
    source = repo.register_source(
        source_type="MAILBOX",
        provider="MICROSOFT_GRAPH",
        status="ACTIVE",
        external_source_ref="matt@example-synthetic.test",
        metadata={"note": "synthetic persistence test"},
    )

    fresh_repo = PostgresSourceRepository(engine=fresh_engine)
    fetched = fresh_repo.get_source(source.source_id)

    assert fetched == source
    assert fetched.external_source_ref == "matt@example-synthetic.test"
    assert fetched.metadata == {"note": "synthetic persistence test"}


def test_governed_entity_hint_is_stored_but_never_enforced_as_a_foreign_key(fresh_engine):
    """PID §9: a source's entity hint is a hint, never an ownership
    assertion. Proof: a hint pointing at an entity_id that does not
    exist anywhere must still succeed."""
    nonexistent_entity_id = identity.generate_id()
    repo = PostgresSourceRepository()
    source = repo.register_source(
        source_type="MAILBOX",
        provider="MICROSOFT_GRAPH",
        status="ACTIVE",
        governed_entity_hint=nonexistent_entity_id,
    )

    fresh_repo = PostgresSourceRepository(engine=fresh_engine)
    fetched = fresh_repo.get_source(source.source_id)
    assert fetched.governed_entity_hint == nonexistent_entity_id


def test_governed_entity_hint_defaults_to_none_when_omitted():
    repo = PostgresSourceRepository()
    source = repo.register_source(source_type="MANUAL_UPLOAD", provider="INTERNAL", status="ACTIVE")
    assert source.governed_entity_hint is None
    assert repo.get_source(source.source_id).governed_entity_hint is None


def test_unknown_source_id_raises_not_found():
    repo = PostgresSourceRepository()
    with pytest.raises(NotFoundError):
        repo.get_source(identity.generate_id())


def test_list_sources_returns_every_registered_source():
    repo = PostgresSourceRepository()
    a = repo.register_source(source_type="MANUAL_UPLOAD", provider="INTERNAL", status="ACTIVE")
    b = repo.register_source(source_type="MAILBOX", provider="GMAIL", status="ACTIVE")

    ids = {s.source_id for s in repo.list_sources()}
    assert {a.source_id, b.source_id} == ids
