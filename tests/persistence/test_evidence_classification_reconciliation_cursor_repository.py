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

    first = repo.advance_cursor(key, last_created_at=t1, last_evidence_ids=["evidence-1"])
    assert first.cursor_key == key
    assert first.last_created_at == t1
    assert first.last_evidence_ids == ("evidence-1",)
    assert first.created_at == first.updated_at

    t2 = t1 + timedelta(seconds=5)
    second = repo.advance_cursor(key, last_created_at=t2, last_evidence_ids=["evidence-2", "evidence-3"])
    assert second.last_created_at == t2
    assert second.last_evidence_ids == ("evidence-2", "evidence-3")
    assert second.created_at == first.created_at, "created_at is preserved across an upsert"
    assert second.updated_at >= first.updated_at

    fetched = repo.get_cursor(key)
    assert fetched == second


def test_in_memory_distinct_cursor_keys_are_independent():
    repo = InMemoryEvidenceClassificationReconciliationCursorRepository()
    now = datetime.now(timezone.utc)
    repo.advance_cursor("boundary-a", last_created_at=now, last_evidence_ids=["a1"])
    repo.advance_cursor("boundary-b", last_created_at=now, last_evidence_ids=["b1"])

    assert repo.get_cursor("boundary-a").last_evidence_ids == ("a1",)
    assert repo.get_cursor("boundary-b").last_evidence_ids == ("b1",)


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
        key, last_created_at=t1, last_evidence_ids=["evidence-1"]
    )
    assert first.cursor_key == key
    assert first.last_created_at == t1
    assert first.last_evidence_ids == ("evidence-1",)

    t2 = t1 + timedelta(seconds=5)
    # A BRAND NEW repository instance for the upsert — proves durability
    # across a "process restart", not in-process memoization, mirroring
    # this codebase's own established discipline (e.g.
    # tests/persistence/test_evidence_persistence.py).
    second = PostgresEvidenceClassificationReconciliationCursorRepository().advance_cursor(
        key, last_created_at=t2, last_evidence_ids=["evidence-2", "evidence-3"]
    )
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

    repo.advance_cursor(key_a, last_created_at=now, last_evidence_ids=["a1"])
    repo.advance_cursor(key_b, last_created_at=now, last_evidence_ids=["b1"])

    assert repo.get_cursor(key_a).last_evidence_ids == ("a1",)
    assert repo.get_cursor(key_b).last_evidence_ids == ("b1",)
