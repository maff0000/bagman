"""Domain-behaviour tests for `services.needs_you.needs_you` (CD-6
Slice 1, PID §98.5/§98.2).

Exercises `InMemoryNeedsYouRepository` and the pure `transition()`
state-machine helper directly — mirrors
`tests/integration/test_intake_domain.py`'s own style for the closest
structural precedent in this codebase. `source_object_reference` values
used here are well-formed synthetic canonical identifiers
(`core.identity.generate_id()`) — `InMemoryNeedsYouRepository`, like
every other in-memory reference repository, does not itself enforce
cross-object referential integrity (that is the real PostgreSQL
partial-unique-index's job, proven in
`tests/persistence/test_needs_you_repository.py`).
"""
from __future__ import annotations

import pytest

from core import identity
from core.errors import ConflictError, InvalidStateTransitionError, NotFoundError, ValidationError
from services.needs_you.needs_you import (
    ALLOWED_TRANSITIONS,
    DEFAULT_PRIORITY,
    ITEM_TYPE_COMPANY_REQUIRED,
    PRIORITIES,
    STATUSES,
    TERMINAL_STATUSES,
    InMemoryNeedsYouRepository,
    transition,
)


@pytest.fixture
def repo() -> InMemoryNeedsYouRepository:
    return InMemoryNeedsYouRepository()


def _create(repo: InMemoryNeedsYouRepository, **overrides):
    defaults = dict(
        item_type=ITEM_TYPE_COMPANY_REQUIRED,
        domain="EVIDENCE_INTAKE",
        question="Which company is this for?",
        allowed_action_type="COMPANY_WHAT_WHY",
    )
    defaults.update(overrides)
    return repo.create_needs_you_item(**defaults)


# ---------------------------------------------------------------------
# vocabulary sanity
# ---------------------------------------------------------------------


def test_status_vocabulary_is_open_open_resolved_dismissed():
    assert STATUSES == {"OPEN", "RESOLVED", "DISMISSED"}
    assert TERMINAL_STATUSES == {"RESOLVED", "DISMISSED"}


def test_allowed_transitions_only_leave_open_and_never_reopen():
    assert ALLOWED_TRANSITIONS["OPEN"] == frozenset({"RESOLVED", "DISMISSED"})
    assert ALLOWED_TRANSITIONS["RESOLVED"] == frozenset()
    assert ALLOWED_TRANSITIONS["DISMISSED"] == frozenset()


def test_default_priority_is_normal_and_is_a_known_priority():
    assert DEFAULT_PRIORITY == "NORMAL"
    assert DEFAULT_PRIORITY in PRIORITIES


# ---------------------------------------------------------------------
# create / get
# ---------------------------------------------------------------------


def test_create_needs_you_item_starts_open_with_no_resolution(repo):
    item = _create(repo)
    assert item.status == "OPEN"
    assert item.resolution is None
    assert item.resolved_at is None
    assert item.resolved_by_actor_type is None
    assert item.resolved_by_actor_id is None
    assert item.priority == DEFAULT_PRIORITY


def test_get_unknown_item_raises_not_found(repo):
    with pytest.raises(NotFoundError):
        repo.get_needs_you_item(identity.generate_id())


def test_to_dict_round_trips_every_required_field(repo):
    item = _create(repo, source_object_reference=identity.generate_id())
    payload = item.to_dict()
    for key in (
        "item_id",
        "item_type",
        "domain",
        "source_object_reference",
        "question",
        "allowed_action_type",
        "priority",
        "status",
        "created_at",
        "resolved_at",
        "resolved_by_actor_type",
        "resolved_by_actor_id",
        "resolution",
        "correlation_id",
        "metadata",
        "schema_version",
    ):
        assert key in payload


# ---------------------------------------------------------------------
# idempotent creation (dedupe on item_type + source_object_reference)
# ---------------------------------------------------------------------


def test_repeated_create_for_same_dedupe_key_returns_the_same_item(repo):
    evidence_id = identity.generate_id()
    first = _create(repo, source_object_reference=evidence_id)
    second = _create(repo, source_object_reference=evidence_id)
    assert first.item_id == second.item_id
    assert len(repo.list_needs_you_items()) == 1


def test_no_source_reference_means_never_deduped(repo):
    first = _create(repo, item_type="GENERIC_QUESTION", source_object_reference=None)
    second = _create(repo, item_type="GENERIC_QUESTION", source_object_reference=None)
    assert first.item_id != second.item_id


def test_find_by_dedupe_key(repo):
    evidence_id = identity.generate_id()
    assert repo.find_by_dedupe_key(ITEM_TYPE_COMPANY_REQUIRED, evidence_id) is None
    created = _create(repo, source_object_reference=evidence_id)
    found = repo.find_by_dedupe_key(ITEM_TYPE_COMPANY_REQUIRED, evidence_id)
    assert found is not None and found.item_id == created.item_id


# ---------------------------------------------------------------------
# resolve / dismiss (transition)
# ---------------------------------------------------------------------


def test_resolve_moves_to_resolved_and_stamps_resolution(repo):
    item = _create(repo)
    resolution = {"entity_id": identity.generate_id(), "what": "Software", "why": "R&D"}
    resolved = repo.resolve_needs_you_item(
        item.item_id, new_status="RESOLVED", resolution=resolution, actor_type="USER", actor_id="matt"
    )
    assert resolved.status == "RESOLVED"
    assert resolved.resolution == resolution
    assert resolved.resolved_at is not None
    assert resolved.resolved_by_actor_type == "USER"
    assert resolved.resolved_by_actor_id == "matt"


def test_dismiss_moves_to_dismissed(repo):
    item = _create(repo)
    dismissed = repo.resolve_needs_you_item(
        item.item_id, new_status="DISMISSED", resolution=None, actor_type="USER", actor_id="matt"
    )
    assert dismissed.status == "DISMISSED"


def test_resolving_an_already_terminal_item_raises_invalid_state_transition(repo):
    item = _create(repo)
    repo.resolve_needs_you_item(
        item.item_id, new_status="RESOLVED", resolution={"note": "ok"}, actor_type="USER", actor_id="matt"
    )
    with pytest.raises(InvalidStateTransitionError):
        repo.resolve_needs_you_item(
            item.item_id, new_status="DISMISSED", resolution=None, actor_type="USER", actor_id="matt"
        )


def test_transition_rejects_an_edge_not_in_the_table():
    item = InMemoryNeedsYouRepository().create_needs_you_item(
        item_type=ITEM_TYPE_COMPANY_REQUIRED,
        domain="EVIDENCE_INTAKE",
        question="q",
        allowed_action_type="A",
    )
    with pytest.raises(InvalidStateTransitionError):
        transition(item, "OPEN", resolution=None, resolved_by_actor_type="USER", resolved_by_actor_id="matt")


def test_transition_rejects_invalid_resulting_contract_shape():
    item = InMemoryNeedsYouRepository().create_needs_you_item(
        item_type=ITEM_TYPE_COMPANY_REQUIRED,
        domain="EVIDENCE_INTAKE",
        question="q",
        allowed_action_type="A",
    )
    with pytest.raises(ValidationError):
        transition(
            item,
            "RESOLVED",
            resolution=None,
            resolved_by_actor_type="NOT_A_VALID_ACTOR_TYPE lowercase and spaces",
            resolved_by_actor_id="",
        )


# ---------------------------------------------------------------------
# listing / ordering
# ---------------------------------------------------------------------


def test_list_open_items_come_before_resolved_items(repo):
    open_item = _create(repo, item_type="GENERIC_QUESTION", source_object_reference=None)
    resolved_item = _create(
        repo, item_type="GENERIC_QUESTION", question="other", source_object_reference=None
    )
    repo.resolve_needs_you_item(
        resolved_item.item_id, new_status="RESOLVED", resolution={"note": "ok"}, actor_type="USER", actor_id="matt"
    )
    ordered = repo.list_needs_you_items()
    ids = [i.item_id for i in ordered]
    assert ids.index(open_item.item_id) < ids.index(resolved_item.item_id)


def test_list_orders_open_items_by_priority_high_first(repo):
    low = _create(repo, item_type="GENERIC_QUESTION", source_object_reference=None, priority="LOW")
    high = _create(repo, item_type="GENERIC_QUESTION", source_object_reference=None, priority="HIGH")
    normal = _create(repo, item_type="GENERIC_QUESTION", source_object_reference=None, priority="NORMAL")
    ids = [i.item_id for i in repo.list_needs_you_items()]
    assert ids.index(high.item_id) < ids.index(normal.item_id) < ids.index(low.item_id)


def test_list_filters_by_status_item_type_and_domain(repo):
    company_item = _create(repo, source_object_reference=identity.generate_id())
    _create(repo, item_type="RULE_APPROVAL", domain="EMAIL_TRIAGE", source_object_reference=None)

    assert [i.item_id for i in repo.list_needs_you_items(item_type=ITEM_TYPE_COMPANY_REQUIRED)] == [
        company_item.item_id
    ]
    assert [i.item_id for i in repo.list_needs_you_items(domain="EVIDENCE_INTAKE")] == [company_item.item_id]
    assert len(repo.list_needs_you_items(status="OPEN")) == 2


def test_list_respects_limit_and_offset(repo):
    for i in range(5):
        _create(repo, item_type="GENERIC_QUESTION", question=f"q{i}", source_object_reference=None)
    page = repo.list_needs_you_items(limit=2, offset=1)
    assert len(page) == 2


# ---------------------------------------------------------------------
# update_item_metadata (operational addendum, ahead of the first real
# large historical sweep) — proof #4 of the WO's required tests
# ---------------------------------------------------------------------


def test_update_item_metadata_merges_not_replaces(repo):
    item = _create(repo, metadata={"a": 1, "b": 2})
    updated = repo.update_item_metadata(item.item_id, metadata_updates={"b": 20, "c": 30})
    assert updated.metadata == {"a": 1, "b": 20, "c": 30}
    # Durable via the repository's own read path too.
    assert repo.get_needs_you_item(item.item_id).metadata == {"a": 1, "b": 20, "c": 30}


def test_update_item_metadata_refuses_to_touch_a_non_open_item(repo):
    item = _create(repo, metadata={"a": 1})
    repo.resolve_needs_you_item(
        item.item_id, new_status="RESOLVED", resolution={"done": True}, actor_type="USER", actor_id="matt"
    )
    with pytest.raises(ConflictError):
        repo.update_item_metadata(item.item_id, metadata_updates={"a": 2})
    # Metadata genuinely untouched by the refused attempt.
    assert repo.get_needs_you_item(item.item_id).metadata == {"a": 1}


def test_update_item_metadata_refuses_on_a_dismissed_item_too(repo):
    item = _create(repo, metadata={"a": 1})
    repo.resolve_needs_you_item(
        item.item_id, new_status="DISMISSED", resolution=None, actor_type="USER", actor_id="matt"
    )
    with pytest.raises(ConflictError):
        repo.update_item_metadata(item.item_id, metadata_updates={"a": 2})


def test_update_item_metadata_unknown_item_id_raises_not_found(repo):
    with pytest.raises(NotFoundError):
        repo.update_item_metadata(identity.generate_id(), metadata_updates={"a": 1})
