<!--
  GENERATED FILE — DO NOT HAND-EDIT.
  Produced by scripts/generate_architecture_memory.py from:
    * every component.yaml manifest in the repository
    * every contracts/**/*.schema.json contract
    * config/base/entities.yaml
  Regenerate with: python3 scripts/generate_architecture_memory.py
  Check for drift with: python3 scripts/generate_architecture_memory.py --check
  This is a projection, not canonical truth — see
  memory/architecture/README.md (PID §27-28).
-->

# BAGMAN Architecture Index

## Components

### `BAGMAN.AGENT` (v1)

Own the BAGMAN AI agent's own reasoning/orchestration layer: the Ask BAGMAN operator-orchestration loop (agent/bagman/ — system-prompt construction, the bounded Claude tool-calling loop, AIInvocation lifecycle management, PID §57 audit emission) and the fixed, read-only/analyse-only governed tool registry Claude may invoke (agent/tools/ — CD-5 PID §34's 8 named tools, mechanically fail-closed dispatch per PID §63). This component decides WHICH of BAGMAN's own internal APIs/repositories a registered tool may call and HOW an Ask BAGMAN conversation is assembled/bounded (PID §53); it never instantiates an Anthropic/LiteLLM client itself (that lives in ai/providers/*/, consumed here only through the provider-neutral ClaudeClientProtocol/BackgroundTaskRunner seams) and never allows Claude to invoke anything outside the fixed tool set or to mutate canonical state (PID §17/§56). agent/policies/ and agent/memory/ remain empty CD-1 placeholders as of this WI — WI-3 found no genuinely CD-5-scoped content for either (see WI-3's delivery report): no autonomous policy engine is authorised yet (PID §16 — the tool registry's own fixed authority classes ARE this WI's governance boundary), and Ask BAGMAN's conversation state is in-request-only (PID §64 — no durable chat-memory-fabric integration is built this WI).

- **Owns:** _(none)_
- **Consumes:** `BAGMAN.AI`, `BAGMAN.CORE`, `BAGMAN.SERVICES.EVIDENCE`, `BAGMAN.EVIDENCE.INTAKE`
- **Produces:** `AI_INVOCATION_REQUESTED`, `AI_INVOCATION_SUCCEEDED`, `AI_INVOCATION_FAILED`, `AI_OUTPUT_REJECTED`
- **Dependencies:** _(none)_
- **External access:** `false`
- **Prohibited:** `direct_email_access`, `direct_bank_access`, `direct_xero_access`, `direct_chargebee_access`, `direct_anthropic_client_outside_ai_providers_claude`, `direct_litellm_client_outside_ai_providers_litellm`, `arbitrary_tool_invocation`, `canonical_state_mutation`

### `BAGMAN.AI` (v2)

Own the governed, provider-NEUTRAL AI invocation domain model and typed task contract/registry framework: the AIInvocation state machine and its one-active-invocation-per-subject concurrency guarantee (CD-5 PID §26-30/§73-76), and the TaskContract/TASK_REGISTRY framework naming CD-5's tasks' metadata and real, usable input/output JSON Schemas (PID §21-24/§55) — WI-1 delivered the original four; WI-3 additively registered a 5th, ASK_BAGMAN v1 (Ask BAGMAN's general operator chat task — see ai/tasks.py's own docstring for why this is a new task rather than a reuse of OPERATOR_DOCUMENT_REVIEW). WI-1 delivered only the domain/contract/persistence slice of BAGMAN.AI's eventual responsibility (PID §7's full statement: "route typed BAGMAN intelligence tasks to an authorised AI provider/capability, enforce task contracts and policy, record invocation provenance, validate structured responses and return proposals without directly mutating canonical business state"). WI-2 lands the BACKGROUND half of that: `ai/providers/litellm/` (the one LiteLLM-speaking adapter, its mechanical alias-only enforcement, and its deterministic fake) and `ai/gateway/` (`run_background_task`'s full REQUESTED -> RUNNING -> terminal orchestration, PID §57 audit emission). WI-3 lands the OPERATOR half's provider adapter: `ai/providers/claude/` — the ONE Anthropic Messages API adapter (PID §6/§8): a pure, BAGMAN-agnostic provider-neutral-result-producing HTTP client (ClaudeClient/ ClaudeClientProtocol) plus its deterministic test/dev double (FakeClaudeClient, PID §61) — still no BAGMAN-specific orchestration logic here (system prompt, tool registry, AIInvocation lifecycle management); that lives in the separate `agent/` component (BAGMAN.AGENT), which calls into this adapter. Each of `ai/providers/litellm/`, `ai/gateway/`, `ai/providers/claude/` now has its own `component.yaml`, consuming this one. Real prompt CONTENT for the three BACKGROUND tasks lives at `ai/prompts/` (PID §31), loaded by `ai/gateway/background.py`. `ai/policy/`, `ai/evaluation/`, and `ai/provenance/` remain WI-5's job to populate.

- **Owns:** `AIInvocation`, `TaskContract`
- **Consumes:** `BAGMAN.CORE`
- **Produces:** _(none)_
- **Dependencies:** `jsonschema`, `rfc3339-validator`, `requests`
- **External access:** `false`
- **Prohibited:** `direct_email_access`, `direct_bank_access`, `direct_xero_access`, `direct_chargebee_access`, `direct_litellm_client_outside_gateway`, `direct_anthropic_client_outside_gateway`, `trinity_star_alias_usage`

### `BAGMAN.AI.GATEWAY` (v1)

Own BACKGROUND-role task orchestration (CD-5 PID §6/§21-30/§57-58/ §73-76, WI-2): run_background_task drives one AIInvocation through its full REQUESTED -> RUNNING -> {SUCCEEDED, FAILED} lifecycle against the ai.providers.litellm adapter, using ai.prompts' versioned prompt assets, validating output via ai.tasks.validate_task_output, and emitting AI_INVOCATION_REQUESTED / AI_INVOCATION_SUCCEEDED / AI_INVOCATION_FAILED / AI_OUTPUT_REJECTED with full causation chaining. Provider- and composition-agnostic by construction (the repository, LiteLLM client, and audit-event emitter are all dependency-injected, never imported from app/api/ or persistence/ directly) so this component is fully unit-testable against deterministic fakes (PID §61). The OPERATOR-role equivalent (Claude) is WI-3's own addition to this same package, not this manifest's current scope — run_background_task explicitly refuses any OPERATOR-role task_id.

- **Owns:** _(none)_
- **Consumes:** `BAGMAN.AI`, `BAGMAN.AI.PROVIDERS.LITELLM`
- **Produces:** `AI_INVOCATION_REQUESTED`, `AI_INVOCATION_SUCCEEDED`, `AI_INVOCATION_FAILED`, `AI_OUTPUT_REJECTED`
- **Dependencies:** _(none)_
- **External access:** `false`
- **Prohibited:** `direct_email_access`, `direct_bank_access`, `direct_xero_access`, `direct_chargebee_access`, `direct_litellm_client_outside_gateway_adapter`, `trinity_star_alias_usage`

### `BAGMAN.AI.PROVIDERS.LITELLM` (v1)

Own the ONE adapter that speaks to the existing Trinity LiteLLM installation (CD-5 PID §6/§8/§9/§50, WI-2): LiteLLMClient (the real POST /v1/chat/completions OpenAI-compatible wire call, alias-only, bounded timeout/retry, fail-closed result normalisation into LiteLLMCompletionResult) and FakeLiteLLMClient (the deterministic substitute every ordinary test and dev/test composition uses, PID §61). Mechanically enforces BAGMAN's own alias-only routing boundary (validate_capability_alias) — this is the one place in the whole repository that could ever construct a request naming a physical model or a trinity-* alias, and it refuses to. Does not decide WHICH alias a task prefers (ai.tasks.TaskContract.preferred_capability owns that) and does not orchestrate a task's lifecycle (ai.gateway owns that) — this component's job ends at "send one bounded HTTP request for a validated alias, return a normalised result, never raise for a transport-level failure".

- **Owns:** `LiteLLMCompletionResult`
- **Consumes:** `BAGMAN.AI`
- **Produces:** _(none)_
- **Dependencies:** _(none)_
- **External access:** `true`
- **Prohibited:** `direct_email_access`, `direct_bank_access`, `direct_xero_access`, `direct_chargebee_access`, `direct_mac_mini_access`, `direct_ollama_mlx_llamacpp_access`, `trinity_star_alias_usage`, `raw_physical_model_name_usage`

### `BAGMAN.CORE` (v1)

Own canonical identity, timestamp, error, contract-validation, and domain-model primitives (GovernedEntity, Source, ExternalReference, Provenance, AuditEvent) plus their in-memory reference repositories, and expose the single BagmanCanonicalAPI orchestration facade (core/api.py) that composes these with services/evidence/ to record every canonical write's audit event — including EVIDENCE_OBSERVED, which is emitted from here even though EvidenceItem itself is owned by services/evidence/ (see the `produces` note below).

- **Owns:** `GovernedEntity`, `Source`, `ExternalReference`, `Provenance`, `AuditEvent`
- **Consumes:** `services/evidence`
- **Produces:** `ENTITY_REGISTERED`, `SOURCE_REGISTERED`, `EVIDENCE_OBSERVED`, `EXTERNAL_REFERENCE_LINKED`, `PROVENANCE_RECORDED`
- **Dependencies:** `jsonschema`, `rfc3339-validator`
- **External access:** `false`
- **Prohibited:** `direct_email_access`, `direct_bank_access`, `direct_xero_access`, `direct_chargebee_access`

### `BAGMAN.EVIDENCE.INTAKE` (v2)

Own the single governed boundary through which untrusted, external/ user-supplied bytes may become canonical BAGMAN evidence: the IntakeRecord domain object, its deterministic seven-state intake state machine (RECEIVED/VALIDATING/QUARANTINED/REJECTED/ACCEPTED/ REGISTERED/FAILED), durable idempotency semantics for a caller-supplied idempotency key (WI-1) — and, added by WI-2, the actual content-validation/quarantine pipeline that runs while a record is VALIDATING: filename safety (PID §14), bounded streaming/spooling with incremental SHA-256 (PID §13/§23), hand-rolled byte-level MIME/archive/executable detection (PID §15-18), an explicit versionable intake policy (PID §34), the EvidenceSafetyScanner abstraction plus a real ClamAV `clamd`-protocol implementation (PID §19/§20), and quarantine/staging object storage (PID §21/§22, via persistence/objects/store.py's prefixed key scheme). WI-2's pipeline stops at ACCEPTED with bytes staged in the object store — it does not itself register a canonical EvidenceItem, expose any HTTP API (WI-3), or own any GUI (WI-4).

- **Owns:** `IntakeRecord`
- **Consumes:** `BAGMAN.CORE`
- **Produces:** _(none)_
- **Dependencies:** `jsonschema`, `rfc3339-validator`
- **External access:** `false`
- **Prohibited:** `direct_email_access`, `direct_bank_access`, `direct_xero_access`, `direct_chargebee_access`

### `BAGMAN.PERSISTENCE.OBJECTS` (v2)

Own the EvidenceObjectStore abstraction (put/put_prefixed/get/exists/ verify_hash) for durable, immutable, content-addressed original-evidence-bytes storage, its MinIO/S3-compatible boto3 implementation, and — added by WI-3 — a narrow in-memory reference implementation used only by the runtime composition root's development/test mode. Core/domain code never depends on a storage-provider SDK directly (PID §53); this component is the only place that does. CD-4 WI-2 added `put_prefixed()` plus the `quarantine_object_key()`/`staging_object_key()` sibling key-shape helpers (PID §21/§22): quarantined material and an ACCEPTED intake's staged bytes are stored under `quarantine/<id>/<hash>` / `intake-staging/<id>/<hash>` — deliberately distinct in shape from `put()`'s canonical `evidence/<evidence_id>/<hash>` — so quarantined/ staged objects are never indistinguishable BY KEY SHAPE from normal available evidence.

- **Owns:** _(none)_
- **Consumes:** `BAGMAN.CORE`
- **Produces:** _(none)_
- **Dependencies:** `boto3`
- **External access:** `false`
- **Prohibited:** `direct_email_access`, `direct_bank_access`, `direct_xero_access`, `direct_chargebee_access`

### `BAGMAN.PERSISTENCE.POSTGRES` (v2)

Own durable, PostgreSQL-backed implementations of every CD-2/CD-4/CD-5 repository interface (GovernedEntity, Source, ExternalReference, EvidenceItem, Provenance, AuditEvent, IntakeRecord, AIInvocation) plus the SQLAlchemy engine/session factory and Alembic migration schema — preserving exactly the same canonical behaviour (immutability, idempotent external-reference/evidence-observation/intake replay, append-only audit, one-active-invocation-per-subject concurrency, CD-5 PID §73) as the in-memory reference implementations core/, services/evidence/, and ai/ ship, durably.

- **Owns:** _(none)_
- **Consumes:** `BAGMAN.CORE`, `BAGMAN.SERVICES.EVIDENCE`, `BAGMAN.EVIDENCE.INTAKE`, `BAGMAN.AI`
- **Produces:** _(none)_
- **Dependencies:** `SQLAlchemy`, `psycopg`, `alembic`
- **External access:** `false`
- **Prohibited:** `direct_email_access`, `direct_bank_access`, `direct_xero_access`, `direct_chargebee_access`

### `BAGMAN.RUNTIME.API` (v5)

Own the FastAPI/Uvicorn HTTP-facing application layer that exposes BagmanCanonicalAPI as a runnable, containerised service: health/ readiness (with a hard no-fallback invariant on readiness failure, and — since CD-4 WI-3 — a mandatory content-safety scanner reachability check alongside PostgreSQL/object-store), version metadata, structured logging, thin internal HTTP wrappers around register_entity/register_source/get_evidence/list_evidence/ trace_provenance, and — CD-4 WI-3 — the governed Evidence Intake HTTP API (POST /internal/intake/evidence, GET /internal/intake[/{id}]): evidence-registration orchestration (staging bytes into canonical storage, registering the EvidenceItem, and the full intake audit causation chain) once WI-2's content-validation pipeline reaches ACCEPTED, plus closure of the CD-3 direct-upload bypass (the old byte-accepting POST /internal/evidence has been removed entirely). Also owns the PID §14 composition root — the one place BAGMAN_RUNTIME_ENV is read to choose between in-memory and PostgreSQL+MinIO+ClamAV-backed repositories/object-store/scanner — and the stable MANUAL_UPLOAD Source resolve-or-create lifecycle (PID §9). CD-4 WI-4 additionally owns serving the first BAGMAN Documents GUI (PID §36-42) as plain static HTML/CSS/vanilla-JS assets (app/api/static/), mounted at "/" via Starlette's StaticFiles — no separate `bagman-ui` runtime/container was introduced (PID §42's "may be selected by FORGE" framework decision: the simplest option that needs zero new infrastructure), so this manifest is deliberately NOT split into a distinct BAGMAN.RUNTIME.UI component; the GUI is judged to genuinely be part of bagman-api's own delivery boundary, not a separate bounded component, since it introduces no new process, dependency surface, or deployment unit of its own. The GUI itself owns/decides no evidence business logic (PID §40) — it is a pure client of the very API routes this same manifest already describes. CD-5 WI-2 additionally owns `/internal/ai/tasks` (POST) / `/internal/ai/invocations[/{id}]` (GET) / `/internal/ai/health` (GET) — thin HTTP wrappers over `ai.gateway.background .run_background_task` and `AIInvocationRepository`, wiring the new `ai_invocation_repository`/`litellm_client` composition fields (in-memory repository + deterministic fake in development/test, PostgresAIInvocationRepository + the real LiteLLMClient in production) — never a second inference-control-plane implementation (PID §8), and never a new audit-event-producing responsibility here: the AI_INVOCATION_*/AI_OUTPUT_REJECTED events are minted by `ai/gateway/background.py` itself (see that component's own `component.yaml`), not by this router. CD-5 WI-3 additionally owns the Ask BAGMAN HTTP surface (POST /internal/operator/chat, app/api/routers/operator.py — a new file, deliberately never added to routers/ai.py, WI-2's own in-parallel file) and composition-root wiring for the Claude operator provider adapter and the fixed agent/tools tool registry (claude_client/tool_registry on RuntimeComposition, reusing the same ai_invocation_repository field WI-2 already added) — the actual Ask BAGMAN orchestration logic and tool registry live in the separate agent/ component (BAGMAN.AGENT), consumed here, never reimplemented in app/api/. CD-5 WI-4 additively extends both AI HTTP surfaces above rather than introducing new ones: `GET /internal/ai/invocations` gains an optional `primary_input_reference` query parameter (reusing WI-1's own generalised-subject filter, added to `AIInvocationRepository .list_invocations`'s ABC/in-memory/PostgreSQL implementations) so a caller can ask "every AIInvocation — any task, any status — about this one canonical subject", which the pre-existing filter set could not answer; and `GET /internal/ai/health` gains the `claude` key WI-2's own docstring had left as an open, additive extension point (`composition.claude_client.is_available()`, a genuinely separate signal from the gateway-wide `bagman-*` checks). `app/api/composition .py`'s development/test builder also gains a `default_response` for its `FakeLiteLLMClient` (task-shape-aware, matching whichever of the three CD-5 background tasks' system prompt is actually running) so a real dev-mode server's "Run analysis" button produces a genuine, clearly-labelled fake result instead of a 500 — closing a gap that blocked this WI's own interactive GUI testing, not a change to any HTTP-visible contract. Finally, WI-4 reorganises `app/api/static/` (previously one `app.js` monolith) into `shell/`/`shared/`/ `features/{overview,documents,ai}/` (PID §41) and adds the Ask BAGMAN drawer, the Documents "AI Analysis" panel, and Overview's AI status area — still plain vanilla ES modules served by the same `StaticFiles` mount, no build step, no new runtime/dependency.

- **Owns:** _(none)_
- **Consumes:** `BAGMAN.CORE`, `BAGMAN.SERVICES.EVIDENCE`, `BAGMAN.EVIDENCE.INTAKE`, `BAGMAN.PERSISTENCE.POSTGRES`, `BAGMAN.PERSISTENCE.OBJECTS`, `BAGMAN.AI`, `BAGMAN.AI.GATEWAY`, `BAGMAN.AI.PROVIDERS.LITELLM`, `BAGMAN.AGENT`
- **Produces:** `INTAKE_RECEIVED`, `INTAKE_VALIDATION_STARTED`, `INTAKE_REJECTED`, `INTAKE_QUARANTINED`, `INTAKE_ACCEPTED`, `EVIDENCE_STORED`, `EVIDENCE_REGISTERED`, `INTAKE_COMPLETED`, `INTAKE_FAILED`
- **Dependencies:** `fastapi`, `starlette`, `uvicorn`, `python-multipart`, `SQLAlchemy`, `alembic`
- **External access:** `false`
- **Prohibited:** `direct_email_access`, `direct_bank_access`, `direct_xero_access`, `direct_chargebee_access`

### `BAGMAN.SERVICES.EVIDENCE` (v1)

Own canonical EvidenceItem identity and its immutability and idempotent-observation semantics (an EvidenceItem, once recorded, is never mutated, and a replayed observation of the same external reference resolves to the existing record rather than creating a duplicate).

- **Owns:** `EvidenceItem`
- **Consumes:** `BAGMAN.CORE`
- **Produces:** _(none)_
- **Dependencies:** `jsonschema`, `rfc3339-validator`
- **External access:** `false`
- **Prohibited:** `direct_email_access`, `direct_bank_access`, `direct_xero_access`, `direct_chargebee_access`

## Contracts

| `$id` | Title | Path |
|-------|-------|------|
| `https://bagman.internal/contracts/ai/bagman.ai_invocation.v1.schema.json` | BAGMAN AIInvocation | `contracts/ai/bagman.ai_invocation.v1.schema.json` |
| `https://bagman.internal/contracts/audit/bagman.audit_event.v1.schema.json` | BAGMAN AuditEvent | `contracts/audit/bagman.audit_event.v1.schema.json` |
| `https://bagman.internal/contracts/common/bagman.identifier.v1.schema.json` | BAGMAN Canonical Identifier | `contracts/common/bagman.identifier.v1.schema.json` |
| `https://bagman.internal/contracts/common/bagman.schema_version.v1.schema.json` | BAGMAN Contract Schema Version | `contracts/common/bagman.schema_version.v1.schema.json` |
| `https://bagman.internal/contracts/common/bagman.utc_timestamp.v1.schema.json` | BAGMAN Canonical UTC Timestamp | `contracts/common/bagman.utc_timestamp.v1.schema.json` |
| `https://bagman.internal/contracts/entity/bagman.entity.v1.schema.json` | BAGMAN GovernedEntity | `contracts/entity/bagman.entity.v1.schema.json` |
| `https://bagman.internal/contracts/evidence/bagman.evidence.v1.schema.json` | BAGMAN EvidenceItem | `contracts/evidence/bagman.evidence.v1.schema.json` |
| `https://bagman.internal/contracts/intake/bagman.intake_record.v1.schema.json` | BAGMAN IntakeRecord | `contracts/intake/bagman.intake_record.v1.schema.json` |
| `https://bagman.internal/contracts/manifest/bagman.component_manifest.v1.schema.json` | BAGMAN Component Manifest | `contracts/manifest/bagman.component_manifest.v1.schema.json` |
| `https://bagman.internal/contracts/provenance/bagman.provenance.v1.schema.json` | BAGMAN Provenance | `contracts/provenance/bagman.provenance.v1.schema.json` |
| `https://bagman.internal/contracts/source/bagman.external_reference.v1.schema.json` | BAGMAN ExternalReference | `contracts/source/bagman.external_reference.v1.schema.json` |
| `https://bagman.internal/contracts/source/bagman.source.v1.schema.json` | BAGMAN Source | `contracts/source/bagman.source.v1.schema.json` |

## Canonical Entities

| Key | Display Name | Type |
|-----|--------------|------|
| `INFOSECURS_LIMITED` | Infosecurs Limited | `company` |
| `MATTHEW_SCOTT_PERSONAL` | Matthew Scott (Personal) | `personal` |
| `NOUSTAI_LIMITED` | Noust AI Limited | `company` |
