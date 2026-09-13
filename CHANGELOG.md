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
  final CD delivery verdict). PID §91's own "an honest partial
  `AI_FOUNDATION_GREEN`, with the Mac-mini/Trinity-escalation/Claude
  tiers' genuine-completion criteria explicitly marked `BLOCKED`
  pending external infrastructure/credentials, is an acceptable interim
  verdict" is what this delivery's own evidence currently supports.
