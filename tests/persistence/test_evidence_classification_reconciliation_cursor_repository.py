"""Round-trip proofs for `services.evidence
.classification_reconciliation_cursor
.EvidenceClassificationReconciliationCursorRepository` — both the
in-memory reference implementation and the real, disposable-Postgres
`PostgresEvidenceClassificationReconciliationCursorRepository` (BAGMAN
accounting platform, `evidence/automatic-classification-activation`
WO — post-merge preflight review correction, item B(iii)).
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

from core import identity
from persistence.postgres.evidence_classification_reconciliation_cursor_repository import (
    PostgresEvidenceClassificationReconciliationCursorRepository,
)
from services.evidence.classification_reconciliation_cursor import (
    InMemoryEvidenceClassificationReconciliationCursorRepository,
)

# ---------------------------------------------------------------------
# InMemoryEvidenceClassificationReconciliationCursorRepository
# ---------------------------------------------------------------------


def test_in_memory_get_cursor_returns_none_before_any_advance():
    repo = InMemoryEvidenceClassificationReconciliationCursorRepository()
    assert repo.get_cursor("2026-01-01T00:00:00+00:00") is None


def test_in_memory_advance_cursor_creates_then_upserts_on_second_call():
    repo = InMemoryEvidenceClassificationReconciliationCursorRepository()
    key = "2026-01-01T00:00:00+00:00"
    t1 = datetime.now(timezone.utc)

    first = repo.advance_cursor(key, lap=0, last_created_at=t1, last_evidence_ids=["evidence-1"])
    assert first.cursor_key == key
    assert first.lap == 0
    assert first.last_created_at == t1
    assert first.last_evidence_ids == ("evidence-1",)
    assert first.created_at == first.updated_at

    t2 = t1 + timedelta(seconds=5)
    second = repo.advance_cursor(key, lap=0, last_created_at=t2, last_evidence_ids=["evidence-2", "evidence-3"])
    assert second.lap == 0
    assert second.last_created_at == t2
    assert second.last_evidence_ids == ("evidence-2", "evidence-3")
    assert second.created_at == first.created_at, "created_at is preserved across an upsert"
    assert second.updated_at >= first.updated_at

    fetched = repo.get_cursor(key)
    assert fetched == second


def test_in_memory_distinct_cursor_keys_are_independent():
    repo = InMemoryEvidenceClassificationReconciliationCursorRepository()
    now = datetime.now(timezone.utc)
    repo.advance_cursor("boundary-a", lap=0, last_created_at=now, last_evidence_ids=["a1"])
    repo.advance_cursor("boundary-b", lap=0, last_created_at=now, last_evidence_ids=["b1"])

    assert repo.get_cursor("boundary-a").last_evidence_ids == ("a1",)
    assert repo.get_cursor("boundary-b").last_evidence_ids == ("b1",)


# ---------------------------------------------------------------------
# `lap` — the second, independent ordering dimension (evidence/
# classification-activation-preflight WO, this round's own permanent-
# stall correction). See `services.evidence
# .classification_reconciliation_cursor`'s own docstring, "lap"
# section, for the full mechanism.
# ---------------------------------------------------------------------


def test_in_memory_higher_lap_supersedes_lower_lap_position_even_when_created_at_is_earlier():
    """A genuinely NEWER lap's proposal must be accepted WHOLESALE even
    though its own `created_at` sits strictly BEHIND the older lap's
    persisted position — this is the actual permanent-stall fix."""
    repo = InMemoryEvidenceClassificationReconciliationCursorRepository()
    key = "sweep-lap-test"
    t_old_lap_final = datetime.now(timezone.utc)
    t_new_lap_partial = t_old_lap_final - timedelta(days=1)  # far EARLIER

    old_lap = repo.advance_cursor(key, lap=0, last_created_at=t_old_lap_final, last_evidence_ids=["old-final"])
    assert old_lap.lap == 0
    assert old_lap.last_created_at == t_old_lap_final

    new_lap = repo.advance_cursor(key, lap=1, last_created_at=t_new_lap_partial, last_evidence_ids=["new-partial"])
    assert new_lap.lap == 1, "the new lap's generation must be accepted"
    assert new_lap.last_created_at == t_new_lap_partial, (
        "a strictly higher lap must supersede the old lap's position WHOLESALE, even though its own "
        "created_at is earlier than the old lap's final position — this is the fix for the permanent-"
        "stall bug (a plain created_at comparison alone would have rejected this as a regression)"
    )
    assert new_lap.last_evidence_ids == ("new-partial",)


def test_in_memory_lower_lap_proposal_never_regresses_a_higher_persisted_lap():
    repo = InMemoryEvidenceClassificationReconciliationCursorRepository()
    key = "sweep-lap-stale-test"
    t1 = datetime.now(timezone.utc)
    t2 = t1 + timedelta(seconds=30)

    repo.advance_cursor(key, lap=2, last_created_at=t1, last_evidence_ids=["lap-2"])
    result = repo.advance_cursor(key, lap=1, last_created_at=t2, last_evidence_ids=["stale-lap-1"])

    assert result.lap == 2, "a lower lap proposal must never regress the persisted lap counter"
    assert result.last_created_at == t1
    assert result.last_evidence_ids == ("lap-2",)


def test_in_memory_equal_lap_falls_through_to_ordinary_created_at_comparison():
    """When `lap` is equal, behaviour is EXACTLY the pre-existing,
    unchanged created_at-based merge (greater replaces, equal unions,
    less no-ops) — proven directly here so the "equal lap" branch is
    not assumed to behave correctly merely because the other branches
    are tested."""
    repo = InMemoryEvidenceClassificationReconciliationCursorRepository()
    key = "same-lap-test"
    t1 = datetime.now(timezone.utc)
    t2 = t1 + timedelta(seconds=5)

    repo.advance_cursor(key, lap=5, last_created_at=t1, last_evidence_ids=["a"])
    advanced = repo.advance_cursor(key, lap=5, last_created_at=t2, last_evidence_ids=["b"])
    assert advanced.lap == 5
    assert advanced.last_created_at == t2
    assert advanced.last_evidence_ids == ("b",), "a strictly later created_at at the SAME lap replaces, unchanged"

    unioned = repo.advance_cursor(key, lap=5, last_created_at=t2, last_evidence_ids=["c"])
    assert unioned.last_evidence_ids == ("b", "c"), "an equal created_at at the SAME lap unions, unchanged"

    stale = repo.advance_cursor(key, lap=5, last_created_at=t1, last_evidence_ids=["d"])
    assert stale.last_created_at == t2
    assert stale.last_evidence_ids == ("b", "c"), "an earlier created_at at the SAME lap is a no-op, unchanged"


# ---------------------------------------------------------------------
# PostgresEvidenceClassificationReconciliationCursorRepository
# ---------------------------------------------------------------------


def test_postgres_get_cursor_returns_none_before_any_advance():
    repo = PostgresEvidenceClassificationReconciliationCursorRepository()
    assert repo.get_cursor(f"nonexistent-{identity.generate_id()}") is None


def test_postgres_advance_cursor_creates_then_upserts_on_second_call_via_a_fresh_instance():
    key = f"boundary-{identity.generate_id()}"
    t1 = datetime.now(timezone.utc)

    first = PostgresEvidenceClassificationReconciliationCursorRepository().advance_cursor(
        key, lap=0, last_created_at=t1, last_evidence_ids=["evidence-1"]
    )
    assert first.cursor_key == key
    assert first.lap == 0
    assert first.last_created_at == t1
    assert first.last_evidence_ids == ("evidence-1",)

    t2 = t1 + timedelta(seconds=5)
    # A BRAND NEW repository instance for the upsert — proves durability
    # across a "process restart", not in-process memoization, mirroring
    # this codebase's own established discipline (e.g.
    # tests/persistence/test_evidence_persistence.py).
    second = PostgresEvidenceClassificationReconciliationCursorRepository().advance_cursor(
        key, lap=0, last_created_at=t2, last_evidence_ids=["evidence-2", "evidence-3"]
    )
    assert second.lap == 0
    assert second.last_created_at == t2
    assert second.last_evidence_ids == ("evidence-2", "evidence-3")
    assert second.created_at == first.created_at, "created_at is preserved across an upsert"

    fetched = PostgresEvidenceClassificationReconciliationCursorRepository().get_cursor(key)
    assert fetched is not None
    assert fetched.last_evidence_ids == ("evidence-2", "evidence-3")
    assert fetched.last_created_at == t2


def test_postgres_distinct_cursor_keys_are_independent():
    repo = PostgresEvidenceClassificationReconciliationCursorRepository()
    now = datetime.now(timezone.utc)
    key_a = f"boundary-a-{identity.generate_id()}"
    key_b = f"boundary-b-{identity.generate_id()}"

    repo.advance_cursor(key_a, lap=0, last_created_at=now, last_evidence_ids=["a1"])
    repo.advance_cursor(key_b, lap=0, last_created_at=now, last_evidence_ids=["b1"])

    assert repo.get_cursor(key_a).last_evidence_ids == ("a1",)
    assert repo.get_cursor(key_b).last_evidence_ids == ("b1",)


def test_postgres_higher_lap_supersedes_lower_lap_position_even_when_created_at_is_earlier():
    """Real-Postgres counterpart of the in-memory proof above — the
    actual permanent-stall fix, proven against the real, durable
    `SELECT ... FOR UPDATE`-guarded merge path."""
    repo = PostgresEvidenceClassificationReconciliationCursorRepository()
    key = f"pg-lap-test-{identity.generate_id()}"
    t_old_lap_final = datetime.now(timezone.utc)
    t_new_lap_partial = t_old_lap_final - timedelta(days=1)

    old_lap = repo.advance_cursor(key, lap=0, last_created_at=t_old_lap_final, last_evidence_ids=["old-final"])
    assert old_lap.lap == 0

    new_lap = repo.advance_cursor(key, lap=1, last_created_at=t_new_lap_partial, last_evidence_ids=["new-partial"])
    assert new_lap.lap == 1
    assert new_lap.last_created_at == t_new_lap_partial, (
        "a strictly higher lap must supersede the old lap's position WHOLESALE, even with an earlier "
        "created_at — the real, durable fix for the permanent-stall bug"
    )
    assert new_lap.last_evidence_ids == ("new-partial",)

    fetched = repo.get_cursor(key)
    assert fetched.lap == 1
    assert fetched.last_created_at == t_new_lap_partial


def test_postgres_lower_lap_proposal_never_regresses_a_higher_persisted_lap():
    repo = PostgresEvidenceClassificationReconciliationCursorRepository()
    key = f"pg-lap-stale-test-{identity.generate_id()}"
    t1 = datetime.now(timezone.utc)
    t2 = t1 + timedelta(seconds=30)

    repo.advance_cursor(key, lap=2, last_created_at=t1, last_evidence_ids=["lap-2"])
    result = repo.advance_cursor(key, lap=1, last_created_at=t2, last_evidence_ids=["stale-lap-1"])

    assert result.lap == 2
    assert result.last_created_at == t1
    assert result.last_evidence_ids == ("lap-2",)
