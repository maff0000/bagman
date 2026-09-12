"""PID §42 'Evidence persistence' + §23/§34 idempotency-across-restart
proof, and PID §55 transaction-atomicity proof for
`PostgresEvidenceRepository.register_evidence`'s two-row (EvidenceItem
+ ExternalReference) write.

`content_hash` values here are fabricated SHA-256-shaped hex strings —
this work item does not integrate with the real object store (WI-2
owns actual evidence bytes); only the shape needs to be contract-valid.
"""
from __future__ import annotations

import hashlib
from datetime import datetime, timezone

import pytest
from sqlalchemy import func, select

from core import identity
from core.errors import DuplicateExternalReferenceError, ImmutabilityViolationError, NotFoundError, ValidationError
from persistence.postgres.entity_repository import PostgresEntityRepository
from persistence.postgres.evidence_repository import PostgresEvidenceRepository
from persistence.postgres.external_reference_repository import PostgresExternalReferenceRepository
from persistence.postgres.models import EvidenceItemRow
from persistence.postgres.session import get_engine
from persistence.postgres.source_repository import PostgresSourceRepository


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _fabricated_content_hash(tag: str) -> str:
    """A fabricated, but SHA-256-shaped (64 lowercase hex chars),
    content hash — distinct per `tag`, never real evidence bytes."""
    return hashlib.sha256(tag.encode("utf-8")).hexdigest()


def _make_source():
    return PostgresSourceRepository().register_source(
        source_type="MANUAL_UPLOAD", provider="INTERNAL", status="ACTIVE"
    )


def _evidence_count() -> int:
    with get_engine().connect() as conn:
        return conn.execute(select(func.count()).select_from(EvidenceItemRow)).scalar_one()


def test_evidence_persists_and_is_retrievable_via_a_fresh_repository_instance(fresh_engine):
    source = _make_source()
    ext_repo = PostgresExternalReferenceRepository()
    repo = PostgresEvidenceRepository(ext_repo)

    now = _utc_now()
    evidence = repo.register_evidence(
        entity_id=None,
        evidence_type="INVOICE",
        source_id=source.source_id,
        observed_at=now,
        received_at=now,
        content_hash=_fabricated_content_hash("a"),
        mime_type="application/pdf",
        size_bytes=1234,
        original_name="synthetic-invoice.pdf",
    )
    assert evidence.entity_id is None  # unresolved, never silently defaulted

    fresh_ext_repo = PostgresExternalReferenceRepository(engine=fresh_engine)
    fresh_repo = PostgresEvidenceRepository(fresh_ext_repo, engine=fresh_engine)
    fetched = fresh_repo.get_evidence(evidence.evidence_id)

    assert fetched == evidence
    assert fetched.content_hash == {"algorithm": "SHA-256", "value": _fabricated_content_hash("a")}


def test_bare_retry_of_same_external_reference_tuple_is_idempotent_via_a_brand_new_repository_instance():
    source = _make_source()
    ext_repo = PostgresExternalReferenceRepository()
    repo = PostgresEvidenceRepository(ext_repo)

    hint = ("MICROSOFT_GRAPH", "EMAIL_MESSAGE", "AAMk-synthetic-idempotency-0001")
    now = _utc_now()

    first = repo.register_evidence(
        entity_id=None,
        evidence_type="EMAIL",
        source_id=source.source_id,
        observed_at=now,
        received_at=now,
        content_hash=_fabricated_content_hash("b"),
        mime_type="message/rfc822",
        size_bytes=2048,
        external_reference=hint,
    )

    # Reconnect with a BRAND NEW repository instance for the retry —
    # this is what actually proves durability across a "process
    # restart" (PID §23), not in-process memoization: neither
    # PostgresEvidenceRepository nor
    # PostgresExternalReferenceRepository holds any Python-level cache
    # at all — every method hits the database directly.
    retry_ext_repo = PostgresExternalReferenceRepository()
    retry_repo = PostgresEvidenceRepository(retry_ext_repo)
    second = retry_repo.register_evidence(
        entity_id=None,
        evidence_type="EMAIL",
        source_id=source.source_id,
        observed_at=now,
        received_at=now,
        content_hash=_fabricated_content_hash("b"),
        mime_type="message/rfc822",
        size_bytes=2048,
        external_reference=hint,
    )

    assert first.evidence_id == second.evidence_id

    with get_engine().connect() as conn:
        evidence_rows = conn.execute(
            select(func.count())
            .select_from(EvidenceItemRow)
            .where(EvidenceItemRow.source_id == source.source_id)
        ).scalar_one()
        reference = retry_ext_repo.find_by_tuple(
            provider=hint[0], source_id=source.source_id, resource_type=hint[1], external_id=hint[2]
        )
    assert evidence_rows == 1  # exactly one EvidenceItem row, no duplicate
    assert reference is not None and reference.canonical_object_id == first.evidence_id


def test_conflicting_external_reference_target_raises_duplicate_and_leaves_no_orphan_evidence_item():
    """PID §55: the EvidenceItem + ExternalReference writes happen in
    ONE transaction — a rejected external-reference conflict must not
    leave a half-committed EvidenceItem row behind."""
    source = _make_source()
    ext_repo = PostgresExternalReferenceRepository()
    repo = PostgresEvidenceRepository(ext_repo)

    hint = ("XERO", "INVOICE", "XERO-INV-CONFLICT-0001")
    other_target_id = identity.generate_id()

    # Claim the tuple for something that is NOT an EvidenceItem first
    # (directly, bypassing register_evidence's idempotent-replay
    # pre-check, which only short-circuits for an EXISTING EvidenceItem
    # target) — this forces register_evidence down the "attempt
    # insert, let the DB constraint decide" path.
    ext_repo.link_external_reference(
        provider=hint[0],
        source_id=source.source_id,
        resource_type=hint[1],
        external_id=hint[2],
        canonical_object_type="GovernedEntity",
        canonical_object_id=other_target_id,
    )

    before_count = _evidence_count()

    now = _utc_now()
    with pytest.raises(DuplicateExternalReferenceError):
        repo.register_evidence(
            entity_id=None,
            evidence_type="INVOICE",
            source_id=source.source_id,
            observed_at=now,
            received_at=now,
            content_hash=_fabricated_content_hash("d"),
            mime_type="application/pdf",
            size_bytes=20,
            external_reference=hint,
        )

    after_count = _evidence_count()
    assert after_count == before_count  # no orphan EvidenceItem row left behind by the rolled-back transaction

    still = ext_repo.find_by_tuple(
        provider=hint[0], source_id=source.source_id, resource_type=hint[1], external_id=hint[2]
    )
    assert still.canonical_object_type == "GovernedEntity"
    assert still.canonical_object_id == other_target_id


def test_update_status_persists_and_rejects_a_non_documented_status():
    source = _make_source()
    repo = PostgresEvidenceRepository(PostgresExternalReferenceRepository())
    now = _utc_now()
    evidence = repo.register_evidence(
        entity_id=None,
        evidence_type="DOCUMENT",
        source_id=source.source_id,
        observed_at=now,
        received_at=now,
        content_hash=_fabricated_content_hash("e"),
        mime_type="application/pdf",
        size_bytes=5,
    )

    updated = repo.update_status(evidence.evidence_id, "AVAILABLE")
    assert updated.status == "AVAILABLE"
    assert repo.get_evidence(evidence.evidence_id).status == "AVAILABLE"

    with pytest.raises(ValidationError):
        # Financial statuses must never appear here (PID §25).
        repo.update_status(evidence.evidence_id, "PAID")


def test_assign_entity_resolves_once_and_rejects_a_second_call():
    entity = PostgresEntityRepository().register_entity(
        entity_type="COMPANY",
        canonical_name="SYNTHETIC_ASSIGN_LTD",
        display_name="Synthetic Assign Ltd",
        status="ACTIVE",
    )
    source = _make_source()
    repo = PostgresEvidenceRepository(PostgresExternalReferenceRepository())
    now = _utc_now()
    evidence = repo.register_evidence(
        entity_id=None,
        evidence_type="DOCUMENT",
        source_id=source.source_id,
        observed_at=now,
        received_at=now,
        content_hash=_fabricated_content_hash("f"),
        mime_type="application/pdf",
        size_bytes=5,
    )
    assert evidence.entity_id is None

    resolved = repo.assign_entity(evidence.evidence_id, entity.entity_id)
    assert resolved.entity_id == entity.entity_id
    assert repo.get_evidence(evidence.evidence_id).entity_id == entity.entity_id

    with pytest.raises(ImmutabilityViolationError):
        repo.assign_entity(evidence.evidence_id, entity.entity_id)


def test_assign_entity_to_a_nonexistent_entity_is_rejected_by_the_real_foreign_key():
    source = _make_source()
    repo = PostgresEvidenceRepository(PostgresExternalReferenceRepository())
    now = _utc_now()
    evidence = repo.register_evidence(
        entity_id=None,
        evidence_type="DOCUMENT",
        source_id=source.source_id,
        observed_at=now,
        received_at=now,
        content_hash=_fabricated_content_hash("g"),
        mime_type="application/pdf",
        size_bytes=5,
    )

    with pytest.raises(NotFoundError):
        repo.assign_entity(evidence.evidence_id, identity.generate_id())

    # Unaffected by the rejected attempt.
    assert repo.get_evidence(evidence.evidence_id).entity_id is None


def test_unknown_evidence_id_raises_not_found():
    repo = PostgresEvidenceRepository(PostgresExternalReferenceRepository())
    with pytest.raises(NotFoundError):
        repo.get_evidence(identity.generate_id())
