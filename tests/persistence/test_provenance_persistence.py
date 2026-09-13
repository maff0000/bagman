"""PID §42 'Provenance' persistence + lineage + orphan-rejection proof."""
from __future__ import annotations

import hashlib
from datetime import datetime, timezone

import pytest

from core import identity
from core.errors import InvalidProvenanceError
from persistence.postgres.evidence_repository import PostgresEvidenceRepository
from persistence.postgres.external_reference_repository import PostgresExternalReferenceRepository
from persistence.postgres.provenance_repository import PostgresProvenanceRepository
from persistence.postgres.source_repository import PostgresSourceRepository


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _fabricated_content_hash(tag: str) -> str:
    return hashlib.sha256(tag.encode("utf-8")).hexdigest()


def _make_source_and_evidence():
    source = PostgresSourceRepository().register_source(
        source_type="MANUAL_UPLOAD", provider="INTERNAL", status="ACTIVE"
    )
    ev_repo = PostgresEvidenceRepository(PostgresExternalReferenceRepository())
    now = _utc_now()
    evidence = ev_repo.register_evidence(
        entity_id=None,
        evidence_type="INVOICE",
        source_id=source.source_id,
        observed_at=now,
        received_at=now,
        content_hash=_fabricated_content_hash("h"),
        mime_type="application/pdf",
        size_bytes=1,
    )
    return source, evidence, ev_repo


def test_provenance_persists_and_traces_evidence_to_source_via_a_fresh_connection(fresh_engine):
    source, evidence, ev_repo = _make_source_and_evidence()
    repo = PostgresProvenanceRepository(ev_repo)

    subject_id = identity.generate_id()
    edge = repo.record_provenance(
        subject_type="Classification",
        subject_id=subject_id,
        evidence_id=evidence.evidence_id,
        relationship="EXTRACTED_FROM",
    )

    fresh_ext_repo = PostgresExternalReferenceRepository(engine=fresh_engine)
    fresh_ev_repo = PostgresEvidenceRepository(fresh_ext_repo, engine=fresh_engine)
    fresh_repo = PostgresProvenanceRepository(fresh_ev_repo, engine=fresh_engine)

    fetched = fresh_repo.get_provenance(edge.provenance_id)
    assert fetched == edge

    traced = fresh_repo.trace_provenance(subject_type="Classification", subject_id=subject_id)
    assert [t.provenance_id for t in traced] == [edge.provenance_id]

    # Full lineage: subject -> Provenance -> EvidenceItem -> Source,
    # resolvable after a complete restart (PID §22).
    traced_evidence = fresh_ev_repo.get_evidence(edge.evidence_id)
    assert traced_evidence.source_id == source.source_id
    fresh_source_repo = PostgresSourceRepository(engine=fresh_engine)
    traced_source = fresh_source_repo.get_source(traced_evidence.source_id)
    assert traced_source.source_id == source.source_id


def test_multiple_evidence_sources_for_one_subject_are_multiple_provenance_records():
    source, evidence_one, ev_repo = _make_source_and_evidence()
    now = _utc_now()
    evidence_two = ev_repo.register_evidence(
        entity_id=None,
        evidence_type="EMAIL",
        source_id=source.source_id,
        observed_at=now,
        received_at=now,
        content_hash=_fabricated_content_hash("i"),
        mime_type="message/rfc822",
        size_bytes=1,
    )
    repo = PostgresProvenanceRepository(ev_repo)
    subject_id = identity.generate_id()

    edge_one = repo.record_provenance(
        subject_type="Classification", subject_id=subject_id, evidence_id=evidence_one.evidence_id,
        relationship="SUPPORTS",
    )
    edge_two = repo.record_provenance(
        subject_type="Classification", subject_id=subject_id, evidence_id=evidence_two.evidence_id,
        relationship="SUPPORTS",
    )

    traced = repo.trace_provenance(subject_type="Classification", subject_id=subject_id)
    assert [t.provenance_id for t in traced] == [edge_one.provenance_id, edge_two.provenance_id]


def test_orphan_evidence_id_is_rejected_with_the_canonical_error():
    ev_repo = PostgresEvidenceRepository(PostgresExternalReferenceRepository())
    repo = PostgresProvenanceRepository(ev_repo)

    with pytest.raises(InvalidProvenanceError):
        repo.record_provenance(
            subject_type="Classification",
            subject_id=identity.generate_id(),
            evidence_id=identity.generate_id(),  # does not exist
            relationship="EXTRACTED_FROM",
        )


def test_unknown_provenance_id_raises_not_found():
    from core.errors import NotFoundError

    ev_repo = PostgresEvidenceRepository(PostgresExternalReferenceRepository())
    repo = PostgresProvenanceRepository(ev_repo)
    with pytest.raises(NotFoundError):
        repo.get_provenance(identity.generate_id())


def test_malformed_provenance_id_raises_not_found_not_persistence_error():
    """PL bug fix (CD-3 WI-3): same class of bug as evidence_repository
    -- a syntactically-invalid (non-UUID-shaped) id must resolve to
    NotFoundError, not PersistenceError."""
    from core.errors import NotFoundError

    ev_repo = PostgresEvidenceRepository(PostgresExternalReferenceRepository())
    repo = PostgresProvenanceRepository(ev_repo)
    with pytest.raises(NotFoundError):
        repo.get_provenance("not-a-valid-uuid")
