"""CD-4 WI-5 acceptance evidence — dependency failure proofs (PID §55,
§56, §57): scanner, object storage, and PostgreSQL, each stopped in turn
against the REAL running stack, proving BAGMAN fails closed/visibly and
recovers cleanly, never silently degrading to a false success.

Real, directly-runnable script (see ``tests/acceptance/README.md``). No
mocks: each dependency is a REAL Docker container
(``bagman-scan``/``bagman-objects``/``bagman-db``), genuinely stopped and
restarted via ``docker compose``.

What this proves
-----------------
(a) PID §55 — stop ``bagman-scan``: ``/ready`` goes 503 with
    ``failed_dependency: "scanner"`` immediately; a real upload attempted
    while the scanner is down FAILS CLOSED (``FAILED``/503,
    ``SCAN_FAILED`` — never ``ACCEPTED``/``REGISTERED``, confirming
    ``ClamAVScanner.scan()``'s transport failure -> ``SCAN_ERROR`` ->
    ``DEFAULT_INTAKE_POLICY.scan_error_treatment == "FAIL"`` ->
    ``ScanFailedError`` chain genuinely holds against a real daemon,
    not just the in-memory fakes in ``tests/integration/``). Then
    restart ``bagman-scan`` and prove recovery: ``/ready`` green again,
    a FRESH upload succeeds.
(b) PID §56 — stop ``bagman-objects`` (MinIO): ``/ready`` 503 +
    ``failed_dependency: "object_store"``; a real upload attempt fails
    visibly (503) rather than falsely reporting success, and the
    resulting ``IntakeRecord`` never claims an ``EvidenceItem`` was
    created. Restart, prove recovery.
(c) PID §57 — stop ``bagman-db`` (PostgreSQL): ``/ready`` 503 +
    ``failed_dependency: "postgres"``; a real upload attempt fails
    loudly (503) with NO silent local/in-memory fallback (the absolute
    CD-3 invariant — see ``app/api/composition.py``'s own docstring).
    Restart, prove recovery.

The stack is returned to fully healthy at the end (this script does not
tear anything down — see ``tests/acceptance/README.md`` for the final
full-teardown script).

Run standalone (brings the stack up itself first):

    python3 tests/acceptance/dependency_failure_proof.py
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import requests

sys.path.insert(0, str(Path(__file__).resolve().parent))

import _lib  # noqa: E402
from _lib import (  # noqa: E402
    BASE_URL,
    compose_build,
    compose_up_service_wait,
    compose_up_wait,
    compose_stop_service,
    run_id,
    section,
    wait_for_ready,
)

ACTOR_ID = "wi5-dependency-failure-proof"


def _post_intake(*, content: bytes, filename: str, entity_hint: str) -> requests.Response:
    metadata = {
        "entity_hint": entity_hint,
        "evidence_type": "DOCUMENT",
        "actor_type": "SYSTEM",
        "actor_id": ACTOR_ID,
        "note": None,
    }
    return requests.post(
        f"{BASE_URL}/internal/intake/evidence",
        data={"metadata": json.dumps(metadata)},
        files={"file": (filename, content, "text/plain")},
        timeout=90,  # see _get_ready()'s comment on botocore's default ~60s connect timeout
    )


def _get_ready() -> requests.Response:
    # Generous client-side timeout (not 5-10s): when bagman-objects is
    # simply stopped (not gracefully closing its port), the TCP SYN to
    # it is silently dropped rather than immediately RST — and
    # MinIOObjectStore/boto3 has no explicit connect/read timeout
    # configured (persistence/objects/minio_store.py's own
    # MinIOConfig has no timeout fields), so botocore's own defaults
    # (60s connect timeout) govern how long /ready's own live
    # object-store probe can take before it fails and returns 503.
    # This is a genuine, worth-flagging operational characteristic —
    # see the CD-4 WI-5 evidence file — not a bug this script papers
    # over: it still, eventually, returns a correct 503.
    return requests.get(f"{BASE_URL}/ready", timeout=90)


def _assert_ready_failed(response: requests.Response, *, expected_dependency: str) -> dict:
    print(f"    /ready -> HTTP {response.status_code}: {response.text}")
    assert response.status_code == 503, f"expected /ready to return 503, got {response.status_code}"
    body = response.json()
    assert body["ready"] is False
    assert body["failed_dependency"] == expected_dependency, (
        f"expected failed_dependency={expected_dependency!r}, got {body.get('failed_dependency')!r}: {body}"
    )
    return body


def _assert_ready_healthy() -> dict:
    body = wait_for_ready(timeout=120)
    print(f"    /ready -> {body}")
    assert body["checks"] == {"postgres": "ok", "object_store": "ok", "scanner": "ok"}, body
    return body


def part_scanner_failure(tag: str) -> None:
    section("(a) PID §55 — SCANNER FAILURE PROOF (stop bagman-scan)")
    _assert_ready_healthy()

    print("\n    stopping bagman-scan...")
    compose_stop_service("bagman-scan")

    print("\n    checking /ready...")
    ready_response = _get_ready()
    _assert_ready_failed(ready_response, expected_dependency="scanner")
    print("    /ready correctly reports 503 failed_dependency=scanner.")

    print("\n    attempting a real upload while the scanner is down...")
    content = f"WI-5 scanner-failure proof body {tag}\n".encode()
    response = _post_intake(content=content, filename=f"wi5-scanner-down-{tag}.txt", entity_hint=f"WI5_SCANFAIL_{tag}")
    print(f"    upload attempt -> HTTP {response.status_code}: {response.text[:300]}")
    assert response.status_code == 503, f"expected 503 (fail closed), got {response.status_code}"
    body = response.json()
    assert body["intake"]["status"] == "FAILED", f"expected intake status FAILED, got {body['intake']}"
    assert body["intake"]["failure_code"] == "SCAN_FAILED", f"expected failure_code SCAN_FAILED, got {body['intake']}"
    assert body["evidence"] is None, f"no EvidenceItem must ever be created on a scanner failure: {body}"
    print("    upload FAILED CLOSED (503, intake FAILED/SCAN_FAILED) — no evidence was created while the scanner was unreachable.")

    print("\n    restarting bagman-scan...")
    compose_up_service_wait("bagman-scan")
    _assert_ready_healthy()

    print("\n    proving recovery: a fresh upload now succeeds...")
    fresh_content = f"WI-5 scanner-recovery proof body {tag}\n".encode()
    recovery_response = _post_intake(
        content=fresh_content, filename=f"wi5-scanner-recovered-{tag}.txt", entity_hint=f"WI5_SCANFAIL_{tag}"
    )
    print(f"    recovery upload -> HTTP {recovery_response.status_code}")
    assert recovery_response.status_code == 201, (
        f"expected 201 after scanner recovery, got {recovery_response.status_code}: {recovery_response.text[:300]}"
    )
    print("    PID §55 SCANNER FAILURE PROOF: fail-closed + clean recovery PROVEN.")


def part_object_store_failure(tag: str) -> None:
    section("(b) PID §56 — OBJECT STORAGE FAILURE PROOF (stop bagman-objects)")
    _assert_ready_healthy()

    print("\n    stopping bagman-objects...")
    compose_stop_service("bagman-objects")

    print("\n    checking /ready...")
    ready_response = _get_ready()
    _assert_ready_failed(ready_response, expected_dependency="object_store")
    print("    /ready correctly reports 503 failed_dependency=object_store.")

    print("\n    attempting a real upload while object storage is down...")
    content = f"WI-5 object-store-failure proof body {tag}\n".encode()
    response = _post_intake(content=content, filename=f"wi5-objects-down-{tag}.txt", entity_hint=f"WI5_OBJFAIL_{tag}")
    print(f"    upload attempt -> HTTP {response.status_code}: {response.text[:300]}")
    assert response.status_code == 503, f"expected 503 (fail visibly), got {response.status_code}"
    error_body = response.json()
    assert error_body.get("error_code") == "STORAGE_ERROR", (
        f"expected error_code STORAGE_ERROR, got {error_body.get('error_code')!r}: {error_body}"
    )
    print("    upload FAILED VISIBLY (503, STORAGE_ERROR) — no EvidenceItem was falsely reported as created.")

    print("\n    restarting bagman-objects...")
    compose_up_service_wait("bagman-objects")
    _assert_ready_healthy()

    print("\n    proving recovery: a fresh upload now succeeds...")
    fresh_content = f"WI-5 object-store-recovery proof body {tag}\n".encode()
    recovery_response = _post_intake(
        content=fresh_content, filename=f"wi5-objects-recovered-{tag}.txt", entity_hint=f"WI5_OBJFAIL_{tag}"
    )
    print(f"    recovery upload -> HTTP {recovery_response.status_code}")
    assert recovery_response.status_code == 201, (
        f"expected 201 after object-store recovery, got {recovery_response.status_code}: {recovery_response.text[:300]}"
    )
    print("    PID §56 OBJECT STORAGE FAILURE PROOF: fail-visibly + clean recovery PROVEN.")


def part_database_failure(tag: str) -> None:
    section("(c) PID §57 — DATABASE FAILURE PROOF (stop bagman-db)")
    _assert_ready_healthy()

    print("\n    stopping bagman-db...")
    compose_stop_service("bagman-db")

    print("\n    checking /ready...")
    ready_response = _get_ready()
    _assert_ready_failed(ready_response, expected_dependency="postgres")
    print("    /ready correctly reports 503 failed_dependency=postgres.")

    print("\n    attempting a real upload while PostgreSQL is down...")
    content = f"WI-5 database-failure proof body {tag}\n".encode()
    response = _post_intake(content=content, filename=f"wi5-db-down-{tag}.txt", entity_hint=f"WI5_DBFAIL_{tag}")
    print(f"    upload attempt -> HTTP {response.status_code}: {response.text[:300]}")
    assert response.status_code == 503, f"expected 503 (fail loudly, no fallback), got {response.status_code}"
    error_body = response.json()
    assert error_body.get("error_code") == "PERSISTENCE_ERROR", (
        f"expected error_code PERSISTENCE_ERROR, got {error_body.get('error_code')!r}: {error_body}"
    )
    print("    upload FAILED LOUDLY (503, PERSISTENCE_ERROR) — no silent local/in-memory fallback occurred (CD-3 invariant held).")

    print("\n    restarting bagman-db...")
    compose_up_service_wait("bagman-db", timeout=180)
    _assert_ready_healthy()

    print("\n    proving recovery: a fresh upload now succeeds...")
    fresh_content = f"WI-5 database-recovery proof body {tag}\n".encode()
    recovery_response = _post_intake(
        content=fresh_content, filename=f"wi5-db-recovered-{tag}.txt", entity_hint=f"WI5_DBFAIL_{tag}"
    )
    print(f"    recovery upload -> HTTP {recovery_response.status_code}")
    assert recovery_response.status_code == 201, (
        f"expected 201 after database recovery, got {recovery_response.status_code}: {recovery_response.text[:300]}"
    )
    print("    PID §57 DATABASE FAILURE PROOF: fail-loudly + no-fallback + clean recovery PROVEN.")


def main() -> None:
    section("SETUP — bring up the real BAGMAN Docker Compose stack")
    compose_build()
    compose_up_wait()

    tag = run_id()

    part_scanner_failure(tag)
    part_object_store_failure(tag)
    part_database_failure(tag)

    section("FINAL STATE")
    final = _assert_ready_healthy()
    print(f"    final /ready = {final}")

    section("SUMMARY")
    print("    (a) scanner failure (PID §55)      : fail-closed + recovery PROVEN")
    print("    (b) object-store failure (PID §56) : fail-visibly + recovery PROVEN")
    print("    (c) database failure (PID §57)     : fail-loudly + no-fallback + recovery PROVEN")
    print("\nPID §55/§56/§57 DEPENDENCY FAILURE PROOF: ALL PARTS COMPLETED AND VERIFIED")
    print("(stack left running, fully healthy)")


if __name__ == "__main__":
    main()
