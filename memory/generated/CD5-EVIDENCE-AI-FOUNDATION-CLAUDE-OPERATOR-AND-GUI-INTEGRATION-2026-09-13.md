# BAGMAN CD-5 — AI Foundation, Claude Operator & GUI Integration — Delivery Evidence

**PID:** `PID.md` v5 ("BAGMAN PID v5 — AI Foundation, Claude Operator & GUI Integration", amended for the locked three-tier topology)
**Delivery branch:** `cd-5/ai-foundation-claude-operator-and-gui-integration`
**This work item's branch:** `cd-5/wi5-evaluation-hardening-and-acceptance` (WI-5, the final work item)
**Base for WI-5:** `7a30d8b` (WI-1 through WI-4 already merged onto the CD-5 delivery branch)
**PL:** Bagman persona (Trinity ecosystem), operating under Forge doctrine (`/srv/forge`) in hub-model mode.
**Date:** 2026-09-13.

Per Forge doctrine this evidence trail lives in the repository, not only in session/fabric state. **This draft is written by the WI-5 Forge Engineer; it is not the final word** — the PL reviews, may amend it, commits it, and dispatches a fresh independent Auditor for the whole CD-5 delivery before any verdict is adjudicated. Nothing in this file should be read as a self-issued verdict.

---

## 1. Delivery summary

Five PID-prescribed work items (PID §89):

| Work item | Commit(s) | Scope |
|---|---|---|
| WI-1 | `14f46e5` | `ai/`: `AIInvocation` domain model + PID §28 state machine, a real database-enforced one-active-invocation-per-subject concurrency guard (generated column + partial unique index), the closed `bagman-fast`/`bagman-core`/`bagman-deep` alias vocabulary, `ai/tasks.py`'s typed task contract/registry (`DOCUMENT_SUMMARY`, `DOCUMENT_TYPE_PROPOSAL`, `ENTITY_PROPOSAL`, `OPERATOR_DOCUMENT_REVIEW`), `contracts/ai/bagman.ai_invocation.v1.schema.json`, Postgres persistence + migration. No provider adapters, no GUI, no live orchestration. |
| WI-2 | `c308f1f` | `ai/providers/litellm/` (the one alias-only LiteLLM adapter, mechanical `trinity-*`/raw-model-name rejection, full fail-closed outcome space, one bounded connect-phase-only retry), `ai/prompts/*/v1.md`, `ai/gateway/background.py` (`run_background_task`'s full lifecycle orchestration), `POST /internal/ai/tasks`/`GET /internal/ai/invocations[/{id}]`/`GET /internal/ai/health`. Real acceptance attempted once, honestly BLOCKED (LiteLLM gateway backing database down). |
| WI-3 | `548f41a` | `ai/providers/claude/` (the Anthropic Messages API adapter), `agent/tools/` (the fixed, closed 8-tool registry — `FORBIDDEN_TOOL_NAMES` enforced at construction, `READ`/`ANALYSE`-only authority classes), `agent/bagman/orchestrator.py` (`handle_operator_message`'s bounded tool-calling loop, additive `ASK_BAGMAN` v1 task), `POST /internal/operator/chat`. Real Claude acceptance checked and confirmed BLOCKED (no Anthropic key file). |
| WI-4 | `cf6fa90` | GUI modularisation (`app/api/static/{shell,shared,features/{overview,documents,ai}}`), the Documents AI panel (PID §45), Ask BAGMAN drawer (PID §42-44), Overview AI status (PID §46-47), a `primary_input_reference` list filter, `GET /internal/ai/health`'s additive `claude` key, and a dev-mode `FakeLiteLLMClient` usability fix. |
| WI-5 (this WI) | *(uncommitted — PL commits after review)* | Evaluation, hardening & acceptance: bidi filename hardening (PID §66) closed; the mandatory `ai/evaluation/` harness (PID §59-61); a genuine real-threaded concurrency proof against the real database (PID §73); **two real, previously-undiscovered infrastructure defects found and fixed** (the `bagman-api` Docker image was missing `ai/`/`agent/` entirely; the container-to-LiteLLM-gateway network path was broken); five new real-stack acceptance scripts covering all three AI tiers, live prompt injection, and AI-surfaces browser acceptance; a repo-wide `trinity-*` alias sweep; this evidence file and the CHANGELOG entry. |

No mailbox, bank, accounting, or billing connectivity was introduced anywhere in CD-5. No second LiteLLM/inference-control-plane architecture was introduced (PID §8). `entity_id` remains `None` on every registered `EvidenceItem` (unchanged from CD-4).

---

## 2. WI-5 — what was actually done

### 2.1 Filename bidi hardening (PID §66) — CLOSED

Closed the CD-4 Auditor's carried-forward backlog item
(`bagman:backlog:filename_bidi_spoofing_hardening`, first flagged during the CD-4 PR #4 delta round's focused Auditor pass — see `app/api/http_headers.py`'s own module docstring).

**The gap:** `services/evidence/intake/filename_safety.py::is_filename_safe`'s NFKC-normalisation round-trip check catches compatibility-equivalence tricks (ligatures, full/half-width variants) but does not touch Unicode bidi/directional-format control characters at all — these survive completely untouched, so a filename such as `"invoice" + U+202E (RLO) + "cod.exe"` (renders, right-to-left from the override onward, as something a human reads as ending `...exe.doc`, while the actual byte sequence still ends literally in `.exe`) passed cleanly before this fix.

**The fix (`services/evidence/intake/filename_safety.py`):**
* A new, explicitly-documented, explicitly-justified rejection set — every code point in Unicode's "Explicit Directional Formatting Characters" family: the 9 the PID names verbatim (U+202A LRE, U+202B RLE, U+202C PDF, U+202D LRO, U+202E RLO, U+2066 LRI, U+2067 RLI, U+2068 FSI, U+2069 PDI) plus three narrower directional marks from the same functional family (U+200E LRM, U+200F RLM, U+061C ALM) — judged genuinely relevant to spoofing (same "invisible/rendering-altering" property) and documented as such in the module's own docstring. No broader confusable-script/homoglyph defence was added — that is a separate, much larger, deliberately out-of-scope problem (PID §66's own "bounded security-hardening item, not open-ended" framing).
* Built via `chr(codepoint)` rather than embedding the literal characters in the source file itself — deliberately, so this `.py` file's own source never visually reorders/hides itself in an editor (documented directly in the code).
* `is_filename_safe`/`assert_filename_safe` both updated; the error message now names "bidi/directional-format control character" as one of its rejection reasons.

**Tests added:**
* `tests/integration/test_intake_filename_safety.py`: a parametrised test asserting all 12 characters are rejected at the start, middle, and end of an otherwise-ordinary filename, plus a dedicated `test_bidi_override_spoofed_extension_attack_is_rejected` reproducing the exact "invoice + RLO + gnp.exe" pattern.
* `tests/integration/test_intake_validation_pipeline.py::test_bidi_override_spoofed_extension_filename_is_rejected_before_any_scanning` — the full end-to-end proof, exactly mirroring the existing path-traversal fixture's own shape: `REJECTED`/`failure_code == "UNSAFE_FILENAME"`, scanner never reached (`_RaisingScanner`).

**Confirmed still correct, unchanged:** `app/api/http_headers.py::safe_content_disposition_header`'s own defense-in-depth behaviour for this class of input — its existing test suite (`tests/app_api/test_evidence_download_headers.py`, 12 tests) re-run unchanged, still passing. This module already excluded non-ASCII bidi characters from its ASCII fallback and now, with intake rejecting the filename earlier, never even receives one through the real pipeline — but its own independent test for this (added by the CD-4 delta-round Auditor) still passes on its own terms.

**This closes the backlog item fully.**

### 2.2 Evaluation harness (`ai/evaluation/`, PID §59-61) — mandatory, built

`ai/evaluation/` (new package): `fixtures.py`, `harness.py`, `injection_reuse.py`, `run.py`, `component.yaml`.

**Design:** every fixture pairs a synthetic "document" (plain text) with a scripted, deterministic `FakeLiteLLMClient` response, and runs it through the REAL `ai.gateway.background.run_background_task` orchestration function (never a reimplementation of it), then asserts on the resulting real `AIInvocation`. Nine golden fixtures across the five PID §59 categories, plus prompt-injection resilience (reused, not reimplemented):

| Category | Fixtures | What it proves |
|---|---|---|
| `STRUCTURED_OUTPUT_VALIDITY` | 1 | A well-formed response validates cleanly against the task's own `output_schema` (`ai.tasks.validate_task_output`, reused). |
| `EXPECTED_CLASSIFICATION` | 2 | The harness's OWN scoring logic can tell a scripted-correct answer from a scripted-incorrect one for the same golden document. **Explicitly NOT a real-model-quality benchmark** — no real model is reachable today (see §2.4/§2.5 below) — this is stated plainly in the module's own docstring, not glossed over. |
| `ABSTENTION_UNCERTAINTY` | 2 | A genuinely ambiguous document scripted with an honest, low-confidence "UNKNOWN" is recognised as `ABSTENTION`, reported DISTINCTLY from a confident WRONG answer to a similar document (scripted `CONFIDENT_INCORRECT`) — the exact "distinguish from a confident wrong answer" requirement named in the dispatch. |
| `MALFORMED_OUTPUT` | 2 | Non-JSON content and valid-JSON-but-schema-invalid content are both recorded `FAILED`/`OUTPUT_NOT_JSON` or `FAILED`/`OUTPUT_SCHEMA_INVALID` — never silently repaired (PID §76). |
| `TIMEOUT_ERROR_HANDLING` | 2 | A scripted `TIMEOUT` and a scripted `TRANSPORT_ERROR` are each recorded as their own distinct outcome class — checked structurally: a transport failure carries NO `validation_result` at all (proven never to be conflated with a malformed-output failure, which always does). |
| Prompt-injection resilience | reused | `ai/evaluation/injection_reuse.py` runs `tests/integration/test_prompt_injection_structural.py` + `tests/security/test_prompt_injection_ask_bagman.py` as a real `pytest` subprocess and folds the result in — reused, never duplicated, per the dispatch's own instruction. |

Entirely deterministic — no live credentials, no network access (PID §61). Runnable two ways: `python3 -m ai.evaluation.run` (a CLI, exit code 0/1) and `pytest tests/integration/test_ai_evaluation_harness.py` (pytest-collected, so `pytest tests/` alone already exercises it). The pytest wrapper also proves the harness's own pass/fail machinery is not vacuous (a deliberately-broken fixture is proven to be reported `FAILED`, mirroring the CD-4 Auditor's own "an assert-less test always passes" lesson, applied one level up).

**Sample real run output** (`python3 -m ai.evaluation.run`):
```
==============================================================================
CD-5 AI EVALUATION HARNESS REPORT (PID §59-61)
==============================================================================

[STRUCTURED_OUTPUT_VALIDITY] 1/1 passed
  [PASS] structured_output_valid_document_summary: well-formed output validated cleanly against the task's own output_schema

[EXPECTED_CLASSIFICATION] 2/2 passed
  [PASS] expected_classification_correct_invoice: classification scoring verdict: CORRECT (proposed_type='INVOICE')
  [PASS] expected_classification_detects_a_wrong_answer: classification scoring verdict: INCORRECT (deliberately scripted wrong, to prove the harness detects it) (proposed_type='CONTRACT')

[ABSTENTION_UNCERTAINTY] 2/2 passed
  [PASS] abstention_on_genuinely_ambiguous_document: recognised as ABSTENTION (confidence=0.1, proposed_type=UNKNOWN) — reported distinctly from a confident wrong answer
  [PASS] confident_incorrect_answer_is_distinguished_from_abstention: recognised as CONFIDENT_INCORRECT (confidence=0.9, proposed_type='STATEMENT' != golden truth 'RECEIPT') — reported distinctly from ABSTENTION

[MALFORMED_OUTPUT] 2/2 passed
  [PASS] malformed_output_not_json_at_all: malformed output correctly flagged FAILED/OUTPUT_NOT_JSON, never silently repaired
  [PASS] malformed_output_valid_json_fails_schema: malformed output correctly flagged FAILED/OUTPUT_SCHEMA_INVALID, never silently repaired

[TIMEOUT_ERROR_HANDLING] 2/2 passed
  [PASS] transport_timeout_is_its_own_outcome_class: ... (LITELLM_TIMEOUT), never conflated with a content-validation failure
  [PASS] transport_error_is_its_own_outcome_class: ... (LITELLM_TRANSPORT_ERROR), never conflated with a content-validation failure

[PROMPT_INJECTION_RESILIENCE (reused WI-2/WI-3 suites)]
  [PASS] reused suites passed: tests/integration/test_prompt_injection_structural.py, tests/security/test_prompt_injection_ask_bagman.py

------------------------------------------------------------------------------
TOTAL: 9/9 golden fixtures passed; prompt-injection reuse suite PASSED
OVERALL: GREEN
------------------------------------------------------------------------------
```

`ai/evaluation/component.yaml` added (`BAGMAN.AI.EVALUATION`); architecture memory regenerated to reflect it.

### 2.3 Concurrency/retry proof against the real database (PID §73/§91)

`tests/persistence/test_ai_invocation_repository.py::test_genuinely_concurrent_threads_racing_the_same_subject_resolve_to_exactly_one_active_invocation` (new): 8 real OS threads, each with its OWN `PostgresAIInvocationRepository`/`Engine` (never shared), synchronised with a `threading.Barrier` and firing `create_invocation` for the exact same `(task_id, task_version, evidence_id)` subject as close to simultaneously as possible.

**Judgment call, documented directly in the test's own module docstring:** unlike CD-4's own `idempotency_and_concurrency_proof.py` (which needed a `docker compose exec`-into-the-running-container trick to force genuine interleaving, because `bagman-api` runs as a single Uvicorn worker with no `await` in its own handler body), this proof did NOT need that: `tests/persistence/` already talks to a REAL, disposable PostgreSQL container over a REAL TCP socket — real Python threads making real socket I/O against a real remote database genuinely release the GIL and interleave. This was verified, not assumed: the test itself inspects each thread's own wall-clock call window and asserts a genuine overlap occurred (`overlap_found`), so this is not merely "8 threads that happened to run one after another."

**Result:** exactly 1 winner (`created`), exactly 7 real `ActiveInvocationConflictError`s (`conflicted`) — resolved by the real `uq_ai_invocations_active_subject` database constraint, never merely the application-level pre-check. Re-run cleanly:
```
$ pytest tests/persistence/test_ai_invocation_repository.py -q -k concurrent
1 passed, 21 deselected in 2.38s
```
Full `tests/persistence/` suite re-run afterward: 104 passed, 17 skipped (unrelated, pre-existing environment-conditional skips) — no regression.

A new full HTTP-plus-real-threads acceptance script (mirroring CD-4's own `idempotency_and_concurrency_proof.py` pattern exactly) was judged NOT additionally warranted for this narrow item: the real-database-constraint property PID §73 asks for is what the persistence-layer proof above already demonstrates against the real constraint directly, and no `bagman-api`-single-worker-serialisation gap analogous to CD-4's own applies here (this test never goes through the single-worker HTTP process at all).

### 2.4 CRITICAL FINDING #1 — the `bagman-api` Docker image was missing `ai/` and `agent/` entirely

Investigating the container-network issue named in this WI's own dispatch, direct inspection of the built `bagman-api:dev` image (`docker run --rm --entrypoint sh bagman-api:dev -c "ls /app"`) showed **no `ai/` and no `agent/` directory at all** — `deployment/docker/api/Dockerfile` was never updated by WI-1/WI-2/WI-3/WI-4 to `COPY` either package into the image, despite `app/api/composition.py`'s production builder importing directly from both (`ai.providers.litellm.client.LiteLLMClient`, `ai.gateway.background.run_background_task`, `ai.providers.claude.client.ClaudeClient`, `agent.tools.handlers.build_default_tool_registry`, ...).

**Consequence, had this shipped as-is:** every real `bagman-api` production container built from any prior CD-5 commit would raise `ModuleNotFoundError` the moment `get_composition()` first built a PRODUCTION composition (lazy, on first request) — i.e. every single `/internal/ai/*` and `/internal/operator/*` endpoint would 500 in the real deployed stack, despite every unit/integration test (which never builds a real Docker image) passing cleanly. This is exactly the class of gap CD-3's own "local-and-CI-green diverging from live/real-deployment-green" lesson warns about, at the container-image level rather than the CI level.

**Fix:** `deployment/docker/api/Dockerfile` — added `COPY ai/ ./ai/` and `COPY agent/ ./agent/`, with a documented rationale directly in the Dockerfile explaining what was found and why. Rebuilt and independently re-verified: `docker run --rm --entrypoint sh bagman-api:dev -c "ls /app/ai; ls /app/agent"` now lists both packages' real contents (`gateway`, `providers`, `tasks.py`, `invocation.py`, `evaluation`, `prompts`, `component.yaml` / `bagman`, `tools`, `policies`, `memory`, `component.yaml`).

### 2.5 CRITICAL FINDING #2 — `bagman-api`'s own container could not reach the real LiteLLM gateway at all (the issue named in this WI's dispatch)

Confirmed exactly as the dispatch described: `deployment/compose/docker-compose.yml`'s `BAGMAN_LITELLM_ENDPOINT: http://localhost:4000` is broken from inside `bagman-api`'s own container — `localhost` there refers to the container itself, not the host.

**Investigated, not assumed:**
* The real Trinity LiteLLM gateway (`local-ai-gateway`, a separate, pre-existing Docker container) publishes port 4000 to the HOST's own network stack (`0.0.0.0:4000`) — confirmed via `docker ps`/`ss -tlnp` — it is not a service of BAGMAN's own Compose project and is not on `bagman-net`.
* `host.docker.internal` + `extra_hosts: ["host.docker.internal:host-gateway"]` is a Docker-Engine-native mechanism (supported since Docker 20.10; this host runs 29.3.0, Linux — not Docker-Desktop-only) — verified working on this exact host via a throwaway container BEFORE editing any BAGMAN file: `docker run --rm --add-host=host.docker.internal:host-gateway alpine curl http://host.docker.internal:4000/health/readiness` returned the real gateway's own JSON.

**Fix applied, entirely within BAGMAN's own compose file** (per PID §15's own "BAGMAN's own compose/deployment config references only the LiteLLM endpoint" framing — this does NOT require joining the separate `local-ai-gateway` container to `bagman-net`, which would be the cross-compose-project change PID §15 reserves for whoever wires the two projects together): `deployment/compose/docker-compose.yml`'s `bagman-api` service now sets `BAGMAN_LITELLM_ENDPOINT: http://host.docker.internal:4000` and adds `extra_hosts: ["host.docker.internal:host-gateway"]`. Fully documented in the compose file's own comments, including exactly what was found broken and why the fix is safe/scoped.

**Verified for real, from inside the real running container**, independently of whether the upstream database was healthy:
```
$ docker exec bagman-api python3 -c "... urlopen('http://host.docker.internal:4000/health/readiness') ..."
STATUS 200 b'{"status":"healthy","db":"Not connected"}'
```
This is a genuine, previously-broken path now genuinely working — confirmed directly, not inferred.

**The Anthropic key (`/srv/bagman-secrets/anthropic_api_key`) was re-checked live and is still absent.** A `secrets:`/`bagman-api.secrets` block for it was deliberately left commented-out rather than declared: a Compose file-based secret whose source file does not exist makes `docker compose up` fail for the WHOLE stack (all four services), not just the Claude path — confirmed by reading Compose's own behaviour, not assumed; declaring it prematurely would have broken every other already-working part of BAGMAN. The block is fully pre-written and commented, ready to uncomment the moment Matt provisions the file — no other change is required (`ai/providers/claude/client.py`'s own default path already matches exactly).

### 2.6 Real acceptance attempts — one honest attempt each, per tier, exactly as instructed

All five new scripts follow `tests/acceptance/README.md`'s existing conventions exactly (no `test_` prefix, real disposable stack, no mocks, independently runnable, none perform a final `down -v`).

**`tests/acceptance/mac_mini_background_tier_live_proof.py`** (PID §14/§83/§87): real `bagman-fast` (`DOCUMENT_TYPE_PROPOSAL`) and `bagman-core` (`ENTITY_PROPOSAL`) calls via the real `POST /internal/ai/tasks` HTTP surface.
* Step 1 — network-path fix proof: **PROVEN** (`{"reachable": true, "status": 200, ...}` from inside the real container).
* Step 2 — live re-check of the LiteLLM gateway's own backing database: **STILL DOWN** (`{"status": "healthy", "db": "Not connected"}`).
* Real calls: both **honestly BLOCKED** — `FAILED`/`LITELLM_PROVIDER_ERROR` (a real HTTP 400 `no_db_connection` response was genuinely received; this is the gateway's own real answer, not a BAGMAN-side defect).
* Canonical evidence unaffected; capability_alias recorded exactly as requested on both invocations (no silent cross-tier fallback); invocation survived a real `bagman-api restart`; a retry created a genuinely new, distinct invocation.

**`tests/acceptance/trinity_escalation_live_proof.py`** (PID §81/§82/§87): no CD-5 task contract currently prefers `bagman-deep` (`DOCUMENT_SUMMARY`/`DOCUMENT_TYPE_PROPOSAL`→`bagman-fast`; `ENTITY_PROPOSAL`→`bagman-core`) — a genuine, documented gap this script surfaces rather than papering over. Exercises the real `LiteLLMClient` directly (`capability_alias="bagman-deep"`), inside the real running container, via `docker compose exec` (the same "no HTTP surface exists for this" pattern `tests/acceptance/_lib.py` already establishes). Real result: **honestly BLOCKED** — `PROVIDER_ERROR`, `HTTP 400: {"error":{"message":"No connected db.","type":"no_db_connection", ...}}` — the real network path, the real adapter, the real gateway, the real alias; the same real database outage as above, not a BAGMAN defect. No silent cross-tier fallback (mechanically enforced by `validate_capability_alias`, independent of this script).

**`tests/acceptance/claude_operator_live_proof.py`** (PID §81/§84/§88): re-checked `/srv/bagman-secrets/anthropic_api_key` live — still absent. Real `POST /internal/operator/chat` call against real synthetic evidence: **honestly BLOCKED** — HTTP 200 (never a crash), `FAILED`/`CLAUDE_AUTHENTICATION_FAILED`, `response_text: null` — the exact, correct fail-closed outcome. Canonical evidence/`/ready` unaffected; `GET /internal/ai/health` proves the background gateway remains independently reported (`claude: unreachable` alongside `bagman_fast/core/deep: ok`).

**`tests/acceptance/prompt_injection_live_proof.py`** (PID §77-80/§85): synthetic evidence containing hostile instructions uploaded through the real intake pipeline (real Postgres/MinIO/ClamAV). Part A: a real `POST /internal/ai/tasks` attempt — honestly BLOCKED for the same reason as above (`LITELLM_PROVIDER_ERROR`); canonical evidence proven unaffected. Part B — **explicitly fake-backed, exercised inside the real running container** (the real orchestration function, not a stand-in): a (fake) provider scripted to behave as if fooled by the injected instruction still resulted in the hostile text reaching the (fake) provider ONLY inside the untrusted user-role message (`hostile_text_in_system_role: false`, `hostile_text_in_user_role: true`), proven via the real `build_messages`/`run_background_task` code path, not a duplicate implementation.

**`tests/acceptance/ai_gui_browser_acceptance_proof.py`** (PID §86): real headless Chromium against the real production stack. All twelve PID §86 checklist items proven — Overview AI status (real `/internal/ai/health` data rendered), Documents regression (real upload/list/detail unaffected by any CD-5 change), the AI Analysis panel's real "Run analysis" click producing a real, honestly-FAILED card (capability_alias/observed-model rows visible regardless of outcome), Ask BAGMAN opening with real document context, a real attempt whose response is an honestly-rendered error turn (`CLAUDE_AUTHENTICATION_FAILED`-shaped, never a raw crash or fabricated reply), the referenced document remaining identifiable via the context chip throughout, and both failure states (AI panel card, Ask BAGMAN error turn) rendering with the GUI's own existing structured error presentation.

**Regression check on the two Dockerfile/compose changes above:** `tests/acceptance/restart_proof.py` and `tests/acceptance/dependency_failure_proof.py` (both pre-existing CD-3/CD-4 scripts) were re-run in full against the freshly rebuilt image/compose file — both PASS cleanly, no regression. **Scoping note, stated honestly:** the other three pre-existing CD-3/CD-4 acceptance scripts (`container_rebuild_proof.py`, `idempotency_and_concurrency_proof.py`, `content_policy_and_quarantine_proof.py`, `restore_into_clean_target_proof.py`) were NOT separately re-run in this WI — their own underlying mechanisms (container rebuild identity, intake concurrency, content-policy fixtures, backup/restore) are unrelated to the two changes made here (Dockerfile `COPY` lines, one compose env-var/`extra_hosts` addition), and `restart_proof.py`/`dependency_failure_proof.py` already exercise the full intake pipeline, the AI-package-now-present composition path, and every dependency-failure mode end-to-end. Flagged here explicitly, not silently decided, per this WI's own dispatch discipline — the PL's call to re-run the remainder if desired.

### 2.7 Repo-wide `trinity-*` alias sweep (PID §9/§75/§87/§92)

`tests/integration/test_architecture_boundaries.py::test_no_trinity_star_alias_literal_anywhere_in_application_source` (new): scans every file (not just `.py`) under every application-source root (`ai`, `agent`, `app`, `core`, `persistence`, `services`, `adapters`, `ui`, `scripts`, `ops`, `config`, `deployment`, `contracts`) for any `trinity-fast`/`trinity-core`/`trinity-deep`/`trinity-embed` literal. Deliberately excludes `tests/` itself — several existing test files legitimately use these strings as adversarial/negative fixture values (proving BAGMAN *rejects* them), which is correct, not a violation; `tests/security/test_ai_litellm_alias_lockdown.py`'s own narrower, WI-2-scoped check already covers `ai/providers`/`ai/gateway`/`ai/prompts`/`app/api/routers/ai.py`. Passes cleanly (0 violations) — the full repository, not just WI-2's own new files.

### 2.8 CI assessment

No new top-level test directory was introduced by WI-5 beyond `tests/integration/test_ai_evaluation_harness.py` (already inside an existing, already-CI-covered directory). `.github/workflows/security.yml` re-read in full: no genuine gap found, no change made — the existing `pytest tests/security tests/contract tests/integration` step already picks up every new/changed test file added by this WI (the bidi-hardening tests, the trinity-* sweep, the evaluation-harness wrapper), and `pytest tests/persistence` already picks up the new real-threaded concurrency test. The five new `tests/acceptance/*_live_proof.py` scripts are deliberately NOT wired into CI, for the exact reason `tests/acceptance/README.md` already states for every other script in that directory: they mutate/exercise the real runtime (real `docker compose build/up`/`restart`/`exec` against the real Docker Compose stack) rather than disposable, uniquely-named fixtures.

### 2.9 Security

```
$ gitleaks detect --source . -v --redact   (full history, 40 commits)
scanned ~1826050 bytes (1.83 MB) in 133ms
no leaks found
```
No secrets/credentials as literals were added anywhere. `/srv/bagman-secrets/*` files were read by path only, never printed, never copied — including the still-absent `anthropic_api_key`, which no script or config ever fabricates a value for. No real malware used anywhere in this WI (the EICAR string was not needed here — no new quarantine-adjacent scenario was judged to add real value beyond CD-4's own already-established proof).

---

## 3. Defects and gaps caught and fixed during WI-5 (this WI's own arc)

1. **(WI-5) `bagman-api`'s Docker image missing `ai/`/`agent/` entirely** — §2.4 above. The single most significant finding: every real production `bagman-api` container built from any prior CD-5 commit would have 500'd on every AI-related request. Fixed in `deployment/docker/api/Dockerfile`.
2. **(WI-5) `bagman-api` container could not reach the real LiteLLM gateway at all** — §2.5 above, the exact issue this WI's own dispatch named. Fixed via `host.docker.internal`/`extra_hosts` in `deployment/compose/docker-compose.yml`.
3. **(WI-5, caught during this WI's own drafting, before it ever reached a committed script)** `trinity_escalation_live_proof.py`'s first draft called `LiteLLMClient()` with no arguments inside the real container — silently falling back to the class's own bare default endpoint (`http://localhost:4000`, WRONG from inside a container, exactly the class of bug found in item #2) rather than reading `BAGMAN_LITELLM_ENDPOINT` from the real environment the way `app/api/composition.py`'s own production builder does. Caught immediately by the script's own honest assertion (`assert result["status"] not in ("TRANSPORT_ERROR", "TIMEOUT")`) refusing to let a real regression pass silently as "expected". Fixed before this evidence file was drafted — see the script's own in-line comment explaining the correct pattern.

No other defects were found during this WI's own work — WI-1 through WI-4's own commits (see git log, quoted in full in §1's own table sources) report clean reconciliation with no fixes needed beyond what shipped in their own commits, and this WI's own re-run of their test suites found no regression.

---

## 4. Full acceptance-criteria walk-through (PID §91)

### Architecture
* One AI gateway; provider adapters isolated; no module-specific raw LLM clients — **confirmed** (WI-1-3's own architecture, re-verified by this WI's repo-wide sweep, §2.7).
* Alias-only routing (`bagman-*` only, never `trinity-*`, never a physical name) — **confirmed, repo-wide** (§2.7 — the full sweep, not just WI-2's own narrower one).
* Claude/Mac-mini/Trinity-escalation tiers distinct; no silent cross-tier/cross-provider fallback — **confirmed** (every acceptance script in §2.6 explicitly checks and confirms this).
* Existing LiteLLM remains the sole inference control plane — **confirmed** (no second LiteLLM/inference architecture exists anywhere in the tree).

### Contracts
* Typed/versioned task contracts; schema-validated outputs; versioned prompts; provider-neutral invocation/result models — **confirmed** (WI-1/WI-2/WI-3's own delivery, re-exercised throughout this WI's own evaluation harness, §2.2).

### Persistence
* Invocation durable; evidence/task provenance durable; retries/history durable; restart safe — **confirmed** (§2.3's genuine real-threaded proof against the real database; §2.6's Mac-mini script's own restart-survival + retry-creates-new-row proof).

### Trinity-escalation tier
* Real LiteLLM call proven via `bagman-deep` — **attempted for real; genuinely reached the real gateway/real alias; BLOCKED for a genuine model completion by the independently re-verified LiteLLM-gateway database outage** (§2.6). Physical model not hardcoded (the alias is the only routing input, mechanically enforced). Failure/recovery: the failure half is proven live; "recovery" cannot be proven until the external database recovers — honestly not claimed here.

### Mac-mini tier
* Per PID §14/§91: **Helm's GREEN report exists** for the readiness gate itself. This WI's own real proof (§2.6) went further than a bare BLOCKED acknowledgement — it found and fixed a genuine, previously-undiscovered BAGMAN-side infrastructure defect (§2.4/§2.5) that would ALSO have blocked this tier even if the upstream database were healthy, and proved (independently of that database) that the fixed network path itself now genuinely works. The remaining blocker (genuine model completion) is external, re-verified live, not assumed.

### Claude
* Real operator chat proven; read-only governed tools; bounded context; no canonical mutation; failure/recovery proven — **failure path proven for real** (§2.6, `claude_operator_live_proof.py`); the success path remains genuinely BLOCKED pending Matt's own credential (re-checked live, still absent).

### GUI
* Modularised enough for continued growth; Ask BAGMAN usable; Documents AI panel usable; AI states/results visible per-tier; capability health visible per-tier; browser acceptance green — **confirmed** (§2.6, `ai_gui_browser_acceptance_proof.py`, all twelve PID §86 steps, against the real stack).

### Safety
* Prompt injection fixtures; no chain-of-thought persistence; no secrets in prompts/logs/repo; local/background work does not silently escalate or go cloud; AI output visibly non-canonical — **confirmed** (§2.2's reused suites + this WI's own live proof, §2.6; §2.9 gitleaks).

### Evaluation
* Deterministic evaluation fixtures; structured-output validation; quality baseline for initial tasks — **confirmed** (§2.2, the full mandatory harness, genuinely built and green).

### Security
* Filename bidi hardening closed (§66); gitleaks green; prior security controls unchanged — **confirmed** (§2.1, §2.9).

### CI
* All existing suites green; AI suites green (against fakes, no live credentials required); architecture projection green (including the `bagman-*`-only alias check); exact live PR head observed green — **local/this-WI's-own-reconciliation confirmed** (§6 below); **live CI observation remains the PL's own job**, exactly like every prior CD delivery in this project (not this Engineer's to claim).

**Per PID §91's own closing note:** a partial `AI_FOUNDATION_GREEN` covering everything except the Mac-mini-tier's and Trinity-escalation tier's genuine-model-completion criteria, with those two portions explicitly marked `BLOCKED` pending the external LiteLLM-gateway database's own recovery (Helm/Trinity-owned infrastructure, re-verified down at every acceptance step in this WI, most recently), and the Claude tier's own success path marked `BLOCKED` pending Matt's credential (re-verified absent), is what this Engineer's own account supports — this is the PID's own explicitly-sanctioned "honest interim verdict," not a fabricated `GREEN` and not a `RED`. **The actual verdict is the architect's/PL's to issue, after PL reconciliation and independent Auditor dispatch (PID §92), not this Engineer's.**

---

## 5. What is honestly NOT yet proven

* **A genuine, successful live completion from any real LLM tier (Mac-mini via `bagman-fast`/`bagman-core`, Trinity-escalation via `bagman-deep`, or Claude).** All three real acceptance attempts in this WI reached the real, correct network path/adapter/gateway/alias/credential-check and received a real, honest, terminal failure — never a fabricated success, never a silent workaround. This is external infrastructure state (Helm/Trinity's LiteLLM-gateway database; Matt's own Anthropic credential), re-verified live at acceptance time as this WI's own dispatch required, not assumed from the pre-dispatch briefing.
* **Mac-mini restart-recovery / load-latency evidence** (PID §14/§83's own explicit list) — genuinely cannot be proven without a working model tier; not fabricated here.
* **A fresh, independent Auditor has not yet reviewed this delivery.** Nothing in this file should be read as that review — it is this WI's own Engineer's account of its own work, as honestly and completely as this WI's dispatch asked for, but Forge doctrine (PID §92) requires a zero-context Auditor's independent reproduction before any verdict is adjudicated. In particular, the Auditor should independently re-verify: the two infrastructure defects found in §2.4/§2.5 (build the image fresh, inspect it; test the network path fresh); the live state of the LiteLLM-gateway database and the Anthropic key file (both can change between this WI's own acceptance run and the Auditor's); and the full PID §91 checklist above.
* **Live CI has not been observed by this WI.** As with every prior WI in this delivery, that is the PL's own job, done after this WI is committed and pushed.
* **The three CD-3/CD-4 acceptance scripts NOT re-run in this WI** (§2.6's own scoping note) — a deliberate, documented choice, not an oversight; the PL may re-run them if a fuller regression sweep is wanted before merge.

---

## 6. Local verification summary (this WI, final pass)

```
$ pytest tests/security tests/contract tests/integration tests/app_api -q   (fresh venv)
623 passed, 7 skipped, 1 warning in 49.91s

$ pytest tests/persistence -q   (fresh venv, own disposable Postgres, includes the new real-threaded concurrency test)
104 passed, 17 skipped in 3.24s

$ python3 -m ai.evaluation.run
OVERALL: GREEN (9/9 golden fixtures + reused prompt-injection suite)

$ gitleaks detect --source . -v --redact   (full history)
40 commits scanned; no leaks found

$ python3 scripts/generate_architecture_memory.py --check
architecture-index.md is up to date.

$ git diff --check
(clean — no whitespace/conflict-marker issues)

--- real Docker Compose stack (production composition) ---
$ python3 tests/acceptance/restart_proof.py                                -> PASS (regression check)
$ python3 tests/acceptance/dependency_failure_proof.py                     -> PASS (regression check)
$ python3 tests/acceptance/mac_mini_background_tier_live_proof.py          -> PASS (real attempt; honestly BLOCKED for a genuine completion — see §2.6)
$ python3 tests/acceptance/trinity_escalation_live_proof.py                -> PASS (real attempt; honestly BLOCKED for a genuine completion — see §2.6)
$ python3 tests/acceptance/claude_operator_live_proof.py                   -> PASS (real attempt; honestly BLOCKED for a genuine completion — see §2.6)
$ python3 tests/acceptance/prompt_injection_live_proof.py                  -> PASS (Part A real attempt honestly BLOCKED; Part B fake-backed structural proof PASSED)
$ python3 tests/acceptance/ai_gui_browser_acceptance_proof.py              -> PASS (real Playwright/Chromium; all PID §86 steps verified)
```

**Final Docker Compose state, deliberately:** fully torn down (`docker compose -p bagman down -v` + `bagman-api:dev` image removed) — independently confirmed zero `bagman-*` containers/volumes/networks/images remain via direct `docker ps -a`/`docker volume ls`/`docker network ls`/`docker images` queries, each returning empty.

---

## 6a. PL reconciliation (independent re-verification, all five work items)

Before committing each work item, the PL independently re-derived every claim above rather than trusting the Engineer's report alone — Forge doctrine's own reconciliation step, the same standard held throughout CD-4:

* **WI-1** (AI contracts/domain/persistence): independently read `ai/invocation.py`'s state machine and the PostgreSQL stored-generated-column + partial-unique-index concurrency mechanism in full, independently re-ran the full suite in a fresh venv (all green), gitleaks, and the architecture-memory check before committing. No defect found.
* **WI-2** (LiteLLM background gateway): independently read `ai/providers/litellm/client.py`'s retry/outcome-classification logic and `ai/gateway/background.py`'s four-outcome handling in full, independently re-ran the full suite (clean venv), gitleaks, and the architecture check. No defect found.
* **WI-3** (Claude operator gateway): independently read `agent/tools/registry.py`'s fail-closed dispatch mechanism and the tool-loop bound in `agent/bagman/orchestrator.py`, independently re-ran the full suite standalone. **Found a real defect the Engineer's own report did not surface**: `ai/providers/claude/client.py` imports `requests` directly (production code) but it was declared only in `requirements-dev.txt` — a genuinely clean `pip install -r requirements.txt` venv raised `ModuleNotFoundError` on that very import. Fixed by the PL directly (moved the declaration, with rationale) before committing.
* **WI-2/WI-3 merge**: dispatched in parallel, each blind to the other's code. The PL hand-resolved the resulting conflicts in `ai/component.yaml`, `ai/providers/__init__.py`, `app/api/component.yaml`, `app/api/composition.py`, and `app/api/main.py` (both had independently added the identical `ActiveInvocationConflictError → 409` mapping), and — the substantive part of this reconciliation — wired `agent/tools/background.py`'s `run_background_analysis` dispatch seam to WI-2's real `ai.gateway.background.run_background_task` for **production** composition via a new `_ProductionBackgroundTaskRunner` adapter (development/test composition keeps WI-3's own deterministic fake unconditionally, per PID §61). Verified this new wiring genuinely works end-to-end with a manual fake-backed round trip (evidence resolution → invocation creation → LiteLLM call → schema validation → `SUCCEEDED`) before committing the merge. Full suite re-verified green post-merge (516/100/56 across the three suite groups), gitleaks clean, architecture check clean.
* **WI-4** (GUI foundation + AI surfaces): independently read the `primary_input_reference` filter addition (both repository implementations) and the `claude` health-check addition, independently re-ran the full suite (clean venv, 520/103/85 across the three groups), gitleaks, architecture check. Went further than a code read: personally booted the real dev-mode server and drove a full real API round trip myself — real intake → real `POST /internal/ai/tasks` (confirmed the dev-mode `FakeLiteLLMClient` fix genuinely works, `SUCCEEDED` with the labelled fake output) → the new `primary_input_reference` filter correctly returning exactly that one invocation; and a real `POST /internal/operator/chat`, both the honest 422 (no context supplied) and a real `SUCCEEDED` response with `response_text`/`referenced_evidence_ids` at the exact shape the GUI expects. No defect found.
* **WI-5** (this work item): independently re-ran the full suite (538/104/85 across the three groups, clean venv), the evaluation harness directly (`python3 -m ai.evaluation.run`, genuinely green), the concurrency race test specifically, gitleaks (full history, 40 commits), and the architecture check. Independently rebuilt the real production Docker image from a clean checkout and confirmed — bypassing the entrypoint script to inspect and import directly — that `ai/`/`agent/` are genuinely present and importable inside it (`import ai.gateway.background; import agent.tools.handlers` succeeds). Independently stood up the full real Docker Compose stack (all four services healthy on first try) and confirmed, myself, that `bagman-api`'s own container can now genuinely reach the real LiteLLM gateway via `host.docker.internal:4000` (`GET /internal/ai/health` reporting `bagman_fast/core/deep: "ok"`, `claude: "unreachable"`, `/ready` fully green) — then ran a real end-to-end intake → `POST /internal/ai/tasks` call myself and independently reproduced the exact documented outcome (`FAILED`/`LITELLM_PROVIDER_ERROR`, the real gateway's own `no_db_connection` response, never a fabricated success). Confirmed the stack torn down cleanly afterward (zero `bagman-*` containers/volumes/networks/images). No further defect found — WI-5's own account holds up completely under independent reproduction, including its own two significant infrastructure-defect findings.

This is the same standard CD-4 held to: an Engineer's own report is never the final word before a commit lands; the PL's own independent reconciliation is — and, across this delivery's two parallel work items (WI-2/WI-3), the PL's own hand-resolution and cross-wiring of the seam between them.

---

## 6b. Independent Auditor (fresh, zero-context) — dispatched after all five WIs were merged

A fresh Auditor with no prior context on this delivery was dispatched against the merged branch (head `9a35918`) to independently re-derive every PID §91 criterion from scratch — not trusting this evidence file's own claims, per Forge doctrine (PID §92). Summary of what the Auditor did and found (full detail held in session record):

* Independently rebuilt a clean venv and re-ran every pytest suite (`tests/security`/`tests/contract`/`tests/integration`, `tests/persistence`, `tests/app_api`) — all green, matching this file's own counts exactly.
* Ran `python3 -m ai.evaluation.run` independently — 9/9 GREEN, matching. Then, specifically to test whether the harness is genuinely non-vacuous (the same scrutiny a past CD-4 audit had to apply to a different, actually-vacuous test elsewhere in this codebase), constructed a deliberately-broken fixture and confirmed the harness correctly reports `RED`/`all_passed=False` — the harness is a real, functioning check, not a hollow wrapper.
* Re-ran the new real-threaded concurrency proof **five times independently** — one winner, the rest genuine `ActiveInvocationConflictError`s, every time.
* Independently rebuilt the production Docker image from a clean checkout and reproduced, from scratch, **both** of this WI's own critical-defect-fix claims: `ai`/`agent` genuinely present and importable inside the built image (bypassing the entrypoint, direct `import` proof); `bagman-api`'s own container genuinely reaching the real LiteLLM gateway via `host.docker.internal:4000` (live `docker exec` proof, live response captured).
* Independently attempted all three real AI-tier calls and the live prompt-injection proof, re-registering their own synthetic evidence rather than reusing anything from this WI's own run: Mac-mini and Trinity-escalation both reached the real gateway and received the real `no_db_connection` failure; Claude reached the real endpoint and received `CLAUDE_AUTHENTICATION_FAILED`; a hostile-instruction-laden synthetic document (including an explicit attempt to get a tool to call `delete_evidence`/mark itself `APPROVED_FOR_PAYMENT`) left canonical evidence completely untouched. Also independently confirmed retry-creates-a-new-invocation and restart-survival live, via a real `docker restart bagman-api`.
* Ran their own real Playwright/Chromium pass against the live stack — all 12 PID §86 steps independently reproduced.
* Independently re-verified the bidi-hardening fix's exact character set against Unicode's own official `Bidi_Control` property (an independent cross-check the WI's own drafting did not explicitly perform) and confirmed it matches exactly.
* Ran gitleaks independently (pinned to the same version CI uses) — clean.
* **Found no defect** beyond what this file already disclosed. One environmental limitation of the audit itself, not a delivery defect: the Auditor's own sandbox had no outbound network access to GitHub, so live CI observation (PID §91's own explicit CI-checklist item) could not be independently performed by the Auditor — exactly as with every prior CD delivery in this project, that step remains the PL's own job, done next (§6c).
* **Issued verdict: `AI_FOUNDATION_GREEN`, explicitly scoped as the PID §91-sanctioned partial form** — the Mac-mini tier's and Trinity-escalation tier's genuine-completion criteria, and Claude's success-path criterion, remain honestly `BLOCKED` by external conditions the Auditor re-verified live themselves (not assumed from this file), which PID §91's own closing note explicitly sanctions as a valid `AI_FOUNDATION_GREEN` rather than a `RED` or a generic `BLOCKED`.

## 6c. Live CI — observed by the PL

Per PID §80/§91 and the standing lesson carried from CD-3/CD-4 (local/Auditor-green and live-CI-green are different claims — always check the second directly, never assume it): PR #5 (`cd-5/ai-foundation-claude-operator-and-gui-integration` → `main`) was opened at `https://github.com/maff0000/bagman/pull/5`, head commit `e758b5f`. The GitHub Actions "Security" workflow ran automatically and was watched directly by the PL to completion (`gh run watch`, not inferred):

```
$ gh pr checks 5
security	pass	1m43s	https://github.com/maff0000/bagman/actions/runs/34786940092/job/103804011632

$ gh api repos/maff0000/bagman/actions/runs/34786940092/jobs
job: security  conclusion=success
  - Set up job:                                                              success
  - Checkout:                                                                success
  - Set up Python:                                                           success
  - Install gitleaks (pinned release binary):                                success
  - Run gitleaks scan:                                                       success
  - Install dependencies:                                                    success
  - Check architecture memory projection:                                   success
  - Run non-Docker test suites (security + contract + domain + ...):        success
  - Run PostgreSQL persistence test suite (own disposable container):        success
  - Run bagman-api runtime test suite (own disposable Postgres + MinIO):     success
  - Post Set up Python / Post Checkout / Complete job:                       success
```

Every individual step succeeded — gitleaks green, architecture-memory drift check green, all three test-suite steps (which now include every CD-5 AI test: the evaluation harness, the real-threaded concurrency proof, the bidi-hardening tests, the repo-wide `trinity-*` sweep) green, nothing skipped. Confirmed via `gh pr view 5`: `mergeable: MERGEABLE`, `mergeStateStatus: CLEAN`, working tree clean.

---

## 7. Exit-gate statement (PID §93) — drafted, not issued

Per PID §93, no live mailbox integration may begin until **AI_FOUNDATION_GREEN** (or an explicitly-scoped partial-GREEN-pending-Mac-mini/Trinity-escalation/Claude, per PID §91's own closing note, at the architect's discretion). This Engineer's own account of the evidence above supports exactly that partial, honest outcome: every PID §91 criterion reachable without a live model completion was independently exercised against the real stack (not merely read about); two genuine, previously-undiscovered infrastructure defects were found and root-cause-fixed (§2.4/§2.5), each independently re-verified after the fix; the mandatory evaluation harness was genuinely built and is genuinely green; and every external blocker (LiteLLM-gateway database, Anthropic credential) was re-checked live at acceptance time, not assumed from the pre-dispatch briefing, with the exact real error captured each time. **The actual verdict — `AI_FOUNDATION_GREEN` (full or explicitly-scoped partial), `AI_FOUNDATION_RED`, or `BLOCKED` — is the PL's/architect's to issue, after PL reconciliation and independent Auditor dispatch (PID §92), not this Engineer's.**
