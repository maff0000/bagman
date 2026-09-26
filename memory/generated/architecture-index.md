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

### `BAGMAN.AGENT` (v3)

[REMOVED 2026-09-16 — see the correction/cleanup note above; text below is WI-3's own original `responsibility` text for agent/bagman/+agent/tools/, preserved verbatim as history. Neither directory exists in this repository any more.] Own the BAGMAN AI agent's own reasoning/orchestration layer: the Ask BAGMAN operator-orchestration loop (agent/bagman/ — system-prompt construction, the bounded Claude tool-calling loop, AIInvocation lifecycle management, PID §57 audit emission) and the fixed, read-only/analyse-only governed tool registry Claude may invoke (agent/tools/ — CD-5 PID §34's 8 named tools, mechanically fail-closed dispatch per PID §63). This component decided WHICH of BAGMAN's own internal APIs/repositories a registered tool could call and HOW an Ask BAGMAN conversation was assembled/bounded (PID §53); it never instantiated an Anthropic/LiteLLM client itself (that lived in ai/providers/*/, consumed here only through the provider-neutral ClaudeClientProtocol/BackgroundTaskRunner seams) and never allowed Claude to invoke anything outside the fixed tool set or to mutate canonical state (PID §17/§56). agent/policies/ and agent/memory/ remain empty CD-1 placeholders — no autonomous policy engine is authorised yet, and Ask BAGMAN's conversation state remains in-request-only (PID §64 — no durable chat-memory-fabric integration exists).

- **Owns:** _(none)_
- **Consumes:** _(none)_
- **Produces:** _(none)_
- **Dependencies:** _(none)_
- **External access:** `false`
- **Prohibited:** `direct_email_access`, `direct_bank_access`, `direct_xero_access`, `direct_chargebee_access`

### `BAGMAN.AGENT.CLAUDE_CODE` (v1)

Own BAGMAN's ONE bounded, headless Claude Code operator invocation path (CD-5 Gate-2 closure, 2026-09-16 — supersedes BAGMAN.AGENT's original agent/bagman/+agent/tools/ direct-Anthropic tool-calling-loop design; see PID.md §97 and the CD-5 evidence file for the full architecture-correction history). agent/claude_code/runner.py is the ONE place in the whole repository that ever constructs a `claude` subprocess invocation — fixed executable/argument contract, never a shell, `--tools ""` + `--restricted` + `--strict-mcp-config` strip the invoked process of every tool/MCP/ambient-settings capability, so its authority is a strict SUBSET of this host's own normal Claude Code development-agent authority, never inherited from it. agent/claude_code/context.py assembles bounded, governed context (evidence/intake/entity, fetched directly from BAGMAN's own canonical services — never a live tool call) BEFORE the one synchronous invocation; agent/claude_code/orchestrator.py owns the AIInvocation lifecycle (REQUESTED -> RUNNING -> SUCCEEDED/FAILED), the ASK_BAGMAN v1 task contract (reused unchanged from BAGMAN.AGENT's own original registration in ai.tasks), and PID §57 audit emission — same public AskBagmanResult shape and same HTTP contract the superseded implementation used, so app/api/routers/operator.py needed no response-shape change. Claude Code owns its own authentication/session mechanism entirely (PID §97) — this component never reads, writes, or transmits an Anthropic API key.

- **Owns:** _(none)_
- **Consumes:** `BAGMAN.AI`, `BAGMAN.CORE`, `BAGMAN.SERVICES.EVIDENCE`, `BAGMAN.EVIDENCE.INTAKE`
- **Produces:** `AI_INVOCATION_REQUESTED`, `AI_INVOCATION_SUCCEEDED`, `AI_INVOCATION_FAILED`, `AI_OUTPUT_REJECTED`
- **Dependencies:** _(none)_
- **External access:** `true`
- **Prohibited:** `direct_email_access`, `direct_bank_access`, `direct_xero_access`, `direct_chargebee_access`, `direct_anthropic_api_key_usage`, `shell_true_subprocess_invocation`, `caller_controlled_executable_or_flags`, `canonical_state_mutation`

### `BAGMAN.AI` (v4)

Own the governed, provider-NEUTRAL AI invocation domain model and typed task contract/registry framework: the AIInvocation state machine and its one-active-invocation-per-subject concurrency guarantee (CD-5 PID §26-30/§73-76), and the TaskContract/TASK_REGISTRY framework naming CD-5's tasks' metadata and real, usable input/output JSON Schemas (PID §21-24/§55) — WI-1 delivered the original four; WI-3 additively registered a 5th, ASK_BAGMAN v1 (Ask BAGMAN's general operator chat task — see ai/tasks.py's own docstring for why this is a new task rather than a reuse of OPERATOR_DOCUMENT_REVIEW). WI-1 delivered only the domain/contract/persistence slice of BAGMAN.AI's eventual responsibility (PID §7's full statement: "route typed BAGMAN intelligence tasks to an authorised AI provider/capability, enforce task contracts and policy, record invocation provenance, validate structured responses and return proposals without directly mutating canonical business state"). WI-2 lands the BACKGROUND half of that: `ai/providers/litellm/` (the one LiteLLM-speaking adapter, its mechanical alias-only enforcement, and its deterministic fake) and `ai/gateway/` (`run_background_task`'s full REQUESTED -> RUNNING -> terminal orchestration, PID §57 audit emission). WI-3 originally landed the OPERATOR half's provider adapter here too: `ai/providers/claude/` — a direct Anthropic Messages API adapter (ClaudeClient/ClaudeClientProtocol/FakeClaudeClient). **Removed 2026-09-16** (CD-5 Gate-2 closure, final delta before merge): the architect ruled BAGMAN's operator intelligence is not a direct Anthropic-API integration at all — the sole authoritative operator path is the bounded headless Claude Code runner owned entirely by `agent/claude_code/` (BAGMAN.AGENT.CLAUDE_CODE), which does not consume this component and speaks no Anthropic wire protocol of any kind. `ai/providers/claude/` was confirmed to have zero remaining live dependents before removal — see `PID.md` §97 and the CD-5 evidence file for the full, preserved history. `ai/providers/litellm/` and `ai/gateway/` are UNAFFECTED by this — both remain live, each with its own `component.yaml`, consuming this one. Real prompt CONTENT for the three BACKGROUND tasks lives at `ai/prompts/` (PID §31), loaded by `ai/gateway/background.py`. `ai/policy/`, `ai/evaluation/`, and `ai/provenance/` remain WI-5's job to populate (`ai/evaluation/` is populated; the other two remain placeholders).
**CD-6 reliability delta (2026-09-17, PID §98/§100.14/§100.16):** `ai.invocation` grew two new terminal states (`TIMED_OUT`, `CANCELLED`), a `conversation_id` primary-reference fallback (PID §29/§73), and a bounded, deterministic stale-`RUNNING` recovery backstop — see that module's own docstring for the full mechanism. The backstop is the one narrow exception to this component's own "does not itself emit audit events" rule below: both concrete `AIInvocationRepository` implementations now accept an `AuditRepository` and emit exactly one new event type, `AI_INVOCATION_STALE_RECOVERED`, when they silently discover and recover an abandoned row — no other caller is ever positioned to observe that transition happening, so it could not be left to the orchestration layer the way every other `AIInvocation` audit event still is.

- **Owns:** `AIInvocation`, `TaskContract`
- **Consumes:** `BAGMAN.CORE`
- **Produces:** `AI_INVOCATION_STALE_RECOVERED`
- **Dependencies:** `jsonschema`, `rfc3339-validator`
- **External access:** `false`
- **Prohibited:** `direct_email_access`, `direct_bank_access`, `direct_xero_access`, `direct_chargebee_access`, `direct_litellm_client_outside_gateway`, `direct_anthropic_client_outside_gateway`, `trinity_star_alias_usage`

### `BAGMAN.AI.EVALUATION` (v1)

Own the mandatory CD-5 evaluation harness (PID §59-61, WI-5): golden/ synthetic fixtures (ai/evaluation/fixtures.py) covering structured- output validity, expected classification (a harness/contract-shape self-test, not a live-model quality benchmark — no real model is reachable today, see the WI-5 delivery report), abstention/ uncertainty distinguished from a confident wrong answer, malformed output (not-JSON and schema-invalid), and timeout/transport error handling — each run for real through the REAL ai.gateway.background.run_background_task orchestration function against a deterministic ai.providers.litellm.fake.FakeLiteLLMClient, never a reimplementation of that logic. Reuses (never duplicates) WI-2's/WI-3's own existing prompt-injection structural proofs (ai/evaluation/injection_reuse.py, invoked as a real pytest subprocess against their existing files) as this harness's prompt-injection-resilience category. Runnable standalone (`python3 -m ai.evaluation.run`) and pytest-collected (tests/integration/test_ai_evaluation_harness.py). Entirely deterministic — no live credentials, no network access (PID §61).

- **Owns:** _(none)_
- **Consumes:** `BAGMAN.AI`, `BAGMAN.AI.GATEWAY`, `BAGMAN.AI.PROVIDERS.LITELLM`
- **Produces:** _(none)_
- **Dependencies:** _(none)_
- **External access:** `false`
- **Prohibited:** `direct_email_access`, `direct_bank_access`, `direct_xero_access`, `direct_chargebee_access`, `live_provider_credentials_required`, `trinity_star_alias_usage`

### `BAGMAN.AI.GATEWAY` (v1)

Own BACKGROUND-role task orchestration (CD-5 PID §6/§21-30/§57-58/ §73-76, WI-2): run_background_task drives one AIInvocation through its full REQUESTED -> RUNNING -> {SUCCEEDED, FAILED} lifecycle against the ai.providers.litellm adapter, using ai.prompts' versioned prompt assets, validating output via ai.tasks.validate_task_output, and emitting AI_INVOCATION_REQUESTED / AI_INVOCATION_SUCCEEDED / AI_INVOCATION_FAILED / AI_OUTPUT_REJECTED with full causation chaining. Provider- and composition-agnostic by construction (the repository, LiteLLM client, and audit-event emitter are all dependency-injected, never imported from app/api/ or persistence/ directly) so this component is fully unit-testable against deterministic fakes (PID §61). The OPERATOR-role equivalent (Claude) is WI-3's own addition to this same package, not this manifest's current scope — run_background_task explicitly refuses any OPERATOR-role task_id.

- **Owns:** _(none)_
- **Consumes:** `BAGMAN.AI`, `BAGMAN.AI.PROVIDERS.LITELLM`
- **Produces:** `AI_INVOCATION_REQUESTED`, `AI_INVOCATION_SUCCEEDED`, `AI_INVOCATION_FAILED`, `AI_OUTPUT_REJECTED`
- **Dependencies:** _(none)_
- **External access:** `false`
- **Prohibited:** `direct_email_access`, `direct_bank_access`, `direct_xero_access`, `direct_chargebee_access`, `direct_litellm_client_outside_gateway_adapter`, `trinity_star_alias_usage`

### `BAGMAN.AI.PROVIDERS.LITELLM` (v1)

Own the ONE adapter that speaks to BAGMAN's LiteLLM gateway (CD-5 PID §6/§8/§9/§50, WI-2): LiteLLMClient (the real POST /v1/chat/completions OpenAI-compatible wire call, alias-only, bounded timeout/retry, fail-closed result normalisation into LiteLLMCompletionResult, and — Gate-1 closure, 2026-09-16 — a per-request response_format JSON-schema structured-output constraint built from the caller's output_schema) and FakeLiteLLMClient (the deterministic substitute every ordinary test and dev/test composition uses, PID §61). Mechanically enforces BAGMAN's own alias-only routing boundary (validate_capability_alias) — this is the one place in the whole repository that could ever construct a request naming a physical model or a trinity-* alias, and it refuses to. Does not decide WHICH alias a task prefers (ai.tasks.TaskContract.preferred_capability owns that) and does not orchestrate a task's lifecycle (ai.gateway owns that) — this component's job ends at "send one bounded HTTP request for a validated alias, return a normalised result, never raise for a transport-level failure". Final topology (Gate-1 closure, 2026-09-16, see the CD-5 evidence file for the full preserved history): the real gateway this adapter reaches is HELM's dedicated, BAGMAN-exclusive Mac AI appliance (its own LiteLLM + PostgreSQL), not the shared Trinity LiteLLM installation this component originally targeted at CD-5's initial dispatch — this manifest's own owns/consumes/produces edges are unaffected, since which real process sits behind the gateway is a deployment-level detail, not an architectural one.

- **Owns:** `LiteLLMCompletionResult`
- **Consumes:** `BAGMAN.AI`
- **Produces:** _(none)_
- **Dependencies:** _(none)_
- **External access:** `true`
- **Prohibited:** `direct_email_access`, `direct_bank_access`, `direct_xero_access`, `direct_chargebee_access`, `direct_mac_mini_access`, `direct_ollama_mlx_llamacpp_access`, `trinity_star_alias_usage`, `raw_physical_model_name_usage`

### `BAGMAN.CORE` (v2)

Own canonical identity, timestamp, error, contract-validation, and domain-model primitives (GovernedEntity, Source, ExternalReference, Provenance, AuditEvent) plus their in-memory reference repositories, and expose the single BagmanCanonicalAPI orchestration facade (core/api.py) that composes these with services/evidence/ to record every canonical write's audit event — including EVIDENCE_OBSERVED, which is emitted from here even though EvidenceItem itself is owned by services/evidence/ (see the `produces` note below). Also owns core/text_matching.py (CD-6 Slice 5 WI-2) — the neutral, dependency- free text-normalisation/predicate-matching primitives (normalize_domain/normalize_address/domain_from_address/ normalize_subject_for_policy/subject_matches_predicate) extracted out of services/mailbox/domain_rule.py so both mailbox-domain-rule policy and evidence-classification-rule matching can depend on ONE shared implementation rather than two independently-maintained copies. Not a new canonical domain TYPE (nothing to add to `owns` below — these are plain functions, the same style as core/identity.py/core/timestamps.py, neither of which is listed in `owns` either).

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

### `BAGMAN.MAILBOX` (v4)

Own the operator-facing mailbox DEFINITION registry (MailboxSource) — a governed record of which real mailbox addresses BAGMAN monitors for evidence, their declared provider adapter, an optional display-only default-entity hint, their own closed ACTIVE/DISABLED/RETIRED lifecycle (retire preserves the row rather than deleting it, so future evidence provenance is never orphaned), and — as of Slice 4 — their own independent connection/authentication state machine (NOT_CONFIGURED/AUTH_REQUIRED/CONNECTED/ERROR). Also owns the provider-neutral sweep orchestration (`services/mailbox/sweep.py`), the durable message projection (MailboxMessage, idempotent on (mailbox_id, immutable_provider_message_id) — a message observed in more than one monitored folder is never duplicated), the sweep-run ledger (MailboxSweepRun, honest SUCCEEDED/PARTIAL/FAILED semantics — never SUCCEEDED if a message that should have ingested failed irrecoverably), the per-folder durable delta cursor (only ever advanced after a full round succeeds), and the per-mailbox sweep exclusivity lease. As of the CD-6 architect amendment, this component ALSO owns the two-stage discover-then-gate mail-processing model: bounded, MIME-free Stage-A discovery (sender domain, attachment metadata, SPF/DKIM/DMARC-style authentication signals captured separately from the visible From domain) for every message, and a mailbox-specific `MailboxDomainRule` policy registry (Stage B) that decides whether a message is worth a full, evidence-creating MIME fetch at all — never fetching/storing full MIME for irrelevant mail. An unknown domain showing a credible, bounded, non-AI accounting-document signal creates exactly one governed Needs You item; operator approval creates the routing rule AND immediately reprocesses the specific triggering message. The historical bootstrap boundary is DERIVED, per mailbox, from each of that mailbox's own in-scope governed entities' recurring `fiscal_year_start_month_day` accounting-period rule (previous- completed-period-start policy — see `services/mailbox/bootstrap_policy.py`), optionally clamped by a real commencement/incorporation floor override (`GovernedEntity.historical_floor_override_at`) — never a static invented default, and never itself the literal stored answer (a real conceptual-conflation bug this component's own second CD-6 architect amendment corrected). The sweep still refuses to run while any in-scope governed entity's `fiscal_year_start_month_day` configuration is missing. The first concrete provider adapter, `services/mailbox/microsoft/` (Microsoft Graph: delegated authorization-code OAuth, `Mail.Read`-only delegated permissions, a read-only Graph client, server-side account-identity verification, and email-evidence ingestion via BAGMAN's own existing CD-4 evidence/provenance architecture — never a second private evidence silo), lives inside this same component rather than a separate one, mirroring `services/xero/`'s own precedent of keeping provider- neutral domain logic and real provider-calling code under one component even though the files are internally separated (see `services/mailbox/mailbox.py`'s own module docstring: "no Microsoft- specific OAuth logic inside the generic mailbox domain model" is a file-layering discipline, not a component-boundary one). This is a READ-ONLY integration against Microsoft mail: no mark-read, move, delete, or send capability exists anywhere behind this component. Email classification, AI, correction/learned rules, and any accounting-relevance decision are explicitly Slice 5's scope — no such field or code path exists anywhere in this component.

- **Owns:** `MailboxSource`, `MailboxMessage`, `MailboxSweepRun`, `MailboxDomainRule`
- **Consumes:** `BAGMAN.CORE`, `BAGMAN.EVIDENCE.INTAKE`
- **Produces:** _(none)_
- **Dependencies:** `jsonschema`, `rfc3339-validator`
- **External access:** `true`
- **Prohibited:** `direct_bank_access`, `direct_chargebee_access`, `direct_xero_access`, `microsoft_graph_write_endpoints`, `mailbox_mark_read_move_or_delete`, `mailbox_send_mail`, `email_classification_or_ai_decisions`

### `BAGMAN.NEEDS_YOU` (v1)

Own the universal, cross-domain NeedsYouItem domain object and its small closed state machine (OPEN -> RESOLVED/DISMISSED) — the one queue every BAGMAN producer (evidence intake today; email triage, invoice review, rule proposals in later CD-6 slices) raises a question into when a human decision is required before BAGMAN can keep going (PID §98.2/§98.5). Deliberately its own top-level component rather than folded into services/evidence/intake/ — see this module's own docstring ("What this is, and why it is its own top-level component") for the full rationale. Does not itself decide WHEN to raise an item for a given domain event (that orchestration lives in the calling HTTP router, e.g. app/api/routers/intake.py's own documented hook, exactly like core.api.BagmanCanonicalAPI never deciding when services.evidence.intake's pipeline should run).

- **Owns:** `NeedsYouItem`
- **Consumes:** `BAGMAN.CORE`
- **Produces:** _(none)_
- **Dependencies:** `jsonschema`, `rfc3339-validator`
- **External access:** `false`
- **Prohibited:** `direct_email_access`, `direct_bank_access`, `direct_xero_access`, `direct_chargebee_access`

### `BAGMAN.PERSISTENCE.OBJECTS` (v3)

Own the EvidenceObjectStore abstraction (put/put_prefixed/get/exists/ verify_hash) for durable, immutable, content-addressed original-evidence-bytes storage — the canonical interface is this S3-compatible abstraction, never a specific provider's own API surface. `MinIOObjectStore`/`MinIOConfig` (names retained from CD-3 WI-2 for continuity, not renamed by the CD-6 MinIO withdrawal WO) are its one boto3-based, S3-compatible-protocol implementation, plus — added by WI-3 — a narrow in-memory reference implementation used only by the runtime composition root's development/test mode. Core/domain code never depends on a storage-provider SDK directly (PID §53); this component is the only place that does. The CURRENT runtime provider behind that boto3 client, as configured in `deployment/compose/docker-compose.yml`'s `bagman-objects` service, is SeaweedFS 4.47 (the original MinIO release line this component was built against is now withdrawn/unmaintained upstream) — this is a swappable deployment/configuration choice, not something this component's code is hard-wired to: `MinIOObjectStore` speaks only the S3 protocol via `boto3`, never anything MinIO-specific, so any other S3-compatible provider works identically. Evidence immutability and hash-integrity verification (PID §17/§18) are enforced entirely by this component's own code (the hash-check-before-write in `put()`/ `put_prefixed()`, the re-hash-on-read in `get()`, the byte-for-byte compare in `verify_hash()`) — never assumed from whatever provider is configured underneath. CD-4 WI-2 added `put_prefixed()` plus the `quarantine_object_key()`/`staging_object_key()` sibling key-shape helpers (PID §21/§22): quarantined material and an ACCEPTED intake's staged bytes are stored under `quarantine/<id>/<hash>` / `intake-staging/<id>/<hash>` — deliberately distinct in shape from `put()`'s canonical `evidence/<evidence_id>/<hash>` — so quarantined/ staged objects are never indistinguishable BY KEY SHAPE from normal available evidence.

- **Owns:** _(none)_
- **Consumes:** `BAGMAN.CORE`
- **Produces:** _(none)_
- **Dependencies:** `boto3`
- **External access:** `false`
- **Prohibited:** `direct_email_access`, `direct_bank_access`, `direct_xero_access`, `direct_chargebee_access`

### `BAGMAN.PERSISTENCE.POSTGRES` (v3)

Own durable, PostgreSQL-backed implementations of every CD-2/CD-4/ CD-5/CD-6 repository interface (GovernedEntity, Source, ExternalReference, EvidenceItem, Provenance, AuditEvent, IntakeRecord, AIInvocation, NeedsYouItem) plus the SQLAlchemy engine/session factory and Alembic migration schema — preserving exactly the same canonical behaviour (immutability, idempotent external-reference/evidence- observation/intake/needs-you-item replay, append-only audit, one-active-invocation-per-subject concurrency, CD-5 PID §73) as the in-memory reference implementations core/, services/evidence/, ai/, and (CD-6) services/needs_you/ ship, durably.

- **Owns:** _(none)_
- **Consumes:** `BAGMAN.CORE`, `BAGMAN.SERVICES.EVIDENCE`, `BAGMAN.EVIDENCE.INTAKE`, `BAGMAN.AI`, `BAGMAN.NEEDS_YOU`
- **Produces:** _(none)_
- **Dependencies:** `SQLAlchemy`, `psycopg`, `alembic`
- **External access:** `false`
- **Prohibited:** `direct_email_access`, `direct_bank_access`, `direct_xero_access`, `direct_chargebee_access`

### `BAGMAN.RUNTIME.API` (v5)

Own the FastAPI/Uvicorn HTTP-facing application layer that exposes BagmanCanonicalAPI as a runnable, containerised service: health/ readiness (with a hard no-fallback invariant on readiness failure, and — since CD-4 WI-3 — a mandatory content-safety scanner reachability check alongside PostgreSQL/object-store), version metadata, structured logging, thin internal HTTP wrappers around register_entity/register_source/get_evidence/list_evidence/ trace_provenance, and — CD-4 WI-3 — the governed Evidence Intake HTTP API (POST /internal/intake/evidence, GET /internal/intake[/{id}]): evidence-registration orchestration (staging bytes into canonical storage, registering the EvidenceItem, and the full intake audit causation chain) once WI-2's content-validation pipeline reaches ACCEPTED, plus closure of the CD-3 direct-upload bypass (the old byte-accepting POST /internal/evidence has been removed entirely). Also owns the PID §14 composition root — the one place BAGMAN_RUNTIME_ENV is read to choose between in-memory and PostgreSQL+MinIO+ClamAV-backed repositories/object-store/scanner — and the stable MANUAL_UPLOAD Source resolve-or-create lifecycle (PID §9). CD-4 WI-4 additionally owns serving the first BAGMAN Documents GUI (PID §36-42) as plain static HTML/CSS/vanilla-JS assets (app/api/static/), mounted at "/" via Starlette's StaticFiles — no separate `bagman-ui` runtime/container was introduced (PID §42's "may be selected by FORGE" framework decision: the simplest option that needs zero new infrastructure), so this manifest is deliberately NOT split into a distinct BAGMAN.RUNTIME.UI component; the GUI is judged to genuinely be part of bagman-api's own delivery boundary, not a separate bounded component, since it introduces no new process, dependency surface, or deployment unit of its own. The GUI itself owns/decides no evidence business logic (PID §40) — it is a pure client of the very API routes this same manifest already describes. CD-5 WI-2 additionally owns `/internal/ai/tasks` (POST) / `/internal/ai/invocations[/{id}]` (GET) / `/internal/ai/health` (GET) — thin HTTP wrappers over `ai.gateway.background .run_background_task` and `AIInvocationRepository`, wiring the new `ai_invocation_repository`/`litellm_client` composition fields (in-memory repository + deterministic fake in development/test, PostgresAIInvocationRepository + the real LiteLLMClient in production) — never a second inference-control-plane implementation (PID §8), and never a new audit-event-producing responsibility here: the AI_INVOCATION_*/AI_OUTPUT_REJECTED events are minted by `ai/gateway/background.py` itself (see that component's own `component.yaml`), not by this router. CD-5 WI-3 originally owned the Ask BAGMAN HTTP surface (POST /internal/operator/chat, app/api/routers/operator.py — a new file, deliberately never added to routers/ai.py, WI-2's own in-parallel file) and composition-root wiring for a direct Anthropic-API provider adapter and a fixed Claude-invocable tool registry (`claude_client`/`tool_registry` on RuntimeComposition). **Superseded and removed 2026-09-16** (CD-5 Gate-2 closure, final delta before merge — see `PID.md` §97 and the CD-5 evidence file for the full history): the architect ruled that design wrong (BAGMAN never held an Anthropic API key in the first place at runtime, and the design was confirmed to have zero live dependents before removal); `RuntimeComposition` now instead wires `claude_code_operator_runner` (fake in development/test, the real `ClaudeCodeOperatorRunner` in production), and `POST /internal/operator/chat` calls `agent.claude_code.orchestrator.handle_operator_message` — the sole authoritative operator implementation, owned entirely by the separate `agent/claude_code/` component (BAGMAN.AGENT.CLAUDE_CODE), consumed here, never reimplemented in app/api/. `GET /internal/ai/health`'s `claude` key (WI-4's own addition) is likewise superseded and removed — `claude_code` is the current, independently-measured signal. CD-5 WI-4 additively extends both AI HTTP surfaces above rather than introducing new ones: `GET /internal/ai/invocations` gains an optional `primary_input_reference` query parameter (reusing WI-1's own generalised-subject filter, added to `AIInvocationRepository .list_invocations`'s ABC/in-memory/PostgreSQL implementations) so a caller can ask "every AIInvocation — any task, any status — about this one canonical subject", which the pre-existing filter set could not answer; and `GET /internal/ai/health` gains the `claude` key WI-2's own docstring had left as an open, additive extension point (`composition.claude_client.is_available()`, a genuinely separate signal from the gateway-wide `bagman-*` checks). `app/api/composition .py`'s development/test builder also gains a `default_response` for its `FakeLiteLLMClient` (task-shape-aware, matching whichever of the three CD-5 background tasks' system prompt is actually running) so a real dev-mode server's "Run analysis" button produces a genuine, clearly-labelled fake result instead of a 500 — closing a gap that blocked this WI's own interactive GUI testing, not a change to any HTTP-visible contract. Finally, WI-4 reorganises `app/api/static/` (previously one `app.js` monolith) into `shell/`/`shared/`/ `features/{overview,documents,ai}/` (PID §41) and adds the Ask BAGMAN drawer, the Documents "AI Analysis" panel, and Overview's AI status area — still plain vanilla ES modules served by the same `StaticFiles` mount, no build step, no new runtime/dependency.
CD-6 Slice 1 (PID §98, "GUI Operations Foundation") additionally owns: a real premium application shell (extending the existing token system in app/api/static/style.css and the existing tab system in app/api/static/shell/shell.js — never replacing either), the universal Needs You queue HTTP surface (GET /internal/needs-you, GET /internal/needs-you/{id}, POST /internal/needs-you/{id}/resolve -- app/api/routers/needs_you.py, new file), the cross-BAGMAN activity/ audit stream (GET /internal/activity -- app/api/routers/activity.py, new file), a new GET /internal/entities read route plus the idempotent canonical three-entity seed lifecycle (app/api/composition.py's own "Stable canonical entity seed lifecycle" section -- mirrors the existing MANUAL_UPLOAD Source lifecycle exactly), and one new hook inside the EXISTING governed intake endpoint (app/api/routers/intake.py::_create_needs_you_item_for_accepted_evidence) that raises exactly one COMPANY_REQUIRED Needs You item the moment an intake attempt reaches REGISTERED -- never a second, parallel upload/ registration path. No mailbox/Graph/IMAP/Gmail, Xero OAuth/posting, or bank-feed code is introduced by this slice (PID §98.11 Slice 1 scope boundary).

- **Owns:** _(none)_
- **Consumes:** `BAGMAN.CORE`, `BAGMAN.SERVICES.EVIDENCE`, `BAGMAN.EVIDENCE.INTAKE`, `BAGMAN.NEEDS_YOU`, `BAGMAN.PERSISTENCE.POSTGRES`, `BAGMAN.PERSISTENCE.OBJECTS`, `BAGMAN.AI`, `BAGMAN.AI.GATEWAY`, `BAGMAN.AI.PROVIDERS.LITELLM`, `BAGMAN.AGENT.CLAUDE_CODE`
- **Produces:** `INTAKE_RECEIVED`, `INTAKE_VALIDATION_STARTED`, `INTAKE_REJECTED`, `INTAKE_QUARANTINED`, `INTAKE_ACCEPTED`, `EVIDENCE_STORED`, `EVIDENCE_REGISTERED`, `INTAKE_COMPLETED`, `INTAKE_FAILED`, `NEEDS_YOU_ITEM_CREATED`, `NEEDS_YOU_ITEM_RESOLVED`, `NEEDS_YOU_ITEM_DISMISSED`
- **Dependencies:** `fastapi`, `starlette`, `uvicorn`, `python-multipart`, `SQLAlchemy`, `alembic`
- **External access:** `false`
- **Prohibited:** `direct_email_access`, `direct_bank_access`, `direct_xero_access`, `direct_chargebee_access`

### `BAGMAN.SERVICES.EVIDENCE` (v3)

Own canonical EvidenceItem identity and its immutability and idempotent-observation semantics (an EvidenceItem, once recorded, is never mutated, and a replayed observation of the same external reference resolves to the existing record rather than creating a duplicate); also owns EvidenceClassification (an append-only, supersession-chained document_type classification of an EvidenceItem) and EvidenceClassificationRule (the separate, deterministic rule-based classification authority behind CD-6 Slice 5 WI-1) — two distinct canonical types this component defines alongside EvidenceItem itself, never folded into it. CD-6 Slice 5 WI-2 adds the deterministic matcher/observed-evidence-guard/preview/governed-rule-lifecycle/ classification-service layer on top of that same data model — no new canonical type, purely additive compute/orchestration logic.

- **Owns:** `EvidenceItem`, `EvidenceClassification`, `EvidenceClassificationRule`
- **Consumes:** `BAGMAN.CORE`
- **Produces:** `EVIDENCE_CLASSIFICATION_RULE_CREATED`, `EVIDENCE_CLASSIFICATION_RULE_RETIRED`, `EVIDENCE_CLASSIFIED`
- **Dependencies:** `jsonschema`, `rfc3339-validator`
- **External access:** `false`
- **Prohibited:** `direct_email_access`, `direct_bank_access`, `direct_xero_access`, `direct_chargebee_access`

### `BAGMAN.XERO` (v2)

Own the durable Company<->Xero-organisation mapping (XeroConnection, its closed connect/disconnect/error/revoke state machine, and the server-side OAuth anti-CSRF/replay `state` token that protects the Authorization Code callback), the synced read-only Chart-of-Accounts reference-data projection (XeroAccount, idempotent on (tenant_id, account_id), history-preserving — never deleted, only re-synced), and the sync-attempt ledger (XeroSyncRun) that makes a failed/partial sync provably unable to ever replace last-known-good projection data (PID §98.4, architect spec §4/§5/§20). Also owns the documented, extensible account-eligibility-filtering policy (which synced accounts a coding dropdown shows by default) and the AI-suggestion candidate-set validation (an AI-proposed AccountID/Code/Name/TaxType must resolve to a real synced eligible account or be treated as UNRESOLVED — never invented). This is a READ/REFERENCE-ONLY integration: the only "write" direction to Xero is the OAuth token lifecycle itself (authorize/refresh/revoke) — no Xero write endpoint (invoice/bill/journal/payment/contact creation, bank reconciliation, tax filing) is implemented anywhere in this component. As of this version, also owns the read-only Contacts/Purchase-Invoices/ BankTransactions client methods (`XeroAccountingClient.list_contacts`/`list_purchase_invoices`/ `list_bank_transactions`, transient/never-persisted — see `services/xero/client.py`'s own module docstring; `list_bank_transactions` is the one method in this component that really paginates real network I/O over multiple pages, deduplicated by `BankTransactionID`) and the bounded, on-demand, non-AI Xero- assisted supplier-domain correlation capability (`services/xero/supplier_correlation.py`) that enriches still-OPEN `MAILBOX_DOMAIN_REVIEW` Needs You items' own metadata as a review aid from TWO independent evidence sources — real ACCPAY purchase-invoice history and real SPEND BankTransaction history, correlated through `ContactID` only, never fuzzy name matching — this component's first real cross-component orchestration function (see that module's own "why this lives in services/xero/" docstring section), never auto-approving anything.

- **Owns:** `XeroConnection`, `XeroAccount`, `XeroSyncRun`
- **Consumes:** `BAGMAN.CORE`, `BAGMAN.MAILBOX`, `BAGMAN.NEEDS_YOU`
- **Produces:** _(none)_
- **Dependencies:** `jsonschema`, `rfc3339-validator`
- **External access:** `true`
- **Prohibited:** `direct_email_access`, `direct_bank_access`, `direct_chargebee_access`, `xero_write_endpoints`

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
| `https://bagman.internal/contracts/evidence/bagman.evidence_classification.v1.schema.json` | BAGMAN EvidenceClassification | `contracts/evidence/bagman.evidence_classification.v1.schema.json` |
| `https://bagman.internal/contracts/evidence/bagman.evidence_classification_rule.v1.schema.json` | BAGMAN EvidenceClassificationRule | `contracts/evidence/bagman.evidence_classification_rule.v1.schema.json` |
| `https://bagman.internal/contracts/intake/bagman.intake_record.v1.schema.json` | BAGMAN IntakeRecord | `contracts/intake/bagman.intake_record.v1.schema.json` |
| `https://bagman.internal/contracts/mailbox/bagman.mailbox_domain_rule.v1.schema.json` | BAGMAN MailboxDomainRule | `contracts/mailbox/bagman.mailbox_domain_rule.v1.schema.json` |
| `https://bagman.internal/contracts/mailbox/bagman.mailbox_message.v1.schema.json` | BAGMAN MailboxMessage | `contracts/mailbox/bagman.mailbox_message.v1.schema.json` |
| `https://bagman.internal/contracts/mailbox/bagman.mailbox_source.v1.schema.json` | BAGMAN MailboxSource | `contracts/mailbox/bagman.mailbox_source.v1.schema.json` |
| `https://bagman.internal/contracts/mailbox/bagman.mailbox_sweep_run.v1.schema.json` | BAGMAN MailboxSweepRun | `contracts/mailbox/bagman.mailbox_sweep_run.v1.schema.json` |
| `https://bagman.internal/contracts/manifest/bagman.component_manifest.v1.schema.json` | BAGMAN Component Manifest | `contracts/manifest/bagman.component_manifest.v1.schema.json` |
| `https://bagman.internal/contracts/needs_you/bagman.needs_you_item.v1.schema.json` | BAGMAN NeedsYouItem | `contracts/needs_you/bagman.needs_you_item.v1.schema.json` |
| `https://bagman.internal/contracts/provenance/bagman.provenance.v1.schema.json` | BAGMAN Provenance | `contracts/provenance/bagman.provenance.v1.schema.json` |
| `https://bagman.internal/contracts/source/bagman.external_reference.v1.schema.json` | BAGMAN ExternalReference | `contracts/source/bagman.external_reference.v1.schema.json` |
| `https://bagman.internal/contracts/source/bagman.source.v1.schema.json` | BAGMAN Source | `contracts/source/bagman.source.v1.schema.json` |
| `https://bagman.internal/contracts/xero/bagman.xero_account.v1.schema.json` | BAGMAN XeroAccount | `contracts/xero/bagman.xero_account.v1.schema.json` |
| `https://bagman.internal/contracts/xero/bagman.xero_connection.v1.schema.json` | BAGMAN XeroConnection | `contracts/xero/bagman.xero_connection.v1.schema.json` |
| `https://bagman.internal/contracts/xero/bagman.xero_sync_run.v1.schema.json` | BAGMAN XeroSyncRun | `contracts/xero/bagman.xero_sync_run.v1.schema.json` |

## Canonical Entities

| Key | Display Name | Type |
|-----|--------------|------|
| `INFOSECURS_LIMITED` | Infosecurs Limited | `company` |
| `MATTHEW_SCOTT_PERSONAL` | Matthew Scott (Personal) | `personal` |
| `NOUSTAI_LIMITED` | Noust AI Limited | `company` |
