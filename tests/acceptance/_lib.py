"""Shared helpers for the CD-3 WI-4 acceptance scripts (PID §43, §44,
§35/§60). Not a pytest fixture module — these scripts are standalone,
directly-runnable programs (see ``tests/acceptance/README.md`` for why
they are deliberately NOT named ``test_*.py``), and this module is
their common plumbing: driving the REAL ``docker compose -p bagman``
stack, talking to the REAL live ``bagman-api`` HTTP surface, and
making the one necessary in-process ``core.api.BagmanCanonicalAPI``
call (via ``docker compose exec`` into the already-running
``bagman-api`` container) for ``record_provenance``/
``record_audit_event`` — operations PID §24's ``/internal/*`` surface
does not expose over HTTP (confirmed by reading
``app/api/routers/internal.py``: only ``register_entity``,
``register_source``, ``register_evidence``, ``get_evidence``,
``get_evidence_content``, and ``trace_provenance`` are wired to HTTP).
No new HTTP endpoint is added anywhere by this module — WI-3's
surface is used exactly as it stands.
"""
from __future__ import annotations

import json
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

import requests

REPO_ROOT = Path(__file__).resolve().parents[2]
COMPOSE_FILE = REPO_ROOT / "deployment" / "compose" / "docker-compose.yml"
BASE_URL = "http://127.0.0.1:8000"

ACTOR_TYPE = "SYSTEM"


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def generate_canonical_id() -> str:
    """A fresh canonical BAGMAN identifier (UUIDv7), generated locally
    via `core.identity.generate_id()` — the same algorithm the running
    server uses — for values (like a derived object's `subject_id`)
    that the canonical/provenance contract requires to be shaped like
    a real BAGMAN identifier (`^[0-9a-f]{8}-...-7[0-9a-f]{3}-...$`),
    not an arbitrary human-readable slug."""
    sys.path.insert(0, str(REPO_ROOT))
    from core import identity

    return identity.generate_id()


def run_id() -> str:
    """A fresh, all-caps-digits identifier fragment safe to embed in
    ``canonical_name``/``status``-shaped fields (which are pattern-
    constrained to ``^[A-Z][A-Z0-9_]*$`` by the entity/source
    contracts) — millisecond-resolution UTC timestamp, so distinct
    runs of an acceptance script against the same persistent stack
    never collide."""
    return datetime.now(timezone.utc).strftime("%Y%m%d%H%M%S%f")


def _print_cmd(cmd: list[str]) -> None:
    print(f"$ {' '.join(cmd)}", flush=True)


def compose_cmd(*args: str) -> list[str]:
    return ["docker", "compose", "-p", "bagman", "-f", str(COMPOSE_FILE), *args]


def run(cmd: list[str], **kwargs) -> subprocess.CompletedProcess:
    """Run ``cmd`` with output streamed straight to this process's own
    stdout/stderr (so a captured `python3 tests/acceptance/*.py`
    invocation's terminal output IS the verbatim transcript — nothing
    is swallowed or summarized here). Raises on a non-zero exit unless
    the caller passes ``check=False``."""
    _print_cmd(cmd)
    kwargs.setdefault("cwd", REPO_ROOT)
    kwargs.setdefault("check", True)
    return subprocess.run(cmd, **kwargs)


def run_capture(cmd: list[str], input_text: Optional[str] = None) -> subprocess.CompletedProcess:
    """Run ``cmd``, capturing stdout/stderr as text — but still prints
    everything it captured afterwards, so the transcript is complete
    even though this variant also lets a caller parse the output."""
    _print_cmd(cmd)
    result = subprocess.run(
        cmd, cwd=REPO_ROOT, input=input_text, capture_output=True, text=True
    )
    if result.stdout:
        print(result.stdout, end="" if result.stdout.endswith("\n") else "\n", flush=True)
    if result.stderr:
        print(result.stderr, end="" if result.stderr.endswith("\n") else "\n", file=sys.stderr, flush=True)
    if result.returncode != 0:
        raise subprocess.CalledProcessError(result.returncode, cmd, result.stdout, result.stderr)
    return result


def compose_build() -> None:
    run(compose_cmd("build"))


def compose_up_wait(timeout: int = 180) -> None:
    run(compose_cmd("up", "-d", "--wait", "--wait-timeout", str(timeout)))


def compose_up_service_wait(service: str, timeout: int = 120) -> None:
    run(compose_cmd("up", "-d", "--wait", "--wait-timeout", str(timeout), service))


def compose_down(volumes: bool = False) -> None:
    args = ["down"]
    if volumes:
        args.append("-v")
    run(compose_cmd(*args))


def compose_rm_service(service: str) -> None:
    run(compose_cmd("rm", "-sf", service))


def compose_ps_service_id(service: str) -> str:
    result = run_capture(compose_cmd("ps", "-q", service))
    return result.stdout.strip()


def compose_exec_python(script_text: str, service: str = "bagman-api") -> str:
    """Run ``script_text`` as a Python program INSIDE the already-
    running ``service`` container via ``docker compose exec -T
    <service> python3 -`` (script fed on stdin). Used ONLY for
    ``core.api.BagmanCanonicalAPI.record_provenance``/
    ``record_audit_event`` calls (and read-only audit-trail
    inspection) that PID §24's HTTP surface does not expose — per the
    work item's own instruction: "fall back to a direct in-process
    call against core.api.BagmanCanonicalAPI configured the same way
    the running server is". Returns captured stdout.
    """
    result = run_capture(compose_cmd("exec", "-T", service, "python3", "-"), input_text=script_text)
    return result.stdout


def parse_marker_json(stdout: str, marker: str) -> dict:
    """Find the line in ``stdout`` starting with ``marker`` and parse
    the JSON that follows it — the convention every inline
    ``compose_exec_python`` script in this directory uses to hand a
    structured result back to the calling acceptance script without
    polluting/parsing the rest of its (human-readable) print output."""
    for line in stdout.splitlines():
        if line.startswith(marker):
            return json.loads(line[len(marker):])
    raise RuntimeError(f"marker {marker!r} not found in compose_exec_python output:\n{stdout}")


# ---------------------------------------------------------------------
# HTTP API helpers (BAGMAN's real /internal/* + /health//ready surface)
# ---------------------------------------------------------------------


def wait_for_ready(timeout: float = 180.0) -> dict:
    deadline = time.monotonic() + timeout
    last_body: dict = {}
    while time.monotonic() < deadline:
        try:
            response = requests.get(f"{BASE_URL}/ready", timeout=5)
            last_body = response.json()
            if response.status_code == 200 and last_body.get("ready") is True:
                return last_body
        except requests.RequestException:
            pass
        time.sleep(1.0)
    raise RuntimeError(f"bagman-api did not become ready in {timeout}s (last body: {last_body})")


def register_entity(canonical_name: str, actor_id: str) -> dict:
    response = requests.post(
        f"{BASE_URL}/internal/entities",
        json={
            "entity_type": "COMPANY",
            "canonical_name": canonical_name,
            "display_name": f"BAGMAN WI-4 Acceptance ({canonical_name})",
            "status": "ACTIVE",
            "actor_type": ACTOR_TYPE,
            "actor_id": actor_id,
        },
        timeout=10,
    )
    response.raise_for_status()
    return response.json()


def register_source(external_source_ref: str, governed_entity_hint: str, actor_id: str) -> dict:
    response = requests.post(
        f"{BASE_URL}/internal/sources",
        json={
            "source_type": "MANUAL_UPLOAD",
            "provider": "INTERNAL",
            "status": "ACTIVE",
            "actor_type": ACTOR_TYPE,
            "actor_id": actor_id,
            "external_source_ref": external_source_ref,
            "governed_entity_hint": governed_entity_hint,
        },
        timeout=10,
    )
    response.raise_for_status()
    return response.json()


def register_evidence(
    *,
    content: bytes,
    original_name: str,
    actor_id: str,
    entity_hint: Optional[str] = None,
    evidence_type: str = "DOCUMENT",
    note: Optional[str] = None,
    idempotency_key: Optional[str] = None,
) -> dict:
    """Create canonical evidence via the CD-4 governed intake endpoint
    (``POST /internal/intake/evidence``) — CD-3's direct-upload bypass
    this helper originally called (``POST /internal/evidence``) has
    been REMOVED entirely (PID §26/§27; see
    ``app/api/routers/internal.py``'s module docstring for the closure
    rationale). This is CD-4 WI-3's real fix for that closure, not a
    workaround: CD-3's acceptance scripts stood in for "the only
    evidence producer that existed at the time"; CD-4 supersedes that
    with a governed one.

    Unlike the removed CD-3 route, CD-4 intake:

    * never accepts an arbitrary caller-chosen ``source_id`` — every
      manual-upload intake resolves to BAGMAN's own single stable
      ``MANUAL_UPLOAD`` source automatically (PID §9);
    * never accepts a real ``entity_id`` — CD-4's recorded
      entity-resolution decision (see ``app/api/composition.py``'s and
      ``app/api/routers/intake.py``'s own module docstrings) always
      registers with ``entity_id=None``; ``entity_hint`` is carried
      through as a free-text HINT only, never asserted ownership;
    * uses an ``Idempotency-Key`` HTTP header, not an
      ``external_reference`` tuple, as its idempotent-retry mechanism
      (PID §25/§53) — a caller wanting the CD-3-style "same external
      observation -> same evidence" proof supplies the SAME
      ``idempotency_key`` on a retried call.

    Raises if the intake did not actually produce canonical evidence
    (e.g. REJECTED/QUARANTINED/FAILED) — every current acceptance
    script's synthetic plain-text fixture is expected to be cleanly
    ``ACCEPTED``/``REGISTERED``.

    Returns the endpoint's ``evidence`` object (``EvidenceItem.to_dict()``),
    with a synthetic ``_request_metadata`` key added (mirroring the
    removed CD-3 helper's own convention) so a caller can byte-for-byte
    replay this same call, and a synthetic ``_intake`` key carrying the
    full ``IntakeRecord.to_dict()`` alongside it.
    """
    metadata = {
        "entity_hint": entity_hint,
        "evidence_type": evidence_type,
        "actor_type": ACTOR_TYPE,
        "actor_id": actor_id,
        "note": note,
    }
    headers = {"Idempotency-Key": idempotency_key} if idempotency_key else {}
    response = requests.post(
        f"{BASE_URL}/internal/intake/evidence",
        data={"metadata": json.dumps(metadata)},
        files={"file": (original_name, content, "text/plain")},
        headers=headers,
        timeout=10,
    )
    response.raise_for_status()
    body = response.json()
    evidence = body.get("evidence")
    if evidence is None:
        raise RuntimeError(
            f"intake did not produce canonical evidence (intake status "
            f"{body['intake']['status']!r}, failure_code="
            f"{body['intake'].get('failure_code')!r}): {body}"
        )
    evidence["_request_metadata"] = {**metadata, "idempotency_key": idempotency_key}
    evidence["_intake"] = body["intake"]
    return evidence


def get_evidence(evidence_id: str) -> dict:
    response = requests.get(f"{BASE_URL}/internal/evidence/{evidence_id}", timeout=10)
    response.raise_for_status()
    return response.json()


def get_evidence_content(evidence_id: str) -> requests.Response:
    response = requests.get(f"{BASE_URL}/internal/evidence/{evidence_id}/content", timeout=10)
    response.raise_for_status()
    return response


def get_provenance(subject_type: str, subject_id: str) -> list[dict]:
    response = requests.get(f"{BASE_URL}/internal/provenance/{subject_type}/{subject_id}", timeout=10)
    response.raise_for_status()
    return response.json()


# ---------------------------------------------------------------------
# in-container BagmanCanonicalAPI calls (provenance/audit — no HTTP
# surface exists for these; see this module's own docstring)
# ---------------------------------------------------------------------

_RESULT_MARKER = "WI4_RESULT_JSON:"


def record_provenance_and_followon_audit(
    *, evidence_id: str, classification_subject_id: str, actor_id: str
) -> dict:
    """Records one derived Provenance edge off ``evidence_id`` plus one
    explicit follow-on AuditEvent threaded onto the same
    correlation/causation chain as the evidence's own
    ``EVIDENCE_OBSERVED`` event — via a direct in-process
    ``core.api.BagmanCanonicalAPI`` call inside the already-running
    ``bagman-api`` container (PID §24's HTTP surface has no
    ``record_provenance``/``record_audit_event`` route; see this
    module's own docstring). Mirrors the pattern
    ``tests/integration/test_runtime_proof.py`` (CD-2's own PID §43
    proof) already established for step 7's "record audit events (a
    short causal chain within one correlation)".

    Returns
    -------
    dict with ``provenance_id``, ``correlation_id``, ``observed_event_id``,
    ``followon_event_id``.
    """
    script = f"""
import json
from app.api.composition import get_composition

composition = get_composition()
api = composition.api

evidence_id = {evidence_id!r}
classification_subject_id = {classification_subject_id!r}
actor_type = {ACTOR_TYPE!r}
actor_id = {actor_id!r}

observed_events = [
    e for e in api.audit_repository.list_by_subject("EvidenceItem", evidence_id)
    if e.event_type == "EVIDENCE_OBSERVED"
]
assert len(observed_events) == 1, f"expected exactly 1 EVIDENCE_OBSERVED event, found {{len(observed_events)}}"
observed_event = observed_events[0]

provenance = api.record_provenance(
    subject_type="Classification",
    subject_id=classification_subject_id,
    evidence_id=evidence_id,
    relationship="EXTRACTED_FROM",
    actor_type=actor_type,
    actor_id=actor_id,
    metadata={{"note": "BAGMAN WI-4 acceptance synthetic derived classification"}},
)

followon = api.record_audit_event(
    event_type="CLASSIFICATION_PROPOSED",
    actor_type=actor_type,
    actor_id=actor_id,
    subject_type="Classification",
    subject_id=classification_subject_id,
    correlation_id=observed_event.correlation_id,
    causation_id=observed_event.audit_event_id,
    payload={{"evidence_id": evidence_id, "provenance_id": provenance.provenance_id}},
)

print({_RESULT_MARKER!r} + json.dumps({{
    "provenance_id": provenance.provenance_id,
    "correlation_id": observed_event.correlation_id,
    "observed_event_id": observed_event.audit_event_id,
    "followon_event_id": followon.audit_event_id,
}}))
"""
    stdout = compose_exec_python(script)
    return parse_marker_json(stdout, _RESULT_MARKER)


def audit_trail_snapshot(*, evidence_id: str, classification_subject_id: str) -> dict:
    """Read-only in-container snapshot of everything this acceptance
    run's audit trail should still contain: every AuditEvent for the
    EvidenceItem subject, every AuditEvent for the Classification
    subject, and the full correlation-chain ordering — used to prove
    "same events, same correlation/causation" survives a restart/
    rebuild/restore byte-for-byte (event ids, types, correlation ids,
    causation ids), not merely "an event of the right type exists
    somewhere"."""
    script = f"""
import json
from app.api.composition import get_composition

composition = get_composition()
api = composition.api

evidence_id = {evidence_id!r}
classification_subject_id = {classification_subject_id!r}

evidence_events = api.audit_repository.list_by_subject("EvidenceItem", evidence_id)
classification_events = api.audit_repository.list_by_subject("Classification", classification_subject_id)

observed_events = [e for e in evidence_events if e.event_type == "EVIDENCE_OBSERVED"]
correlation_id = observed_events[0].correlation_id if observed_events else None
correlation_chain = api.audit_repository.list_by_correlation(correlation_id) if correlation_id else []

print({_RESULT_MARKER!r} + json.dumps({{
    "evidence_events": [
        {{"audit_event_id": e.audit_event_id, "event_type": e.event_type,
          "correlation_id": e.correlation_id, "causation_id": e.causation_id}}
        for e in evidence_events
    ],
    "classification_events": [
        {{"audit_event_id": e.audit_event_id, "event_type": e.event_type,
          "correlation_id": e.correlation_id, "causation_id": e.causation_id}}
        for e in classification_events
    ],
    "correlation_chain_event_ids": [e.audit_event_id for e in correlation_chain],
}}))
"""
    stdout = compose_exec_python(script)
    return parse_marker_json(stdout, _RESULT_MARKER)


# ---------------------------------------------------------------------
# ops/ backup & restore invocation (same shape as the Makefile's
# backup/restore targets — see Makefile, PID §34-35/§50)
# ---------------------------------------------------------------------


def backup_postgres() -> Path:
    """Runs ops/backup_postgres.sh (default output dir) and returns the
    resulting dump file's Path (parsed from the script's own stdout,
    which prints exactly that path as its only stdout line)."""
    result = run_capture([str(REPO_ROOT / "ops" / "backup_postgres.sh")])
    lines = [line for line in result.stdout.splitlines() if line.strip()]
    return Path(lines[-1].strip())


def restore_postgres(dump_file: Path) -> None:
    run([str(REPO_ROOT / "ops" / "restore_postgres.sh"), str(dump_file)])


def _objects_container_run(*extra_args: str) -> list[str]:
    return compose_cmd(
        "run", "--rm", "--no-deps", "-e", "PYTHONPATH=/app",
        *extra_args,
    )


def backup_objects(timestamp: str) -> Path:
    """Runs ops/backup_objects.py inside a throwaway bagman-api
    container (same shape as the Makefile's `backup` target), writing
    into backups/objects/<timestamp>/ on the host. Returns that
    directory's host-side Path."""
    container_path = f"/host-backups/objects/{timestamp}"
    cmd = _objects_container_run(
        "-v", f"{REPO_ROOT}/ops:/host-ops:ro",
        "-v", f"{REPO_ROOT}/backups:/host-backups",
        "--entrypoint", "python3", "bagman-api",
        "/host-ops/backup_objects.py", container_path,
    )
    run(cmd)
    return REPO_ROOT / "backups" / "objects" / timestamp


def restore_objects(timestamp: str) -> None:
    """Runs ops/restore_objects.py inside a throwaway bagman-api
    container against a directory previously produced by
    backup_objects() (same shape as the Makefile's `restore` target)."""
    container_path = f"/host-backups/objects/{timestamp}"
    cmd = _objects_container_run(
        "-v", f"{REPO_ROOT}/ops:/host-ops:ro",
        "-v", f"{REPO_ROOT}/backups:/host-backups:ro",
        "--entrypoint", "python3", "bagman-api",
        "/host-ops/restore_objects.py", container_path,
    )
    run(cmd)


# ---------------------------------------------------------------------
# final-teardown verification (PID §60 step 20 / this work item's own
# "confirm nothing bagman-* remains" requirement)
# ---------------------------------------------------------------------


def docker_query(*args: str) -> list[str]:
    result = run_capture(["docker", *args])
    return [line for line in result.stdout.splitlines() if line.strip()]


def remaining_bagman_containers() -> list[str]:
    return docker_query("ps", "-a", "--filter", "name=bagman-", "--format", "{{.Names}}")


def remaining_bagman_volumes() -> list[str]:
    return docker_query("volume", "ls", "--filter", "name=bagman-", "--format", "{{.Name}}")


def remaining_bagman_networks() -> list[str]:
    return docker_query("network", "ls", "--filter", "name=bagman-net", "--format", "{{.Name}}")


def remaining_bagman_images() -> list[str]:
    return docker_query("images", "--filter", "reference=bagman-api*", "--format", "{{.Repository}}:{{.Tag}}")


def remove_bagman_api_image() -> None:
    images = remaining_bagman_images()
    for image in images:
        run(["docker", "rmi", image], check=False)


def section(title: str) -> None:
    print("\n" + "=" * 78)
    print(title)
    print("=" * 78)
