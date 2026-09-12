"""PID §43 REQUIRED RUNTIME PROOF.

"Source inspection alone is insufficient. FORGE must provide executable
proof demonstrating at minimum" the ten numbered steps below — through
`core.api.BagmanCanonicalAPI`, the same facade a real caller would use.
This file is written as ONE ordered, coherent story: each `print()`
line is numbered to match PID §43's own step numbering, and prints the
concrete result (IDs, counts, verdicts) of that step, so the captured
`pytest -s` output can be cited directly as delivery evidence — "the
output must be deterministic and retained in delivery evidence" (PID
§43). Run as:

    pytest -s tests/integration/test_runtime_proof.py -v

Deterministic here means: the STRUCTURE and the OUTCOME of every step
are the same on every run (same counts, same relationships, same
rejections) — not that the literal UUIDv7/timestamp values are
byte-identical across runs (those are, correctly, time- and
randomness-derived; PID §43 does not ask for the underlying canonical
identifiers themselves to be fixed).
"""
from __future__ import annotations

import pytest

from core import identity
from core.errors import DuplicateExternalReferenceError, ValidationError

ACTOR_TYPE = "SYSTEM"
ACTOR_ID = "bagman-runtime-proof-harness"


def test_pid_43_required_runtime_proof(api, utc_now, synthetic_invoice_path, synthetic_invoice_content_hash):
    print("\n" + "=" * 78)
    print("BAGMAN CD-2 — PID §43 REQUIRED RUNTIME PROOF")
    print("=" * 78)

    # -----------------------------------------------------------------
    # 1. create synthetic governed entity
    # -----------------------------------------------------------------
    entity = api.register_entity(
        entity_type="COMPANY",
        canonical_name="NOUSTAI_LIMITED",
        display_name="Noust AI Limited",
        status="ACTIVE",
        actor_type=ACTOR_TYPE,
        actor_id=ACTOR_ID,
    )
    print(f"\n[1] CREATE SYNTHETIC GOVERNED ENTITY")
    print(f"    entity_id      = {entity.entity_id}")
    print(f"    entity_type    = {entity.entity_type}")
    print(f"    canonical_name = {entity.canonical_name}")
    print(f"    status         = {entity.status}")
    assert entity.entity_id
    assert entity.canonical_name == "NOUSTAI_LIMITED"

    # -----------------------------------------------------------------
    # 2. register synthetic source
    # -----------------------------------------------------------------
    source = api.register_source(
        source_type="MANUAL_UPLOAD",
        provider="INTERNAL",
        external_source_ref="synthetic-runtime-proof-upload",
        governed_entity_hint=entity.canonical_name,
        status="ACTIVE",
        actor_type=ACTOR_TYPE,
        actor_id=ACTOR_ID,
    )
    print(f"\n[2] REGISTER SYNTHETIC SOURCE")
    print(f"    source_id           = {source.source_id}")
    print(f"    source_type         = {source.source_type}")
    print(f"    provider            = {source.provider}")
    print(f"    governed_entity_hint = {source.governed_entity_hint} (a HINT only, PID §9 — not an ownership assertion)")
    assert source.source_id

    # -----------------------------------------------------------------
    # 3. register synthetic evidence
    # 4. hash evidence (shown on the returned object's content_hash)
    # -----------------------------------------------------------------
    now = utc_now()
    evidence = api.register_evidence(
        entity_id=None,  # deliberately unresolved at observation time — resolved explicitly below
        evidence_type="INVOICE",
        source_id=source.source_id,
        observed_at=now,
        received_at=now,
        content_hash=synthetic_invoice_content_hash,
        mime_type="text/plain",
        original_name=synthetic_invoice_path.name,
        size_bytes=synthetic_invoice_path.stat().st_size,
        actor_type=ACTOR_TYPE,
        actor_id=ACTOR_ID,
    )
    print(f"\n[3] REGISTER SYNTHETIC EVIDENCE (fixture: {synthetic_invoice_path.name})")
    print(f"    evidence_id  = {evidence.evidence_id}")
    print(f"    entity_id    = {evidence.entity_id!r} (explicitly UNRESOLVED, not silently defaulted)")
    print(f"    evidence_type = {evidence.evidence_type}")
    print(f"    source_id    = {evidence.source_id}")
    print(f"    status       = {evidence.status}")
    print(f"\n[4] CONTENT HASH (produced as part of step 3's registration)")
    print(f"    algorithm = {evidence.content_hash['algorithm']}")
    print(f"    value     = {evidence.content_hash['value']}")
    assert evidence.entity_id is None
    assert evidence.content_hash["value"] == synthetic_invoice_content_hash
    assert len(evidence.content_hash["value"]) == 64

    # Resolve entity ownership explicitly (a separate, deliberate act —
    # PID §4's entity isolation invariant: never a silent inference).
    resolved_evidence = api.evidence_repository.assign_entity(evidence.evidence_id, entity.entity_id)
    print(f"    -> entity ownership explicitly resolved: entity_id = {resolved_evidence.entity_id}")
    assert resolved_evidence.entity_id == entity.entity_id

    # -----------------------------------------------------------------
    # 5. associate external reference
    # -----------------------------------------------------------------
    external_reference = api.link_external_reference(
        provider="MICROSOFT_GRAPH",
        source_id=source.source_id,
        resource_type="EMAIL_ATTACHMENT",
        external_id="AAMkAG-synthetic-runtime-proof-attachment-0001",
        canonical_object_type="EvidenceItem",
        canonical_object_id=evidence.evidence_id,
        actor_type=ACTOR_TYPE,
        actor_id=ACTOR_ID,
    )
    print(f"\n[5] ASSOCIATE EXTERNAL REFERENCE")
    print(f"    external_reference_id = {external_reference.external_reference_id}")
    print(f"    provider/resource_type/external_id = "
          f"{external_reference.provider}/{external_reference.resource_type}/{external_reference.external_id}")
    print(f"    canonical_object_type/id = {external_reference.canonical_object_type}/{external_reference.canonical_object_id}")
    assert external_reference.canonical_object_id == evidence.evidence_id

    # -----------------------------------------------------------------
    # 6. create derived provenance relationship
    # -----------------------------------------------------------------
    classification_subject_id = identity.generate_id()
    provenance = api.record_provenance(
        subject_type="Classification",
        subject_id=classification_subject_id,
        evidence_id=evidence.evidence_id,
        relationship="EXTRACTED_FROM",
        actor_type=ACTOR_TYPE,
        actor_id=ACTOR_ID,
        metadata={"note": "synthetic derived classification for the runtime proof"},
    )
    print(f"\n[6] CREATE DERIVED PROVENANCE RELATIONSHIP")
    print(f"    provenance_id = {provenance.provenance_id}")
    print(f"    subject_type/subject_id = {provenance.subject_type}/{provenance.subject_id}")
    print(f"    relationship  = {provenance.relationship} (non-OBSERVED_FROM derived relationship)")
    print(f"    evidence_id   = {provenance.evidence_id}")
    assert provenance.relationship == "EXTRACTED_FROM"
    assert provenance.evidence_id == evidence.evidence_id

    # -----------------------------------------------------------------
    # 7. record audit events (a short causal chain within one correlation)
    # -----------------------------------------------------------------
    audit_events_for_evidence = api.audit_repository.list_by_subject("EvidenceItem", evidence.evidence_id)
    observed_event = next(e for e in audit_events_for_evidence if e.event_type == "EVIDENCE_OBSERVED")
    print(f"\n[7] RECORD AUDIT EVENTS")
    print(f"    EVIDENCE_OBSERVED audit_event_id = {observed_event.audit_event_id}")
    print(f"    correlation_id = {observed_event.correlation_id}, causation_id = {observed_event.causation_id!r} (first in chain)")

    followon_event = api.record_audit_event(
        event_type="CLASSIFICATION_PROPOSED",
        actor_type=ACTOR_TYPE,
        actor_id=ACTOR_ID,
        subject_type="Classification",
        subject_id=classification_subject_id,
        correlation_id=observed_event.correlation_id,
        causation_id=observed_event.audit_event_id,
        payload={"evidence_id": evidence.evidence_id, "provenance_id": provenance.provenance_id},
    )
    print(f"    CLASSIFICATION_PROPOSED audit_event_id = {followon_event.audit_event_id}")
    print(f"    correlation_id = {followon_event.correlation_id}, causation_id = {followon_event.causation_id} (caused by the event above)")

    causal_chain = api.audit_repository.list_by_correlation(observed_event.correlation_id)
    print(f"    full causal chain for correlation_id {observed_event.correlation_id}: "
          f"{[e.event_type for e in causal_chain]}")
    assert [e.audit_event_id for e in causal_chain] == [
        observed_event.audit_event_id,
        followon_event.audit_event_id,
    ]
    assert causal_chain[0].causation_id is None
    assert causal_chain[1].causation_id == observed_event.audit_event_id

    # -----------------------------------------------------------------
    # 8. trace evidence lineage end-to-end
    # -----------------------------------------------------------------
    lineage = api.trace_provenance(subject_type="Classification", subject_id=classification_subject_id)
    print(f"\n[8] TRACE EVIDENCE LINEAGE END-TO-END")
    print(f"    lineage edges for Classification {classification_subject_id}: {len(lineage)}")
    for i, edge in enumerate(lineage, start=1):
        print(
            f"    edge {i}: {edge['provenance'].subject_type}({edge['provenance'].subject_id}) "
            f"--[{edge['provenance'].relationship}]--> "
            f"EvidenceItem({edge['evidence'].evidence_id}) "
            f"--observed via--> Source({edge['source'].source_id}, provider={edge['source'].provider})"
        )
    assert len(lineage) == 1
    assert lineage[0]["evidence"].evidence_id == evidence.evidence_id
    assert lineage[0]["source"].source_id == source.source_id

    # -----------------------------------------------------------------
    # 9. reject malformed/invalid cases
    # -----------------------------------------------------------------
    print(f"\n[9] REJECT MALFORMED/INVALID CASES")

    with pytest.raises(ValidationError) as validation_excinfo:
        api.register_source(
            source_type="mailbox",  # violates ^[A-Z][A-Z_]*$ -> ValidationError at the domain layer
            provider="INTERNAL",
            status="ACTIVE",
            actor_type=ACTOR_TYPE,
            actor_id=ACTOR_ID,
        )
    print(f"    (a) malformed register_source(source_type='mailbox') -> "
          f"{type(validation_excinfo.value).__name__}: {validation_excinfo.value.message}")
    assert validation_excinfo.value.error_code == "VALIDATION_ERROR"

    conflicting_evidence = api.register_evidence(
        entity_id=None,
        evidence_type="INVOICE",
        source_id=source.source_id,
        observed_at=now,
        received_at=now,
        content_hash="d" * 64,
        mime_type="text/plain",
        size_bytes=1,
        actor_type=ACTOR_TYPE,
        actor_id=ACTOR_ID,
    )
    with pytest.raises(DuplicateExternalReferenceError) as duplicate_excinfo:
        api.link_external_reference(
            provider="MICROSOFT_GRAPH",
            source_id=source.source_id,
            resource_type="EMAIL_ATTACHMENT",
            external_id="AAMkAG-synthetic-runtime-proof-attachment-0001",  # same tuple as step 5
            canonical_object_type="EvidenceItem",
            canonical_object_id=conflicting_evidence.evidence_id,  # different target -> genuine conflict
            actor_type=ACTOR_TYPE,
            actor_id=ACTOR_ID,
        )
    print(f"    (b) conflicting link_external_reference (same tuple, different target) -> "
          f"{type(duplicate_excinfo.value).__name__}: {duplicate_excinfo.value.message}")
    assert duplicate_excinfo.value.error_code == "DUPLICATE_EXTERNAL_REFERENCE"

    # -----------------------------------------------------------------
    # 10. repeat an idempotent observation safely (the exact bare-retry
    #     scenario WI-2's repair fixed) — via the public API, not an
    #     internal repository call.
    # -----------------------------------------------------------------
    retry_evidence = api.register_evidence(
        entity_id=None,
        evidence_type="INVOICE",
        source_id=source.source_id,
        observed_at=now,
        received_at=now,
        content_hash=synthetic_invoice_content_hash,
        mime_type="text/plain",
        original_name=synthetic_invoice_path.name,
        size_bytes=synthetic_invoice_path.stat().st_size,
        actor_type=ACTOR_TYPE,
        actor_id=ACTOR_ID,
        external_reference=("MICROSOFT_GRAPH", "EMAIL_ATTACHMENT", "AAMkAG-synthetic-runtime-proof-attachment-0001"),
    )
    print(f"\n[10] REPEAT AN IDEMPOTENT OBSERVATION SAFELY (bare retry, same external_reference tuple)")
    print(f"    original evidence_id = {evidence.evidence_id}")
    print(f"    retried  evidence_id = {retry_evidence.evidence_id}")
    print(f"    same record? {retry_evidence.evidence_id == evidence.evidence_id}")

    events_after_retry = api.audit_repository.list_by_subject("EvidenceItem", evidence.evidence_id)
    observed_events_after_retry = [e for e in events_after_retry if e.event_type == "EVIDENCE_OBSERVED"]
    print(f"    EVIDENCE_OBSERVED audit events for this evidence_id after retry: "
          f"{len(observed_events_after_retry)} (must still be exactly 1 -- no duplicate audit log entry)")

    assert retry_evidence.evidence_id == evidence.evidence_id, (
        "a bare retry with the same external_reference tuple must resolve to the "
        "SAME EvidenceItem, not create a duplicate"
    )
    assert len(observed_events_after_retry) == 1, (
        "the idempotent replay must not append a second EVIDENCE_OBSERVED audit event"
    )

    print("\n" + "=" * 78)
    print("PID §43 RUNTIME PROOF: ALL 10 STEPS COMPLETED AND VERIFIED")
    print("=" * 78 + "\n")
