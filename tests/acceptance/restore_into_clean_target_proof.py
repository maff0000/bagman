#!/usr/bin/env python3
"""PID §35 / §60 STEPS 17-20 REQUIRED RESTORE-INTO-CLEAN-TARGET PROOF
(CD-3 WI-4) — the fullest proof in this work item.

Against the REAL running stack (brought up if not already), using the
REAL `ops/backup_postgres.sh` / `ops/backup_objects.py` /
`ops/restore_postgres.sh` / `ops/restore_objects.py` tooling (not a
reimplementation of it):

  1. With the stack up and synthetic data present (registered fresh
     here: entity/source/evidence/provenance/audit), take a backup via
     ops/backup_postgres.sh and ops/backup_objects.py.
  2. Destroy the persistence layer completely: `docker compose down
     -v` (removes the named volumes — bagman-postgres-data,
     bagman-object-data).
  3. Bring the stack back up (`docker compose up -d`) — a genuinely
     clean, empty target: fresh volumes, migrations reapply against an
     empty schema, an empty bucket.
  4. Restore: ops/restore_postgres.sh (the dump from step 1) and
     ops/restore_objects.py (the export from step 1).
  5. Verify, via the live API: every canonical ID from before the
     destroy is present and identical, content bytes/hash are
     byte-identical, provenance/audit trail is intact.
  6. Clean shutdown at the end (`docker compose down -v`, confirm
     nothing bagman-* remains — containers, volumes, network, and the
     image built for this work item).

Run directly (last in the acceptance sequence — see
tests/acceptance/README.md / `make accept`):
    python3 tests/acceptance/restore_into_clean_target_proof.py
"""
from __future__ import annotations

import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import _lib  # noqa: E402


def main() -> int:
    rid = _lib.run_id()
    actor_id = f"bagman-wi4-restore-proof-{rid}"

    _lib.section(f"BAGMAN CD-3 WI-4 — PID §35/§60 REQUIRED RESTORE-INTO-CLEAN-TARGET PROOF (run {rid})")

    # -----------------------------------------------------------------
    # ensure the stack is up + fresh synthetic data present
    # -----------------------------------------------------------------
    print(f"\n[0] ENSURE STACK IS UP; REGISTER FRESH SYNTHETIC DATA")
    _lib.compose_build()
    _lib.compose_up_wait()
    ready_body = _lib.wait_for_ready()
    print(f"    /ready -> {ready_body}")
    assert ready_body["ready"] is True

    entity = _lib.register_entity(canonical_name=f"WI4_RESTORE_{rid}", actor_id=actor_id)
    source = _lib.register_source(
        external_source_ref=f"wi4-restore-proof-{rid}",
        governed_entity_hint=entity["canonical_name"],
        actor_id=actor_id,
    )
    content = f"BAGMAN WI-4 restore-into-clean-target-proof synthetic evidence bytes (run {rid})\n".encode("utf-8")
    external_id = f"wi4-restore-proof-{rid}-file"
    evidence = _lib.register_evidence(
        entity_id=entity["entity_id"],
        source_id=source["source_id"],
        content=content,
        original_name=f"wi4-restore-proof-{rid}.txt",
        external_reference_external_id=external_id,
        actor_id=actor_id,
    )
    evidence_id = evidence["evidence_id"]
    content_hash = evidence["content_hash"]
    print(f"    entity_id      = {entity['entity_id']}")
    print(f"    source_id      = {source['source_id']}")
    print(f"    evidence_id    = {evidence_id}")
    print(f"    content_hash   = {content_hash}")

    classification_subject_id = _lib.generate_canonical_id()
    prov_result = _lib.record_provenance_and_followon_audit(
        evidence_id=evidence_id,
        classification_subject_id=classification_subject_id,
        actor_id=actor_id,
    )
    print(f"    provenance_id  = {prov_result['provenance_id']}")

    lineage_before = _lib.get_provenance("Classification", classification_subject_id)
    audit_before = _lib.audit_trail_snapshot(
        evidence_id=evidence_id, classification_subject_id=classification_subject_id
    )
    evidence_before = _lib.get_evidence(evidence_id)
    content_before = _lib.get_evidence_content(evidence_id).content
    assert content_before == content
    print(f"    baseline captured: evidence metadata, {len(content_before)} content bytes, "
          f"{len(lineage_before)} provenance edge(s), audit trail.")

    # -----------------------------------------------------------------
    # [1] backup
    # -----------------------------------------------------------------
    print(f"\n[1] BACKUP (ops/backup_postgres.sh + ops/backup_objects.py)")
    pg_dump_file = _lib.backup_postgres()
    print(f"    postgres dump  = {pg_dump_file}")
    assert pg_dump_file.is_file() and pg_dump_file.stat().st_size > 0

    objects_ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    objects_dir = _lib.backup_objects(objects_ts)
    print(f"    objects export = {objects_dir}")
    assert objects_dir.is_dir()
    exported_files = sorted(p for p in objects_dir.rglob("*") if p.is_file())
    print(f"    {len(exported_files)} object file(s) exported: {[str(p.relative_to(objects_dir)) for p in exported_files]}")
    assert exported_files, "expected at least the evidence object just registered to be exported"

    # -----------------------------------------------------------------
    # [2] destroy the persistence layer completely
    # -----------------------------------------------------------------
    print(f"\n[2] DESTROY PERSISTENCE LAYER COMPLETELY (docker compose down -v)")
    volumes_before_destroy = set(_lib.remaining_bagman_volumes())
    print(f"    bagman-* volumes before destroy: {sorted(volumes_before_destroy)}")
    assert {"bagman-postgres-data", "bagman-object-data"} <= volumes_before_destroy

    _lib.compose_down(volumes=True)

    volumes_after_destroy = set(_lib.remaining_bagman_volumes())
    print(f"    bagman-* volumes after destroy:  {sorted(volumes_after_destroy)}")
    assert "bagman-postgres-data" not in volumes_after_destroy
    assert "bagman-object-data" not in volumes_after_destroy
    print(f"    CONFIRMED: named volumes genuinely destroyed.")

    # -----------------------------------------------------------------
    # [3] bring the stack back up — genuinely clean, empty target
    # -----------------------------------------------------------------
    print(f"\n[3] BRING STACK BACK UP (fresh empty volumes; migrations reapply from scratch)")
    _lib.compose_up_wait()
    ready_body_clean = _lib.wait_for_ready()
    print(f"    /ready -> {ready_body_clean}")
    assert ready_body_clean["ready"] is True

    try:
        _lib.get_evidence(evidence_id)
        raise AssertionError(
            f"expected evidence_id {evidence_id} to be ABSENT from the freshly-recreated, "
            "not-yet-restored database, but it was found"
        )
    except AssertionError:
        raise
    except Exception as exc:  # a 404 NotFoundError is exactly the expected outcome here
        print(f"    GET /internal/evidence/{evidence_id} on the clean target correctly fails: {exc}")

    # -----------------------------------------------------------------
    # [4] restore
    # -----------------------------------------------------------------
    print(f"\n[4] RESTORE (ops/restore_postgres.sh + ops/restore_objects.py)")
    _lib.restore_postgres(pg_dump_file)
    _lib.restore_objects(objects_ts)

    # -----------------------------------------------------------------
    # [5] verify, via the live API, everything survived
    # -----------------------------------------------------------------
    print(f"\n[5] VERIFY: SAME CANONICAL IDS / BYTES / HASH / LINEAGE / AUDIT, ON THE RESTORED TARGET")

    evidence_after = _lib.get_evidence(evidence_id)
    print(f"    evidence (after restore) = {evidence_after}")
    for field in (
        "evidence_id", "entity_id", "evidence_type", "source_id", "content_hash",
        "mime_type", "size_bytes", "status", "storage_reference", "original_name",
    ):
        assert evidence_after[field] == evidence_before[field], (
            f"field '{field}' changed across destroy+restore: "
            f"before={evidence_before[field]!r} after={evidence_after[field]!r}"
        )
    print(f"    ALL evidence metadata fields identical after restore.")

    content_after = _lib.get_evidence_content(evidence_id)
    print(f"    GET .../content (after restore) -> {len(content_after.content)} bytes, "
          f"hash-header={content_after.headers.get('X-Bagman-Content-Hash-Value')}")
    assert content_after.content == content, "evidence bytes changed across destroy+restore"
    assert content_after.headers["X-Bagman-Content-Hash-Value"] == content_hash["value"]
    print(f"    content bytes byte-identical; SHA-256 identical.")

    lineage_after = _lib.get_provenance("Classification", classification_subject_id)
    print(f"    provenance/lineage (after restore) = {lineage_after}")
    assert lineage_after == lineage_before, "provenance/lineage changed across destroy+restore"
    print(f"    provenance/lineage identical after restore.")

    audit_after = _lib.audit_trail_snapshot(
        evidence_id=evidence_id, classification_subject_id=classification_subject_id
    )
    print(f"    audit trail (after restore) = {audit_after}")
    assert audit_after == audit_before, "audit trail changed across destroy+restore"
    print(f"    audit trail (events, correlation_id, causation_id) identical after restore.")

    # -----------------------------------------------------------------
    # [6] final clean shutdown — confirm nothing bagman-* remains
    # -----------------------------------------------------------------
    print(f"\n[6] FINAL CLEAN SHUTDOWN (docker compose down -v + remove the built image)")
    _lib.compose_down(volumes=True)
    _lib.remove_bagman_api_image()

    remaining_containers = _lib.remaining_bagman_containers()
    remaining_volumes = _lib.remaining_bagman_volumes()
    remaining_networks = _lib.remaining_bagman_networks()
    remaining_images = _lib.remaining_bagman_images()

    print(f"    remaining bagman-* containers = {remaining_containers}")
    print(f"    remaining bagman-* volumes    = {remaining_volumes}")
    print(f"    remaining bagman-net networks = {remaining_networks}")
    print(f"    remaining bagman-api images   = {remaining_images}")

    assert not remaining_containers, f"containers still present after final teardown: {remaining_containers}"
    assert not remaining_volumes, f"volumes still present after final teardown: {remaining_volumes}"
    assert not remaining_networks, f"networks still present after final teardown: {remaining_networks}"
    assert not remaining_images, f"images still present after final teardown: {remaining_images}"
    print(f"    CONFIRMED: no bagman-* containers/volumes/networks/images remain.")

    _lib.section("PID §35/§60 RESTORE-INTO-CLEAN-TARGET PROOF: ALL STEPS COMPLETED AND VERIFIED")
    print(f"    evidence_id   = {evidence_id}")
    print(f"    provenance_id = {prov_result['provenance_id']}")
    print(f"    Full clean shutdown confirmed — no bagman-* runtime state remains.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
