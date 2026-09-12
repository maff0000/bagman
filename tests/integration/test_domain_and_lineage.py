"""Domain-behavior integration tests (PID §31/§42), black-box through
`core.api.BagmanCanonicalAPI` — the way a real caller would use it, not
via any repository's internals directly except where PID §33 simply
does not expose an operation on the facade (`assign_entity`,
`list_by_correlation`, `list_by_subject`, `list_evidence` are not in
PID §33's required facade operation list, so these tests reach them
via the facade's own public `*_repository` attributes — still the
facade's public surface, never a bypass of it).

Uses the fictional parties from PID §30 / `tests/fixtures/README.md`
(`Synthetic Cloud Services Ltd`, via the fixture invoice) and the two
non-personal governed entities from PID §23 (`NOUSTAI_LIMITED`,
`INFOSECURS_LIMITED`) — never a fourth, invented entity.
"""
from __future__ import annotations

import pytest

from core import identity
from core.errors import (
    DuplicateExternalReferenceError,
    ImmutabilityViolationError,
    InvalidProvenanceError,
    ValidationError,
)

ACTOR_TYPE = "SYSTEM"
ACTOR_ID = "bagman-integration-tests"


def test_multiple_governed_entities_coexist_with_independent_identities(api):
    noustai = api.register_entity(
        entity_type="COMPANY",
        canonical_name="NOUSTAI_LIMITED",
        display_name="Noust AI Limited",
        status="ACTIVE",
        actor_type=ACTOR_TYPE,
        actor_id=ACTOR_ID,
    )
    infosecurs = api.register_entity(
        entity_type="COMPANY",
        canonical_name="INFOSECURS_LIMITED",
        display_name="Infosecurs Limited",
        status="ACTIVE",
        actor_type=ACTOR_TYPE,
        actor_id=ACTOR_ID,
    )

    assert noustai.entity_id != infosecurs.entity_id
    assert noustai.canonical_name == "NOUSTAI_LIMITED"
    assert infosecurs.canonical_name == "INFOSECURS_LIMITED"

    # Independently retrievable, not merged/aliased in any way.
    assert api.entity_repository.get_entity(noustai.entity_id).canonical_name == "NOUSTAI_LIMITED"
    assert (
        api.entity_repository.get_entity(infosecurs.entity_id).canonical_name
        == "INFOSECURS_LIMITED"
    )
    assert {e.entity_id for e in api.entity_repository.list_entities()} == {
        noustai.entity_id,
        infosecurs.entity_id,
    }


def test_unresolved_entity_ownership_then_resolved_then_reassignment_rejected(
    api, utc_now, synthetic_invoice_content_hash
):
    noustai = api.register_entity(
        entity_type="COMPANY",
        canonical_name="NOUSTAI_LIMITED",
        display_name="Noust AI Limited",
        status="ACTIVE",
        actor_type=ACTOR_TYPE,
        actor_id=ACTOR_ID,
    )
    source = api.register_source(
        source_type="MANUAL_UPLOAD",
        provider="INTERNAL",
        status="ACTIVE",
        actor_type=ACTOR_TYPE,
        actor_id=ACTOR_ID,
    )
    now = utc_now()

    evidence = api.register_evidence(
        entity_id=None,  # explicitly unresolved
        evidence_type="INVOICE",
        source_id=source.source_id,
        observed_at=now,
        received_at=now,
        content_hash=synthetic_invoice_content_hash,
        mime_type="text/plain",
        size_bytes=630,
        actor_type=ACTOR_TYPE,
        actor_id=ACTOR_ID,
    )
    assert evidence.entity_id is None  # unresolved, never silently defaulted

    fetched = api.get_evidence(evidence.evidence_id)
    assert fetched.entity_id is None

    resolved = api.evidence_repository.assign_entity(evidence.evidence_id, noustai.entity_id)
    assert resolved.entity_id == noustai.entity_id
    assert api.get_evidence(evidence.evidence_id).entity_id == noustai.entity_id

    # Reassignment is explicitly out of CD-2 scope (see
    # services/evidence/evidence.py's assign_entity docstring).
    with pytest.raises(ImmutabilityViolationError):
        api.evidence_repository.assign_entity(evidence.evidence_id, noustai.entity_id)


def test_duplicate_content_from_different_sources_remain_distinct_records(
    api, utc_now, synthetic_invoice_content_hash
):
    source_a = api.register_source(
        source_type="MAILBOX",
        provider="MICROSOFT_GRAPH",
        external_source_ref="synthetic-mailbox-a@example.test",
        status="ACTIVE",
        actor_type=ACTOR_TYPE,
        actor_id=ACTOR_ID,
    )
    source_b = api.register_source(
        source_type="MAILBOX",
        provider="GMAIL",
        external_source_ref="synthetic-mailbox-b@example.test",
        status="ACTIVE",
        actor_type=ACTOR_TYPE,
        actor_id=ACTOR_ID,
    )
    now = utc_now()

    evidence_a = api.register_evidence(
        entity_id=None,
        evidence_type="INVOICE",
        source_id=source_a.source_id,
        observed_at=now,
        received_at=now,
        content_hash=synthetic_invoice_content_hash,
        mime_type="text/plain",
        size_bytes=630,
        actor_type=ACTOR_TYPE,
        actor_id=ACTOR_ID,
        external_reference=("MICROSOFT_GRAPH", "EMAIL_ATTACHMENT", "synth-attachment-a-0001"),
    )
    evidence_b = api.register_evidence(
        entity_id=None,
        evidence_type="INVOICE",
        source_id=source_b.source_id,
        observed_at=now,
        received_at=now,
        content_hash=synthetic_invoice_content_hash,  # identical bytes/hash
        mime_type="text/plain",
        size_bytes=630,
        actor_type=ACTOR_TYPE,
        actor_id=ACTOR_ID,
        external_reference=("GMAIL", "EMAIL_ATTACHMENT", "synth-attachment-b-0001"),
    )

    assert evidence_a.evidence_id != evidence_b.evidence_id
    assert evidence_a.source_id != evidence_b.source_id
    assert evidence_a.content_hash == evidence_b.content_hash  # same content...
    assert {e.evidence_id for e in api.evidence_repository.list_evidence()} == {
        evidence_a.evidence_id,
        evidence_b.evidence_id,
    }  # ...but two distinct observations, never collapsed into one (PID §8/WI-2 fix)


def test_external_reference_link_is_idempotent_and_conflict_is_rejected(
    api, utc_now, synthetic_invoice_content_hash
):
    source = api.register_source(
        source_type="MAILBOX",
        provider="MICROSOFT_GRAPH",
        status="ACTIVE",
        actor_type=ACTOR_TYPE,
        actor_id=ACTOR_ID,
    )
    now = utc_now()
    evidence = api.register_evidence(
        entity_id=None,
        evidence_type="EMAIL_ATTACHMENT",
        source_id=source.source_id,
        observed_at=now,
        received_at=now,
        content_hash=synthetic_invoice_content_hash,
        mime_type="text/plain",
        size_bytes=630,
        actor_type=ACTOR_TYPE,
        actor_id=ACTOR_ID,
    )

    first = api.link_external_reference(
        provider="MICROSOFT_GRAPH",
        source_id=source.source_id,
        resource_type="EMAIL_MESSAGE",
        external_id="AAMkAG-synthetic-message-0001",
        canonical_object_type="EvidenceItem",
        canonical_object_id=evidence.evidence_id,
        actor_type=ACTOR_TYPE,
        actor_id=ACTOR_ID,
    )
    # Same tuple, same target -> idempotent replay: same record, no duplicate.
    replay = api.link_external_reference(
        provider="MICROSOFT_GRAPH",
        source_id=source.source_id,
        resource_type="EMAIL_MESSAGE",
        external_id="AAMkAG-synthetic-message-0001",
        canonical_object_type="EvidenceItem",
        canonical_object_id=evidence.evidence_id,
        actor_type=ACTOR_TYPE,
        actor_id=ACTOR_ID,
    )
    assert replay.external_reference_id == first.external_reference_id

    # The replay must not double-log EXTERNAL_REFERENCE_LINKED.
    linked_events = [
        e
        for e in api.audit_repository.list_by_subject("ExternalReference", first.external_reference_id)
        if e.event_type == "EXTERNAL_REFERENCE_LINKED"
    ]
    assert len(linked_events) == 1

    # Same tuple, a DIFFERENT canonical target -> genuine conflict.
    other_evidence = api.register_evidence(
        entity_id=None,
        evidence_type="EMAIL_ATTACHMENT",
        source_id=source.source_id,
        observed_at=now,
        received_at=now,
        content_hash="b" * 64,
        mime_type="text/plain",
        size_bytes=1,
        actor_type=ACTOR_TYPE,
        actor_id=ACTOR_ID,
    )
    with pytest.raises(DuplicateExternalReferenceError):
        api.link_external_reference(
            provider="MICROSOFT_GRAPH",
            source_id=source.source_id,
            resource_type="EMAIL_MESSAGE",
            external_id="AAMkAG-synthetic-message-0001",  # same tuple as above
            canonical_object_type="EvidenceItem",
            canonical_object_id=other_evidence.evidence_id,  # different target
            actor_type=ACTOR_TYPE,
            actor_id=ACTOR_ID,
        )


def test_evidence_provenance_traces_full_lineage_with_a_derived_relationship(
    api, utc_now, synthetic_invoice_content_hash
):
    source = api.register_source(
        source_type="MANUAL_UPLOAD",
        provider="INTERNAL",
        status="ACTIVE",
        actor_type=ACTOR_TYPE,
        actor_id=ACTOR_ID,
    )
    now = utc_now()
    evidence = api.register_evidence(
        entity_id=None,
        evidence_type="INVOICE",
        source_id=source.source_id,
        observed_at=now,
        received_at=now,
        content_hash=synthetic_invoice_content_hash,
        mime_type="text/plain",
        size_bytes=630,
        actor_type=ACTOR_TYPE,
        actor_id=ACTOR_ID,
    )

    # A hypothetical derived fact (e.g. a future Classification record) —
    # CD-2 does not implement classification itself (out of scope), but
    # the Provenance primitive must support tracing ANY canonical subject
    # back to its evidence, via a non-OBSERVED_FROM relationship.
    derived_subject_id = identity.generate_id()
    provenance = api.record_provenance(
        subject_type="Classification",
        subject_id=derived_subject_id,
        evidence_id=evidence.evidence_id,
        relationship="EXTRACTED_FROM",
        actor_type=ACTOR_TYPE,
        actor_id=ACTOR_ID,
    )
    assert provenance.relationship == "EXTRACTED_FROM"

    lineage = api.trace_provenance(subject_type="Classification", subject_id=derived_subject_id)
    assert len(lineage) == 1
    edge = lineage[0]
    assert edge["provenance"].provenance_id == provenance.provenance_id
    assert edge["provenance"].relationship == "EXTRACTED_FROM"
    assert edge["evidence"].evidence_id == evidence.evidence_id
    assert edge["source"].source_id == source.source_id


def test_orphan_provenance_is_rejected_at_the_domain_layer(api):
    """The domain-level proof `tests/contract/test_provenance_contract.py`
    explicitly defers to: a raw schema cannot know which `evidence_id`s
    exist, but `core/provenance.py`'s `InMemoryProvenanceRepository`
    (constructed with the live `EvidenceRepository`) can and does."""
    nonexistent_evidence_id = identity.generate_id()  # well-formed, never registered
    with pytest.raises(InvalidProvenanceError):
        api.record_provenance(
            subject_type="Classification",
            subject_id=identity.generate_id(),
            evidence_id=nonexistent_evidence_id,
            relationship="OBSERVED_FROM",
            actor_type=ACTOR_TYPE,
            actor_id=ACTOR_ID,
        )


def test_audit_causality_chain_within_one_correlation(
    api, utc_now, synthetic_invoice_content_hash
):
    source = api.register_source(
        source_type="MANUAL_UPLOAD",
        provider="INTERNAL",
        status="ACTIVE",
        actor_type=ACTOR_TYPE,
        actor_id=ACTOR_ID,
    )
    now = utc_now()
    correlation_id = identity.generate_id()

    evidence = api.register_evidence(
        entity_id=None,
        evidence_type="INVOICE",
        source_id=source.source_id,
        observed_at=now,
        received_at=now,
        content_hash=synthetic_invoice_content_hash,
        mime_type="text/plain",
        size_bytes=630,
        actor_type=ACTOR_TYPE,
        actor_id=ACTOR_ID,
        correlation_id=correlation_id,
    )

    chain_so_far = api.audit_repository.list_by_correlation(correlation_id)
    assert len(chain_so_far) == 1
    first_event = chain_so_far[0]
    assert first_event.event_type == "EVIDENCE_OBSERVED"
    assert first_event.causation_id is None  # first in chain (PID §15)

    follow_on = api.record_audit_event(
        event_type="CLASSIFICATION_PROPOSED",  # PID §13's illustrative future event type; open string
        actor_type=ACTOR_TYPE,
        actor_id=ACTOR_ID,
        subject_type="EvidenceItem",
        subject_id=evidence.evidence_id,
        correlation_id=correlation_id,
        causation_id=first_event.audit_event_id,
        payload={"note": "synthetic follow-on event for the causal-chain proof"},
    )

    chain = api.audit_repository.list_by_correlation(correlation_id)
    assert [e.audit_event_id for e in chain] == [first_event.audit_event_id, follow_on.audit_event_id]
    assert chain[0].causation_id is None
    assert chain[1].causation_id == first_event.audit_event_id
    assert chain[1].correlation_id == chain[0].correlation_id == correlation_id


def test_malformed_domain_call_is_rejected_with_canonical_validation_error(api):
    """Proves `core/contract_validation.py` is wired into the LIVE call
    path (`SourceRepository.register_source`), not just exercised by
    the raw-schema contract tests: a domain-layer call with a
    structurally invalid field is rejected with the canonical
    `ValidationError`, never a raw `jsonschema` exception."""
    with pytest.raises(ValidationError):
        api.register_source(
            source_type="mailbox",  # violates ^[A-Z][A-Z_]*$
            provider="INTERNAL",
            status="ACTIVE",
            actor_type=ACTOR_TYPE,
            actor_id=ACTOR_ID,
        )
