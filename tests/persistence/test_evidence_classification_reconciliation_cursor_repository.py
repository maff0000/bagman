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

    first = repo.advance_cursor(key, lap=0, last_created_at=t1, last_evidence_ids=["evidence-1"], target=t1)
    assert first.cursor_key == key
    assert first.lap == 0
    assert first.last_created_at == t1
    assert first.last_evidence_ids == ("evidence-1",)
    assert first.target == t1
    assert first.created_at == first.updated_at

    t2 = t1 + timedelta(seconds=5)
    # Proposes a DIFFERENT target at the SAME lap — must be discarded
    # (see "target" tests below for the dedicated proof); asserted here
    # too so this ordinary upsert test does not silently assume it.
    second = repo.advance_cursor(
        key, lap=0, last_created_at=t2, last_evidence_ids=["evidence-2", "evidence-3"], target=t2
    )
    assert second.lap == 0
    assert second.last_created_at == t2
    assert second.last_evidence_ids == ("evidence-2", "evidence-3")
    assert second.target == t1, "an equal-lap proposal must never change the already-persisted target"
    assert second.created_at == first.created_at, "created_at is preserved across an upsert"
    assert second.updated_at >= first.updated_at

    fetched = repo.get_cursor(key)
    assert fetched == second


def test_in_memory_distinct_cursor_keys_are_independent():
    repo = InMemoryEvidenceClassificationReconciliationCursorRepository()
    now = datetime.now(timezone.utc)
    repo.advance_cursor("boundary-a", lap=0, last_created_at=now, last_evidence_ids=["a1"], target=now)
    repo.advance_cursor("boundary-b", lap=0, last_created_at=now, last_evidence_ids=["b1"], target=now)

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
    t_new_lap_target = t_old_lap_final + timedelta(days=2)  # the NEW lap's own freshly-snapshotted target

    old_lap = repo.advance_cursor(
        key, lap=0, last_created_at=t_old_lap_final, last_evidence_ids=["old-final"], target=t_old_lap_final
    )
    assert old_lap.lap == 0
    assert old_lap.last_created_at == t_old_lap_final
    assert old_lap.target == t_old_lap_final

    new_lap = repo.advance_cursor(
        key, lap=1, last_created_at=t_new_lap_partial, last_evidence_ids=["new-partial"], target=t_new_lap_target
    )
    assert new_lap.lap == 1, "the new lap's generation must be accepted"
    assert new_lap.last_created_at == t_new_lap_partial, (
        "a strictly higher lap must supersede the old lap's position WHOLESALE, even though its own "
        "created_at is earlier than the old lap's final position — this is the fix for the permanent-"
        "stall bug (a plain created_at comparison alone would have rejected this as a regression)"
    )
    assert new_lap.last_evidence_ids == ("new-partial",)
    assert new_lap.target == t_new_lap_target, (
        "a strictly higher lap proposal introduces its own NEW target wholesale — the one path by which a "
        "new target value may ever be established"
    )


def test_in_memory_lower_lap_proposal_never_regresses_a_higher_persisted_lap():
    repo = InMemoryEvidenceClassificationReconciliationCursorRepository()
    key = "sweep-lap-stale-test"
    t1 = datetime.now(timezone.utc)
    t2 = t1 + timedelta(seconds=30)

    repo.advance_cursor(key, lap=2, last_created_at=t1, last_evidence_ids=["lap-2"], target=t1)
    result = repo.advance_cursor(key, lap=1, last_created_at=t2, last_evidence_ids=["stale-lap-1"], target=t2)

    assert result.lap == 2, "a lower lap proposal must never regress the persisted lap counter"
    assert result.last_created_at == t1
    assert result.last_evidence_ids == ("lap-2",)
    assert result.target == t1, "a lower-lap proposal's own target must never regress the persisted target either"


def test_in_memory_equal_lap_falls_through_to_ordinary_created_at_comparison():
    """When `lap` is equal, behaviour is EXACTLY the pre-existing,
    unchanged created_at-based merge (greater replaces, equal unions,
    less no-ops) — proven directly here so the "equal lap" branch is
    not assumed to behave correctly merely because the other branches
    are tested. `target`, meanwhile, must NEVER change across any of
    these equal-lap proposals, regardless of what each one proposes for
    it (see `services.evidence.classification_reconciliation_cursor`'s
    own "target" docstring section — this is the concurrency-safety
    invariant `_resolve_cursor_advance` itself enforces)."""
    repo = InMemoryEvidenceClassificationReconciliationCursorRepository()
    key = "same-lap-test"
    t1 = datetime.now(timezone.utc)
    t2 = t1 + timedelta(seconds=5)
    frozen_target = t1 - timedelta(hours=1)

    repo.advance_cursor(key, lap=5, last_created_at=t1, last_evidence_ids=["a"], target=frozen_target)
    advanced = repo.advance_cursor(
        key, lap=5, last_created_at=t2, last_evidence_ids=["b"], target=t2 + timedelta(hours=10)
    )
    assert advanced.lap == 5
    assert advanced.last_created_at == t2
    assert advanced.last_evidence_ids == ("b",), "a strictly later created_at at the SAME lap replaces, unchanged"
    assert advanced.target == frozen_target, "target must never change on an equal-lap proposal, even a 'greater' one"

    unioned = repo.advance_cursor(
        key, lap=5, last_created_at=t2, last_evidence_ids=["c"], target=t2 + timedelta(hours=20)
    )
    assert unioned.last_evidence_ids == ("b", "c"), "an equal created_at at the SAME lap unions, unchanged"
    assert unioned.target == frozen_target, "target must never change on an equal-lap proposal, even an 'equal' one"

    stale = repo.advance_cursor(key, lap=5, last_created_at=t1, last_evidence_ids=["d"], target=t2 + timedelta(hours=30))
    assert stale.last_created_at == t2
    assert stale.last_evidence_ids == ("b", "c"), "an earlier created_at at the SAME lap is a no-op, unchanged"
    assert stale.target == frozen_target, "target must never change on an equal-lap proposal, even a 'lesser' one"


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
        key, lap=0, last_created_at=t1, last_evidence_ids=["evidence-1"], target=t1
    )
    assert first.cursor_key == key
    assert first.lap == 0
    assert first.last_created_at == t1
    assert first.last_evidence_ids == ("evidence-1",)
    assert first.target == t1

    t2 = t1 + timedelta(seconds=5)
    # A BRAND NEW repository instance for the upsert — proves durability
    # across a "process restart", not in-process memoization, mirroring
    # this codebase's own established discipline (e.g.
    # tests/persistence/test_evidence_persistence.py). Proposes a
    # DIFFERENT target at the SAME lap — must be discarded.
    second = PostgresEvidenceClassificationReconciliationCursorRepository().advance_cursor(
        key, lap=0, last_created_at=t2, last_evidence_ids=["evidence-2", "evidence-3"], target=t2
    )
    assert second.lap == 0
    assert second.last_created_at == t2
    assert second.last_evidence_ids == ("evidence-2", "evidence-3")
    assert second.target == t1, "an equal-lap proposal must never change the already-persisted target"
    assert second.created_at == first.created_at, "created_at is preserved across an upsert"

    fetched = PostgresEvidenceClassificationReconciliationCursorRepository().get_cursor(key)
    assert fetched is not None
    assert fetched.last_evidence_ids == ("evidence-2", "evidence-3")
    assert fetched.last_created_at == t2
    assert fetched.target == t1


def test_postgres_distinct_cursor_keys_are_independent():
    repo = PostgresEvidenceClassificationReconciliationCursorRepository()
    now = datetime.now(timezone.utc)
    key_a = f"boundary-a-{identity.generate_id()}"
    key_b = f"boundary-b-{identity.generate_id()}"

    repo.advance_cursor(key_a, lap=0, last_created_at=now, last_evidence_ids=["a1"], target=now)
    repo.advance_cursor(key_b, lap=0, last_created_at=now, last_evidence_ids=["b1"], target=now)

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
    t_new_lap_target = t_old_lap_final + timedelta(days=2)

    old_lap = repo.advance_cursor(
        key, lap=0, last_created_at=t_old_lap_final, last_evidence_ids=["old-final"], target=t_old_lap_final
    )
    assert old_lap.lap == 0

    new_lap = repo.advance_cursor(
        key, lap=1, last_created_at=t_new_lap_partial, last_evidence_ids=["new-partial"], target=t_new_lap_target
    )
    assert new_lap.lap == 1
    assert new_lap.last_created_at == t_new_lap_partial, (
        "a strictly higher lap must supersede the old lap's position WHOLESALE, even with an earlier "
        "created_at — the real, durable fix for the permanent-stall bug"
    )
    assert new_lap.last_evidence_ids == ("new-partial",)
    assert new_lap.target == t_new_lap_target, (
        "a strictly higher lap proposal introduces its own NEW target wholesale — the real, durable version "
        "of this invariant"
    )

    fetched = repo.get_cursor(key)
    assert fetched.lap == 1
    assert fetched.last_created_at == t_new_lap_partial
    assert fetched.target == t_new_lap_target


def test_postgres_lower_lap_proposal_never_regresses_a_higher_persisted_lap():
    repo = PostgresEvidenceClassificationReconciliationCursorRepository()
    key = f"pg-lap-stale-test-{identity.generate_id()}"
    t1 = datetime.now(timezone.utc)
    t2 = t1 + timedelta(seconds=30)

    repo.advance_cursor(key, lap=2, last_created_at=t1, last_evidence_ids=["lap-2"], target=t1)
    result = repo.advance_cursor(key, lap=1, last_created_at=t2, last_evidence_ids=["stale-lap-1"], target=t2)

    assert result.lap == 2
    assert result.last_created_at == t1
    assert result.last_evidence_ids == ("lap-2",)
    assert result.target == t1, "a lower-lap proposal's own target must never regress the persisted target either"


def test_postgres_equal_lap_proposal_never_changes_an_already_persisted_target():
    """Real-Postgres counterpart of the in-memory "target never changes
    on an equal-lap proposal" proof — the dedicated concurrency-safety
    invariant `_resolve_cursor_advance` itself enforces (see
    `services.evidence.classification_reconciliation_cursor`'s own
    "target" docstring section), proven here against the real, durable
    `SELECT ... FOR UPDATE`-guarded merge path, independent of any
    genuine thread race (the real-thread race version lives in
    `tests/persistence/test_evidence_classification_job_reconciliation_sweep.py`)."""
    repo = PostgresEvidenceClassificationReconciliationCursorRepository()
    key = f"pg-equal-lap-target-test-{identity.generate_id()}"
    t1 = datetime.now(timezone.utc)
    t2 = t1 + timedelta(seconds=5)
    frozen_target = t1 - timedelta(hours=1)

    repo.advance_cursor(key, lap=5, last_created_at=t1, last_evidence_ids=["a"], target=frozen_target)
    advanced = repo.advance_cursor(
        key, lap=5, last_created_at=t2, last_evidence_ids=["b"], target=t2 + timedelta(hours=10)
    )
    assert advanced.lap == 5
    assert advanced.last_created_at == t2
    assert advanced.target == frozen_target, "an equal-lap 'greater created_at' proposal must never change target"

    fetched = repo.get_cursor(key)
    assert fetched.target == frozen_target
