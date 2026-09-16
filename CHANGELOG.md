# Changelog

All notable changes to BAGMAN will be documented in this file.

## 2026-09-12 — CD-1 — Foundation Security Scaffold

- Established repository structure (component and directory skeleton).
- Established security doctrine (public-repository invariant, external
  secret handling, secret consumption model).
- Established configuration contract (three-layer model).
- Added security test suite (`tests/security/`, PID §15 Test A-E) and
  CI secret-scanning gate (`.github/workflows/security.yml`).
- No business capability implemented.
- **Verdict: FOUNDATION_GREEN** (independent Auditor review + PL
  spot-check, commit `7f60a6c`). See
  `memory/generated/CD1-FOUNDATION-SECURITY-SCAFFOLD-EVIDENCE-2026-09-12.md`
  for the full evidence trail.

## 2026-09-12 — CD-2 — Canonical Entity, Evidence, Provenance & Audit Foundation

- Canonical JSON Schema contracts (entity, evidence, source,
  external_reference, provenance, audit_event, plus common identity/
  timestamp/schema-version primitives).
- Domain models + in-memory repositories (`core/`, `services/evidence/`)
  and the `BagmanCanonicalAPI` facade — UUIDv7 identity, immutable
  evidence with two narrow controlled mutations, idempotent external-
  reference/evidence-observation handling, append-only audit events
  with correlation/causation.
- Component manifest schema + manifests for `core/` and
  `services/evidence/` + a deterministic architecture-memory projection
  (`scripts/generate_architecture_memory.py`, with drift detection).
- Contract, architectural-boundary, and domain/lineage integration
  tests, synthetic fixtures, and the PID §43 10-step runtime proof
  (102 tests total).
- CI extended to run the full suite; pytest rootpath fixed generally
  via `pyproject.toml`.
- No mailbox, bank, accounting, or billing connectivity implemented.
- **Verdict: FOUNDATION_MODEL_GREEN** (independent Auditor review + PL
  spot-check, commit `890c58b`). See
  `memory/generated/CD2-CANONICAL-ENTITY-EVIDENCE-PROVENANCE-AUDIT-EVIDENCE-2026-09-12.md`
  for the full evidence trail.

## 2026-09-12 — CD-3 — Runtime & Evidence Store

- PostgreSQL persistence: SQLAlchemy models, Alembic migrations, six
  durable repository implementations (`persistence/postgres/`)
  preserving every CD-2 guarantee, now proven durable across restart.
- Object evidence storage: `EvidenceObjectStore` abstraction + a
  boto3/MinIO implementation (`persistence/objects/`) with hash
  verification and immutable, content-addressed storage.
- `bagman-api` (FastAPI) container runtime + Docker Compose
  (`bagman-db`/`bagman-objects`/`bagman-api` on a dedicated network,
  real secrets, no host port exposure except `bagman-api` on
  `127.0.0.1`), a composition root switching in-memory vs.
  Postgres/MinIO backends with a hard no-silent-fallback guarantee,
  health/readiness, `/version`, and an internal evidence upload/
  download API.
- Backup/restore tooling (`ops/`) and three required acceptance
  proofs (`tests/acceptance/`): full restart (idempotency intact),
  container rebuild (`bagman-api` only), and destroy-then-restore-
  into-a-provably-clean-target — all three run for real against
  Docker, not simulated.
- No mailbox, bank, accounting, or billing connectivity implemented.
- **Verdict: RUNTIME_FOUNDATION_GREEN** (independent Auditor review +
  PL spot-check, commit `5d03198`; two real live-CI-only failures found
  and fixed post-audit; one PID §59 CI-control gap — the architecture-
  memory drift check missing from CI — found by architect delta review
  and fixed; final approved head `2708918`). See
  `memory/generated/CD3-RUNTIME-AND-EVIDENCE-STORE-EVIDENCE-2026-09-12.md`
  for the full evidence trail.

## 2026-09-13 — CD-4 — Evidence Intake & Manual Upload Foundation

- Governed Evidence Intake: `IntakeRecord` domain model + PID §7 state
  machine (RECEIVED/VALIDATING/QUARANTINED/REJECTED/ACCEPTED/
  REGISTERED/FAILED), durable PostgreSQL persistence with a real
  partial-unique-index-backed idempotency-conflict doctrine.
- Full content-validation pipeline: filename safety, hand-rolled
  content-type sniffing (detected vs. reported MIME), bounded/
  streaming ingest with incremental SHA-256, a real `ClamAVScanner`
  (raw `clamd` protocol, no new dependency) behind an
  `EvidenceSafetyScanner` abstraction, explicit archive/executable/
  unsupported-type/scan-failure policy, and quarantine.
- `POST /internal/intake/evidence` — the single governed HTTP boundary
  through which untrusted bytes may become canonical evidence — plus
  paginated `GET /internal/intake`/`GET /internal/intake/{id}`. The
  CD-3 direct-upload bypass (`POST /internal/evidence` accepting raw
  file bytes) is removed entirely, not merely deprecated.
- The first BAGMAN Documents GUI (`app/api/static/`): plain HTML/CSS/
  vanilla-JS, no framework/build step, served directly from
  `bagman-api`. Real upload, honest workflow-status rendering, list/
  detail/download, distinct QUARANTINED/REJECTED/FAILED presentation.
- `bagman-scan` (ClamAV) added to the Docker Compose runtime; `/ready`
  extended to prove scanner reachability alongside postgres/
  object_store, preserving the existing no-fallback/no-caching
  doctrine.
- Acceptance/hardening (WI-5): re-ran and confirmed every WI-1-4
  acceptance script against the real stack; found and root-cause-fixed
  one real concurrency race (a genuinely concurrent narrow-replay
  request could raise an unhandled `InvalidStateTransitionError`
  instead of resolving cleanly — reproduced with real barrier-
  synchronized threads against a real PostgreSQL, fixed in
  `app/api/routers/intake.py`/`services/evidence/intake/
  validation_pipeline.py`); four new real-stack proof scripts
  (idempotency/concurrency, content-policy/quarantine fixtures against
  a real ClamAV daemon, dependency-failure with recovery, real-browser
  acceptance via Playwright against the real Docker Compose stack);
  nine new architecture-boundary tests (PID §67).
- No mailbox, bank, accounting, or billing connectivity implemented.
- A fresh, independent Auditor reviewed the whole delivery cold,
  re-ran everything from scratch, and found one additional real defect
  (a pre-existing CD-2-era architecture test with no `assert`
  statement, silently vacuous — fixed) and one blocking condition (no
  live CI had ever run against any CD-4 branch, since no PR yet
  existed). Both closed: PR #4 opened, live CI observed genuinely
  green at head `3592c82`
  (`https://github.com/maff0000/bagman/actions/runs/34751927607`).
  Full evidence, including the Auditor's own report and the live-CI
  confirmation, at
  `memory/generated/CD4-EVIDENCE-INTAKE-AND-MANUAL-UPLOAD-FOUNDATION-EVIDENCE-2026-09-13.md`.
- **Verdict: pending the architect's ruling** (PID §72/§73 — the same
  standing as CD-1/CD-2/CD-3; the PL does not self-issue the final CD
  delivery verdict).

## 2026-09-13 — CD-5 — AI Foundation, Claude Operator & GUI Integration

- Locked three-tier AI topology (PID amendment, prior to WI-1 dispatch):
  Claude as operator intelligence; a dedicated, BAGMAN-exclusive Mac
  mini (`bagman-fast`/`bagman-core`) as primary background inference;
  Trinity compute (`bagman-deep`) as an escalation tier — all reached
  through the ONE existing Trinity LiteLLM installation, never a
  second inference-control-plane architecture.
- `ai/`: provider-neutral `AIInvocation` domain model + state machine,
  a real database-enforced one-active-invocation-per-subject
  concurrency guard, the typed task contract/registry
  (`DOCUMENT_SUMMARY`/`DOCUMENT_TYPE_PROPOSAL`/`ENTITY_PROPOSAL`/
  `OPERATOR_DOCUMENT_REVIEW`/`ASK_BAGMAN`), the one alias-only
  LiteLLM adapter (mechanical `trinity-*`/raw-model-name rejection),
  the Anthropic Messages API adapter, `run_background_task`'s full
  orchestration lifecycle, and the mandatory evaluation harness
  (`ai/evaluation/` — structured-output validity, expected
  classification, abstention/uncertainty, malformed output, timeout/
  error handling, and reused prompt-injection resilience proofs).
- `agent/`: a fixed, closed 8-tool read-only/analyse-only registry
  Claude may invoke, and Ask BAGMAN's bounded tool-calling
  orchestration loop, with structural prompt-injection defences
  (evidence/tool-result content is always DATA, never authority).
- `POST /internal/ai/tasks`, `GET /internal/ai/invocations[/{id}]`,
  `GET /internal/ai/health`, `POST /internal/operator/chat`.
- GUI modularisation (`app/api/static/{shell,shared,features/
  {overview,documents,ai}}`), the Documents AI panel, the Ask BAGMAN
  drawer, and Overview AI status — all visually distinguishing
  AI-proposed from canonical data.
- Filename bidi/directional-format-control hardening (closing a CD-4
  Auditor backlog item): a filename carrying a Unicode bidi-override
  character (the classic visually-disguised-extension attack) is now
  rejected at intake, structurally and adversarially tested.
- Acceptance/hardening (WI-5): the mandatory evaluation harness (above);
  a genuine real-threaded concurrency proof against a real disposable
  PostgreSQL; a repo-wide `trinity-*`-alias-absence sweep; five new
  real-Docker-Compose-stack acceptance scripts covering all three AI
  tiers, live prompt injection, and AI-surfaces browser acceptance
  (Playwright) — **found and fixed two real, previously-undiscovered
  infrastructure defects**: the `bagman-api` Docker image was missing
  the entire `ai/`/`agent/` packages (every real AI endpoint would have
  crashed with `ModuleNotFoundError` in any actual deployment), and
  `bagman-api`'s own container could not reach the real LiteLLM gateway
  over the network at all (`localhost` inside a container is not the
  host) — fixed via `host.docker.internal`/`extra_hosts: host-gateway`,
  verified reachable from inside the real running container. Every real
  external AI-tier acceptance attempt (Mac-mini, Trinity-escalation,
  Claude) reached the real, now-fixed network/adapter/credential path
  and received an honest, correctly-recorded `BLOCKED` outcome —
  re-verified live, not assumed — from external infrastructure this
  delivery does not own (the existing LiteLLM gateway's own backing
  database; Matt's not-yet-provisioned Anthropic credential).
- No mailbox, bank, accounting, or billing connectivity implemented.
  No second LiteLLM/inference-control-plane architecture introduced.
- Full evidence, including the honest per-tier BLOCKED findings and
  the two infrastructure defects found and fixed, at
  `memory/generated/CD5-EVIDENCE-AI-FOUNDATION-CLAUDE-OPERATOR-AND-GUI-INTEGRATION-2026-09-13.md`.
- **Verdict: pending the architect's ruling** (PID §92/§93 — the same
  standing as every prior CD delivery; the PL does not self-issue the
  final CD delivery verdict).

### 2026-09-16 — Gate-1 real-provider-acceptance closure

- **Architect correction**: the partial-`AI_FOUNDATION_GREEN` framing
  above was ruled broader than PID §91 permits — until real-provider
  acceptance is proven per tier, the honest state is `BLOCKED`, not any
  form of `GREEN`.
- HELM retired the shared Trinity LiteLLM gateway (its backing database
  outage traced to `user_api_key_auth()`'s own DB dependency, not
  merely a bad credential) in favour of a new, BAGMAN-exclusive
  dedicated AI appliance (`BAGMAN_AI_APPLIANCE_GREEN`,
  `http://192.168.11.4:4100` on the dedicated Mac mini) —
  `BAGMAN_LITELLM_ENDPOINT` repointed accordingly; the WI-5
  `host.docker.internal`/`extra_hosts` fix removed as no longer needed
  (a real LAN address).
- Real acceptance against the new appliance surfaced a genuine
  application-layer reliability gap (not fabricated success, not
  silently tuned around): `bagman-core` exceeded its task SLA under
  real latency; `bagman-fast` returned empty content after exhausting
  its token budget. HELM's `think:false` + plain-JSON-mode fix
  (`BAGMAN_LOCAL_AI_TASKS_RED` — infrastructure genuinely fixed, an
  application-layer contract gap correctly exposed rather than hidden)
  plus an authorised, narrow BAGMAN delta closed it: `ai.gateway.
  background.run_background_task` now passes each task's own
  `output_schema` to the LiteLLM adapter as a per-request
  JSON-schema-constrained `response_format` (Ollama structured
  outputs), while BAGMAN's own `validate_task_output` remains the
  unconditional, canonical safety boundary regardless of provider-side
  structured-generation support. No `timeout_seconds` changed.
- Real 20x acceptance, against the real rebuilt `bagman-api` and the
  real appliance: `DOCUMENT_TYPE_PROPOSAL`/`bagman-fast` 20/20
  SUCCEEDED (p95 9.06s vs 20s SLA); `DOCUMENT_SUMMARY`/`bagman-fast`
  5/5 SUCCEEDED with its own distinct schema explicitly confirmed;
  `ENTITY_PROPOSAL`/`bagman-core` 20/20 SUCCEEDED (p95 13.95s vs 30s
  SLA); `bagman-deep` reconfirmed GREEN, unaffected.
- **Gate 1 (all three background tiers) is CLOSED.** Gate 2 (Claude)
  remains OPEN pending `/srv/bagman-secrets/anthropic_api_key`. Full
  evidence at
  `memory/generated/CD5-EVIDENCE-AI-FOUNDATION-CLAUDE-OPERATOR-AND-GUI-INTEGRATION-2026-09-13.md`
  §6b (correction note), §6d, §6e. **CD-5 remains `BLOCKED`, not
  `GREEN`, until Gate 2 also closes.**

### 2026-09-16 (same day, later) — Gate-1 isolation experiment and architect GREEN ruling

- A controlled 50-call isolated (zero concurrent load) `ENTITY_PROPOSAL`/
  `bagman-core` experiment reached **49/50** — a real, low-rate schema-
  compliance gap, safety net proven robust throughout (every failure
  correctly rejected, never a false success). Per the architect's own
  stop-rule, this halted Gate-1 closure and was handed to HELM rather
  than worked around in BAGMAN code.
- **Final root cause**: not a Gemma/Ollama/LiteLLM/BAGMAN-timeout/
  concurrency-only defect — a stale appliance-side
  `extra_body.format:"json"` override, left from before this Gate-1
  delta existed, was clobbering BAGMAN's correctly-generated
  schema-constrained request on some calls. HELM removed the stale
  override, retaining only `think:false`.
- **Final authoritative acceptance**: `bagman-core` 100/100 isolated +
  20/20 under controlled load; `bagman-fast` 10/10; `bagman-deep`
  unaffected. Alias-only enforcement and BAGMAN's own unconditional
  post-response validation reconfirmed unaffected. No BAGMAN code
  changed to reach this result.
- **Architect ruling: `BAGMAN_CORE_STRUCTURED_GREEN` — Gate 1 (all
  three background tiers) is GREEN.** Final architecture: BAGMAN → a
  dedicated, BAGMAN-exclusive Mac AI appliance (its own LiteLLM +
  PostgreSQL) → `bagman-fast`/`bagman-core` (local Mac model) and
  `bagman-deep` (Trinity escalation) — the shared Trinity LiteLLM
  gateway is no longer BAGMAN's primary AI control plane. Claude
  remains a wholly independent Anthropic operator path. Full addendum
  at `PID.md` §96; full evidence at
  `memory/generated/CD5-EVIDENCE-AI-FOUNDATION-CLAUDE-OPERATOR-AND-GUI-INTEGRATION-2026-09-13.md`
  §6g/§6h.
- **Gate 2 (Claude) is the sole remaining CD-5 blocker** —
  `/srv/bagman-secrets/anthropic_api_key` still does not exist as of
  this entry. **CD-5 remains `BLOCKED`, not the final unqualified
  `AI_FOUNDATION_GREEN`, until Gate 2 also closes.**

### 2026-09-16 (same day, later still) — Gate-2 architecture correction and closure

- **Architect correction**: BAGMAN does not use a direct Anthropic API
  key for its operator intelligence — the "Gate 2 blocked on
  `/srv/bagman-secrets/anthropic_api_key`" framing above is itself
  superseded. The PL investigated the architect's "already-proven"
  framing before implementing anything (a host-wide, read-only search)
  and found no existing implementation to reuse; the architect
  authorised a new, bounded implementation instead, built from two
  separately-proven precedents (synchronous FastAPI AI wrappers,
  headless `claude -p` invocation).
- New `agent/claude_code/` package: the ONE place BAGMAN ever execs a
  `claude` subprocess. Fixed executable/argument contract (never
  `shell=True`, never shell-string interpolation); `--tools ""` +
  `--restricted` + `--strict-mcp-config` strip the invoked process of
  every tool/MCP/ambient-settings capability — its authority is a
  strict subset of this host's own normal Claude Code development-agent
  authority, never inherited from it. No live tool-calling loop —
  BAGMAN assembles governed context before one bounded, synchronous
  invocation. Same `AskBagmanResult`/`AIInvocation`/`ASK_BAGMAN` v1
  contract the superseded design used, so the GUI needed zero code
  changes to its Ask BAGMAN drawer (one real, separate Overview-widget
  bug — reading the wrong health-check key — was found and fixed
  while preparing the browser proof).
- Real Docker packaging: the actual `claude` native binary baked into
  the `bagman-api` image; a dedicated Claude Code OAuth/session
  credential (never an API key) mounted read-only.
- Real live acceptance, against the real rebuilt container: HTTP-level
  proof (no Anthropic key needed; real transport and response; browser
  cannot influence process control; no source mutation/shell/SQL
  access; prompt-injection content stays data; a real dependency-
  failure/recovery proof; no silent fallback to `bagman-fast/core/deep`;
  canonical state unchanged; full provenance recorded) — PASS. Real
  Playwright browser proof (a real question submitted from the actual
  HTML page, a real answer rendering back synchronously, a reliable
  second turn) — PASS.
- `ai/providers/claude/`, `agent/tools/`, and `agent/bagman/
  orchestrator.py` are NOT deleted — classified as one cohesive
  obsolete unit (with one nuance: the 8 tool implementations'
  underlying logic could resurface differently later) and returned for
  the architect's ruling before any removal.
- Full evidence at
  `memory/generated/CD5-EVIDENCE-AI-FOUNDATION-CLAUDE-OPERATOR-AND-GUI-INTEGRATION-2026-09-13.md`
  §6i-§6m; `PID.md` §97.
- **Both Gate 1 and Gate 2 are now GREEN. The final, unqualified
  `AI_FOUNDATION_GREEN` verdict is met in substance**, pending the
  architect's own formal issuance after a fresh, focused independent
  Auditor reviews the Gate-2 delta specifically.
