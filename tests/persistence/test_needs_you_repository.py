"""PID §98.5 'Needs You persistence' proofs (CD-6 Slice 1).

Mirrors `tests/persistence/test_intake_repository.py`'s own style and
rigor for the closest structural precedent to `NeedsYouItem` — a
workflow-attempt object with a small closed state machine and a
partial-unique-index-backed idempotent-creation guarantee. `evidence_id`
values here are fabricated UUIDv7-shaped strings (this delivery's
persistence proof does not need a real EvidenceItem row behind
`source_object_reference` — that column is deliberately NOT a foreign
key, see `persistence/postgres/needs_you_models.py`'s own docstring).
"""
from __future__ import annotations

import pytest
from sqlalchemy import func, select

from core import identity
from core.errors import InvalidStateTransitionError, NotFoundError, ValidationError
from persistence.postgres.needs_you_models import NeedsYouItemRow
from persistence.postgres.needs_you_repository import PostgresNeedsYouRepository
from persistence.postgres.session import get_engine


def _item_count_for_dedupe_key(item_type: str, source_object_reference: str) -> int:
    with get_engine().connect() as conn:
        return conn.execute(
            select(func.count())
            .select_from(NeedsYouItemRow)
            .where(
                NeedsYouItemRow.item_type == item_type,
                NeedsYouItemRow.source_object_reference == source_object_reference,
            )
        ).scalar_one()


# ---------------------------------------------------------------------
# Create / get round trip
# ---------------------------------------------------------------------


def test_needs_you_item_persists_and_is_retrievable_via_a_fresh_repository_instance(fresh_engine):
    repo = PostgresNeedsYouRepository()
    evidence_id = identity.generate_id()

    created = repo.create_needs_you_item(
        item_type="COMPANY_REQUIRED",
        domain="EVIDENCE_INTAKE",
        source_object_reference=evidence_id,
        question="Which company is this for?",
        allowed_action_type="COMPANY_WHAT_WHY",
    )
    assert created.status == "OPEN"
    assert created.resolved_at is None
    assert created.resolution is None
    assert created.priority == "NORMAL"

    fresh_repo = PostgresNeedsYouRepository(engine=fresh_engine)
    fetched = fresh_repo.get_needs_you_item(created.item_id)
    assert fetched == created


def test_unknown_item_id_raises_not_found():
    repo = PostgresNeedsYouRepository()
    with pytest.raises(NotFoundError):
        repo.get_needs_you_item(identity.generate_id())


def test_malformed_item_id_raises_not_found_not_persistence_error():
    repo = PostgresNeedsYouRepository()
    with pytest.raises(NotFoundError):
        repo.get_needs_you_item("not-a-valid-uuid")


def test_malformed_item_id_in_resolve_raises_not_found():
    repo = PostgresNeedsYouRepository()
    with pytest.raises(NotFoundError):
        repo.resolve_needs_you_item(
            "not-a-valid-uuid",
            new_status="RESOLVED",
            resolution={"entity_id": identity.generate_id()},
            actor_type="USER",
            actor_id="matt",
        )


def test_malformed_item_type_is_rejected_as_a_validation_error_before_any_db_round_trip():
    repo = PostgresNeedsYouRepository()
    with pytest.raises(ValidationError):
        repo.create_needs_you_item(
            item_type="not valid",
            domain="EVIDENCE_INTAKE",
            question="x",
            allowed_action_type="COMPANY_WHAT_WHY",
        )


# ---------------------------------------------------------------------
# State transitions persist durably
# ---------------------------------------------------------------------


def test_resolve_persists_and_rejects_re_resolving_an_already_terminal_item(fresh_engine):
    repo = PostgresNeedsYouRepository()
    created = repo.create_needs_you_item(
        item_type="COMPANY_REQUIRED",
        domain="EVIDENCE_INTAKE",
        question="Which company is this for?",
        allowed_action_type="COMPANY_WHAT_WHY",
    )

    resolution = {"entity_id": identity.generate_id(), "what": "Software", "why": "R&D testing"}
    resolved = repo.resolve_needs_you_item(
        created.item_id,
        new_status="RESOLVED",
        resolution=resolution,
        actor_type="USER",
        actor_id="matt",
    )
    assert resolved.status == "RESOLVED"
    assert resolved.resolution == resolution
    assert resolved.resolved_at is not None
    assert resolved.resolved_by_actor_type == "USER"
    assert resolved.resolved_by_actor_id == "matt"

    fresh_repo = PostgresNeedsYouRepository(engine=fresh_engine)
    fetched = fresh_repo.get_needs_you_item(created.item_id)
    assert fetched.status == "RESOLVED"
    assert fetched.resolution == resolution

    with pytest.raises(InvalidStateTransitionError):
        fresh_repo.resolve_needs_you_item(
            created.item_id,
            new_status="DISMISSED",
            resolution=None,
            actor_type="USER",
            actor_id="matt",
        )
    # Rejected re-transition must not have changed persisted state.
    assert repo.get_needs_you_item(created.item_id).status == "RESOLVED"


def test_dismiss_persists_with_no_resolution_payload_required():
    repo = PostgresNeedsYouRepository()
    created = repo.create_needs_you_item(
        item_type="GENERIC_QUESTION",
        domain="EVIDENCE_INTAKE",
        question="Is this relevant at all?",
        allowed_action_type="COMPANY_WHAT_WHY",
    )
    dismissed = repo.resolve_needs_you_item(
        created.item_id, new_status="DISMISSED", resolution=None, actor_type="USER", actor_id="matt"
    )
    assert dismissed.status == "DISMISSED"
    assert dismissed.resolution is None
    assert dismissed.resolved_at is not None


# ---------------------------------------------------------------------
# Idempotent creation — durable across a fresh repository instance
# ---------------------------------------------------------------------


def test_repeated_create_for_same_type_and_source_reference_is_idempotent_via_a_brand_new_instance():
    """The CD-6 acceptance requirement this test exists to prove: 'a
    replayed intake request does not create a duplicate Needs You
    item' — proved here at the repository layer directly (the HTTP-
    layer proof lives in `tests/app_api/test_needs_you_endpoint.py`)."""
    repo = PostgresNeedsYouRepository()
    evidence_id = identity.generate_id()

    first = repo.create_needs_you_item(
        item_type="COMPANY_REQUIRED",
        domain="EVIDENCE_INTAKE",
        source_object_reference=evidence_id,
        question="Which company is this for?",
        allowed_action_type="COMPANY_WHAT_WHY",
    )

    # Brand new repository instance — no Python-level cache anywhere in
    # PostgresNeedsYouRepository, so this is what actually proves
    # durability across a "process restart", not in-process memoization.
    retry_repo = PostgresNeedsYouRepository()
    second = retry_repo.create_needs_you_item(
        item_type="COMPANY_REQUIRED",
        domain="EVIDENCE_INTAKE",
        source_object_reference=evidence_id,
        question="Which company is this for?",
        allowed_action_type="COMPANY_WHAT_WHY",
    )

    assert first.item_id == second.item_id
    assert _item_count_for_dedupe_key("COMPANY_REQUIRED", evidence_id) == 1


def test_different_item_type_for_same_source_reference_is_not_deduped():
    """The dedupe key is `(item_type, source_object_reference)`
    together — a DIFFERENT item_type about the same evidence is a
    genuinely distinct question, not a replay."""
    repo = PostgresNeedsYouRepository()
    evidence_id = identity.generate_id()

    company_item = repo.create_needs_you_item(
        item_type="COMPANY_REQUIRED",
        domain="EVIDENCE_INTAKE",
        source_object_reference=evidence_id,
        question="Which company is this for?",
        allowed_action_type="COMPANY_WHAT_WHY",
    )
    classification_item = repo.create_needs_you_item(
        item_type="CLASSIFICATION_REVIEW",
        domain="EVIDENCE_INTAKE",
        source_object_reference=evidence_id,
        question="Confirm this document's classification?",
        allowed_action_type="CLASSIFICATION_CONFIRM",
    )
    assert company_item.item_id != classification_item.item_id


def test_items_with_no_source_reference_are_never_deduped():
    repo = PostgresNeedsYouRepository()
    first = repo.create_needs_you_item(
        item_type="GENERIC_QUESTION", domain="EVIDENCE_INTAKE", question="q", allowed_action_type="ANSWER"
    )
    second = repo.create_needs_you_item(
        item_type="GENERIC_QUESTION", domain="EVIDENCE_INTAKE", question="q", allowed_action_type="ANSWER"
    )
    assert first.item_id != second.item_id


def test_find_by_dedupe_key_is_a_read_only_non_raising_query():
    repo = PostgresNeedsYouRepository()
    assert repo.find_by_dedupe_key("COMPANY_REQUIRED", identity.generate_id()) is None

    evidence_id = identity.generate_id()
    created = repo.create_needs_you_item(
        item_type="COMPANY_REQUIRED",
        domain="EVIDENCE_INTAKE",
        source_object_reference=evidence_id,
        question="Which company is this for?",
        allowed_action_type="COMPANY_WHAT_WHY",
    )
    found = repo.find_by_dedupe_key("COMPANY_REQUIRED", evidence_id)
    assert found is not None
    assert found.item_id == created.item_id


# ---------------------------------------------------------------------
# Listing / ordering
# ---------------------------------------------------------------------


def test_list_needs_you_items_puts_open_items_first_then_by_priority():
    repo = PostgresNeedsYouRepository()
    low = repo.create_needs_you_item(
        item_type="GENERIC_QUESTION", domain="EVIDENCE_INTAKE", question="low", allowed_action_type="A", priority="LOW"
    )
    high = repo.create_needs_you_item(
        item_type="GENERIC_QUESTION", domain="EVIDENCE_INTAKE", question="high", allowed_action_type="A", priority="HIGH"
    )
    normal = repo.create_needs_you_item(
        item_type="GENERIC_QUESTION", domain="EVIDENCE_INTAKE", question="normal", allowed_action_type="A"
    )
    resolved = repo.create_needs_you_item(
        item_type="GENERIC_QUESTION", domain="EVIDENCE_INTAKE", question="resolved-one", allowed_action_type="A"
    )
    repo.resolve_needs_you_item(
        resolved.item_id, new_status="RESOLVED", resolution={"note": "done"}, actor_type="USER", actor_id="matt"
    )

    items = repo.list_needs_you_items()
    ids = [i.item_id for i in items]
    # every OPEN item appears before the RESOLVED one
    assert ids.index(resolved.item_id) > ids.index(low.item_id)
    assert ids.index(resolved.item_id) > ids.index(high.item_id)
    assert ids.index(resolved.item_id) > ids.index(normal.item_id)
    # HIGH before NORMAL before LOW among the OPEN items
    assert ids.index(high.item_id) < ids.index(normal.item_id) < ids.index(low.item_id)


def test_list_needs_you_items_filters_by_status_item_type_and_domain():
    repo = PostgresNeedsYouRepository()
    item = repo.create_needs_you_item(
        item_type="COMPANY_REQUIRED", domain="EVIDENCE_INTAKE", question="q", allowed_action_type="A"
    )
    repo.create_needs_you_item(
        item_type="RULE_APPROVAL", domain="EMAIL_TRIAGE", question="q2", allowed_action_type="B"
    )

    assert [i.item_id for i in repo.list_needs_you_items(item_type="COMPANY_REQUIRED")] == [item.item_id]
    assert [i.item_id for i in repo.list_needs_you_items(domain="EVIDENCE_INTAKE")] == [item.item_id]
    open_items = repo.list_needs_you_items(status="OPEN")
    assert len(open_items) == 2
    assert all(i.status == "OPEN" for i in open_items)
