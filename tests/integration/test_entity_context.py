"""Domain tests for ``core.entity_context`` (WO-BAGMAN-117 §6.A.2,
§6.A.11 acceptance criterion 19-22's "new, independent domain tests").

Proves :func:`resolve_entity_context`/:meth:`EntityContext.filter_owned`
are behaviourally EQUIVALENT to the two existing ``app/api/routers/
xero.py`` patterns they generalise (``_require_entity`` and the
redundant cross-entity ownership re-check in
``get_account_suggestion``) — without touching ``xero.py`` itself (see
``core/entity_context.py``'s own module docstring for why that file is
out of this WO's authorised scope).

Lives under ``tests/integration/`` (not a new ``tests/core/``
directory) alongside this codebase's existing pure-domain-behaviour
tests (``test_background_job_domain.py``, ``test_intake_domain.py``,
...) — deliberately NOT ``tests/persistence/`` (whose
``_clean_tables`` fixture is autoused and depends on a real, disposable
PostgreSQL container; these tests need no database at all) and not
``tests/contract/`` (raw JSON-Schema shape only — this module has no
contract, see its own docstring).
"""
from __future__ import annotations

import pytest

from core.entity import InMemoryEntityRepository
from core.entity_context import EntityContext, resolve_entity_context
from core.errors import NotFoundError


class _FakeApi:
    def __init__(self, entity_repository):
        self.entity_repository = entity_repository


class _FakeComposition:
    """The minimal duck-typed shape ``resolve_entity_context`` needs —
    deliberately NOT ``app.api.composition.RuntimeComposition`` itself
    (core/ must not depend on app/, PID §53)."""

    def __init__(self, entity_repository):
        self.api = _FakeApi(entity_repository)


@pytest.fixture
def entity_repository():
    return InMemoryEntityRepository()


@pytest.fixture
def composition(entity_repository):
    return _FakeComposition(entity_repository)


# ---------------------------------------------------------------------
# resolve_entity_context <-> xero.py::_require_entity equivalence
# ---------------------------------------------------------------------


def test_resolve_entity_context_returns_a_context_for_a_real_entity(composition, entity_repository):
    entity = entity_repository.register_entity(
        entity_type="COMPANY", canonical_name="SYNTHETIC_CTX_LTD", display_name="Synthetic Ctx Ltd", status="ACTIVE"
    )

    context = resolve_entity_context(composition, entity.entity_id)

    assert isinstance(context, EntityContext)
    assert context.entity_id == entity.entity_id
    assert context.entity == entity


def test_resolve_entity_context_raises_not_found_for_an_unknown_entity_id_exactly_like_require_entity(composition):
    """Mirrors ``xero.py::_require_entity``'s own documented contract
    exactly: "an honest 404 for a typo'd/unknown entity_id" — same
    exception type, raised the same way (via
    ``EntityRepository.get_entity``), so a caller migrating from
    ``_require_entity`` to this function sees identical behaviour."""
    with pytest.raises(NotFoundError):
        resolve_entity_context(composition, "00000000-0000-0000-0000-000000000000")


def test_resolve_entity_context_propagates_not_found_unwrapped(composition, entity_repository):
    """Not re-wrapped into some new entity_context-specific error — the
    exact same ``core.errors.NotFoundError`` ``EntityRepository.get_entity``
    itself raises, propagated directly."""
    try:
        resolve_entity_context(composition, "no-such-entity")
    except NotFoundError as exc:
        assert "no-such-entity" in str(exc)
    else:
        pytest.fail("expected NotFoundError")


# ---------------------------------------------------------------------
# EntityContext.filter_owned <-> xero.py::get_account_suggestion's
# cross-entity ownership re-check equivalence
# ---------------------------------------------------------------------


class _FakeOwnedRecord:
    """A minimal stand-in for any object carrying its own `entity_id`
    (a XeroAccountSuggestion/XeroAccountAssignment today, any future
    entity-scoped record tomorrow) — `filter_owned` is duck-typed and
    does not care about any other attribute."""

    def __init__(self, entity_id: str):
        self.entity_id = entity_id


def test_filter_owned_returns_the_object_unchanged_when_entity_matches(composition, entity_repository):
    entity = entity_repository.register_entity(
        entity_type="COMPANY", canonical_name="SYNTHETIC_OWNED_LTD", display_name="X", status="ACTIVE"
    )
    context = resolve_entity_context(composition, entity.entity_id)
    record = _FakeOwnedRecord(entity_id=entity.entity_id)

    assert context.filter_owned(record) is record


def test_filter_owned_treats_a_different_entitys_record_identically_to_not_found(composition, entity_repository):
    """This is the exact behaviour ``xero.py::get_account_suggestion``'s
    own inline check already implements — ``core/entity_context.py``'s
    own module docstring documents the exact line this generalises.
    Proven equivalent here without modifying that file."""
    entity_a = entity_repository.register_entity(
        entity_type="COMPANY", canonical_name="SYNTHETIC_A_LTD", display_name="A", status="ACTIVE"
    )
    entity_b = entity_repository.register_entity(
        entity_type="COMPANY", canonical_name="SYNTHETIC_B_LTD", display_name="B", status="ACTIVE"
    )
    context_a = resolve_entity_context(composition, entity_a.entity_id)
    record_owned_by_b = _FakeOwnedRecord(entity_id=entity_b.entity_id)

    assert context_a.filter_owned(record_owned_by_b) is None


def test_filter_owned_passes_through_none_unchanged(composition, entity_repository):
    entity = entity_repository.register_entity(
        entity_type="COMPANY", canonical_name="SYNTHETIC_NONE_LTD", display_name="X", status="ACTIVE"
    )
    context = resolve_entity_context(composition, entity.entity_id)

    assert context.filter_owned(None) is None
