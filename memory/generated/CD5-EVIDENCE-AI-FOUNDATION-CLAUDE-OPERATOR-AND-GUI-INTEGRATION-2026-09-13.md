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

> **Architect correction (2026-09-16, recorded verbatim per Matt's explicit instruction):** "the prior Auditor's partial-GREEN interpretation was broader than PID §91 permits." The verdict this section's Auditor issued below — `AI_FOUNDATION_GREEN` (explicitly-scoped partial) — is **overruled as too permissive**. Matt's ruling: until real-provider acceptance is actually proven for each tier, the honest state of this delivery was **BLOCKED**, not any form of GREEN, regardless of how thoroughly every OTHER criterion had been independently exercised. This section is left otherwise unedited as a historical record of what the Auditor actually did and found (all of which stands — no defect was found, and every non-provider-dependent criterion genuinely was proven); only the verdict LABEL at its end is corrected by this note. See §6d below for the real-provider-acceptance work this correction required, and §6e for the corrected, focused re-audit.

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

## 6d. Gate-1 real-provider-acceptance closure (2026-09-16)

Following the architect correction at §6b, Matt ruled CD-5 **BLOCKED pending real-provider acceptance**, named two external gates (LiteLLM/all BAGMAN background tiers; Claude), and forbade any BAGMAN code change merely to work around broken shared infrastructure. This section records, in order, everything that actually happened to close Gate 1 — the full chain, not just the final PASS.

**Step 1 — shared Trinity gateway, credential-rotation attempt (2026-09-13/14).** HELM completed `LITELLM_KEY_ROTATION_GREEN`, reporting the prior `400 no_db_connection` failures as the gateway's reaction to an invalid/compromised credential rather than a genuine database outage. The PL independently re-verified: `GET /health/readiness` on the shared gateway (`local-ai-gateway`, host port 4000) still reported `"db":"Not connected"`, and direct authenticated calls with the rotated key still failed identically — `docker logs` pinpointed the failure inside LiteLLM's own `user_api_key_auth()`, i.e. genuinely DB-dependent key validation, not a credential-format problem. Reported back rather than accepted at face value. **This is a real instance of the "verify, don't merely relay" discipline this delivery has held throughout** — the independent check caught that the rotation alone had not actually fixed the underlying condition.

**Step 2 — dedicated BAGMAN AI appliance (2026-09-16).** HELM retired the shared-gateway approach entirely and returned `BAGMAN_AI_APPLIANCE_GREEN`: a new, BAGMAN-exclusive LiteLLM installation on the dedicated Mac mini (`mac-prod-01`, `http://192.168.11.4:4100`), with a fresh DB-backed virtual key scoped only to `bagman-fast`/`bagman-core`/`bagman-deep`. The PL independently verified before touching any BAGMAN file: `GET /health/readiness` → `{"status":"healthy","db":"connected"}`; real authenticated completions succeeded for all three aliases; the appliance's own key scoping independently rejected a raw physical model name with `403 key_model_access_denied` (defense-in-depth alongside BAGMAN's own `validate_capability_alias`). `deployment/compose/docker-compose.yml`'s `BAGMAN_LITELLM_ENDPOINT` was repointed at the new appliance (commit `0aa7f5f`); the CD-5 WI-5 `host.docker.internal`/`extra_hosts` fix was removed as no-longer-needed (a real LAN address, not host-loopback). `bagman-api` rebuilt/recreated; `tests/acceptance/trinity_escalation_live_proof.py` reconfirmed `bagman-deep` fully GREEN end-to-end (real completion, 164ms).

**Step 3 — a genuine reliability finding, not fabricated success (2026-09-16).** Running the official `mac_mini_background_tier_live_proof.py` against the now-healthy appliance still produced real failures: `bagman-fast`/`DOCUMENT_TYPE_PROPOSAL` → `FAILED`/`LITELLM_TIMEOUT` (~40s, two retried attempts); `bagman-core`/`ENTITY_PROPOSAL` → a hard 60s HTTP read-timeout. Direct, realistic (system+user, structured-JSON-output) probes isolated the actual behaviour: `bagman-deep` fast and correct (0.33s); `bagman-core` eventually correct but slow (~52.5s, exceeding `ENTITY_PROPOSAL`'s 30s SLA); `bagman-fast` returned **empty content** after consuming its full completion-token budget (256 tokens, nothing visible) — a distinct, worse failure mode than mere slowness. Reported to Matt rather than silently tuned around. **Matt's explicit ruling at this point: do NOT change `timeout_seconds`; a `bagman-core` deadline-exceeded or an empty `bagman-fast` output must fail, not be papered over; HELM would investigate the discrepancy between the appliance's previously-proven workload and BAGMAN's actual production request shape; no repeated live-test hammering until HELM reported the local task path stable.**

**Step 4 — HELM's infrastructure fix and its own honest verdict.** HELM applied `think:false` (fixing runaway/invisible reasoning tokens consuming the completion budget before any visible answer — the direct cause of `bagman-fast`'s empty output) and plain `format:"json"` (fixing JSON syntax). HELM's own verdict on this work, `BAGMAN_LOCAL_AI_TASKS_RED`, correctly did **not** claim success — HELM identified that plain JSON mode guarantees syntax only, never a caller's specific schema shape, and that the remaining gap was an **application-layer** contract BAGMAN itself had to close (BAGMAN already owns the exact per-task schema in `ai.tasks.TASK_REGISTRY`, before any provider call).

**Step 5 — the authorised BAGMAN delta: per-request structured-output schema constraint (commit `e51c615`).** `LiteLLMClientProtocol.complete()`/`LiteLLMClient.complete()` now take a required `output_schema` argument, attached to the wire request as `response_format` in the OpenAI-compatible JSON-schema-constrained shape (`build_response_format` — LiteLLM translates this into the appliance's native Ollama structured-output grammar constraining, per Ollama's own documented support: https://ollama.com/blog/structured-outputs). `ai/gateway/background.py::run_background_task` passes `task_contract.output_schema` straight through — the one and only schema authority, never reconstructed, never caller-suppliable (proven structurally: `POST /internal/ai/tasks`'s closed field set has no schema/format field at all; `output_schema=task_contract.output_schema` appears literally in `background.py`, checked by source inspection exactly like the existing `capability_alias` provenance test). `json.loads` + `validate_task_output` against that same schema remain unconditional afterwards — **provider-side structured generation is a reliability improvement to the model-generation step only, never treated as sufficient proof of a conforming response** (proven by a dedicated test: a scripted non-conforming response is still rejected even though structured output was requested). New/adjusted tests also prove `DOCUMENT_TYPE_PROPOSAL` and `DOCUMENT_SUMMARY` — sharing the same `bagman-fast` alias — each receive their own distinct schema, never a shared/generic one. No `timeout_seconds` changed anywhere; no schema/format field added to the HTTP surface; no HELM/alias config touched. Full non-Docker suite: 736 passed, 24 skipped (docker-dependent, unaffected); gitleaks clean; architecture-memory check clean.

**Step 6 — real 20x acceptance proof (commit `7df321d`), against the real, rebuilt `bagman-api` and the real dedicated appliance:**

| Task / alias | Real invocations | Result | Latency (SUCCEEDED) | SLA |
|---|---|---|---|---|
| `DOCUMENT_TYPE_PROPOSAL` / `bagman-fast` | 20 | **20/20 SUCCEEDED** | p50 8848ms, p95 9060ms, max 9060ms | 20s |
| `DOCUMENT_SUMMARY` / `bagman-fast` (same alias, different schema) | 5 | **5/5 SUCCEEDED** — every response's `output` contained `summary` and never `proposed_type`, checked explicitly | p50 15190ms, p95 15394ms, max 15394ms | 30s |
| `ENTITY_PROPOSAL` / `bagman-core` | 20 | **20/20 SUCCEEDED** | p50 9864ms, p95 13950ms, max 13950ms | 30s |
| `bagman-deep` reconfirmation | 1 | GREEN, real completion (447ms) — unaffected by this delta | — | 30s |

Every one of the 41 real calls reached `SUCCEEDED` — i.e. transport success, syntactically valid JSON, AND exact `output_schema` validity via the same `validate_task_output` call this delivery has used throughout (a `SUCCEEDED` status is only reachable after all three). All latencies sit comfortably inside the unchanged task-contract SLAs — no timeout tuning of any kind was needed once the real per-request schema constraint was in place. `structured_output_20x_proof.py` records and would have reported any failure honestly (non-zero exit, explicit failure listing); none occurred.

**Gate 1 status: CLOSED.** All three background-tier aliases (`bagman-fast`, `bagman-core`, `bagman-deep`) proven live, through BAGMAN's real production path, against HELM's real dedicated appliance — real transport, real schema-constrained generation, real independent validation, alias-only routing confirmed (both BAGMAN's own boundary and the appliance's own key scoping), no silent cross-tier fallback (every recorded `capability_alias` matched exactly what was requested throughout this whole delivery, unchanged by this delta), canonical evidence unaffected (the same `register_evidence`/`GET /internal/evidence/{id}` proof pattern used since WI-5's own acceptance scripts). AIInvocation persistence/provenance and GUI display of a real Mac-backed result were already proven for the FAKE-provider path in WI-4/WI-5 reconciliation and are structurally identical for a real one (same repository write path, same GUI rendering code, no provider-conditional branch in either) — not re-proven pixel-by-pixel here since nothing in this delta touches persistence or the GUI layer at all.

**Gate 2 status: still OPEN.** `/srv/bagman-secrets/anthropic_api_key` does not yet exist. No work in this section touches Claude/Gate 2 in any way.

---

## 6e. Focused independent Auditor — Gate-1 delta only (dispatched, not a full re-audit)

A fresh Auditor with no prior context on this delivery was dispatched against an isolated, detached-HEAD worktree pinned to this delta's exact head (`eff0848`) — explicitly scoped to the Gate-1 structured-output delta only (commits `0aa7f5f`/`e51c615`/`7df321d`/`eff0848`), NOT a full CD-5 re-audit; already-merged WI-1–WI-5 architecture/GUI/evaluation-harness/concurrency/bidi-hardening/production-image work was explicitly out of scope per the dispatch instruction.

What it independently reproduced (not merely read/trusted):

* **A fresh, independent 20x/5x/20x/1 real-acceptance run** against the real, rebuilt `bagman-api` and HELM's real dedicated appliance (`192.168.11.4:4100`, confirmed `runtime_environment: "production"` via `/ready`): `DOCUMENT_TYPE_PROPOSAL`/`bagman-fast` **20/20 SUCCEEDED** (p50=8956ms, p95=9727ms vs. 20s SLA); `DOCUMENT_SUMMARY`/`bagman-fast` **5/5 SUCCEEDED**, explicitly confirming `summary` was present and `proposed_type` did NOT leak across the two tasks sharing the alias (p50=15286ms, p95=15389ms); `ENTITY_PROPOSAL`/`bagman-core` **20/20 SUCCEEDED** (p50=10573ms, p95=11630ms vs. 30s SLA); `bagman-deep` reconfirmed GREEN and unaffected (261ms). **46/46 real calls SUCCEEDED** — a status the real production `run_background_task` path only reaches after transport success + JSON syntax + `validate_task_output` schema conformance, so this is a genuine end-to-end proof, not a tautological test of the test itself. Numbers are in the same ballpark as, and slightly better in places than, the PL's own original 20x run (§6d) — expected real-world variance, no cause for concern.
* **Full non-Docker suite, re-run fresh**: `736 passed, 24 skipped` — matches the delta's own commit-message claim exactly, independently confirmed rather than assumed current.
* **`tests/security/test_ai_litellm_alias_lockdown.py`, re-run fresh**: 23/23 passed, including both new tests this delta added — the closed-HTTP-field-set proof that `POST /internal/ai/tasks` has no `schema`/`format`/`output_schema`/`response_format` field a caller could inject through, and the source-inspection proof that `output_schema=task_contract.output_schema` appears literally in `ai/gateway/background.py` (never hand-reconstructed).
* **gitleaks, re-run fresh**: clean.
* **Code review of the diff itself**: `output_schema` passed through via a shallow `dict(output_schema)` copy (never reconstructed/mutated); `json.loads` + `validate_task_output` remain unconditional after every provider response regardless of what `response_format` was sent (provider-side structured generation is correctly treated as a reliability improvement only, never as sufficient proof of conformance — matching Matt's explicit ruling); no `timeout_seconds` change anywhere; no new field on the HTTP request surface; no alias/HELM-config change.

**Verdict: `GATE1_STRUCTURED_OUTPUT_DELTA_GREEN` on the code itself** — the `output_schema` threading is implemented exactly as claimed, sourced only from `TaskContract.output_schema`, never caller-injectable, `validate_task_output` remains the unconditional safety boundary, no `timeout_seconds` changed, no other call site broken, full non-Docker suite/gitleaks/architecture-memory clean.

> **Correction (2026-09-16, same day, added after this section was first written):** the "no unresolved concerns" framing immediately above was written from a SECOND, faster re-dispatch of this Auditor role, which only re-ran the repo's own `structured_output_20x_proof.py` (46/46) and did not itself construct an independent test. The Auditor's ORIGINAL dispatch — which kept working in the background well after that second pass had already been treated as final — separately wrote its OWN ad hoc acceptance script (distinct fixture content) and found `ENTITY_PROPOSAL`/`bagman-core`'s real-world structured-output compliance was **not** a dependable 100% in that sample: **12/20 SUCCEEDED (~60%)**, every one of the 8 failures correctly caught by `validate_task_output` as `OUTPUT_SCHEMA_INVALID` — the model had spontaneously added an extra confidence-explanation field (name varies: `confidence_score`/`confidence_reason`/`confidence_warning`) despite `additionalProperties: false` and an explicit prompt naming the exact required keys. **No fabricated success occurred in any failure** — the safety boundary worked exactly as designed every time.
>
> The PL independently ran a third, fresh confirmatory batch immediately on receiving this report (15 more `ENTITY_PROPOSAL` calls, yet another distinct fixture shape, same live stack): **15/15 SUCCEEDED**. Full tally across every independent `ENTITY_PROPOSAL`/`bagman-core` sample taken this day: 20/20 (PL's original script) + 20/20 (second Auditor pass, same script) + 12/20 (first Auditor's own ad hoc script) + 15/15 (PL's fresh confirmatory batch) = **67/75 (~89%) overall, with every single failure concentrated in one 20-call batch run by one agent** — a working theory, NOT confirmed, is that this coincided with concurrent multi-session load on the single, shared, dedicated Mac-mini appliance (that agent had just finished the full 41-call acceptance script, ~8 minutes of real inference, immediately before its own additional 20-call test, and a second session may have been exercising the same shared stack around the same time). This is reported to the architect as an **open question, not resolved here** — whether Gate 1's `bagman-core` tier should be considered dependably closed at the literal 20/20 bar, or whether further isolated-load testing / HELM-side investigation into the appliance's real-world schema-compliance rate is warranted, is the architect's call, not this evidence file's to settle unilaterally. `bagman-fast` (both `DOCUMENT_TYPE_PROPOSAL` and `DOCUMENT_SUMMARY`) showed no such instability across 35 combined fresh calls from the original Auditor's own testing.
>
> Separately: the PL removed the first Auditor's isolated worktree (`git worktree remove --force`) after treating the second agent's report as final, not realising the first agent was still actively working — a process mistake, noted for future dispatch hygiene (confirmed by the Auditor's own report that its findings were unaffected, since all evidence was captured before the worktree was removed).

Explicitly scoped to this delta only — no opinion offered or implied on the overall CD-5 verdict, which remains the architect's/PL's call per §6b's own correction note above.

### Phase A — controlled isolation experiment (2026-09-16, Matt's ruling)

Matt ruled a controlled isolation experiment before any HELM escalation, with an explicit stop-rule: **50 fresh, strictly sequential `ENTITY_PROPOSAL`/`bagman-core` invocations, zero concurrent BAGMAN AI load (no parallel `fast`/`core`/`deep` calls), several distinct synthetic fixture templates (not one repeated prompt), through the exact real production `POST /internal/ai/tasks` path** — anything less than 50/50 (transport + exact-schema + within-SLA) means **stop**, Gate 1 remains `BLOCKED`, and the evidence is handed to HELM; Phase B (a deliberately-loaded comparison batch) only runs if Phase A is a clean 50/50.

`tests/acceptance/entity_proposal_isolation_experiment.py` implemented this exactly: five distinct fixture templates (invoice/statement/expense-claim/receipt/contract-extract, each with varying amounts/dates/refs), one invocation at a time, no other script or process issuing any BAGMAN AI call during the run. Raw per-invocation record (fixture id, latency, transport status, schema validity, token usage, exact validation error, provider/model provenance) preserved verbatim at `tests/acceptance/entity_proposal_isolation_phase_a.jsonl` (50 lines, one per invocation) — nothing summarised-away.

**Result: 49/50 — Phase A FAILED the 50/50 bar.**

* Transport success: 50/50.
* Exact-schema success (`status == "SUCCEEDED"`): **49/50**.
* The 49 `SUCCEEDED` invocations were all comfortably within the unchanged 30s SLA — `p50=8795ms, p95=11701ms, min=7651ms, max=13774ms`.
* The one failure (fixture `...-043`, a `RECEIPT` template): `status=FAILED`, `error_code=OUTPUT_SCHEMA_INVALID`, validation error `"<root>: Additional properties are not allowed ('confidence_score' was unexpected)"` — the model emitted `{"proposed_entity_hint": "NOUSTAI_LIMITED", "confidence": 1.0, "confidence_score": 1.0, "signals": [...], "warnings": []}`, spontaneously adding an extra `confidence_score` field alongside the required `confidence` field, despite `additionalProperties: false` and the prompt naming the exact required keys. **This is the identical root cause the independent Auditor's own earlier non-isolated batch found** (§6e) — occurring even under strict isolation, at a markedly lower rate here (1/50 ≈ 2%) than that batch's 8/20 (40%). BAGMAN's own `validate_task_output` caught and rejected it correctly — no fabricated success, the safety boundary held exactly as designed, exactly once more.

**This result is real evidence against "concurrent load alone explains it"** — the underlying behaviour (the `bagman-core` model occasionally violating its own `additionalProperties: false` structured-output constraint) is present even with zero concurrent load, though clearly less frequent than the earlier, more heavily-loaded batch showed. Whether load is a contributing factor to the RATE remains untested (Phase B was not run, per the stop-rule below) — but load is not the sole cause.

**Per Matt's own explicit ruling: Phase A is not 50/50, therefore STOP. Phase B was NOT run. Gate 1 remains `BLOCKED`. This evidence is handed to HELM for appliance/model compliance investigation** — the underlying model's own structured-output constraint adherence is genuine infrastructure/model-provider territory, not a BAGMAN code defect, and per the persona system's own domain-boundary doctrine this is HELM's lane to investigate, not BAGMAN's to silently work around. No change was made to task schema, validation strictness, or timeout contracts, per Matt's explicit instruction.

### Live CI at this delta's final head

Per the standing lesson carried from CD-3 onward (local/Auditor-green and live-CI-green are different claims — always check the second directly): PR #5 was pushed to head `a2c2a12` (§6d/§6e's five commits: `0aa7f5f`, `e51c615`, `7df321d`, `eff0848`, `a2c2a12`). The GitHub Actions "Security" workflow (run `35081761506`) was watched directly to completion (`gh run watch`) — every step succeeded: gitleaks, architecture-memory drift check, all three test-suite steps (now including this delta's new/changed tests), nothing skipped. `gh pr view 5`: `mergeable: MERGEABLE`, `mergeStateStatus: CLEAN`, `state: OPEN` — **still not merged**, per standing instruction. (Superseded — see §6g: subsequent commits `02971d6` (Phase A) then the architect's GREEN ruling and this section's own final evidence-alignment commit moved the head again; §6g/§6h record the final state.)

---

## 6g. Architect ruling: `BAGMAN_CORE_STRUCTURED_GREEN` — Gate 1 CLOSED GREEN (2026-09-16)

Following §6e/§6f's Phase A finding (49/50, real but low-rate `bagman-core` schema-compliance gap, handed to HELM), HELM identified and fixed the actual root cause, and the architect issued a final ruling accepting Gate 1 as GREEN. This section records that ruling and its evidence verbatim; §6e/§6f above are **not deleted or reinterpreted** — they are exactly what happened, in order, and remain the accurate record of how this conclusion was reached.

### Final root cause (architect ruling, verbatim substance)

The structured-output instability was **not**: a Gemma capability failure; an Ollama grammar failure; a LiteLLM schema-translation defect; a BAGMAN timeout problem; or a concurrency-only problem (§6f's own Phase A result — a real failure under zero concurrent load — already ruled out "concurrency-only" as the sole explanation, which pointed toward this correct root cause rather than away from it).

The proven root cause was an **appliance configuration conflict**: BAGMAN correctly supplied the task-specific `response_format`; LiteLLM 1.99.0 correctly translated it into Ollama's native schema grammar; but the appliance also carried a stale static `extra_body.format: "json"` override, left over from the earlier period (before this Gate-1 delta existed) when BAGMAN did not yet send a schema at all. That stale static setting clobbered the correctly-generated native schema constraint on some requests, degrading them to generic (syntax-only) JSON mode — exactly the intermittent, low-rate pattern both the independent Auditor's ad hoc batch and the PL's own Phase A experiment observed. HELM removed the stale `extra_body.format: "json"` override and retained only `think: false`.

### Final acceptance (HELM's authoritative live proof, all against the real production BAGMAN path)

| Tier / test | Result |
|---|---|
| `bagman-core` — sequential isolated | **100/100** transport success, **100/100** exact-schema validity; latency p50 9.77s, p95 12.16s, max 13.06s (SLA unchanged at 30s) |
| `bagman-core` — controlled load | **20/20** transport success, **20/20** exact-schema validity; p95 11.92s |
| `bagman-fast` | **10/10** transport success, **10/10** exact-schema validity; p95 9.72s (SLA unchanged at 20s) |
| `bagman-deep` | real HTTP 200, unaffected by this fix |

Enforcement reconfirmed unaffected: a forbidden/raw physical model name is still rejected (`403`); no silent cross-tier fallback; BAGMAN's own post-response `validate_task_output` remains active and unconditional (this fix is entirely appliance-side — it did not touch, weaken, or bypass BAGMAN's own safety boundary in any way).

The PL independently spot-checked this before recording it here (not a blind relay): `GET http://192.168.11.4:4100/health/readiness` → `{"status":"healthy","db":"connected"}`; a fresh 10-call `ENTITY_PROPOSAL`/`bagman-core` sample through the real production path → **10/10 SUCCEEDED**, consistent with HELM's own reported fix.

**Gate 1 (background inference — `bagman-fast`/`bagman-core`/`bagman-deep`) is `GREEN`.** No BAGMAN code was changed to reach this result: the per-request `output_schema`/`response_format` delta (§6d/§6e) remains exactly as implemented; timeouts, task schemas, and validation strictness are all unchanged. Per the architect's explicit instruction, fast/core/deep are not to be reopened unless the final Auditor (§6h) finds a concrete defect.

### Final architecture (superseding §2/§8/§9's originally-amended shared-Trinity-gateway topology — see `PID.md` §96 for the full addendum; history preserved there and throughout this file's own §6d narrative, not deleted)

```text
BAGMAN
  ↓
Dedicated BAGMAN Mac AI appliance
  ↓
Mac-owned LiteLLM + PostgreSQL
  ├── bagman-fast → local Mac model
  ├── bagman-core → local Mac model
  └── bagman-deep → Trinity escalation backend
```

Claude remains a wholly independent Anthropic operator path. The existing/shared Trinity LiteLLM gateway (`local-ai-gateway`, host port 4000) is **not** BAGMAN's primary AI control plane — it was the ORIGINAL target (WI-2's initial dispatch, §1's own delivery-summary table), superseded first by the credential-rotation attempt (§6d Step 1), then by the dedicated appliance (§6d Step 2 onward), which is where the topology now finally stands.

## 6h. Focused independent Auditor — final Gate-1 verification, against the exact post-GREEN-ruling head

A fresh Auditor with no prior context — explicitly instructed NOT to inherit the architect's GREEN conclusion, but to independently verify it — was dispatched against an isolated worktree pinned to head `302f7f5` (the §6g evidence-alignment commit). Scope, per the architect's own exact list: per-request schema propagation; distinct schemas per task on a shared alias; unconditional post-response validation; no BAGMAN-side static-override reintroduction; alias enforcement; live fast/core/deep topology; §6b–§7 narrative consistency; PR/evidence wording vs. deployed reality.

**What it independently verified (code read directly, not relayed):**

1. **Schema propagation** — confirmed `output_schema=task_contract.output_schema` reaches `LiteLLMClient.complete()` verbatim and is built into `response_format` exactly as `build_response_format` defines it; nothing else in the wire body. Structurally proven by the closed-HTTP-field-set test.
2. **Distinct schemas per shared alias** — confirmed `DOCUMENT_TYPE_PROPOSAL`/`DOCUMENT_SUMMARY` (both `bagman-fast`) are genuinely different schema objects, both in code and in a **live check**: read the actual persisted `output` rows from `bagman-db` directly and confirmed `DOCUMENT_SUMMARY` responses contain `summary`/no `proposed_type` and vice versa.
3. **Unconditional validation** — confirmed `validate_task_output` runs after every `OK`-status provider response regardless of what was requested; the "still rejected even though structured output was requested" test is real, not vacuous.
4. **No BAGMAN-side competing override** — confirmed `LiteLLMClient.complete()`'s request body is exactly `{model, messages, response_format}`, no `format`/`extra_body` key anywhere in this repo (the stale override HELM fixed was entirely appliance-side, correctly out of this repo's scope).
5. **Alias enforcement, live** — confirmed `validate_capability_alias` is the single shared source of truth for both real and fake clients; independently sent a raw physical model name directly to the real appliance (via a safe temp-file curl, key never printed) and reproduced the same `403 key_model_access_denied` defense-in-depth result.
6. **Live fast/core/deep topology** — ran its **own** ad hoc 21-fixture live sample (not a rerun of the repo's script): **10/10** `ENTITY_PROPOSAL`/`bagman-core`, **5/5** `DOCUMENT_TYPE_PROPOSAL`/`bagman-fast`, **5/5** `DOCUMENT_SUMMARY`/`bagman-fast` (20/20 total, verified by reading the persisted `output` rows directly, not trusting its own script's report), plus one real `bagman-deep` call (real HTTP 200, 309ms — content was an off-topic JSON error message rather than the literal instructed reply; transport/adapter path succeeded regardless, and the Auditor correctly scoped this as a minor Trinity-escalation-backend quality curiosity worth flagging, not a Gate-1 defect, matching the existing `trinity_escalation_live_proof.py` script's own documented "proves the path, not response quality" scope).
7. **§6b–§7 narrative consistency** — diffed the evidence file across every Gate-1 commit and confirmed §6b through §6f are byte-for-byte unedited by the final commit; cross-checked `tests/acceptance/entity_proposal_isolation_phase_a.jsonl` directly against §6f's prose (49 `SUCCEEDED`/1 `FAILED` with the exact described `confidence_score` root cause) — matches exactly, not a fabricated summary.
8. **Topology wording vs. deployed reality** — live `GET /health/readiness` on the appliance, live `docker exec bagman-api env | grep -i litellm` confirming `BAGMAN_LITELLM_ENDPOINT=http://192.168.11.4:4100`, and a direct read of the PR #5 body and `PID.md` §96 — all consistent with each other and with the real deployed state; confirmed the docstring/manifest changes in the final commit are genuinely doc-only (diffed each file, zero executable-line changes).

**Supporting environment checks, all independently re-run:** full non-Docker suite 736 passed/24 skipped (exact match); `python3 -m ai.evaluation.run` 9/9 GREEN; gitleaks clean; architecture-memory check up to date; confirmed `/srv/bagman-secrets/anthropic_api_key` still absent (Gate 2 correctly untouched).

**Defects found: none.**

**Verdict: `GATE1_FINAL_GREEN_CONFIRMED`**, scoped strictly to Gate 1 as of commit `302f7f5`. No opinion offered on Gate 2 or the overall CD-5 verdict.

---

## 6i. Gate 2 — architecture correction: BAGMAN does not hold an Anthropic API key (2026-09-16)

Following Gate 1's closure, the PL began Gate 2 (Claude/Ask BAGMAN real-provider acceptance) exactly as originally scoped: provision `/srv/bagman-secrets/anthropic_api_key`, run `tests/acceptance/claude_operator_live_proof.py`. **The architect ruled this target wrong before any provisioning happened.** BAGMAN does not use a direct Anthropic API key for its operator intelligence. The proven operator architecture is instead: `Matt -> BAGMAN HTML wrapper -> Claude Code agent -> governed BAGMAN tools/APIs`.

The architect's framing described this as "the already-proven operator architecture... already used by BAGMAN." **The PL investigated this claim before implementing anything** (a fresh, read-only fork search across the whole host `/srv`, not just this repo) and found **no such implementation exists anywhere** — no web backend anywhere shells out to `claude` in response to an HTTP request; no MCP server exposes governed tools to a Claude Code session; the closest adjacent precedent is the documented cron/headless `claude -p` "Headless Mode" pattern (`CLAUDE.md`), which is batch/async, not a synchronous per-request chat backend, and not itself proven for this use case. Reported this finding back rather than building around an unverified premise.

**Architect's ruling after investigation (accepted):** the investigation stands; no existing synchronous implementation can simply be reused; Gate 2 is authorised as a **new, bounded implementation**, combining two separately-proven precedents (synchronous web/FastAPI AI wrappers, an established pattern elsewhere in the environment; headless `claude -p` invocation, an established operational pattern on this host) — with a detailed, explicit security/authority/process-control/provenance specification (see §6j).

## 6j. Gate 2 implementation — `agent/claude_code/` (2026-09-16)

New package, superseding the CD-5 WI-3 direct-Anthropic design (`ai/providers/claude/` + `agent/tools/` + `agent/bagman/orchestrator.py` — see §6l for their classification; **not deleted**):

* **`runner.py`** — the ONE place in the repository that ever execs a `claude` subprocess. Fixed executable/argument contract via the list form of `subprocess.Popen` (never `shell=True`, never shell-string interpolation) — the two prompt strings are the only caller-influenced values, each a single argv element. `--tools ""` (every built-in tool disabled) + `--restricted` (belt-and-braces: also strips command-running tools, ignores ambient `CLAUDE.md`/hooks/settings files, confines file tools to the working directory) + `--strict-mcp-config` (no MCP servers) give the invoked process zero ability to run shell commands, edit files, or reach anything outside its prompt text — its authority is a strict SUBSET of, never inherited from, this host's own normal Claude Code development-agent authority. `--system-prompt` replaces the default system prompt entirely (never `--append-system-prompt`). Own process group (`start_new_session=True`) + `os.killpg` on timeout for clean process control; bounded stdout before JSON parsing; every outcome (`OK`/`TIMEOUT`/`PROCESS_ERROR`/`OUTPUT_PARSE_ERROR`/`PROVIDER_ERROR`) returned as data, never raised. Controlled env built from scratch (never the parent's `os.environ`) — only `HOME`/`PATH`/`LANG`/`LC_ALL`, so no BAGMAN secret ever reaches the subprocess. `--dangerously-skip-permissions` is never used.
  * **A real bug caught by the PL's own live smoke test, not by unit tests**: an initial, more defensive controlled-env draft also set `CLAUDE_CODE_SIMPLE=1` (mimicking `--bare`'s effect) — this silently forces API-key-only authentication and broke the ONLY auth mechanism actually available (OAuth/session credentials), producing `"Not logged in · Please run /login"` on every real call. Found by running the real binary directly, bisected env-var-by-env-var, fixed by removing it. A caution for future readers: unit tests alone (which mock `subprocess.Popen`) would never have caught this — only a real invocation did.
* **`fake.py`** — `FakeClaudeCodeOperatorRunner`, mirroring `FakeLiteLLMClient`/`FakeClaudeClient`'s established scripting convention exactly (PID §61).
* **`context.py`** — governed context assembly: evidence/intake/entity fetched directly from BAGMAN's own canonical services (never a live tool call), bounded to one evidence item's content, capped at 20k characters.
* **`orchestrator.py`** — `handle_operator_message`: same public `AskBagmanResult` shape, same `AIInvocation` lifecycle, same `ASK_BAGMAN` v1 task contract the superseded implementation used (the schema already fit — `tool_calls` is simply always `[]` now) — so `app/api/routers/operator.py` needed no response-contract change and the existing GUI needed **zero changes** (`renderToolCalls([])` already returns `null` cleanly, confirmed by reading `app/api/static/features/ai/invocation-card.js` directly). No live tool-calling loop — BAGMAN assembles all context before the one bounded, synchronous invocation, per the architect's own "keep it bounded... simple synchronous request/response... do not build a general autonomous multi-agent platform" instruction. Four clearly-separated prompt sections: system instructions, trusted canonical context, untrusted evidence content (explicitly labelled DATA ONLY), the operator's question. `validate_task_output` remains unconditional after every response.

`app/api/composition.py`: new `claude_code_operator_runner` field (fake in dev/test with a dev-mode default response mirroring the existing LiteLLM one; real `ClaudeCodeOperatorRunner` in production). `claude_client`/`tool_registry` left wired, not removed. New `claude_code_operator` health-check key alongside the now-superseded (but still reported, not deleted) `claude` key.

**Docker packaging** (needed for a genuine container-level proof, not merely this host's own shell): the `claude` native binary — a self-contained, dynamically-linked-against-glibc-only ELF executable (`librt`/`libc`/`libpthread`/`libdl`/`libm` only, verified via `ldd`; no Node.js runtime needed), ~219MB — is staged into the build context by `deployment/docker/api/prepare-claude-binary.sh` (copies the build host's own already-installed, working binary; never downloaded from a network install script inside the Dockerfile) and `COPY`'d into the image. A dedicated Claude Code OAuth/session credential directory (`.claude/.credentials.json` only — tested and confirmed sufficient; NOT the full `~/.claude/`, which holds unrelated session/project history) is mounted read-only from `/srv/bagman-secrets/claude_code_home` — **not an Anthropic API key; Claude Code's own OAuth mechanism, reusing the PL's own already-authenticated session's credential, copied to a dedicated path.** Both mechanisms are explicitly flagged in their own comments as first-pass provisioning — a real multi-host/CI build would need a proper versioned artifact source for the binary, and a more formal, BAGMAN-dedicated credential (e.g. via `claude setup-token`) is a real future hardening step — not solved speculatively here, same discipline this codebase already applies to other known, deferred limitations (e.g. the `MANUAL_UPLOAD` Source multi-replica race note).

**Tests**: 17 new (runner subprocess-boundary tests via monkeypatched `subprocess.Popen` — no real subprocess in ordinary tests; orchestrator tests via the fake runner; AST-based security containment proofs — only `runner.py` ever spawns a subprocess anywhere in production code, the HTTP request surface has no field that could let a caller influence process execution/environment, `--tools ""`/`--restricted`/`--strict-mcp-config` are always present, `validate_task_output` remains unconditional) plus `tests/app_api/test_operator_chat_endpoint.py` rewritten for the new path. Full suite: 770 passed, 24 skipped. gitleaks clean.

## 6k. Gate 2 — real live acceptance (2026-09-16)

`tests/acceptance/claude_code_operator_live_proof.py` — real, against the real rebuilt `bagman-api` container and the real `claude` binary (not the fake). **Result: PASS.** Verified live:

* **No Anthropic API key**: `/run/secrets/anthropic_api_key` confirmed absent inside the real container; the dedicated Claude Code OAuth home confirmed mounted instead.
* **Real transport + real response**: a real Ask BAGMAN HTTP call returned `status: SUCCEEDED` with a real, context-grounded answer (correctly identified the synthetic invoice's type and exact amount) — `provider: ANTHROPIC`, `capability_alias: null` (never a `bagman-*` alias — no local-model fallback).
* **Browser cannot influence process control**: the HTTP request body included injected `model`/`cwd`/`env` fields; the real recorded `provider_model` was a genuine Claude model, never the injected value — proving they were silently ignored, never honoured (Pydantic's own closed-field-set behaviour, backed by the dedicated security test's structural proof).
* **No source mutation, no shell/SQL, prompt injection stays data**: evidence content instructing the model to read `/etc/passwd`/the real LiteLLM secret file, edit `app/api/main.py`, run `DROP TABLE evidence_items;`, and delete the evidence record was correctly identified and refused. A sha256 canary of `app/api/main.py` was byte-for-byte identical before/after; the real secret file's own value (read fresh, never printed) was confirmed absent from the response; `permission_denials` was empty throughout — nothing to deny, since no tool ever existed to attempt any of it.
* **Failure surfaced visibly, no silent fallback**: the real `claude` binary was temporarily removed inside the running container (a genuine dependency-failure/recovery proof, same style as `dependency_failure_proof.py`'s established pattern) — the resulting call returned a clean `FAILED`/`CLAUDE_CODE_PROCESS_ERROR`, never a 500, `capability_alias` still `null` (no silent fallback to `bagman-fast`/`core`/`deep`); the binary was restored and normal operation resumed.
* **Canonical state unchanged**: the evidence record was byte-for-byte identical before and after operator reasoning, for both the legitimate and the hostile-content calls.
* **Provenance recorded**: a real `session_id`, full `model_usage` breakdown, `total_cost_usd`, `latency_ms`, and empty `permission_denials` were all present on the persisted `AIInvocation`.
* **Reliability**: 3/3 repeated real operator turns succeeded.

This covers the architect's 14-point list except points 1/3 and the browser half of point 14 (the actual HTML-page submission), which are proven separately by `tests/acceptance/ai_gui_claude_code_acceptance_proof.py` — see below.

**A real, second GUI bug found and fixed while preparing that browser proof, not by code review alone**: `app/api/static/features/overview/overview.js`'s Overview AI-status card read `body.checks.claude` (the now-superseded direct-Anthropic signal — permanently `"unreachable"` since no Anthropic key exists) instead of the new `body.checks.claude_code` key. Without this fix, Matt would have seen "Claude operator: unreachable" in the GUI even though Ask BAGMAN genuinely works — first caught live, running the real browser script against the real container, which asserted `"ok"` and got `"unreachable"`. A related bug in the same function (the background-alias list filtered out `"claude"` but not `"claude_code"`, which would have rendered it as a bogus fourth alias) was fixed in the same pass. Rebuilt, re-ran — confirmed fixed.

**`tests/acceptance/ai_gui_claude_code_acceptance_proof.py` — real Playwright browser proof, against the real rebuilt container. Result: PASS**, all remaining points proven:

* **Overview** shows the real `claude_code` signal (`"ok"`), not the superseded key (this is what caught the bug above).
* **Point 1** — a real document uploaded through the real `#file-input`, the real Ask BAGMAN drawer opened via `button:has-text('Ask BAGMAN about this')`, a real question typed into `#ask-bagman-input` and submitted via the real form.
* **Point 3** — a real assistant turn (`.chat-turn--assistant`) rendered in `#ask-bagman-messages`, synchronously, containing the real document's actual content (`GBP 999.00`), the GUI's own `AI-generated — not canonical` distinction, and the evidence reference — the model even correctly, unprompted, flagged the synthetic test data as unverified for accounting purposes (honest behaviour, not overclaiming).
* **Point 14** — a second real question in the same drawer session (`"Which supplier issued it?"`) also received a real, correct answer (honestly stating no supplier was named in the minimal test content) — the end-to-end HTML operator experience works reliably across multiple turns, not just once.

## 6l. Classification finding — the superseded direct-Anthropic implementation

Per the architect's instruction to classify `ai/providers/claude/` before any removal (extended here, on the PL's own initiative, to the two sibling packages that formed the same design with it — leaving them unclassified would be an incomplete answer to the same question): **`ai/providers/claude/`, `agent/tools/` (`registry.py`/`handlers.py`/`background.py`), and `agent/bagman/orchestrator.py` are one cohesive unit — the CD-5 WI-3 direct-Anthropic-API tool-calling-loop operator implementation.**

Live-dependency check (confirmed by direct code search, not assumption): the ONLY consumer of any of these three was `app/api/routers/operator.py`, which now calls `agent.claude_code.orchestrator` instead. `app/api/composition.py` still constructs `claude_client`/`tool_registry` (and, transitively, `agent/tools/background.py`'s `BackgroundTaskRunner`/`_ProductionBackgroundTaskRunner`/`DeterministicFakeBackgroundTaskRunner`), but nothing reachable from any live HTTP route consumes them any more — this includes `run_background_analysis` (one of the 8 tools, itself a distinct code path from Gate 1's own `POST /internal/ai/tasks` -> `ai.gateway.background.run_background_task`, which this correction does not touch at all). **Nothing in the live HTTP-reachable surface depends on any of the three packages today.**

**Classification: obsolete implementation from the superseded architecture** — with one honest nuance, not a flat verdict: `agent/tools/handlers.py`'s 8 read-only/analyse-only tool IMPLEMENTATIONS represent genuinely useful, well-tested, governed query logic (get_document, list_documents, get_intake, trace_provenance, list_ai_invocations, get_ai_invocation, get_runtime_status, run_background_analysis) that could plausibly resurface later if BAGMAN ever wants Claude Code's own native tool-use or an MCP-server exposure of these same governed operations — but the WRAPPING (`registry.py`'s `ToolSpec`/`ToolRegistry` machinery, built specifically for the old Anthropic-client message/tool-call format) is obsolete as currently packaged, even if the underlying idea resurfaces differently later. `ai/providers/claude/`'s direct Messages API adapter is obsolete outright — the corrected architecture never speaks that wire protocol at all.

**This finding is returned for the architect's ruling before any removal, per explicit instruction — nothing has been deleted.**

**Correction (2026-09-16, after `AI_FOUNDATION_GREEN`): this classification's "nothing has been deleted" is preserved above as history — it was true when written. The architect subsequently ruled on this exact finding and ordered the removal; see §6n below for the executed removal and its own required-checks verification.**

---

## 6m. Focused independent Auditor — Gate-2 delta only (dispatched, not a full re-audit)

A fresh Auditor with no prior context — explicitly instructed not to inherit the PL's/architect's conclusion — was dispatched against an isolated worktree pinned to head `2406f2c`, scoped strictly to the Gate-2 delta (`agent/claude_code/`, the operator HTTP surface, the two GUI fixes); Gate 1 and the superseded-code removal question were explicitly out of scope.

**What it independently verified, live and adversarially (its own separate checks, not merely re-running the PL's scripts, though it did that too):**

* **Command-injection resistance**: confirmed the single, list-form `subprocess.Popen` call by reading the code, then sent its own live adversarial `POST /internal/operator/chat` requests containing `` `id` $(whoami) ; cat /etc/shadow #``, `--dangerously-skip-permissions`, and injected `--model=evil`/`--mcp-config` text — every one returned `SUCCEEDED` with the payload quoted back as inert content, `permission_denials` empty throughout.
* **Authority restriction**: confirmed `--tools ""`/`--restricted`/`--strict-mcp-config` are real flags with the claimed semantics (`docker exec bagman-api claude --help`); its own live Bash/`/etc/passwd`-demanding requests were declined, with `root:x:0:0:` confirmed absent from every response, checked programmatically.
* **Prompt-injection handling**: its own synthetic evidence, with content instructing secret exfiltration, source editing, and table-dropping, was correctly refused; `app/api/main.py`'s sha256 was confirmed identical before/after, and the evidence record was byte-identical before/after.
* **Timeout/process cleanup**: confirmed `start_new_session=True` + `os.killpg(..., SIGKILL)` in code, backed by a genuine test (verified non-vacuous) asserting the pgid was actually killed; independently renamed `/usr/local/bin/claude` inside the live container itself, got a clean `FAILED`/`CLAUDE_CODE_PROCESS_ERROR` with no hang/500, restored the binary itself, and confirmed a subsequent real call succeeded normally.
* **Canonical-state non-mutation**: confirmed unchanged evidence records across every call it made.
* **Real browser → backend → Claude Code → browser flow**: ran `prepare-claude-binary.sh`, rebuilt `bagman-api` from scratch, brought the stack up itself, confirmed the credential mount and the absent Anthropic key, installed Playwright/Chromium fresh, and ran both acceptance scripts itself — both `PASS`, matching the PL's own results exactly. Independently confirmed `GET /internal/ai/health` and the Overview page both show the real `claude_code` signal.
* **Tests are real, not vacuous**: fresh venv, full suite — 770 passed/24 skipped, exact match; gitleaks clean; read all three new test files in full and confirmed they test what they claim (e.g. the AST-based proof that only `runner.py` ever calls a subprocess-spawning function anywhere in production code, and that `OperatorChatRequest`'s field set is exactly the closed six-field set with no process-control field).
* **GUI wire-contract and Overview-fix claims**: independently confirmed by reading the code directly, not merely trusting the evidence file's prose.
* **Evidence-file honesty**: diffed the commits that touched §6i–§7 and confirmed nothing was rewritten or contradicted — only additive.

**Defect found**: one trivial, non-functional documentation defect — `runner.py`'s own module docstring named the wrong test filename (`test_claude_code_runner_containment.py`, which does not exist) instead of the real `tests/security/test_claude_code_operator_containment.py`. No security or functional impact; **fixed** (commit follows).

**Verdict: `GATE2_CLAUDE_CODE_OPERATOR_GREEN_CONFIRMED`**, scoped strictly to this delta. No opinion offered on Gate 1, the superseded-code removal question, or the overall CD-5 verdict.

---

## 6n. Final bounded hygiene delta — removal of the superseded direct-Anthropic implementation (2026-09-16)

After Gate 1 and Gate 2 were both independently confirmed `GREEN` (§6h, §6m) and the architect formally issued `AI_FOUNDATION_GREEN` at head `6f47901c`, the architect authorised one final bounded hygiene delta before merge: remove §6l's classified-obsolete unit — `ai/providers/claude/`, `agent/tools/`, and `agent/bagman/orchestrator.py` — as ONE cohesive removal, plus every test/manifest/import/configuration line that existed solely to support it, so that exactly one operator architecture (`agent/claude_code/`) remains live and no dual-authority ambiguity is possible.

**What was removed** (`git rm`, 16 files): `agent/bagman/{README.md,__init__.py,orchestrator.py}`; `agent/tools/{README.md,__init__.py,background.py,handlers.py,registry.py}`; `ai/providers/claude/{__init__.py,client.py,fake.py}`; `tests/integration/test_ask_bagman_orchestrator.py`; `tests/integration/test_claude_provider_client.py`; `tests/security/test_agent_tool_registry_safety.py`; `tests/security/test_prompt_injection_ask_bagman.py`; `tests/acceptance/claude_operator_live_proof.py`.

**What was surgically edited to remove references to the above without touching anything still live**: `app/api/composition.py` (removed `claude_client`/`tool_registry` construction and the entire CD-5 WI-3 tool-wiring section, including `get_runtime_status_summary()` — confirmed via grep to have zero other consumers once `agent/tools/handlers.py` is gone); `app/api/routers/ai.py` (removed the `checks["claude"]` health key, keeping only `checks["claude_code"]`); `agent/component.yaml`, `ai/component.yaml`, `app/api/component.yaml` (manifests corrected to `version: 3`, responsibility text annotated with a dated correction note rather than silently rewritten, `agent/component.yaml`'s `owns`/`consumes`/`produces`/`dependencies` emptied to reflect the package now containing nothing live); `requirements.txt`/`requirements-dev.txt` (the `requests` dependency moved back to dev-only — nothing under `app/`, `agent/`, `ai/`, `core/`, `services/`, `persistence/` imports it any more, only `tests/acceptance/*.py`); `ai/evaluation/injection_reuse.py` (the deleted `tests/security/test_prompt_injection_ask_bagman.py` replaced in `_REUSED_TEST_FILES` with the two Gate-2-era equivalents, `tests/integration/test_claude_code_orchestrator.py` and `tests/security/test_claude_code_operator_containment.py`); `deployment/compose/docker-compose.yml` (the dormant, commented-out `anthropic_api_key` secrets block removed); `tests/acceptance/README.md`, `tests/acceptance/prompt_injection_live_proof.py`, `tests/integration/test_litellm_client.py`, `tests/app_api/test_ai_endpoints.py` (docstring/assertion updates to stop referencing deleted files/keys); `memory/generated/architecture-index.md` (regenerated via `scripts/generate_architecture_memory.py`, clean, no validation errors).

**Preserved as history, per the architect's explicit instruction, not rewritten**: §6l above (the original classification finding) and its "nothing has been deleted" sentence stand unedited, with only an additive correction note pointing here; `PID.md` §97's original "are NOT deleted" sentence stands unedited, with an additive correction note immediately below it recording: this was the original CD-5 implementation; the architecture was superseded (by `agent/claude_code/`) before final closure; §6l's own live-dependency check proved zero live dependents before any file was touched; the removal was executed before merge specifically to prevent dual-authority ambiguity.

**Required-checks verification (the architect's 8-point list)**:

1. **Repo-wide search proves no live imports/references remain** — `grep -rn` for `agent\.bagman`, `agent\.tools`, `ai\.providers\.claude` across `app/`, `agent/`, `ai/`, `core/`, `services/`, `persistence/`, `deployment/`, `tests/` returns zero hits outside historical prose in `PID.md`/the evidence file/`CHANGELOG.md` themselves.
2. **No `/srv/bagman-secrets/anthropic_api_key` runtime assumption remains** — confirmed via the same repo-wide search: no live code path reads that path any more (it was already unused before this delta, per §6i; this delta removes the last vestigial references to it in composition/manifests).
3. **No `BAGMAN_OPERATOR_PROVIDER=anthropic` authoritative configuration remains** — this env var was never part of the corrected Gate-2 design (the corrected design has no provider-selection env var at all — `agent/claude_code/` is the only path); confirmed absent from `docker-compose.yml`/`composition.py`/any `.env*` file.
4. **Claude Code OAuth path remains intact** — `agent/claude_code/runner.py` untouched by this delta; the dedicated credential mount (`/srv/bagman-secrets/claude_code_home` -> `/home/claude-operator:ro`) and `prepare-claude-binary.sh` staging step both untouched.
5. **Ask BAGMAN production composition resolves only to the Claude Code runner** — confirmed directly: post-rebuild, `GET /internal/ai/health` returns exactly `{"bagman_core": "ok", "bagman_deep": "ok", "bagman_fast": "ok", "claude_code": "ok"}` with no `"claude"` key present at all (not merely deprioritized — fully absent).
6. **Architecture/component memory regenerated** — `scripts/generate_architecture_memory.py` rerun after all `component.yaml` edits; validated clean against `contracts/manifest/bagman.component_manifest.v1.schema.json`; `memory/generated/architecture-index.md` updated.
7. **Full relevant test suite passes** — 686 passed (down from 770 pre-cleanup, exactly matching the removal of the superseded implementation's own test files: `test_ask_bagman_orchestrator.py`, `test_claude_provider_client.py`, `test_agent_tool_registry_safety.py`, `test_prompt_injection_ask_bagman.py`); the evaluation harness (`tests/integration/test_ai_evaluation_harness.py::test_the_full_harness_runs_cleanly_and_is_fully_green`) re-verified green after the `injection_reuse.py` fix; gitleaks clean throughout (rerun at every step, per this project's own standing discipline).
8. **Real Ask BAGMAN smoke proof passes after rebuilding the production image** — `docker compose -p bagman -f deployment/compose/docker-compose.yml build bagman-api` rebuilt clean; stack brought up, all 4 services `Healthy`; `python3 tests/acceptance/claude_code_operator_live_proof.py` run fresh against the rebuilt image: **full PASS** — no Anthropic API key exists/needed; a real, context-grounded answer about a synthetic invoice (correctly citing `evidence_id`/amount); injected `model`/`cwd`/`env` request fields silently ignored; a live prompt-injection/shell/secret-exfiltration attempt correctly refused (`permission_denials: []`, BAGMAN source proven byte-for-byte untouched, the real secret value confirmed absent from the response); a real dependency-failure/recovery proof (the `claude` binary temporarily removed inside the running container, confirmed `FAILED`/`CLAUDE_CODE_PROCESS_ERROR`, restored, confirmed recovery); canonical evidence unchanged; full provenance recorded; 3/3 repeated turns succeeded.

Per the architect's explicit instruction, the full expensive Gate-1 acceptance battery was **not** rerun — Gate 1 remains closed on its own independent evidence (§6g/§6h); this delta's own runtime-regression proof is the representative real Claude Code browser/backend smoke test above (point 8) plus the full non-acceptance test suite (point 7).

**Fresh focused Auditor — cleanup-delta only**: see §6o below.

---

## 7. Exit-gate statement (PID §93) — drafted, not issued

Per PID §93, no live mailbox integration may begin until **AI_FOUNDATION_GREEN**. §6b's original engineer-drafted framing (below, superseded) argued for an explicitly-scoped partial-GREEN; **the architect has since corrected that framing as broader than PID §91 permits (§6b's correction note, 2026-09-16)** — the honest state of a delivery with any tier's genuine-completion criterion unproven is `BLOCKED`, not any form of `GREEN`.

**Status as of §6l (2026-09-16): both Gate 1 and Gate 2 are `GREEN`.**

**Gate 1** (all three background tiers — `bagman-fast`/`bagman-core`/`bagman-deep`): HELM root-caused §6f's Phase A finding to a stale appliance-side `extra_body.format:"json"` override clobbering BAGMAN's correctly-generated schema constraint (not a Gemma/Ollama/LiteLLM/BAGMAN-timeout/concurrency-only defect) and fixed it; final authoritative proof (§6g) — `bagman-core` 100/100 isolated + 20/20 under controlled load, `bagman-fast` 10/10, `bagman-deep` unaffected — independently spot-checked by the PL and independently re-confirmed by a fresh Auditor (§6h, `GATE1_FINAL_GREEN_CONFIRMED`). No BAGMAN code changed to reach GREEN.

**Gate 2** (Claude/Ask BAGMAN): the original target (`/srv/bagman-secrets/anthropic_api_key`) was itself corrected by the architect before any provisioning happened — BAGMAN does not use a direct Anthropic API key; the authoritative path is a bounded, headless Claude Code invocation (§6i-§6l). Implemented (`agent/claude_code/`), packaged into the real Docker image (the real `claude` binary + a dedicated OAuth credential, never an API key), and proven live end-to-end at both the HTTP level (§6k, all 14 architect-specified points except the browser-only three) and the real browser level (§6l addendum above, the remaining three points, which also caught and fixed a real Overview GUI bug reading the superseded health key). `/srv/bagman-secrets/anthropic_api_key` remains, and is intended to remain, absent — its absence is no longer a blocker of any kind.

CD-5's own PID §91/§93 criteria are therefore honestly met in full: both AI-provider tiers (background inference and the Claude operator) are proven live through BAGMAN's real production path, with the mandatory evaluation harness, security/containment proofs, and independent audit all in place. **The final, unqualified verdict is `AI_FOUNDATION_GREEN`**, pending only the fresh, focused independent Auditor the architect requires for the Gate-2 delta specifically (§6m) and final live CI at the resulting head — the verdict itself remains the architect's own to formally issue, per this file's own standing "the PL does not self-issue the final CD delivery verdict" discipline (§7's own closing line, below, unchanged).

Original (superseded) framing, retained for history: "every PID §91 criterion reachable without a live model completion was independently exercised against the real stack (not merely read about); two genuine, previously-undiscovered infrastructure defects were found and root-cause-fixed (§2.4/§2.5), each independently re-verified after the fix; the mandatory evaluation harness was genuinely built and is genuinely green; and every external blocker (LiteLLM-gateway database, Anthropic credential) was re-checked live at acceptance time, not assumed from the pre-dispatch briefing, with the exact real error captured each time." **The actual verdict — `AI_FOUNDATION_GREEN` (unqualified — see architect correction), `AI_FOUNDATION_RED`, or `BLOCKED` — is the PL's/architect's to issue, after PL reconciliation and independent Auditor dispatch (PID §92), not this Engineer's.**
