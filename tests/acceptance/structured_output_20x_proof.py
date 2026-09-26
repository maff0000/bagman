"""CD-5 Gate-1 closure — 20x structured-output real acceptance proof
(Matt's ruling, 2026-09-16, following HELM's BAGMAN_LOCAL_AI_TASKS_RED
finding and the authorised per-request output_schema delta in
`ai/providers/litellm/client.py`/`ai/gateway/background.py`).

**PARTIALLY DEPRECATED (CD-6 §103 Inference Architecture Ruling,
2026-09-26)**: section 4 below ("bagman-deep — reconfirm existing GREEN
path unaffected by this delta") exercises `bagman-deep`, a routine
Trinity-hosted escalation tier CD-6 §103 has now RETIRED — see
`ai.invocation.BACKGROUND_CAPABILITY_ALIASES`'s own docstring for the
full history. Calling `capability_alias='bagman-deep'` today would
correctly be REJECTED by `ai.providers.litellm.client
.validate_capability_alias` (it is no longer a member of the closed
set) — this section's own "GREEN — unaffected"/"REGRESSED" verdict
language no longer means what it did when this script was written; do
NOT run this script and interpret section 4's outcome as meaningful
without updating it first. Sections 1-3 (`bagman-fast`/`bagman-core`
structured-output proofs) are UNCHANGED and still valid — `bagman-fast`/
`bagman-core` are unaffected by this ruling (still the same two
generation profiles against the same Mac-resident model). This
section is preserved as historical evidence (this project's own
"never delete, only mark superseded" convention), not rewritten to
target `trinity-core` instead — that decision (and the required
one-time `trinity-core` compatibility validation, PID §103.4 item 5) is
explicitly the PL's own separate, later, live step; see this
delivery's own report for why this particular section was left alone
rather than mechanically adapted like `trinity_escalation_live_proof.py`
was.

Real, directly-runnable script (see `tests/acceptance/README.md`). No
mocks: drives the REAL running Docker Compose stack's REAL `bagman-api`
container against HELM's real dedicated BAGMAN AI appliance
(`http://192.168.11.4:4100`), using the real `bagman-*` virtual key.

What this proves
------------------
* `bagman-fast` / `DOCUMENT_TYPE_PROPOSAL`: 20 real invocations, each
  independently required to reach `SUCCEEDED` (transport success +
  syntactically valid JSON + exact `output_schema` validity — WI-1's
  own `run_background_task` only reaches `SUCCEEDED` after all three,
  see `ai/gateway/background.py`'s own module docstring) inside the
  task's existing, UNCHANGED `timeout_seconds` SLA (a `FAILED`/
  `LITELLM_TIMEOUT` outcome is only possible when the SLA was actually
  exceeded — the client itself enforces this, so `SUCCEEDED` already
  implies "inside SLA").
* `bagman-fast` / `DOCUMENT_SUMMARY`: a smaller, representative batch
  through the SAME alias, proving its DIFFERENT schema (`summary`
  field, not `proposed_type`) is genuinely respected — not a
  coincidentally-shared shape.
* `bagman-core` / `ENTITY_PROPOSAL`: 20 real invocations, same bar.
* `bagman-deep`: one real reconfirmation via the existing
  `trinity_escalation_live_proof.py`-equivalent direct adapter call,
  proving this delta did not regress the already-GREEN tier (no task
  contract currently prefers `bagman-deep`, per that script's own
  documented gap).

Every outcome — pass or fail — is recorded and reported honestly; nothing
here retries a FAILED invocation silently or discards an unwelcome
result. If fewer than 20/20 succeed, this script says so plainly rather
than declaring victory.

Run standalone (brings the stack up itself first):

    python3 tests/acceptance/structured_output_20x_proof.py
"""
from __future__ import annotations

import json
import sys
import time
from pathlib import Path

import requests

sys.path.insert(0, str(Path(__file__).resolve().parent))

import _lib  # noqa: E402
from _lib import (  # noqa: E402
    BASE_URL,
    compose_build,
    compose_up_wait,
    register_evidence,
    run_id,
    section,
    wait_for_ready,
)

ACTOR_ID = "wi-gate1-structured-output-20x-proof"


def _run_task(task_id: str, evidence_id: str) -> dict:
    started = time.monotonic()
    response = requests.post(
        f"{BASE_URL}/internal/ai/tasks",
        json={
            "task_id": task_id,
            "task_version": 1,
            "input_references": {"evidence_id": evidence_id},
            "actor_type": "SYSTEM",
            "actor_id": ACTOR_ID,
        },
        timeout=90,
    )
    wall_ms = int((time.monotonic() - started) * 1000)
    response.raise_for_status()  # transport success at the HTTP-endpoint layer
    body = response.json()
    body["_wall_ms"] = wall_ms
    return body


def _register_fixture(tag: str, i: int) -> str:
    evidence = register_evidence(
        content=f"INVOICE #{tag}-{i:03d}\nBill to: Gate-1 structured-output proof\nTotal Due: ${100 + i}.00\n".encode(),
        original_name=f"gate1-structured-{tag}-{i:03d}.txt",
        actor_id=ACTOR_ID,
        entity_hint="GATE1_STRUCTURED_OUTPUT_PROOF",
    )
    return evidence["evidence_id"]


def _run_batch(*, task_id: str, alias: str, tag: str, count: int) -> list[dict]:
    results = []
    for i in range(count):
        evidence_id = _register_fixture(tag, i)
        invocation = _run_task(task_id, evidence_id)
        assert invocation["capability_alias"] == alias, (
            f"expected capability_alias={alias!r}, got {invocation['capability_alias']!r}"
        )
        outcome = {
            "i": i,
            "ai_invocation_id": invocation["ai_invocation_id"],
            "status": invocation["status"],
            "error_code": invocation.get("error_code"),
            "latency_ms": invocation.get("latency_ms"),
            "wall_ms": invocation["_wall_ms"],
            "output": invocation.get("output"),
        }
        results.append(outcome)
        mark = "OK" if outcome["status"] == "SUCCEEDED" else f"FAIL({outcome['error_code']})"
        print(f"    [{task_id} #{i + 1:02d}/{count}] {mark} latency_ms={outcome['latency_ms']} wall_ms={outcome['wall_ms']}")
    return results


def _summarise(label: str, results: list[dict]) -> bool:
    succeeded = [r for r in results if r["status"] == "SUCCEEDED"]
    failed = [r for r in results if r["status"] != "SUCCEEDED"]
    n = len(results)
    latencies = sorted(r["latency_ms"] for r in succeeded if r["latency_ms"] is not None)
    p50 = latencies[len(latencies) // 2] if latencies else None
    p95_idx = min(len(latencies) - 1, int(len(latencies) * 0.95)) if latencies else None
    p95 = latencies[p95_idx] if latencies else None
    print(f"\n    {label}: {len(succeeded)}/{n} SUCCEEDED")
    if latencies:
        print(f"    {label} latency (SUCCEEDED only): p50={p50}ms p95={p95}ms min={latencies[0]}ms max={latencies[-1]}ms")
    if failed:
        for r in failed:
            print(f"    {label} FAILURE #{r['i']}: error_code={r['error_code']} ai_invocation_id={r['ai_invocation_id']}")
    return len(succeeded) == n


def main() -> None:
    section("SETUP — bring up the real BAGMAN Docker Compose stack (rebuilt with the structured-output delta)")
    compose_build()
    compose_up_wait()
    ready = wait_for_ready()
    print(f"    /ready -> {ready}")

    tag = run_id()

    section("1. bagman-fast / DOCUMENT_TYPE_PROPOSAL — 20 real invocations")
    fast_results = _run_batch(task_id="DOCUMENT_TYPE_PROPOSAL", alias="bagman-fast", tag=tag, count=20)
    fast_all_ok = _summarise("DOCUMENT_TYPE_PROPOSAL (bagman-fast)", fast_results)

    section("2. bagman-fast / DOCUMENT_SUMMARY — representative batch, proving a DIFFERENT schema is respected")
    summary_results = _run_batch(task_id="DOCUMENT_SUMMARY", alias="bagman-fast", tag=tag, count=5)
    summary_all_ok = _summarise("DOCUMENT_SUMMARY (bagman-fast)", summary_results)
    for r in summary_results:
        if r["status"] == "SUCCEEDED":
            assert "summary" in r["output"], f"DOCUMENT_SUMMARY output missing 'summary' field: {r['output']}"
            assert "proposed_type" not in r["output"], (
                f"DOCUMENT_SUMMARY output contains 'proposed_type' — the DOCUMENT_TYPE_PROPOSAL schema leaked "
                f"across tasks sharing the same alias: {r['output']}"
            )
    print("    CONFIRMED (for every SUCCEEDED run): DOCUMENT_SUMMARY's own schema ('summary', not 'proposed_type') was honoured.")

    section("3. bagman-core / ENTITY_PROPOSAL — 20 real invocations")
    core_results = _run_batch(task_id="ENTITY_PROPOSAL", alias="bagman-core", tag=tag, count=20)
    core_all_ok = _summarise("ENTITY_PROPOSAL (bagman-core)", core_results)

    # DEPRECATED (CD-6 §103, 2026-09-26): bagman-deep is RETIRED — see
    # this module's own docstring's prominent deprecation note above.
    # `client.complete(capability_alias='bagman-deep', ...)` below will
    # now correctly raise ValidationError before any network I/O
    # (validate_capability_alias's closed-set check) rather than
    # exercising a live GREEN path — this section is preserved as
    # historical evidence only, not runnable-and-meaningful today.
    # TODO(PL): decide whether to retarget this section at
    # `trinity-core` as part of the separate, later, live
    # trinity-core compatibility validation (PID §103.4 item 5).
    section("4. bagman-deep — reconfirm existing GREEN path unaffected by this delta [DEPRECATED, see module docstring]")
    deep_script = (
        "import json, os\n"
        "from ai.providers.litellm.client import LiteLLMClient\n"
        "client = LiteLLMClient(\n"
        "    endpoint=os.environ['BAGMAN_LITELLM_ENDPOINT'],\n"
        "    api_key_file=os.environ.get('BAGMAN_LITELLM_API_KEY_FILE', '/run/secrets/litellm_gateway_key'),\n"
        ")\n"
        "result = client.complete(\n"
        "    capability_alias='bagman-deep',\n"
        "    system_instructions='You are a background analysis component. Respond with the single word: OK.',\n"
        f"    evidence_content='Gate-1 structured-output proof {tag} deep-tier reconfirmation',\n"
        "    output_schema={'type': 'object'},\n"
        "    timeout_seconds=30.0,\n"
        ")\n"
        "print('DEEP_RESULT_JSON:' + json.dumps({'status': result.status.value, 'content': result.content, 'latency_ms': result.latency_ms}))\n"
    )
    stdout = _lib.compose_exec_python(deep_script)
    deep_result = _lib.parse_marker_json(stdout, "DEEP_RESULT_JSON:")
    print(f"    bagman-deep result: {deep_result}")
    deep_ok = deep_result["status"] == "OK"
    print(f"    bagman-deep: {'GREEN — unaffected' if deep_ok else 'REGRESSED — investigate immediately'}")

    section("SUMMARY")
    print(f"    bagman-fast  / DOCUMENT_TYPE_PROPOSAL (20x) : {'20/20 GREEN' if fast_all_ok else 'NOT 20/20 — see failures above'}")
    print(f"    bagman-fast  / DOCUMENT_SUMMARY (5x, distinct schema) : {'GREEN' if summary_all_ok else 'NOT ALL GREEN — see failures above'}")
    print(f"    bagman-core  / ENTITY_PROPOSAL (20x)        : {'20/20 GREEN' if core_all_ok else 'NOT 20/20 — see failures above'}")
    print(f"    bagman-deep  reconfirmation                 : {'GREEN' if deep_ok else 'REGRESSED'}")
    overall = fast_all_ok and summary_all_ok and core_all_ok and deep_ok
    print(f"\nCD-5 GATE-1 STRUCTURED-OUTPUT 20x PROOF: {'PASS' if overall else 'NOT YET PASSING'} — result recorded honestly above")
    print("(stack left running, fully healthy)")

    if not overall:
        sys.exit(1)


if __name__ == "__main__":
    main()
