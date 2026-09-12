"""PID §42 'Audit' persistence proof: audit events survive, and
correlation/causation + `list_by_correlation`/`list_by_subject`
retrieval work via a fresh connection. Also proves the class is
structurally append-only (PID §21) — no update/delete method exists.
"""
from __future__ import annotations

import pytest

from core import identity
from core.errors import ValidationError
from persistence.postgres.audit_repository import PostgresAuditRepository


def test_audit_events_persist_correlation_and_causation_survive_via_a_fresh_connection(fresh_engine):
    repo = PostgresAuditRepository()
    correlation_id = identity.generate_id()
    subject_id = identity.generate_id()

    first = repo.record_audit_event(
        event_type="EVIDENCE_OBSERVED",
        actor_type="SYSTEM",
        actor_id="bagman-persistence-tests",
        subject_type="EvidenceItem",
        subject_id=subject_id,
        correlation_id=correlation_id,
        causation_id=None,
    )
    second = repo.record_audit_event(
        event_type="PROVENANCE_RECORDED",
        actor_type="SYSTEM",
        actor_id="bagman-persistence-tests",
        subject_type="EvidenceItem",
        subject_id=subject_id,
        correlation_id=correlation_id,
        causation_id=first.audit_event_id,
    )

    fresh_repo = PostgresAuditRepository(engine=fresh_engine)

    fetched_first = fresh_repo.get_audit_event(first.audit_event_id)
    assert fetched_first == first
    assert fetched_first.causation_id is None

    by_correlation = fresh_repo.list_by_correlation(correlation_id)
    assert [e.audit_event_id for e in by_correlation] == [first.audit_event_id, second.audit_event_id]

    by_subject = fresh_repo.list_by_subject("EvidenceItem", subject_id)
    assert [e.audit_event_id for e in by_subject] == [first.audit_event_id, second.audit_event_id]
    assert by_subject[1].causation_id == first.audit_event_id


def test_invalid_actor_type_is_rejected():
    repo = PostgresAuditRepository()
    with pytest.raises(ValidationError):
        repo.record_audit_event(
            event_type="EVIDENCE_OBSERVED",
            actor_type="NOT_A_REAL_ACTOR_TYPE",
            actor_id="x",
            subject_type="EvidenceItem",
            subject_id=identity.generate_id(),
            correlation_id=identity.generate_id(),
            causation_id=None,
        )


def test_unknown_audit_event_id_raises_not_found():
    from core.errors import NotFoundError

    repo = PostgresAuditRepository()
    with pytest.raises(NotFoundError):
        repo.get_audit_event(identity.generate_id())


def test_audit_repository_class_defines_no_update_or_delete_method():
    """Structural proof of PID §21's 'repository design should make
    audit mutation difficult by default' — no update/delete method
    exists on this class at all."""
    forbidden_names = {"update_audit_event", "delete_audit_event", "update", "delete", "remove"}
    present = forbidden_names & set(dir(PostgresAuditRepository))
    assert not present, f"PostgresAuditRepository must expose no mutation method; found {present}"
