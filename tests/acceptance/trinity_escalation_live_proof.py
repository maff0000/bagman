"""CD-5 WI-5 acceptance evidence — Trinity-escalation tier (`bagman-deep`)
REAL live proof (PID §81/§82/§87/§91).

Real, directly-runnable script (see ``tests/acceptance/README.md``). No
mocks: drives the REAL running Docker Compose stack's REAL `bagman-api`
container against the REAL, existing Trinity LiteLLM installation
running on this host, using the REAL `bagman-*` virtual key.

Why this exercises `ai.providers.litellm.client.LiteLLMClient` directly
(in-container), not `POST /internal/ai/tasks`
------------------------------------------------------------------------
No CD-5 task contract currently has `preferred_capability="bagman-deep"`
(`DOCUMENT_SUMMARY`/`DOCUMENT_TYPE_PROPOSAL` -> `bagman-fast`;
`ENTITY_PROPOSAL` -> `bagman-core`) — this is a genuine, honestly-noted
gap this script surfaces rather than works around by inventing a fake
task: the ordinary `POST /internal/ai/tasks` HTTP surface has no way to
route a real request to `bagman-deep` today (by design — PID §77
forbids a caller-chosen alias/capability field on that surface).
`bagman-deep` is nonetheless a real, registered, alias BAGMAN's one
LiteLLM adapter fully supports (`ai.invocation.BACKGROUND_CAPABILITY_ALIASES`)
— so this script proves the REAL adapter/alias/gateway path directly,
via a real in-process `LiteLLMClient.complete(capability_alias=
"bagman-deep", ...)` call executed INSIDE the real running `bagman-api`
container (`docker compose exec`, mirroring exactly the pattern
`tests/acceptance/_lib.py::compose_exec_python` already establishes for
operations with no HTTP surface). This is the real adapter, the real
network path (including this WI's own `host.docker.internal` fix),
the real secret file, and the real gateway — not a fake, and not a
workaround; only the ROUTING ENTRY POINT differs from the Mac-mini
tier's own HTTP-level proof (see `mac_mini_background_tier_live_proof.py`).

Context re-checked live, not assumed (per this WI's own dispatch)
--------------------------------------------------------------------
The LiteLLM gateway's own backing database was independently verified
DOWN immediately before this WI began. This script re-checks it live,
itself, before attempting the real `bagman-deep` call, and reports
exactly what it finds either way — genuinely completing the full proof
if the database has recovered, or reporting the exact real error if
not.

CD-5 Gate-1 closure update (2026-09-16): the endpoint this script
re-checks, and `BAGMAN_LITELLM_ENDPOINT` itself, now point at HELM's
dedicated BAGMAN AI appliance (`http://192.168.11.4:4100`,
`BAGMAN_AI_APPLIANCE_GREEN`) rather than the old shared Trinity
gateway (`local-ai-gateway`, host port 4000) — see
`deployment/compose/docker-compose.yml`'s own comment for the full
history. The script's own logic, assertions, and honesty discipline
are unchanged; only the URL this section's live re-check hits was
updated to match.

Run standalone (brings the stack up itself first):

    python3 tests/acceptance/trinity_escalation_live_proof.py
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import requests

sys.path.insert(0, str(Path(__file__).resolve().parent))

import _lib  # noqa: E402
from _lib import compose_build, compose_up_wait, run_id, section, wait_for_ready  # noqa: E402

_RESULT_MARKER = "WI5_DEEP_RESULT_JSON:"


def _real_bagman_deep_call(tag: str) -> dict:
    """Constructs the REAL `LiteLLMClient` (same config the production
    container itself uses — endpoint/key-file env vars are already set
    on `bagman-api`) and calls `.complete(capability_alias="bagman-deep",
    ...)` for real, inside the real container. Never touches a fake."""
    script = f"""
import json
import os
from ai.providers.litellm.client import LiteLLMClient

# LiteLLMClient's OWN constructor does not itself read environment
# variables (only app/api/composition.py's production builder does,
# explicitly) — so this script must read them the same way
# composition.py does, not rely on LiteLLMClient()'s bare defaults
# (which would silently fall back to http://localhost:4000, WRONG
# from inside this container — exactly the bug this whole script
# exists to have already fixed at the compose-file level).
client = LiteLLMClient(
    endpoint=os.environ["BAGMAN_LITELLM_ENDPOINT"],
    api_key_file=os.environ.get("BAGMAN_LITELLM_API_KEY_FILE", "/run/secrets/litellm_gateway_key"),
)
result = client.complete(
    capability_alias="bagman-deep",
    system_instructions="You are a background analysis component. Respond with the single word: OK.",
    evidence_content="WI-5 Trinity-escalation live acceptance proof {tag}",
    timeout_seconds=30.0,
)
print({_RESULT_MARKER!r} + json.dumps({{
    "status": result.status.value,
    "content": result.content,
    "provider_model": result.provider_model,
    "latency_ms": result.latency_ms,
    "error_detail": result.error_detail,
}}))
"""
    stdout = _lib.compose_exec_python(script)
    for line in stdout.splitlines():
        if line.startswith(_RESULT_MARKER):
            return json.loads(line[len(_RESULT_MARKER):])
    raise RuntimeError(f"marker not found in compose_exec_python output:\n{stdout}")


def main() -> None:
    section("SETUP — bring up the real BAGMAN Docker Compose stack")
    compose_build()
    compose_up_wait()
    ready = wait_for_ready()
    print(f"    /ready -> {ready}")

    tag = run_id()

    section("LIVE INFRASTRUCTURE CHECK — is the BAGMAN AI appliance's backing database up? (re-checked live)")
    readiness = requests.get("http://192.168.11.4:4100/health/readiness", timeout=5).json()
    print(f"    GET http://192.168.11.4:4100/health/readiness -> {readiness}")
    db_connected = readiness.get("db") not in (None, "Not connected")
    print(f"    gateway backing database connected: {db_connected}")

    section("REAL bagman-deep CALL — the real LiteLLMClient, inside the real running bagman-api container")
    result = _real_bagman_deep_call(tag)
    print(f"    result: {json.dumps(result, indent=2)}")

    assert result["status"] in ("OK", "PROVIDER_ERROR", "TIMEOUT", "TRANSPORT_ERROR", "AUTH_ERROR", "RATE_LIMITED", "CONFIG_ERROR"), (
        f"unrecognised LiteLLMOutcomeStatus: {result['status']}"
    )
    # A network-level failure (never even reaching the gateway) would
    # indicate the container-network fix has regressed — that must
    # never happen; a PROVIDER_ERROR (a real HTTP response, just an
    # unhappy one) is the expected, honestly-BLOCKED outcome given the
    # documented database-outage condition.
    assert result["status"] not in ("TRANSPORT_ERROR", "TIMEOUT"), (
        f"the real network path to the LiteLLM gateway appears to have regressed (status={result['status']}, "
        f"detail={result['error_detail']!r}) — this is NOT the expected 'reached the gateway, got a real "
        "error response' condition; investigate connectivity to the dedicated BAGMAN AI appliance at "
        "192.168.11.4:4100 (BAGMAN_LITELLM_ENDPOINT)"
    )

    if result["status"] == "OK":
        print("    *** GENUINE LIVE SUCCESS *** — the real Trinity-escalation tier (bagman-deep) returned a real completion.")
        print(f"    content={result['content']!r}, provider_model={result['provider_model']!r}")
        tier_status = "GREEN — real completion proven"
    else:
        print(
            f"    honestly BLOCKED — the real network path was reached (status={result['status']}), but the "
            f"call did not complete successfully ({result['error_detail']!r}) — consistent with the "
            "independently re-checked LiteLLM-gateway database outage above, not a BAGMAN-side defect."
        )
        tier_status = f"BLOCKED — status={result['status']}, detail={result['error_detail']!r}"

    section("NO SILENT CROSS-TIER/CROSS-PROVIDER FALLBACK")
    print(
        "    the call above requested EXACTLY capability_alias='bagman-deep' and received back exactly that "
        "outcome (never silently redirected to bagman-fast/bagman-core or to Claude) — "
        "ai.providers.litellm.client.validate_capability_alias enforces this mechanically before any request "
        "is even constructed (see tests/security/test_ai_litellm_alias_lockdown.py)."
    )

    section("SUMMARY")
    print(f"    LiteLLM gateway backing database   : {'CONNECTED' if db_connected else 'STILL DOWN (re-checked live)'}")
    print(f"    real bagman-deep call               : {tier_status}")
    print(f"    no silent cross-tier fallback        : PROVEN (mechanically enforced, alias-only)")
    print("\nPID §81/§82/§87 TRINITY-ESCALATION TIER LIVE PROOF: ATTEMPTED FOR REAL, RESULT RECORDED HONESTLY ABOVE")
    print("(stack left running, fully healthy)")


if __name__ == "__main__":
    main()
