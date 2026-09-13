#!/usr/bin/env python3
"""PID §43 REQUIRED RUNTIME RESTART PROOF (CD-3 WI-4).

Against the REAL `docker compose -p bagman` runtime and the REAL live
`bagman-api` HTTP surface (no mocks, real secrets at
/srv/bagman-secrets/):

  1. `docker compose up -d` (build first if needed).
  2. Register a synthetic entity, source, and evidence (real small
     synthetic bytes) through the live HTTP API. Record every
     canonical ID, the content hash, and the storage_reference.
  3. Record provenance and one more audit event (via a direct
     in-process core.api.BagmanCanonicalAPI call inside the running
     bagman-api container — PID §24's HTTP surface has no
     record_provenance/record_audit_event route; see tests/acceptance/
     _lib.py's own docstring).
  4. `docker compose down` (WITHOUT -v) — volumes survive.
  5. `docker compose up -d` again. Wait for healthy/ready.
  6. Retrieve the SAME canonical IDs via the live API again. Assert
     every field is identical, the content hash is identical, GET
     .../content returns byte-identical bytes, provenance/audit trail
     is intact (same events, same correlation/causation).
  7. Repeat the exact same external-observation call through the live
     API — assert it returns the SAME evidence_id as before the
     restart (PID §23/§43's idempotency-survives-restart guarantee).

Run directly:
    python3 tests/acceptance/restart_proof.py
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import _lib  # noqa: E402


def main() -> int:
    rid = _lib.run_id()
    actor_id = f"bagman-wi4-restart-proof-{rid}"

    _lib.section(f"BAGMAN CD-3 WI-4 — PID §43 REQUIRED RUNTIME RESTART PROOF (run {rid})")

    # -----------------------------------------------------------------
    # [1] docker compose up -d (build first if needed)
    # -----------------------------------------------------------------
    print(f"\n[1] BUILD + START bagman-db / bagman-objects / bagman-api")
    _lib.compose_build()
    _lib.compose_up_wait()
    ready_body = _lib.wait_for_ready()
    print(f"    /ready -> {ready_body}")
    assert ready_body["ready"] is True
    assert ready_body["runtime_environment"] == "production"

    # -----------------------------------------------------------------
    # [2] register synthetic entity / source / evidence via the live API
    # -----------------------------------------------------------------
    print(f"\n[2] REGISTER SYNTHETIC ENTITY / SOURCE / EVIDENCE (via live HTTP API)")

    entity = _lib.register_entity(canonical_name=f"WI4_RESTART_{rid}", actor_id=actor_id)
    print(f"    entity_id      = {entity['entity_id']}")
    print(f"    canonical_name = {entity['canonical_name']}")
    assert entity["canonical_name"] == f"WI4_RESTART_{rid}"

    source = _lib.register_source(
        external_source_ref=f"wi4-restart-proof-{rid}",
        governed_entity_hint=entity["canonical_name"],
        actor_id=actor_id,
    )
    print(f"    source_id      = {source['source_id']}")

    content = f"BAGMAN WI-4 restart-proof synthetic evidence bytes (run {rid})\n".encode("utf-8")
    # CD-4 WI-3: evidence is created via the governed intake endpoint
    # (POST /internal/intake/evidence), not the removed CD-3 direct
    # route. It never accepts a caller-chosen source_id (manual upload
    # always resolves to BAGMAN's own stable MANUAL_UPLOAD source, PID
    # §9) or a real entity_id (CD-4 always registers entity_id=None —
    # see app/api/routers/intake.py's own module docstring); an
    # idempotency_key is CD-4's retry mechanism, replacing CD-3's
    # external_reference tuple.
    idempotency_key = f"wi4-restart-proof-{rid}-file"
    evidence = _lib.register_evidence(
        entity_hint=entity["canonical_name"],
        content=content,
        original_name=f"wi4-restart-proof-{rid}.txt",
        actor_id=actor_id,
        idempotency_key=idempotency_key,
    )
    evidence_id = evidence["evidence_id"]
    content_hash = evidence["content_hash"]
    storage_reference = evidence["storage_reference"]
    print(f"    evidence_id       = {evidence_id}")
    print(f"    content_hash      = {content_hash}")
    print(f"    storage_reference = {storage_reference}")
    print(f"    resolved source_id (stable MANUAL_UPLOAD source) = {evidence['source_id']}")
    # CD-4 entity-resolution decision (recorded, not relitigated here):
    # intake always registers entity_id=None; the entity SELECTION is
    # carried as entity_hint on the IntakeRecord instead.
    assert evidence["entity_id"] is None
    assert evidence["_intake"]["entity_hint"] == entity["canonical_name"]
    assert content_hash["value"] == __import__("hashlib").sha256(content).hexdigest()

    content_response = _lib.get_evidence_content(evidence_id)
    print(f"    GET .../content -> {len(content_response.content)} bytes, "
          f"X-Bagman-Content-Hash-Value={content_response.headers.get('X-Bagman-Content-Hash-Value')}")
    assert content_response.content == content

    # -----------------------------------------------------------------
    # [3] record provenance + one more audit event
    # -----------------------------------------------------------------
    print(f"\n[3] RECORD PROVENANCE + FOLLOW-ON AUDIT EVENT (in-container BagmanCanonicalAPI call)")
    classification_subject_id = _lib.generate_canonical_id()
    prov_result = _lib.record_provenance_and_followon_audit(
        evidence_id=evidence_id,
        classification_subject_id=classification_subject_id,
        actor_id=actor_id,
    )
    print(f"    provenance_id       = {prov_result['provenance_id']}")
    print(f"    correlation_id      = {prov_result['correlation_id']}")
    print(f"    observed_event_id   = {prov_result['observed_event_id']}")
    print(f"    followon_event_id   = {prov_result['followon_event_id']}")

    lineage_before = _lib.get_provenance("Classification", classification_subject_id)
    print(f"    GET /internal/provenance/Classification/{classification_subject_id} -> {len(lineage_before)} edge(s)")
    assert len(lineage_before) == 1
    assert lineage_before[0]["provenance"]["provenance_id"] == prov_result["provenance_id"]
    assert lineage_before[0]["evidence"]["evidence_id"] == evidence_id
    # CD-4: evidence resolves to BAGMAN's stable MANUAL_UPLOAD source
    # (never the arbitrary `source` registered above), so this checks
    # internal consistency (trace_provenance resolves the SAME source
    # the EvidenceItem itself points to) rather than equality with a
    # caller-created Source.
    assert lineage_before[0]["source"]["source_id"] == evidence["source_id"]

    audit_before = _lib.audit_trail_snapshot(
        evidence_id=evidence_id, classification_subject_id=classification_subject_id
    )
    print(f"    audit trail before restart: {audit_before}")
    # CD-4 intake emits both EVIDENCE_OBSERVED (CD-2) and its own
    # EVIDENCE_REGISTERED (PID §30/§31 causal-chain closure back to the
    # IntakeRecord) against the EvidenceItem subject — see
    # app/api/routers/intake.py's own module docstring for why both are
    # kept.
    assert len(audit_before["evidence_events"]) == 2
    assert len(audit_before["classification_events"]) == 2  # PROVENANCE_RECORDED + CLASSIFICATION_PROPOSED

    # -----------------------------------------------------------------
    # [4] docker compose down (WITHOUT -v)
    # -----------------------------------------------------------------
    print(f"\n[4] DOCKER COMPOSE DOWN (no -v — volumes must survive)")
    _lib.compose_down(volumes=False)

    # -----------------------------------------------------------------
    # [5] docker compose up -d again
    # -----------------------------------------------------------------
    print(f"\n[5] DOCKER COMPOSE UP -D (restart)")
    _lib.compose_up_wait()
    ready_body_after = _lib.wait_for_ready()
    print(f"    /ready -> {ready_body_after}")
    assert ready_body_after["ready"] is True

    # -----------------------------------------------------------------
    # [6] retrieve the SAME canonical IDs, assert everything identical
    # -----------------------------------------------------------------
    print(f"\n[6] RETRIEVE SAME EVIDENCE VIA LIVE API AFTER RESTART")
    evidence_after = _lib.get_evidence(evidence_id)
    print(f"    evidence (after restart) = {evidence_after}")
    for field in (
        "evidence_id", "entity_id", "evidence_type", "source_id", "content_hash",
        "mime_type", "size_bytes", "status", "storage_reference", "original_name",
    ):
        assert evidence_after[field] == evidence[field], (
            f"field '{field}' changed across restart: before={evidence[field]!r} "
            f"after={evidence_after[field]!r}"
        )
    print(f"    ALL metadata fields identical across restart.")

    content_after = _lib.get_evidence_content(evidence_id)
    print(f"    GET .../content (after restart) -> {len(content_after.content)} bytes, "
          f"hash-header={content_after.headers.get('X-Bagman-Content-Hash-Value')}")
    assert content_after.content == content, "evidence bytes changed across restart"
    assert content_after.headers["X-Bagman-Content-Hash-Value"] == content_hash["value"]
    print(f"    content bytes byte-identical across restart; SHA-256 identical.")

    lineage_after = _lib.get_provenance("Classification", classification_subject_id)
    print(f"    provenance/lineage (after restart) = {lineage_after}")
    assert lineage_after == lineage_before, "provenance/lineage changed across restart"
    print(f"    provenance/lineage identical across restart.")

    audit_after = _lib.audit_trail_snapshot(
        evidence_id=evidence_id, classification_subject_id=classification_subject_id
    )
    print(f"    audit trail (after restart) = {audit_after}")
    assert audit_after == audit_before, "audit trail changed across restart"
    print(f"    audit trail (events, correlation_id, causation_id) identical across restart.")

    # -----------------------------------------------------------------
    # [7] repeat the exact same external observation -> same evidence_id
    # -----------------------------------------------------------------
    print(f"\n[7] REPEAT THE EXACT SAME INTAKE REQUEST (idempotency survives restart)")
    retry_evidence = _lib.register_evidence(
        entity_hint=entity["canonical_name"],
        content=content,
        original_name=f"wi4-restart-proof-{rid}.txt",
        actor_id=actor_id,
        idempotency_key=idempotency_key,
    )
    print(f"    original evidence_id = {evidence_id}")
    print(f"    retried  evidence_id = {retry_evidence['evidence_id']}")
    assert retry_evidence["evidence_id"] == evidence_id, (
        "a bare retry of the same Idempotency-Key after a full restart "
        "must resolve to the SAME EvidenceItem, not create a duplicate"
    )
    print(f"    SAME evidence_id returned — idempotency survived the restart.")

    audit_after_retry = _lib.audit_trail_snapshot(
        evidence_id=evidence_id, classification_subject_id=classification_subject_id
    )
    observed_count = sum(
        1 for e in audit_after_retry["evidence_events"] if e["event_type"] == "EVIDENCE_OBSERVED"
    )
    print(f"    EVIDENCE_OBSERVED audit events after retry: {observed_count} (must still be exactly 1)")
    assert observed_count == 1, "the idempotent replay must not append a second EVIDENCE_OBSERVED audit event"

    _lib.section("PID §43 RUNTIME RESTART PROOF: ALL STEPS COMPLETED AND VERIFIED")
    print(f"    evidence_id            = {evidence_id}")
    print(f"    entity_id (unresolved, CD-4) = {evidence['entity_id']}")
    print(f"    source_id (MANUAL_UPLOAD)    = {evidence['source_id']}")
    print(f"    provenance_id          = {prov_result['provenance_id']}")
    print(f"    content_hash           = {content_hash['value']}")
    print(f"    (stack left running — see container_rebuild_proof.py / restore_into_clean_target_proof.py)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
