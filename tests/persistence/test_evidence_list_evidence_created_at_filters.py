"""Unit tests for `EvidenceRepository.list_evidence`'s own new
`created_at_from`/`created_at_to`/`order_by_created_at` parameters
(BAGMAN accounting platform, `evidence/automatic-classification-activation`
WO — post-merge preflight review correction, item A(iii)) — proving
they filter correctly, and INDEPENDENTLY of `received_at_from`/
`received_at_to`, in both `InMemoryEvidenceRepository` and
`PostgresEvidenceRepository`.
"""
from __future__ import annotations

import hashlib
from datetime import datetime, timedelta, timezone

from core import identity
from core.external_reference import InMemoryExternalReferenceRepository
from persistence.postgres.evidence_repository import PostgresEvidenceRepository
from persistence.postgres.external_reference_repository import PostgresExternalReferenceRepository
from persistence.postgres.source_repository import PostgresSourceRepository
from services.evidence.evidence import InMemoryEvidenceRepository


def _content_hash(tag: str) -> dict:
    return {"algorithm": "SHA-256", "value": hashlib.sha256(tag.encode("utf-8")).hexdigest()}


# ---------------------------------------------------------------------
# InMemoryEvidenceRepository
# ---------------------------------------------------------------------


def _in_memory_repo() -> InMemoryEvidenceRepository:
    return InMemoryEvidenceRepository(InMemoryExternalReferenceRepository())


def test_in_memory_created_at_filters_are_independent_of_received_at():
    repo = _in_memory_repo()
    now = datetime.now(timezone.utc)

    # received_at is deliberately set in the OPPOSITE order to each
    # item's own (server-assigned) created_at, to prove the two filter
    # pairs read genuinely different columns, never conflated.
    early = repo.register_evidence(
        entity_id=None, evidence_type="DOCUMENT", source_id=identity.generate_id(),
        observed_at=now, received_at=now + timedelta(days=100),  # received_at far in the future
        content_hash=_content_hash("early"), mime_type="application/pdf", size_bytes=1, storage_reference=None,
    )
    late = repo.register_evidence(
        entity_id=None, evidence_type="DOCUMENT", source_id=identity.generate_id(),
        observed_at=now, received_at=now - timedelta(days=100),  # received_at far in the past
        content_hash=_content_hash("late"), mime_type="application/pdf", size_bytes=1, storage_reference=None,
    )
    assert early.created_at < late.created_at
    midpoint = early.created_at + (late.created_at - early.created_at) / 2

    only_early = repo.list_evidence(created_at_to=midpoint)
    assert [i.evidence_id for i in only_early] == [early.evidence_id]

    only_late = repo.list_evidence(created_at_from=midpoint)
    assert [i.evidence_id for i in only_late] == [late.evidence_id]

    # received_at_from/received_at_to still filter independently, and
    # give the OPPOSITE selection for this same data (received_at was
    # deliberately set in reverse order to created_at above) — proving
    # neither filter pair silently governs the other.
    only_early_by_received_at = repo.list_evidence(received_at_from=now)
    assert [i.evidence_id for i in only_early_by_received_at] == [early.evidence_id]

    only_late_by_received_at = repo.list_evidence(received_at_to=now)
    assert [i.evidence_id for i in only_late_by_received_at] == [late.evidence_id]


def test_in_memory_order_by_created_at():
    repo = _in_memory_repo()
    now = datetime.now(timezone.utc)
    first = repo.register_evidence(
        entity_id=None, evidence_type="DOCUMENT", source_id=identity.generate_id(),
        observed_at=now, received_at=now, content_hash=_content_hash("first"), mime_type="application/pdf",
        size_bytes=1, storage_reference=None,
    )
    second = repo.register_evidence(
        entity_id=None, evidence_type="DOCUMENT", source_id=identity.generate_id(),
        observed_at=now, received_at=now, content_hash=_content_hash("second"), mime_type="application/pdf",
        size_bytes=1, storage_reference=None,
    )
    assert first.created_at < second.created_at

    ascending = repo.list_evidence(created_at_from=first.created_at, order_by_created_at=True)
    ids = [i.evidence_id for i in ascending]
    assert ids.index(first.evidence_id) < ids.index(second.evidence_id)

    # Default ordering (order_by_created_at=False, unchanged for every
    # existing caller) is still received_at DESC — never affected by
    # this new, purely additive parameter.
    default_order = repo.list_evidence()
    relevant = [i.evidence_id for i in default_order if i.evidence_id in (first.evidence_id, second.evidence_id)]
    assert set(relevant) == {first.evidence_id, second.evidence_id}


# ---------------------------------------------------------------------
# PostgresEvidenceRepository
# ---------------------------------------------------------------------


def _postgres_repo() -> PostgresEvidenceRepository:
    return PostgresEvidenceRepository(PostgresExternalReferenceRepository())


def _postgres_source_id() -> str:
    return PostgresSourceRepository().register_source(
        source_type="MANUAL_UPLOAD", provider="INTERNAL", status="ACTIVE"
    ).source_id


def test_postgres_created_at_filters_are_independent_of_received_at():
    repo = _postgres_repo()
    source_id = _postgres_source_id()
    now = datetime.now(timezone.utc)

    early = repo.register_evidence(
        entity_id=None, evidence_type="DOCUMENT", source_id=source_id,
        observed_at=now, received_at=now + timedelta(days=100),
        content_hash=_content_hash("pg-early"), mime_type="application/pdf", size_bytes=1, storage_reference=None,
    )
    late = repo.register_evidence(
        entity_id=None, evidence_type="DOCUMENT", source_id=source_id,
        observed_at=now, received_at=now - timedelta(days=100),
        content_hash=_content_hash("pg-late"), mime_type="application/pdf", size_bytes=1, storage_reference=None,
    )
    assert early.created_at < late.created_at
    midpoint = early.created_at + (late.created_at - early.created_at) / 2

    only_early = repo.list_evidence(created_at_to=midpoint)
    assert {i.evidence_id for i in only_early} == {early.evidence_id}

    only_late = repo.list_evidence(created_at_from=midpoint)
    assert {i.evidence_id for i in only_late} == {late.evidence_id}

    only_early_by_received_at = repo.list_evidence(received_at_from=now)
    assert {i.evidence_id for i in only_early_by_received_at} == {early.evidence_id}

    only_late_by_received_at = repo.list_evidence(received_at_to=now)
    assert {i.evidence_id for i in only_late_by_received_at} == {late.evidence_id}


def test_postgres_order_by_created_at():
    repo = _postgres_repo()
    source_id = _postgres_source_id()
    now = datetime.now(timezone.utc)
    first = repo.register_evidence(
        entity_id=None, evidence_type="DOCUMENT", source_id=source_id,
        observed_at=now, received_at=now, content_hash=_content_hash("pg-first"), mime_type="application/pdf",
        size_bytes=1, storage_reference=None,
    )
    second = repo.register_evidence(
        entity_id=None, evidence_type="DOCUMENT", source_id=source_id,
        observed_at=now, received_at=now, content_hash=_content_hash("pg-second"), mime_type="application/pdf",
        size_bytes=1, storage_reference=None,
    )
    assert first.created_at < second.created_at

    ascending = repo.list_evidence(created_at_from=first.created_at, order_by_created_at=True)
    ids = [i.evidence_id for i in ascending]
    assert ids.index(first.evidence_id) < ids.index(second.evidence_id)

    default_order = repo.list_evidence()
    relevant = [i.evidence_id for i in default_order if i.evidence_id in (first.evidence_id, second.evidence_id)]
    assert set(relevant) == {first.evidence_id, second.evidence_id}
