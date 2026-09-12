"""PID §42 'External reference' + §10/§23/§34 composite-uniqueness/
idempotency proofs, exercised against the REAL database constraint
(`uq_external_references_tuple`) — not application logic alone.
"""
from __future__ import annotations

import pytest
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from core import identity
from core.errors import DuplicateExternalReferenceError
from persistence.postgres.external_reference_repository import PostgresExternalReferenceRepository
from persistence.postgres.models import ExternalReferenceRow
from persistence.postgres.session import get_engine, session_scope
from persistence.postgres.source_repository import PostgresSourceRepository


def _make_source():
    return PostgresSourceRepository().register_source(
        source_type="MANUAL_UPLOAD", provider="INTERNAL", status="ACTIVE"
    )


def test_replay_of_same_tuple_and_target_returns_existing_row_no_duplicate_created(fresh_engine):
    source = _make_source()
    repo = PostgresExternalReferenceRepository()
    target_id = identity.generate_id()

    first = repo.link_external_reference(
        provider="MICROSOFT_GRAPH",
        source_id=source.source_id,
        resource_type="EMAIL_MESSAGE",
        external_id="AAMk-synthetic-0001",
        canonical_object_type="EvidenceItem",
        canonical_object_id=target_id,
    )
    second = repo.link_external_reference(
        provider="MICROSOFT_GRAPH",
        source_id=source.source_id,
        resource_type="EMAIL_MESSAGE",
        external_id="AAMk-synthetic-0001",
        canonical_object_type="EvidenceItem",
        canonical_object_id=target_id,
    )

    assert first.external_reference_id == second.external_reference_id

    # Reconnect with a brand-new repository instance to prove the
    # replay result is genuinely durable, not in-process memoization.
    fresh_repo = PostgresExternalReferenceRepository(engine=fresh_engine)
    found = fresh_repo.find_by_tuple(
        provider="MICROSOFT_GRAPH",
        source_id=source.source_id,
        resource_type="EMAIL_MESSAGE",
        external_id="AAMk-synthetic-0001",
    )
    assert found is not None and found.external_reference_id == first.external_reference_id

    with Session(fresh_engine) as session:
        count = (
            session.query(ExternalReferenceRow)
            .filter_by(
                provider="MICROSOFT_GRAPH",
                source_id=source.source_id,
                resource_type="EMAIL_MESSAGE",
                external_id="AAMk-synthetic-0001",
            )
            .count()
        )
    assert count == 1


def test_conflicting_target_for_same_tuple_raises_duplicate_error(fresh_engine):
    source = _make_source()
    repo = PostgresExternalReferenceRepository()
    original_target = identity.generate_id()
    conflicting_target = identity.generate_id()

    repo.link_external_reference(
        provider="XERO",
        source_id=source.source_id,
        resource_type="INVOICE",
        external_id="XERO-INV-0001",
        canonical_object_type="EvidenceItem",
        canonical_object_id=original_target,
    )

    with pytest.raises(DuplicateExternalReferenceError):
        repo.link_external_reference(
            provider="XERO",
            source_id=source.source_id,
            resource_type="INVOICE",
            external_id="XERO-INV-0001",
            canonical_object_type="EvidenceItem",
            canonical_object_id=conflicting_target,
        )

    # The original mapping must remain exactly as it was — the
    # rejected conflicting call must not have altered it.
    fresh_repo = PostgresExternalReferenceRepository(engine=fresh_engine)
    still = fresh_repo.find_by_tuple(
        provider="XERO", source_id=source.source_id, resource_type="INVOICE", external_id="XERO-INV-0001"
    )
    assert still is not None
    assert still.canonical_object_id == original_target

    with Session(fresh_engine) as session:
        count = (
            session.query(ExternalReferenceRow)
            .filter_by(provider="XERO", source_id=source.source_id, resource_type="INVOICE", external_id="XERO-INV-0001")
            .count()
        )
    assert count == 1  # the rejected attempt created no second row


def test_the_same_external_id_may_reappear_under_a_different_provider_without_collision():
    source = _make_source()
    repo = PostgresExternalReferenceRepository()

    first = repo.link_external_reference(
        provider="MICROSOFT_GRAPH",
        source_id=source.source_id,
        resource_type="EMAIL_MESSAGE",
        external_id="SHARED-EXTERNAL-ID",
        canonical_object_type="EvidenceItem",
        canonical_object_id=identity.generate_id(),
    )
    second = repo.link_external_reference(
        provider="GMAIL",
        source_id=source.source_id,
        resource_type="EMAIL_MESSAGE",
        external_id="SHARED-EXTERNAL-ID",
        canonical_object_type="EvidenceItem",
        canonical_object_id=identity.generate_id(),
    )

    assert first.external_reference_id != second.external_reference_id


def test_database_itself_rejects_a_raw_duplicate_tuple_insert_bypassing_the_repository():
    """Proves the uniqueness enforcement is a REAL database constraint,
    not merely something this repository's Python code happens to
    check — a direct, hand-built row insert with a colliding tuple
    (going around `PostgresExternalReferenceRepository` entirely) is
    rejected by PostgreSQL itself."""
    source = _make_source()
    repo = PostgresExternalReferenceRepository()
    first = repo.link_external_reference(
        provider="STARLING",
        source_id=source.source_id,
        resource_type="BANK_TRANSACTION",
        external_id="STARLING-TXN-0001",
        canonical_object_type="EvidenceItem",
        canonical_object_id=identity.generate_id(),
    )

    with pytest.raises(IntegrityError):
        with session_scope() as session:
            session.add(
                ExternalReferenceRow(
                    external_reference_id=identity.generate_id(),
                    provider="STARLING",
                    resource_type="BANK_TRANSACTION",
                    external_id="STARLING-TXN-0001",
                    canonical_object_type="EvidenceItem",
                    canonical_object_id=identity.generate_id(),
                    source_id=source.source_id,
                    first_observed_at=first.first_observed_at,
                    metadata_={},
                )
            )

    with get_engine().connect() as conn:
        count = conn.execute(
            select(func.count())
            .select_from(ExternalReferenceRow)
            .where(
                ExternalReferenceRow.provider == "STARLING",
                ExternalReferenceRow.source_id == source.source_id,
                ExternalReferenceRow.resource_type == "BANK_TRANSACTION",
                ExternalReferenceRow.external_id == "STARLING-TXN-0001",
            )
        ).scalar_one()
    assert count == 1
