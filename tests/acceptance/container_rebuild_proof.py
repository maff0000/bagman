#!/usr/bin/env python3
"""PID §44 REQUIRED CONTAINER REBUILD PROOF (CD-3 WI-4).

Distinct from restart_proof.py's full-stack restart: this proves data
survives *application container replacement specifically* — i.e. that
persistence lives in bagman-db/bagman-objects' durable volumes, not in
bagman-api's own container filesystem layer.

Against the REAL running stack (brought up if not already):

  1. Ensure the stack is up (build + `docker compose up -d --wait`).
  2. Register fresh synthetic entity/source/evidence via the live API.
  3. Confirm it is reachable BEFORE any container surgery (sanity).
  4. Remove ONLY the bagman-api container (`docker compose rm -sf
     bagman-api` — NOT bagman-db/bagman-objects, and no `-v`).
     Confirm bagman-db/bagman-objects' own container IDs are
     unchanged (they were never touched).
  5. Recreate bagman-api (`docker compose up -d bagman-api --wait`).
  6. Confirm the SAME data (metadata + byte-identical content) is
     still reachable via the live API.

Run directly:
    python3 tests/acceptance/container_rebuild_proof.py
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import _lib  # noqa: E402


def main() -> int:
    rid = _lib.run_id()
    actor_id = f"bagman-wi4-container-rebuild-proof-{rid}"

    _lib.section(f"BAGMAN CD-3 WI-4 — PID §44 REQUIRED CONTAINER REBUILD PROOF (run {rid})")

    # -----------------------------------------------------------------
    # [1] ensure the stack is up
    # -----------------------------------------------------------------
    print(f"\n[1] ENSURE STACK IS UP")
    _lib.compose_build()
    _lib.compose_up_wait()
    ready_body = _lib.wait_for_ready()
    print(f"    /ready -> {ready_body}")
    assert ready_body["ready"] is True

    db_container_id_before = _lib.compose_ps_service_id("bagman-db")
    objects_container_id_before = _lib.compose_ps_service_id("bagman-objects")
    print(f"    bagman-db container id (before)      = {db_container_id_before}")
    print(f"    bagman-objects container id (before)  = {objects_container_id_before}")
    assert db_container_id_before and objects_container_id_before

    # -----------------------------------------------------------------
    # [2] register fresh synthetic entity/source/evidence
    # -----------------------------------------------------------------
    print(f"\n[2] REGISTER FRESH SYNTHETIC ENTITY / SOURCE / EVIDENCE (via live HTTP API)")
    entity = _lib.register_entity(canonical_name=f"WI4_REBUILD_{rid}", actor_id=actor_id)
    source = _lib.register_source(
        external_source_ref=f"wi4-rebuild-proof-{rid}",
        governed_entity_hint=entity["canonical_name"],
        actor_id=actor_id,
    )
    content = f"BAGMAN WI-4 container-rebuild-proof synthetic evidence bytes (run {rid})\n".encode("utf-8")
    # CD-4 WI-3: via the governed intake endpoint, not the removed CD-3
    # direct route — see tests/acceptance/_lib.py's register_evidence()
    # docstring. Manual upload always resolves to BAGMAN's own stable
    # MANUAL_UPLOAD source and always registers entity_id=None;
    # entity_hint/idempotency_key replace the old entity_id/source_id/
    # external_reference parameters.
    evidence = _lib.register_evidence(
        entity_hint=entity["canonical_name"],
        content=content,
        original_name=f"wi4-rebuild-proof-{rid}.txt",
        actor_id=actor_id,
        idempotency_key=f"wi4-rebuild-proof-{rid}-file",
    )
    evidence_id = evidence["evidence_id"]
    print(f"    entity_id (unresolved, CD-4) = {evidence['entity_id']}")
    print(f"    source_id (MANUAL_UPLOAD)    = {evidence['source_id']}")
    print(f"    evidence_id = {evidence_id}")
    print(f"    content_hash = {evidence['content_hash']}")

    # -----------------------------------------------------------------
    # [3] confirm reachable BEFORE container surgery
    # -----------------------------------------------------------------
    print(f"\n[3] CONFIRM REACHABLE BEFORE bagman-api CONTAINER SURGERY")
    before = _lib.get_evidence(evidence_id)
    content_before = _lib.get_evidence_content(evidence_id).content
    assert before["evidence_id"] == evidence_id
    assert content_before == content
    print(f"    evidence + content confirmed reachable (sanity check passed).")

    # -----------------------------------------------------------------
    # [4] remove ONLY the bagman-api container
    # -----------------------------------------------------------------
    print(f"\n[4] REMOVE ONLY bagman-api CONTAINER (docker compose rm -sf bagman-api)")
    _lib.compose_rm_service("bagman-api")

    db_container_id_mid = _lib.compose_ps_service_id("bagman-db")
    objects_container_id_mid = _lib.compose_ps_service_id("bagman-objects")
    api_container_id_mid = _lib.compose_ps_service_id("bagman-api")
    print(f"    bagman-db container id (after rm bagman-api)      = {db_container_id_mid}")
    print(f"    bagman-objects container id (after rm bagman-api)  = {objects_container_id_mid}")
    print(f"    bagman-api container id (after rm)                 = {api_container_id_mid!r} (must be empty)")
    assert db_container_id_mid == db_container_id_before, "bagman-db container was unexpectedly touched"
    assert objects_container_id_mid == objects_container_id_before, "bagman-objects container was unexpectedly touched"
    assert api_container_id_mid == "", "bagman-api container should have been removed"
    print(f"    CONFIRMED: bagman-db/bagman-objects untouched (same container IDs); bagman-api removed.")

    # -----------------------------------------------------------------
    # [5] recreate bagman-api
    # -----------------------------------------------------------------
    print(f"\n[5] RECREATE bagman-api (docker compose up -d --wait bagman-api)")
    _lib.compose_up_service_wait("bagman-api")
    ready_body_after = _lib.wait_for_ready()
    print(f"    /ready -> {ready_body_after}")
    assert ready_body_after["ready"] is True

    api_container_id_after = _lib.compose_ps_service_id("bagman-api")
    print(f"    bagman-api container id (after recreate) = {api_container_id_after}")
    assert api_container_id_after and api_container_id_after != api_container_id_mid

    # -----------------------------------------------------------------
    # [6] confirm the SAME data is still reachable
    # -----------------------------------------------------------------
    print(f"\n[6] CONFIRM SAME DATA STILL REACHABLE AFTER bagman-api CONTAINER REBUILD")
    after = _lib.get_evidence(evidence_id)
    print(f"    evidence (after rebuild) = {after}")
    for field in (
        "evidence_id", "entity_id", "evidence_type", "source_id", "content_hash",
        "mime_type", "size_bytes", "status", "storage_reference", "original_name",
    ):
        assert after[field] == before[field], (
            f"field '{field}' changed across bagman-api rebuild: "
            f"before={before[field]!r} after={after[field]!r}"
        )

    content_after = _lib.get_evidence_content(evidence_id).content
    assert content_after == content, "evidence bytes changed across bagman-api rebuild"
    print(f"    metadata identical; content bytes byte-identical ({len(content_after)} bytes).")

    _lib.section("PID §44 CONTAINER REBUILD PROOF: ALL STEPS COMPLETED AND VERIFIED")
    print(f"    evidence_id = {evidence_id}")
    print(f"    Persistence proven to live in bagman-db/bagman-objects volumes, "
          f"NOT in bagman-api's own container filesystem layer.")
    print(f"    (stack left running — see restore_into_clean_target_proof.py)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
