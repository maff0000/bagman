"""CD-5 Gate-1 — ENTITY_PROPOSAL/bagman-core controlled isolation
experiment (Matt's ruling, 2026-09-16, following the independent
Auditor's 12/20 finding — see memory/generated/CD5-EVIDENCE-...md §6e).

Phase A: isolated baseline. 50 fresh, STRICTLY SEQUENTIAL (one
invocation at a time — the caller of this script is responsible for
ensuring no other BAGMAN AI load, fast/core/deep, runs concurrently;
this script itself never issues concurrent requests) ENTITY_PROPOSAL
invocations through the exact real production path
(POST /internal/ai/tasks against the real bagman-api container), each
against a distinct synthetic fixture drawn from several different
templates (not one repeated prompt). Records, per invocation: fixture
id, latency, transport status, output-schema validity, token usage
(when available), the exact validation error on rejection, and
provider/model provenance — written as JSON Lines to the path given on
the command line (or a default under this script's own directory) so
the full raw record survives independently of this script's own
stdout summary.

Phase B (controlled-load comparison) is a SEPARATE script
(`entity_proposal_load_comparison_experiment.py`) — only run per
Matt's ruling if Phase A reaches exactly 50/50.

Run standalone (assumes the real stack is already up and healthy —
does NOT bring it up itself, unlike the other acceptance scripts,
specifically so this script's own startup never introduces any AI
load of its own before/around the measurement window):

    python3 tests/acceptance/entity_proposal_isolation_experiment.py [output.jsonl]
"""
from __future__ import annotations

import json
import sys
import time
from pathlib import Path

import requests

sys.path.insert(0, str(Path(__file__).resolve().parent))

import _lib  # noqa: E402
from _lib import BASE_URL, register_evidence, run_id, section, wait_for_ready  # noqa: E402

ACTOR_ID = "gate1-entity-proposal-isolation-experiment"
TASK_ID = "ENTITY_PROPOSAL"
ALIAS = "bagman-core"
N = 50

#: Several distinct fixture templates (PID §63-style variety) — cycled
#: through with distinct numbers/amounts per invocation, never one
#: repeated prompt. Deliberately varied document TYPES so the model
#: sees genuinely different real content each time, not a near-copy.
_TEMPLATES = [
    lambda tag, i: f"INVOICE #{tag}-{i:03d}\nBill to: Isolation Experiment Ltd\nSupplier: NoustAI Limited\nTotal Due: GBP {200 + i * 13}.40\nDue Date: 2026-10-{(i % 28) + 1:02d}\n",
    lambda tag, i: f"BANK STATEMENT\nAccount holder: Infosecurs Limited\nStatement period: September 2026\nClosing balance: GBP {5000 + i * 47}.00\nRef: {tag}-{i:03d}\n",
    lambda tag, i: f"EXPENSE CLAIM\nClaimant: M. Scott\nEntity: Matthew Scott (personal)\nCategory: Travel\nAmount: GBP {40 + i * 3}.75\nClaim ID: {tag}-{i:03d}\n",
    lambda tag, i: f"SUPPLIER RECEIPT\nMerchant: Office Supplies Co\nBilled entity: NoustAI Limited\nAmount: GBP {15 + i * 2}.99\nReceipt #: {tag}-{i:03d}\n",
    lambda tag, i: f"SERVICE CONTRACT EXTRACT\nParties: Infosecurs Limited and Vendor Co\nContract value: GBP {10000 + i * 250}.00\nRef: {tag}-{i:03d}\n",
]


def _run_one(tag: str, i: int) -> dict:
    template = _TEMPLATES[i % len(_TEMPLATES)]
    content = template(tag, i).encode()
    fixture_id = f"{tag}-{i:03d}"

    evidence = register_evidence(
        content=content,
        original_name=f"isolation-{fixture_id}.txt",
        actor_id=ACTOR_ID,
        entity_hint="GATE1_ISOLATION_EXPERIMENT",
    )

    started = time.monotonic()
    try:
        response = requests.post(
            f"{BASE_URL}/internal/ai/tasks",
            json={
                "task_id": TASK_ID,
                "task_version": 1,
                "input_references": {"evidence_id": evidence["evidence_id"]},
                "actor_type": "SYSTEM",
                "actor_id": ACTOR_ID,
            },
            timeout=90,
        )
        wall_ms = int((time.monotonic() - started) * 1000)
        transport_ok = response.status_code == 200
        response.raise_for_status()
        body = response.json()
    except requests.RequestException as exc:
        return {
            "fixture_id": fixture_id,
            "template_index": i % len(_TEMPLATES),
            "evidence_id": evidence["evidence_id"],
            "transport_ok": False,
            "transport_error": str(exc)[:500],
            "wall_ms": int((time.monotonic() - started) * 1000),
        }

    record = {
        "fixture_id": fixture_id,
        "template_index": i % len(_TEMPLATES),
        "evidence_id": evidence["evidence_id"],
        "ai_invocation_id": body["ai_invocation_id"],
        "transport_ok": transport_ok,
        "wall_ms": wall_ms,
        "status": body["status"],
        "capability_alias": body["capability_alias"],
        "provider_model": body.get("provider_model"),
        "latency_ms": body.get("latency_ms"),
        "error_code": body.get("error_code"),
        "validation_result": body.get("validation_result"),
        "usage_metadata": body.get("usage_metadata"),
        "output": body.get("output"),
    }
    return record


def main() -> None:
    out_path = Path(sys.argv[1]) if len(sys.argv) > 1 else Path(__file__).resolve().parent / "entity_proposal_isolation_phase_a.jsonl"

    section("PHASE A — ENTITY_PROPOSAL/bagman-core isolated baseline (50 sequential, no concurrent AI load)")
    ready = wait_for_ready()
    print(f"    /ready -> {ready}")
    print(f"    writing raw per-invocation records to: {out_path}")

    tag = run_id()
    records = []
    with out_path.open("w", encoding="utf-8") as f:
        for i in range(N):
            record = _run_one(tag, i)
            records.append(record)
            f.write(json.dumps(record) + "\n")
            f.flush()
            mark = "OK" if record.get("status") == "SUCCEEDED" else f"FAIL({record.get('error_code') or record.get('transport_error')})"
            print(f"    [{i + 1:02d}/{N}] fixture={record['fixture_id']} {mark} latency_ms={record.get('latency_ms')} wall_ms={record.get('wall_ms')}")

    section("PHASE A SUMMARY")
    transport_ok = sum(1 for r in records if r.get("transport_ok"))
    succeeded = sum(1 for r in records if r.get("status") == "SUCCEEDED")
    latencies = sorted(r["latency_ms"] for r in records if r.get("status") == "SUCCEEDED" and r.get("latency_ms") is not None)
    sla_ms = 30_000  # ENTITY_PROPOSAL_V1.timeout_seconds == 30, unchanged (ai/tasks.py)
    within_sla = sum(1 for ms in latencies if ms <= sla_ms)

    print(f"    transport success : {transport_ok}/{N}")
    print(f"    exact-schema success (status == SUCCEEDED) : {succeeded}/{N}")
    print(f"    within existing {sla_ms}ms SLA (of the {succeeded} SUCCEEDED) : {within_sla}/{succeeded if succeeded else 0}")
    if latencies:
        p50 = latencies[len(latencies) // 2]
        p95 = latencies[min(len(latencies) - 1, int(len(latencies) * 0.95))]
        print(f"    latency (SUCCEEDED only) : p50={p50}ms p95={p95}ms min={latencies[0]}ms max={latencies[-1]}ms")

    failures = [r for r in records if r.get("status") != "SUCCEEDED"]
    if failures:
        print(f"\n    {len(failures)} FAILURE(S) — exact validation errors, never rerun/discarded:")
        for r in failures:
            errs = (r.get("validation_result") or {}).get("errors") if r.get("validation_result") else None
            print(f"      fixture={r['fixture_id']} status={r.get('status')} error_code={r.get('error_code')} "
                  f"transport_error={r.get('transport_error')} validation_errors={errs}")
            if r.get("output"):
                print(f"        raw non-conforming output: {json.dumps(r['output'])[:400]}")

    acceptance = transport_ok == N and succeeded == N and within_sla == succeeded
    print(f"\nPHASE A ACCEPTANCE (50/50 transport + 50/50 exact-schema + all within SLA): {'PASS' if acceptance else 'FAIL'}")
    print(f"Raw per-invocation JSONL record: {out_path}")

    if not acceptance:
        sys.exit(1)


if __name__ == "__main__":
    main()
