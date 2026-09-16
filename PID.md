# BAGMAN PID v5 — AI Foundation, Claude Operator & GUI Integration

**AMENDED before dispatch, per Matt's locked AI-topology ruling (2026-09-13, prior to any WI-1 dispatch).** This version supersedes the original CD-5 PID text in full. The amendment replaces every "Trinity is BAGMAN's primary background worker" assumption with a three-tier topology (Claude / dedicated Mac mini / Trinity-as-escalation). Everything else in the original CD-5 doctrine — the hard AI invariant, one gateway, typed contracts, durable provenance, GUI-first, no silent fallback, no canonical AI writes — is unchanged and remains fully binding.

## 1. Product Identity

**Product:** BAGMAN
**Repository:** `github.com/maff0000/bagman`
**Authoritative working tree:** `/srv/bagman`
**Canonical CD-4 merge:** `c91e709db31126274f7ebe46503d269fb7f2a16e`
**Primary branch:** `main`

CD-4 is closed.

This PID authorises:

> **CD-5 — BAGMAN AI Foundation, Claude Operator & GUI Integration**

CD-5 establishes the governed intelligence layer used by every future BAGMAN module.

It does **not** yet connect live mailboxes, banks, Xero, Chargebee or HMRC.

---

# 2. LOCKED AI TOPOLOGY (supersedes the original two-role model)

BAGMAN has **three** distinct intelligence tiers, not two. This is locked and binding for all of CD-5 and beyond.

## A. Claude — operator intelligence

Claude remains BAGMAN's primary interactive/operator-facing intelligence. Claude powers:

* Ask BAGMAN
* explanation
* review
* investigation
* cross-module reasoning
* operator collaboration
* drafting
* assurance

Claude uses governed BAGMAN tools and APIs. Claude is not the routine high-volume worker.

## B. Dedicated Mac mini — primary BAGMAN background inference

A dedicated Apple Mac mini M4 with 16 GB unified memory is being provisioned **exclusively for BAGMAN**. This machine SHALL be BAGMAN's normal, always-available background inference resource.

Expected workload includes: document classification, document summarisation, email triage (later), invoice extraction (later), supplier recognition, entity proposals, accounting-category proposals, routine anomaly screening, other repetitive/background reasoning.

The initial model is expected to be a suitable local approximately-12B-class model, currently likely Gemma-family, but **BAGMAN SHALL NOT depend on that physical model identity.** The Mac mini is dedicated to BAGMAN and should be treated as guaranteed BAGMAN inference capacity.

## C. Trinity compute — escalation/deep tier

Trinity remains available for larger or more difficult reasoning workloads. It is **NOT** BAGMAN's primary routine worker — Trinity also services heavy trading workloads. BAGMAN may escalate difficult cases to Trinity through governed routing only.

---

# 3. Product Objective

At completion, BAGMAN shall have the three deliberately separate AI tiers of §2, never conflated:

* **Operator Intelligence** — Claude, as described in §2A.
* **Background Inference (primary)** — the dedicated Mac mini, as described in §2B, reached only via BAGMAN-specific LiteLLM aliases (§5).
* **Background Inference (escalation)** — Trinity compute, as described in §2C, reached only via a separate BAGMAN-specific escalation alias.

Physical model names, revisions, hostnames and GPU/device placement belong to the inference authority (Helm/Trinity), never to BAGMAN.

---

# 4. Governing Principle

The architecture is:

```text
                         MATT
                          │
                          ▼
                  BAGMAN GUI / CHAT
                          │
                          ▼
                       CLAUDE
                          │
                    governed tools
                          │
                          ▼
┌────────────────── BAGMAN CANONICAL SERVICES ──────────────────┐
│ Evidence │ Intake │ Audit │ future Finance/Tax/etc.           │
└──────────────────────────┬─────────────────────────────────────┘
                           │
               routine background tasks
                           ▼
                  BAGMAN AI GATEWAY
                           │
                           ▼
              existing TRINITY LITELLM (sole inference gateway)
                     │              │
            bagman-fast/core   bagman-deep
                     │              │
                     ▼              ▼
           DEDICATED MAC MINI    TRINITY COMPUTE
           (BAGMAN-exclusive)    (escalation tier)
```

Neither AI path writes canonical truth directly.

---

# 5. Hard AI Invariant (unchanged, still absolute)

AI output is a proposal. Never:

```text
LLM output
   ↓
canonical truth
```

Always:

```text
canonical evidence/state
        ↓
governed AI task
        ↓
structured proposal
        ↓
validation
        ↓
policy / confidence / approval
        ↓
canonical state
```

CD-5 shall establish this as an enforceable architectural boundary.

---

# 6. One BAGMAN AI Gateway

Introduce a single bounded subsystem.

Suggested structure:

```text
ai/
├── gateway/
├── providers/
│   ├── claude/
│   └── litellm/
├── tasks/
├── contracts/
├── prompts/
├── policy/
├── evaluation/
└── provenance/
```

or equivalent.

Every future BAGMAN module consumes the gateway. Modules SHALL NOT independently instantiate: Anthropic clients, LiteLLM clients, Ollama/MLX/llama.cpp clients, OpenAI-compatible clients, or raw HTTP LLM requests.

Note the provider directory is named `litellm/`, not `trinity/` — the gateway talks to ONE inference control plane (the existing Trinity LiteLLM installation, §8), which happens to route some BAGMAN aliases to the Mac mini and others to Trinity compute. BAGMAN's own code has no separate "Mac mini provider" and no separate "Trinity provider" — there is exactly one LiteLLM-speaking adapter, parameterised only by which BAGMAN alias a task requests.

---

# 7. Component Identity

Introduce an actual governed component:

```text
BAGMAN.AI
```

Responsibility:

> Route typed BAGMAN intelligence tasks to an authorised AI provider/capability, enforce task contracts and policy, record invocation provenance, validate structured responses and return proposals without directly mutating canonical business state.

---

# 8. ONE INFERENCE CONTROL PLANE — the existing Trinity LiteLLM installation

**DO NOT create a second LiteLLM architecture for BAGMAN.** The existing Trinity LiteLLM container remains the sole governed inference gateway for all of BAGMAN's background inference (both the Mac mini and Trinity-escalation tiers).

The intended topology:

```text
BAGMAN
  ↓
existing Trinity LiteLLM
  ├── bagman-fast  → dedicated Mac mini
  ├── bagman-core  → dedicated Mac mini
  └── bagman-deep  → Trinity heavier model
```

* BAGMAN SHALL NOT call the Mac mini directly.
* BAGMAN SHALL NOT call Ollama/MLX/llama.cpp/other local-serving endpoints directly.
* BAGMAN SHALL NOT know the Mac mini hostname/IP except within deployment-level integration configuration owned by the inference authority where strictly necessary (i.e. never in BAGMAN application code).
* BAGMAN consumes capability aliases only.

The gateway recognises two provider ROLE classes at the BAGMAN-application level:

```text
OPERATOR    → Claude
BACKGROUND  → existing Trinity LiteLLM (routed via BAGMAN-specific aliases, §9)
```

This is semantic routing. A caller requests a task/capability. It must not select arbitrary raw models, and BAGMAN's own code never distinguishes "Mac mini" from "Trinity compute" — that distinction lives entirely inside the alias→physical-backend mapping the existing LiteLLM installation already owns and governs.

---

# 9. BAGMAN-Specific Aliases (supersedes the original `trinity-fast/core/deep/embed` naming)

Introduce BAGMAN-owned logical aliases in the existing LiteLLM authority:

```text
bagman-fast
bagman-core
bagman-deep
```

Initial routing intent (owned and enforced by the existing LiteLLM installation, not by BAGMAN):

* `bagman-fast` → dedicated Mac mini
* `bagman-core` → dedicated Mac mini
* `bagman-deep` → Trinity stronger/heavier model

These aliases are BAGMAN-specific so BAGMAN routing can evolve independently from trading-system aliases (`trinity-fast`/`trinity-core`/`trinity-deep`/`trinity-embed`). **Do NOT overload or repurpose the `trinity-*` aliases** — those may be shared by other systems (e.g. trading). BAGMAN's own architecture-boundary tests (§75/§87) must assert that no BAGMAN source file references a `trinity-*` alias at all, only `bagman-*`.

The existing Trinity alias governance remains authoritative for physical model mapping. BAGMAN does not define, own, or vote on that mapping.

*(Embedding capability: no `bagman-embed` alias is introduced in CD-5 — retrieval/search work is out of scope, per §85/§68 of the original PID. If a future delivery needs one, it is scoped then, not assumed here.)*

---

# 10. Physical Model Abstraction

BAGMAN source code SHALL NOT contain:

* Gemma physical model names
* MLX model names
* Ollama model tags
* quantisation filenames
* Mac mini hostnames
* GPU/device assumptions
* Trinity physical checkpoint names

Those belong below the alias boundary, owned by the inference authority (Helm/Trinity). BAGMAN should record observed physical-model provenance returned by LiteLLM where available (audit-only, per §24 of the original doctrine, retained), but it must never route on it.

---

# 11. Routing Policy

CD-5 shall treat the normal routing pattern as:

```text
NORMAL / ROUTINE          → bagman-fast or bagman-core → dedicated Mac mini
COMPLEX / ESCALATED       → bagman-deep                → Trinity
OPERATOR INTERACTION      → Claude
```

No automatic cross-tier fallback is authorised merely because one tier is busy.

* If the Mac mini is unavailable: the routine task fails visibly or remains retryable. BAGMAN does not silently redirect every task to Trinity. Explicit policy may later permit selected escalation, but CD-5 does not build that escalation-on-failure path itself.
* If Trinity is unavailable: the deep/escalated task fails visibly. The Mac mini does not pretend to be equivalent unless a future task-specific policy explicitly allows it.
* If Claude (operator) is unavailable: the chat surface reports failure clearly; canonical services and background inference remain independently usable; no silent substitution of a local model for the operator role.

---

# 12. Existing LiteLLM Remains Authority

The current Trinity LiteLLM installation remains responsible for: alias routing, backend selection, virtual keys, model-provider configuration, endpoint abstraction, request routing, central inference observability.

**BAGMAN must not duplicate these functions.**

---

# 13. BAGMAN Credential

BAGMAN should use **one** BAGMAN-scoped LiteLLM virtual key.

Preferred secret location:

```text
/srv/bagman-secrets/trinity_litellm_api_key
```

mounted into runtime as:

```text
/run/secrets/trinity_litellm_api_key
```

The key should be permitted to invoke only the BAGMAN aliases required by CD-5 (`bagman-fast`, `bagman-core`, `bagman-deep`, and any future specifically-authorised BAGMAN alias). No unrestricted physical-model access.

A second, separate secret governs the Claude operator provider:

```text
/srv/bagman-secrets/anthropic_api_key  →  /run/secrets/anthropic_api_key
```

Both follow the exact secret-file convention already established in CD-3/CD-4 (`postgres_password`, `minio_root_user*`, never a literal in code/tests/logs/Git).

**Both credentials are provisioned by Matt directly** (a BAGMAN-scoped LiteLLM virtual key minted under a `bagman-runtime` identity, and a BAGMAN-scoped Anthropic API key from Matt's own account) — this is explicitly not something FORGE self-services. Neither secret's value may ever appear in chat, Git, the PID, the evidence file, `.env`, Compose YAML, a test fixture, or command history.

---

# 14. HELM Dependency — the Mac-mini readiness gate

HELM is being separately authorised to prepare and prove the Mac mini. **Until HELM reports the dedicated inference node GREEN, do not fabricate live Mac-backed acceptance.**

Until that report:

**FORGE MAY build:**

* provider-neutral gateway code (the one LiteLLM-speaking adapter, §6/§8)
* task contracts (§17 of the original doctrine, renumbered §29 below)
* deterministic fakes for both `bagman-*` background tasks and the Claude operator path
* the GUI (Ask BAGMAN, Documents AI panel, Overview AI status — all buildable and demoable against fakes, per §12 of Matt's amendment / GUI-first doctrine)
* persistence (durable AI invocation records, §20 of the original doctrine)
* evaluation fixtures (§53 of the original doctrine)

**FORGE MAY NOT claim, until HELM's GREEN report exists:**

* a real, live proof that `bagman-fast`/`bagman-core` genuinely reaches the dedicated Mac mini
* a real, live proof of Mac-mini restart recovery
* real load/latency evidence from the Mac mini

Until HELM's report, this is a genuine `BLOCKED` condition for those specific acceptance criteria (§39 verdict vocabulary) — not something to route around, mock into a false "live" claim, or quietly skip. The real Trinity-LiteLLM-to-Claude proof (Claude operator tier) and the `bagman-deep`-to-Trinity-escalation proof are independent of the Mac mini and are NOT blocked by this gate — those may proceed on their own schedule once their own credentials (§13) exist.

HELM's GREEN report must, at minimum, demonstrate: a stable model-serving runtime; a reachable endpoint from the existing Trinity LiteLLM; a BAGMAN-dedicated service identity; restart persistence; load/latency evidence; the chosen model/runtime identity; security/network proof; and an end-to-end LiteLLM alias proof (i.e. LiteLLM itself, calling into the Mac mini via `bagman-fast`/`bagman-core`, returns a genuine completion) — all owned and produced by HELM, not by FORGE/BAGMAN.

---

# 15. Network Doctrine

BAGMAN-to-LiteLLM network access must be explicit and documented. Do not casually expose LiteLLM publicly to make connectivity easy. Preferred: a private, trusted network path, or another bounded internal route. Exact host/network implementation is determined from the real Trinity/Mac-mini environment as HELM provisions it — BAGMAN's own compose/deployment config references only the LiteLLM endpoint, never the Mac mini directly (§8).

---

# 16. Resource Isolation Principle — standing BAGMAN architecture doctrine

> **BAGMAN routine inference SHALL preferentially use dedicated BAGMAN compute so that critical financial-administration workloads are not dependent on contention from unrelated trading workloads.**

This is the reason for the Mac mini. It is not merely a cheaper local-inference option. This principle is recorded as standing doctrine (alongside the already-standing GUI-first and AI-role doctrines) and must be checked before any future delivery that touches BAGMAN's inference topology.

---

# 17. Claude Doctrine

Claude is BAGMAN's interactive operator intelligence. Claude may receive requests such as:

* "What needs my attention?"
* "Explain this document."
* "Why was this quarantined?"
* "What evidence belongs to NoustAI?"
* "Summarise what BAGMAN currently knows."
* "Review this local-AI proposal."
* "What should I do next?"

Claude operates using governed BAGMAN tools/APIs. Claude must not receive raw database credentials or arbitrary SQL access. Claude must not receive host-shell authority. Claude must not directly mutate canonical financial/tax/customer state.

---

# 18. Claude Model Configuration

BAGMAN may depend on the **Claude provider**, but should not scatter a specific Claude checkpoint throughout code. Use external configuration such as:

```text
BAGMAN_OPERATOR_PROVIDER=anthropic
BAGMAN_OPERATOR_MODEL=<configured Claude model>
```

or an equivalent governed configuration contract. Model version used for each invocation must be recorded. Secrets remain external (§13). No Anthropic API key in Git.

---

# 19. No Direct Ollama/MLX/llama.cpp Access

BAGMAN must not call Ollama, MLX, llama.cpp, or any other local-serving endpoint directly. Path:

```text
BAGMAN → existing Trinity LiteLLM → bagman-* alias → physical backend (Mac mini or Trinity)
```

not:

```text
BAGMAN → Ollama :11434 / MLX / Mac mini directly
```

---

# 20. No Silent AI Fallback

If a task is classified `BACKGROUND` and its assigned tier (Mac mini via `bagman-fast`/`bagman-core`, or Trinity via `bagman-deep`) is unavailable, BAGMAN must not silently send it to a different tier, to Claude, or to another cloud provider. Likewise an operator-Claude failure must not silently route the conversation to a local model. Fallback requires explicit policy. CD-5 default: **fail visibly, preserve work state, permit retry.**

---

# 21. AI Task Registry

Do not expose a generic `ask_llm(prompt)` as the product architecture. Introduce typed tasks.

Recommended CD-5 tasks:

```text
DOCUMENT_SUMMARY            (bagman-fast or bagman-core)
DOCUMENT_TYPE_PROPOSAL      (bagman-fast or bagman-core)
ENTITY_PROPOSAL             (bagman-core)
OPERATOR_DOCUMENT_REVIEW    (Claude)
```

The first three exercise the Mac-mini tier (via `bagman-fast`/`bagman-core`) once §14's gate opens; against deterministic fakes until then. The fourth exercises Claude and is not blocked by §14.

---

# 22. Task Contract

Every task definition should declare:

```text
task_id
task_version
role
preferred_capability
input_schema
output_schema
timeout
confidence_policy
data_policy
```

Example:

```text
DOCUMENT_TYPE_PROPOSAL
version: 1
role: BACKGROUND
preferred_capability: bagman-fast
```

Callers request the task, not the model.

---

# 23. Capability Routing

Initial routing guidance:

### `bagman-fast` (→ dedicated Mac mini)

Use for: simple document type proposal; lightweight summarisation; high-volume email triage later; straightforward extraction/classification.

### `bagman-core` (→ dedicated Mac mini)

Use for: invoice understanding later; entity proposals; category proposals; normal ambiguity; workflow reasoning.

### `bagman-deep` (→ Trinity, escalation tier)

Use for: difficult ambiguity; review/assurance; conflicting evidence; unusual/high-risk background analysis — cases genuinely warranting escalation beyond the dedicated Mac mini's capacity.

*(No embedding alias in CD-5 — see §9's closing note.)*

---

# 24. Structured Output Only

Background tasks must produce schema-validated structured output. Not `"I think this appears to be..."` as the API contract. Prefer:

```json
{
  "proposed_type": "INVOICE",
  "confidence": 0.94,
  "signals": ["invoice number present", "total amount present"],
  "warnings": []
}
```

Exact contracts must be versioned.

---

# 25. No Hidden Chain-of-Thought Persistence

BAGMAN shall **not** require or store private chain-of-thought. Persist only useful auditable information: `decision_summary`, `signals`, `warnings`, `confidence`, input references, output proposal, provider/model identity, validation results. "Why did BAGMAN do this?" is answered through evidence, structured reasons, policy and audit — not hidden model scratch reasoning.

---

# 26. AI Invocation Identity

Every inference shall receive a canonical BAGMAN AI invocation ID: `ai_invocation_id` — distinct from evidence ID, intake ID, audit event ID, workflow ID.

---

# 27. Durable AI Invocation Record

Introduce durable PostgreSQL persistence. Conceptual fields:

```text
ai_invocation_id
task_id
task_version
role
provider              (e.g. "litellm" or "anthropic" — the ADAPTER, not the physical backend)
capability_alias      (e.g. "bagman-fast", "bagman-deep", or the Claude model config)
provider_model        (physical model identity observed from the provider response, when available — audit-only, §10)
started_at
completed_at
status
correlation_id
actor
input_references
prompt_contract_version
output
confidence
validation_result
error_code
usage_metadata
latency_ms
```

Avoid storing unnecessary full sensitive prompts when references are sufficient.

---

# 28. Invocation States

```text
REQUESTED
RUNNING
SUCCEEDED
FAILED
REJECTED
```

A more refined equivalent is allowed. AI provider failure must remain observable.

---

# 29. Input Provenance

AI inputs must be traceable. For document tasks, record canonical references such as `evidence_id`, `intake_id`, `entity_id`. Do not create opaque AI analysis disconnected from source evidence.

---

# 30. Output Provenance

An AI proposal shall remain linked to: input evidence, task/version, provider adapter, capability alias, actual provider model (when available), time, validation result. If a later model produces a different answer, both analyses remain distinguishable.

---

# 31. Prompt Governance

Task prompts are code/configuration assets. Store versioned prompt templates/contracts in Git, e.g. `ai/prompts/document_type/v1.*`. Changing material task instructions requires version change or documented compatibility rationale. Do not construct uncontrolled prompt strings throughout services.

---

# 32. Prompt Injection Doctrine

Documents, emails and user-supplied evidence are untrusted content. AI tasks must clearly separate system/task instructions from untrusted evidence content. Evidence containing "Ignore all previous instructions…" is data, not authority. Add prompt-injection fixtures/tests.

---

# 33. Tool Authority

Background local AI (Mac mini or Trinity escalation) gets **no tools** in CD-5 — bounded task inputs, structured proposals only. Claude operator tooling must be explicit and read-oriented initially.

---

# 34. Initial Claude Tool Set

For CD-5, Claude may receive read-only BAGMAN tools such as:

```text
get_runtime_status
list_documents
get_document
get_intake
trace_provenance
list_ai_invocations
get_ai_invocation
run_background_analysis
```

Exact names may vary. Claude shall not receive: `raw_sql`, `host_shell`, `docker`, `arbitrary_http`, `delete_evidence`, `write_accounting`, `send_email`, `move_money`.

---

# 35. Background Analysis Tool

Claude should be able to request a governed local analysis:

```text
Claude → run_background_analysis(task="DOCUMENT_TYPE_PROPOSAL", evidence_id=...) → BAGMAN AI Gateway → existing LiteLLM → bagman-fast/core → Mac mini
```

Claude does not call LiteLLM itself. This preserves one gateway and complete provenance.

---

# 36. First Real AI Vertical Slice

CD-5 must prove one useful end-to-end background workflow: **AI-assisted document understanding**. For a registered document: (1) operator opens Documents; (2) requests AI analysis; (3) BAGMAN runs one or more background tasks through the gateway (against the deterministic fake until §14's gate opens, then against the real Mac-mini-backed alias once HELM reports GREEN); (4) structured proposal is validated; (5) invocation is persisted; (6) GUI shows proposal, confidence, model/capability provenance and warnings; (7) canonical EvidenceItem remains unchanged.

---

# 37. Automatic Background Analysis

CD-5 may optionally permit automatic analysis immediately after successful document registration if policy permits. This must be configurable, observable, retryable, non-blocking to evidence preservation, and incapable of converting AI output to canonical financial truth. If the assigned inference tier is down, the document remains safely registered.

---

# 38. AI Failure Must Not Break Evidence Intake

Evidence intake is canonical. AI is enrichment. Therefore: evidence registration success + AI analysis failure must leave valid evidence + visible AI failure/retry state. Never roll back valid evidence because an LLM tier is unavailable.

---

# 39. Verdict Vocabulary

Use:

### AI_FOUNDATION_GREEN
All mandatory CD-5 criteria proven.

### AI_FOUNDATION_RED
One or more mandatory criteria failed.

### BLOCKED
External conditions prevent required proof (e.g., §14's Mac-mini gate not yet opened by HELM).

No generic `GREEN`.

---

# 40. GUI-First Doctrine (standing, binding)

> Every meaningful BAGMAN capability acquires its operator-facing GUI surface early within the same delivery.

CD-5 therefore begins with GUI architecture work, not ends with it. **Do not wait for the Mac-mini integration before developing the GUI surfaces** — once WI-1 contracts are locked, build the AI panel, invocation-state UI, Ask BAGMAN, and health/state visibility using deterministic provider fakes until the real Mac/LiteLLM path is available (§14). Matt must be able to inspect and steer the product throughout CD-5.

---

# 41. GUI Modularisation

The existing bootstrap GUI must be reorganised sufficiently to prevent `app.js` becoming a monolith. A framework migration is **not automatically authorised**. A reasonable vanilla structure could become:

```text
static/
├── shell/
├── shared/
├── features/
│   ├── overview/
│   ├── documents/
│   └── ai/
└── index.html
```

Exact structure is FORGE's choice. The architectural goal is feature ownership and composability.

---

# 42. Ask BAGMAN

Introduce a persistent operator chat surface: **Ask BAGMAN**. Claude-backed. May initially appear as a side panel, drawer, or dedicated workspace, but should be accessible throughout the application shell.

---

# 43. Ask BAGMAN Interaction

Minimum experience:

```text
Matt: What is this document?

BAGMAN:
This appears to be an invoice.
Local analysis confidence: 94%.
Supplier candidate: ...
Evidence: <document>
```

Claude may combine canonical document metadata, provenance, prior local-AI proposals, and runtime state, using governed tools.

---

# 44. Claude Must Identify Evidence

Answers about BAGMAN state should refer to canonical evidence/records. Avoid unsupported conversational assertions. The GUI should make relevant referenced records clickable where practical.

---

# 45. AI Panel in Documents

Document detail shall gain an AI section. At minimum display: task, status, proposal, confidence, capability, actual model, completed, warnings, validation. Operator should be able to: run analysis, retry failed analysis, ask BAGMAN about this. No approve-to-canonical workflow yet unless separately authorised.

---

# 46. Overview AI Status

Overview should gain a compact AI status area showing: Claude operator availability; LiteLLM gateway availability; per-alias health where practical (`bagman-fast`/`bagman-core`/`bagman-deep` each distinctly, since the Mac mini and Trinity tiers can fail independently); recent background failures; pending analyses. Do not expose raw infrastructure noise as the main UX.

---

# 47. Operations Visibility

Add enough operational UI/API state that Matt can distinguish: Claude unavailable; Mac-mini tier unavailable (`bagman-fast`/`bagman-core`); Trinity-escalation tier unavailable (`bagman-deep`); task failed; structured validation failed; model returned usable proposal. These are different failure classes — the three-tier topology makes this distinction more important than in the original two-role design, not less.

---

# 48. AI Health

AI dependencies should have explicit health semantics. Do not necessarily make all AI dependencies part of core BAGMAN `/ready` (evidence/runtime services should remain usable if AI is unavailable). Prefer separate readiness: `/ready` for canonical BAGMAN runtime, `/internal/ai/health` for intelligence capability — the latter should report per-tier status (Claude / Mac-mini aliases / Trinity-escalation alias) distinctly, not one flattened boolean.

---

# 49. Claude Connectivity

The Claude provider adapter must: use explicit timeout; bound retries; distinguish rate limiting; distinguish authentication failure; map provider exceptions into BAGMAN errors; never log API keys; avoid logging complete sensitive prompts by default.

---

# 50. LiteLLM Connectivity

The one LiteLLM-speaking adapter (§6/§8) must provide: explicit endpoint configuration; alias-only requests (no physical endpoint in module code); timeout; bounded retry; structured output handling; per-alias health check; safe error mapping. It must never need to know or care whether a given alias resolves to the Mac mini or to Trinity — that distinction is the existing LiteLLM installation's job alone.

---

# 51. AI Data Policy

Not every piece of BAGMAN evidence should automatically be sent to every AI provider/tier. Introduce provider-aware data policy, e.g.:

```text
LOCAL_OK
CLOUD_OPERATOR_OK
CLOUD_RESTRICTED
```

or equivalent. CD-5 may begin with a simple policy, but it must exist. Note: with the Mac-mini tier now dedicated BAGMAN-exclusive hardware, "local" has a clearer meaning than in the original PID's Trinity-shared-fabric framing — this reinforces rather than weakens the §51/§16 privacy rationale.

---

# 52. Local-First Background Privacy

Routine background processing should default to the dedicated Mac-mini tier (`bagman-fast`/`bagman-core`). This is both an architectural and privacy advantage — reinforced by the Mac mini's dedicated, BAGMAN-exclusive nature (§16). Claude is used for interactive operator reasoning, not indiscriminately for batch processing every document. Trinity-escalation (`bagman-deep`) is reserved for genuinely difficult cases, not a routine default.

---

# 53. Claude Context Minimisation

Claude should receive only context required for the operator request: metadata, extracted/selected content, relevant evidence references, AI proposals — not entire databases or unrelated documents.

---

# 54. Canonical vs AI-Derived Data

GUI must visually distinguish **Canonical** from **AI proposed**. Do not blur them into one field.

---

# 55. Confidence Doctrine

Confidence is task-specific metadata, not universal truth. Each task contract may define what confidence means for it. Do not establish one arbitrary global threshold such as 0.8 for every task.

---

# 56. Decision Policy

CD-5 does not authorise AI to execute material business decisions. Background AI (either tier) may propose. Claude may recommend. Canonical action requires deterministic service/policy handling and, later, workflow approval where appropriate.

---

# 57. AI Audit Events

Introduce meaningful audit events: `AI_INVOCATION_REQUESTED`, `AI_INVOCATION_SUCCEEDED`, `AI_INVOCATION_FAILED`, `AI_OUTPUT_REJECTED`, `AI_ANALYSIS_RETRIED`. Avoid token-level noise.

---

# 58. Correlation

An invocation triggered from document intake should share useful workflow correlation: evidence registered → AI invocation → AI proposal. The lineage should be navigable.

---

# 59. Evaluation Harness (mandatory)

A swappable local-model architecture is valuable only if task quality can be measured. Introduce `ai/evaluation/` with synthetic/golden task fixtures. At minimum evaluate: structured-output validity; expected classification; abstention/uncertainty; prompt-injection resilience; malformed output; timeout/error handling.

---

# 60. Alias Regression Testing

When the physical backend behind `bagman-fast`/`bagman-core`/`bagman-deep` changes (a new Mac-mini model, a Trinity model swap), BAGMAN should eventually be able to rerun task evaluations and determine whether behaviour degraded. CD-5 shall establish the harness needed for this. It does not need to automate promotion decisions.

---

# 61. Test Determinism

Ordinary unit/CI tests must not depend on nondeterministic live LLM answers. Use deterministic provider fakes, contract fixtures, or recorded synthetic responses where appropriate. Live AI acceptance is separate evidence (§14, §67-§70).

---

# 62. Tool-Use Guardrails

Claude tool calls must pass through a BAGMAN tool registry/router. Each tool declares: name, description, input contract, authority class, side effects. CD-5 tools should be `READ`/`ANALYSE`, not material-action tools.

---

# 63. No Arbitrary Tool Invocation

Claude must not be able to invent a method name and cause BAGMAN to execute it. Only registered tools may run. Invalid tool requests fail closed.

---

# 64. Operator Conversation Persistence

CD-5 may persist chat sessions/messages if useful, but: conversation state is not canonical financial truth; avoid storing unnecessary secrets; messages should be associated with operator/session identity; retention policy can remain simple initially. An in-session-only chat is acceptable for the initial vertical slice if persistence would materially expand CD-5's scope.

---

# 65. No AI Memory Authority

Agent/chat memory cannot override canonical records. If chat says "That invoice belongs to Infosecurs", that is not canonical entity assignment until a governed operation records such a decision.

---

# 66. Filename Bidi Hardening (carried forward from CD-4)

Close the CD-4 Auditor's filename-spoofing backlog item (Unicode bidi/format control characters, recorded at `bagman:backlog:filename_bidi_spoofing_hardening`). At minimum: identify bidi/format controls relevant to spoofing; neutralise/reject them in filename safety policy; ensure displayed/download filenames cannot visually disguise dangerous extension/order; add adversarial tests. Bounded security-hardening item, not open-ended.

---

# 67. GUI Design Standard

Do not merely add a textarea and call it chat. Maintain BAGMAN's intended premium operator feel: strong hierarchy, restrained design, responsive shell, clear state, useful tables, detail panels, minimal visual noise, dark/light readiness if practical. The user should be able to judge and steer the product continuously.

---

# 68. API Surfaces

Potential endpoints:

```text
POST /internal/ai/tasks
GET  /internal/ai/invocations
GET  /internal/ai/invocations/{id}
GET  /internal/ai/health

POST /internal/operator/chat
```

Exact routing may vary. Do not expose generic raw prompt completion endpoints.

---

# 69. Background Task Request

Conceptually:

```json
{
  "task_id": "DOCUMENT_TYPE_PROPOSAL",
  "task_version": 1,
  "subject": { "evidence_id": "..." }
}
```

BAGMAN loads authorised context. Caller should not upload arbitrary giant prompts directly.

---

# 70. Provider Response Normalisation

The AI gateway should normalise provider differences into BAGMAN-owned results. Modules should not depend on Anthropic/LiteLLM response JSON structures.

---

# 71. Usage Metadata

Where available, record safe usage information: latency, input tokens, output tokens, provider request ID. This will later help cost/performance governance. Do not make provider billing data canonical financial truth.

---

# 72. Background Job Execution

CD-5 should not introduce a complex distributed queue merely for AI. If asynchronous execution is useful, use the simplest reliable approach compatible with the current runtime. Redis remains prohibited absent new architectural evidence. PostgreSQL-backed job/state handling is preferable to introducing infrastructure prematurely.

---

# 73. Concurrency

Multiple requests to analyse the same evidence/task/version should have explicit semantics. Recommended: **one active invocation per `(evidence_id, task_id, task_version)`** unless a deliberate retry/new-run request is recorded. Do not accidentally create dozens of duplicate background analyses from repeated GUI clicks.

---

# 74. Retry Semantics

Retries create auditable invocation attempts. Do not overwrite the history of a failed AI request.

---

# 75. Timeouts

No LLM call may wait forever. Task/provider policy must define timeouts. Timeout should result in visible `FAILED / TIMEOUT` with safe retry.

---

# 76. Structured Validation Failure

If an LLM returns malformed JSON or violates output schema: do not repair it silently into canonical truth; record validation failure; optionally perform a bounded retry if policy explicitly allows it; preserve observable failure.

---

# 77. AI Security Boundaries

Hard prohibitions: no AI credentials in repo; no raw DB credentials in prompts; no unrestricted SQL; no host shell; no Docker socket; no arbitrary outbound URL fetch; no tool discovery outside registered BAGMAN tools; no silent cross-tier/cross-provider fallback; no prompt-controlled provider/model/alias name.

---

# 78. Logging

Logs may include: `ai_invocation_id`, `task_id`, `capability_alias`, provider adapter, status, latency, correlation_id. Do not log: API keys, full private documents, raw chat prompts by default, chain of thought.

---

# 79. Component Manifests

Add/update actual manifests for `BAGMAN.AI`, `BAGMAN.RUNTIME.API`, and GUI ownership as appropriate. Architecture-memory projection remains mandatory and drift-checked.

---

# 80. CI

Existing controls remain: gitleaks; architecture-memory drift; security; contracts; integration; PostgreSQL persistence; app runtime. Add AI suites while preserving the CD-3 rule: **live CI result is distinct evidence and must actually be observed.** CI must not require real Claude/LiteLLM credentials — all ordinary CI-collected tests run against deterministic fakes.

---

# 81. Live-AI Acceptance

Real-provider acceptance belongs to explicit acceptance scripts/environment, not ordinary PR unit CI. Required acceptance classes:

```text
Claude live proof                 (independent of §14's gate)
Trinity-escalation live proof     (bagman-deep — independent of §14's gate, needs only §13's LiteLLM key)
Mac-mini live proof               (bagman-fast/core — BLOCKED until HELM reports GREEN, §14)
GUI browser proof                 (buildable against fakes now; re-run once live tiers are available)
failure-mode proof                (each tier independently, per §85 below)
```

---

# 82. Trinity-Escalation Failure Proof

Make the Trinity-escalation tier (`bagman-deep`) unavailable. Prove: the escalated task fails visibly; canonical evidence remains intact; no silent fallback to the Mac-mini tier or to Claude; retry becomes possible after recovery.

---

# 83. Mac-Mini Failure Proof (BLOCKED until §14 opens)

Once HELM reports GREEN: make the Mac-mini tier (`bagman-fast`/`bagman-core`) unavailable. Prove: the routine background task fails visibly; canonical evidence remains intact; no silent escalation to Trinity and no silent substitution by Claude; retry becomes possible after recovery.

---

# 84. Claude Failure Proof

Make the Claude operator provider unavailable or provide a controlled failure. Prove: chat reports failure clearly; BAGMAN canonical services remain operational; background inference (either tier) remains independently usable; no local-model substitution silently changes operator behaviour.

---

# 85. Prompt-Injection Proof

Create synthetic evidence containing hostile instructions. Prove local analysis (either background tier) treats it as content. Where the evidence reaches Claude through an operator tool, prove the tool/task instruction remains authoritative and evidence text cannot grant additional tools/authority.

---

# 86. GUI Browser Acceptance

Browser proof must cover: (1) BAGMAN loads; (2) Documents still works; (3) AI section visible; (4) trigger local analysis; (5) status transitions visible; (6) result appears; (7) provenance/model information visible (including which alias/tier served the request); (8) open Ask BAGMAN; (9) ask about synthetic document; (10) Claude responds; (11) referenced document remains identifiable; (12) failure state rendered cleanly. This proof is buildable and runnable against deterministic fakes now; re-run against the real Mac-mini tier once §14 opens.

---

# 87. Required End-to-End Background Proof (split by tier)

**Against deterministic fakes, in this delivery, regardless of §14:**

1. upload synthetic document; 2. evidence registers; 3. request `DOCUMENT_TYPE_PROPOSAL`; 4. gateway routes to the configured `bagman-*` alias (fake backend); 5. physical model is not selected by caller; 6. fake returns response; 7. output schema validates; 8. invocation persists; 9. GUI displays proposal/confidence; 10. canonical evidence remains unchanged; 11. restart BAGMAN; 12. invocation remains visible; 13. retry semantics proven.

**Against the real Trinity-escalation tier (`bagman-deep`), once §13's LiteLLM key exists — not blocked by §14:** the same 13 steps, for real, against Trinity compute.

**Against the real Mac-mini tier (`bagman-fast`/`bagman-core`), once HELM reports GREEN (§14) — BLOCKED until then:** the same 13 steps, for real, against the dedicated Mac mini, PLUS: (14) taking the Mac mini offline causes visible routine-AI failure, not silent reroute; (15) Mac-mini restart recovers service; (16) Trinity trading workloads are not a prerequisite for routine BAGMAN inference remaining available.

---

# 88. Required Claude Proof

Against real Claude, once §13's Anthropic key exists — not blocked by §14: (1) open Ask BAGMAN; (2) ask a question about synthetic evidence; (3) BAGMAN determines allowed context/tools; (4) Claude receives bounded context; (5) Claude returns answer; (6) relevant evidence/tool results are visible; (7) invocation/provider provenance recorded as appropriate; (8) no canonical mutation occurs.

---

# 89. Work Breakdown (amended for the three-tier topology and the Helm dependency gate)

### WI-1 — AI Contracts, Domain & Persistence

* AI task contract; invocation model; schemas; PostgreSQL migration/repository; prompt versioning; audit vocabulary; provider-neutral result model.
* No live-provider dependency. Not blocked by anything.

### WI-2 — LiteLLM Background Gateway (Mac-mini + Trinity-escalation tiers, one adapter)

* Inspect/prove the EXISTING LiteLLM connection (already confirmed reachable at the local LiteLLM endpoint during PID review — full inspection is this WI's job, not a prior assumption).
* Build the ONE alias-only client (§6/§8/§50) — parameterised only by `bagman-fast`/`bagman-core`/`bagman-deep`, with zero knowledge of which physical backend a given alias resolves to.
* Task routing, structured output, failure handling — per-tier observable, per §47/§48.
* Real Trinity-escalation (`bagman-deep`) integration proof — buildable once §13's LiteLLM key exists, NOT blocked by §14.
* Real Mac-mini (`bagman-fast`/`bagman-core`) integration proof — explicitly BLOCKED until HELM reports the Mac mini GREEN (§14). Build and test everything else in this WI against deterministic fakes for the Mac-mini-routed aliases in the meantime.

### WI-3 — Claude Operator Gateway & Tool Registry

* Claude provider adapter; Ask BAGMAN backend; read-only governed tool registry; Claude→BAGMAN tool execution loop; real Claude integration proof (once §13's Anthropic key exists). Not blocked by §14.

### WI-4 — GUI Foundation & AI Surfaces

**Start this early, not after WI-1–3 are all finished**, per the GUI-first doctrine (§40). Modularise the frontend; Ask BAGMAN surface; Documents AI panel; Overview AI status (per-tier health, §46); real invocation states/results; preserve existing Documents UX. May proceed in parallel using locked API contracts/fakes once WI-1's contracts are established — not blocked by §14 (build and demo against fakes; swap in real Mac-mini results once available, no GUI redesign required since the contract is provider-neutral by construction, §70).

### WI-5 — Evaluation, Hardening & Acceptance

* Golden fixtures; malformed output; prompt injection; Claude/Trinity-escalation failure proofs (real); Mac-mini failure proof (real, BLOCKED until §14 opens — this WI may need to run in two passes: everything else now, the Mac-mini-specific proof once HELM reports GREEN); concurrency/retry; bidi filename hardening (§66); browser acceptance; fresh independent audit; live CI.

**Sequencing note:** WI-1 first (nothing else can proceed without its contracts). WI-2, WI-3, and WI-4 (GUI) may then proceed with meaningful parallelism — WI-4 against WI-1's locked contracts and fakes, WI-2's Trinity-escalation and Claude-adjacent pieces (WI-3) against real credentials once §13 is satisfied, WI-2's Mac-mini piece against fakes only until §14 opens. WI-5 closes the delivery and is where the Mac-mini-specific proof lands whenever HELM's report arrives, even if that is after the rest of CD-5 is otherwise ready.

---

# 90. Explicitly Out of Scope

CD-5 SHALL NOT implement: Microsoft Graph; Gmail; IMAP; live mailbox polling; automatic invoice posting; Xero; bank connectivity; transaction reconciliation; Chargebee; customer suspension; tax filing; HMRC; automatic R&D claims; vector database; unrestricted autonomous agent; arbitrary web browsing by AI; arbitrary shell access; autonomous money movement; autonomous destructive actions; silent AI-to-canonical writes; a second LiteLLM/inference-control-plane architecture (§8); direct BAGMAN-to-Mac-mini or BAGMAN-to-Ollama/MLX calls (§19).

---

# 91. Acceptance Criteria

CD-5 is complete only when independently proven:

## Architecture
* One AI gateway; provider adapters isolated; no module-specific raw LLM clients; alias-only routing (`bagman-*` only, never `trinity-*`, never a physical name); Claude/Mac-mini/Trinity-escalation tiers distinct; no silent cross-tier/cross-provider fallback; existing LiteLLM remains the sole inference control plane (no second one introduced).

## Contracts
* Typed/versioned task contracts; schema-validated outputs; versioned prompts; provider-neutral invocation/result models.

## Persistence
* Invocation durable; evidence/task provenance durable; retries/history durable; restart safe.

## Trinity-escalation tier
* Real LiteLLM call proven via `bagman-deep`; physical model not hardcoded; physical execution identity captured when available; failure/recovery proven.

## Mac-mini tier
* **BLOCKED until HELM reports GREEN (§14).** Once open: real LiteLLM call proven via `bagman-fast`/`bagman-core`, routed to the dedicated Mac mini; physical model not hardcoded; failure/recovery proven; Mac-mini unavailability causes visible failure, not silent reroute; Trinity trading load is not a prerequisite for routine BAGMAN inference.

## Claude
* Real operator chat proven; read-only governed tools; bounded context; no canonical mutation; failure/recovery proven.

## GUI
* Modularised enough for continued growth; Ask BAGMAN usable; Documents AI panel usable; AI states/results visible per-tier; capability health visible per-tier; browser acceptance green (against fakes now; against real Mac-mini tier once available).

## Safety
* Prompt injection fixtures; no chain-of-thought persistence; no secrets in prompts/logs/repo; local/background work does not silently escalate or go cloud; AI output visibly non-canonical.

## Evaluation
* Deterministic evaluation fixtures; structured-output validation; quality baseline for initial tasks.

## Security
* Filename bidi hardening closed (§66); gitleaks green; prior security controls unchanged.

## CI
* All existing suites green; AI suites green (against fakes, no live credentials required in CI); architecture projection green (including the `bagman-*`-only alias check); exact live PR head observed green.

**A partial `AI_FOUNDATION_GREEN` covering everything except the Mac-mini-tier criteria, with the Mac-mini portion explicitly marked `BLOCKED` pending HELM, is an acceptable and honest interim verdict** — it is not the same as `AI_FOUNDATION_RED`, and it is not a fabricated `GREEN` either. The architect will decide how to sequence final closure once HELM's report exists.

---

# 92. Independent Auditor

A fresh zero-context Auditor shall independently: read PID; inspect provider boundaries; search for physical local-model hardcoding; search for direct Ollama/MLX/LiteLLM/Anthropic usage outside approved adapters; search specifically for any `trinity-fast`/`trinity-core`/`trinity-deep`/`trinity-embed` reference in BAGMAN source (must be absent — only `bagman-*` aliases are permitted); inspect task schemas/prompts; inspect AI persistence/audit; run deterministic tests; run gitleaks; run architecture-memory check; run real Claude proof; run real Trinity-escalation proof; run real Mac-mini proof **if and only if HELM's GREEN report exists at audit time** (otherwise confirm the gate is honestly still closed and nothing fakes around it); independently induce available-tier failures; run prompt-injection proof; inspect GUI/browser workflow; verify canonical evidence does not change from mere AI proposal; inspect exact live CI head/run; walk every §91 criterion. The Auditor shall not inherit Engineer reasoning.

---

# 93. Exit Gate

No live mailbox integration begins until **AI_FOUNDATION_GREEN** (or an explicitly-scoped partial-GREEN-pending-Mac-mini per §91's closing note, at the architect's discretion). Once CD-5 is closed, the first mailbox adapter can immediately feed a system that already knows how to: preserve evidence; scan it safely; reason about it locally (dedicated Mac-mini tier); escalate when genuinely warranted (Trinity tier); surface that reasoning; let Claude discuss it with Matt; preserve complete provenance.

---

# 94. Expected Next Delivery

Likely: **CD-6 — Mail Intake Adapters & Inbox Operations**. Initial sources: Microsoft Graph/Exchange, Gmail, IMAP. All must feed the existing CD-4 governed intake boundary. Email triage/routing uses CD-5's `bagman-fast`/`bagman-core` background tasks (escalating to `bagman-deep` where warranted). Operator review/explanation uses Claude.

---

# 95. Standing Product Doctrine

The long-term pattern is:

```text
Machines collect.
Dedicated local AI processes routinely.
Trinity handles genuine escalation.
BAGMAN validates and governs.
Claude reasons with Matt.
Canonical services decide what is true.
Matt is interrupted only when necessary.
```

That is the BAGMAN architecture.

---

# 96. Topology Finalization Addendum (Architect ruling, 2026-09-16)

This addendum records the final, deployed shape of the "Dedicated local AI processes routinely" tier named throughout this PID (§2/§8/§9 and elsewhere) — it does not replace or delete anything above; §2/§8/§9's original text stands as the historical record of the locked-topology amendment as first issued. Preserve history: describe the prior architecture as superseded, never as though it never existed.

**As originally amended (§2/§8/§9):** `bagman-fast`/`bagman-core`/`bagman-deep` all routed through the SAME, pre-existing, shared Trinity LiteLLM installation — explicitly never a second inference-control-plane.

**As finally deployed, CD-5 Gate-1 closure (2026-09-16):** HELM has since stood up a dedicated, BAGMAN-exclusive Mac AI appliance — its own LiteLLM + PostgreSQL, not the shared Trinity installation — fronting the same dedicated Mac mini for `bagman-fast`/`bagman-core` and escalating to Trinity compute for `bagman-deep`:

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

Claude remains a wholly separate operator path (§8/§13/§17 unaffected in SPIRIT — Claude is still BAGMAN's operator intelligence, still governed by BAGMAN tools/APIs, still never touches canonical state directly). **Correction (2026-09-16, later the same day — see §97 below): the specific claim that this is "an independent Anthropic operator path" reached via a direct Anthropic API key is itself superseded — read §97 before relying on this sentence.** The existing/shared Trinity LiteLLM gateway is **no longer BAGMAN's primary background-inference control plane** — BAGMAN's own three aliases (`bagman-fast`/`bagman-core`/`bagman-deep`), its alias-only routing boundary, its per-request structured-output schema contract, and its unconditional post-response validation are all unaffected by which real gateway process sits behind them; this is a deployment-level topology change, not an architectural/contract one. Full closure history — the credential-rotation attempt, the appliance provisioning, the `bagman-fast`/`bagman-core` reliability investigation (root-caused to a stale appliance-side `extra_body.format:"json"` override clobbering BAGMAN's correctly-generated schema constraint, fixed by HELM), and the full acceptance evidence — is preserved in `memory/generated/CD5-EVIDENCE-AI-FOUNDATION-CLAUDE-OPERATOR-AND-GUI-INTEGRATION-2026-09-13.md`.

---

# 97. Operator Architecture Correction (Architect ruling, 2026-09-16, same day as §96)

This addendum corrects §13's "second, separate secret governs the Claude operator provider" text and §17-18's implicit assumption of a direct Anthropic Messages API integration. **Preserve history: §13/§17/§18 above are NOT deleted or rewritten — they record what the original CD-5 design assumed. This section records what is actually authoritative now.**

**Original design (§13/§17/§18, as first written):** BAGMAN holds a BAGMAN-scoped Anthropic API key at `/srv/bagman-secrets/anthropic_api_key`, mounted at `/run/secrets/anthropic_api_key`, and a direct Anthropic Messages API adapter (`ai/providers/claude/`) calls Claude directly, with a BAGMAN-owned tool-calling loop (`agent/tools/`, `agent/bagman/orchestrator.py`) exposing 8 governed read-only/analyse-only tools Claude may invoke live, mid-conversation.

**Corrected, authoritative design (2026-09-16):** BAGMAN does **not** use a direct Anthropic API key for its operator intelligence. The proven operator architecture is:

```text
Matt
  ↓
BAGMAN Ask BAGMAN HTML UI
  ↓
bagman-api
  ↓
bounded Claude Code operator runner
  ↓
claude -p
  ↓
governed BAGMAN read/analyse context
  ↓
response
  ↓
Ask BAGMAN UI
```

`agent/claude_code/` (new package) owns this: `runner.py` is the one place in the whole repository that ever execs a `claude` subprocess — fixed executable/argument contract, `--tools ""` + `--restricted` + `--strict-mcp-config` strip the invoked process of every tool/MCP/ambient-settings capability, so its authority is a strict SUBSET of, never inherited from, this host's own normal Claude Code development-agent authority. There is no live tool-calling loop in this design — BAGMAN's own application layer (`context.py`) assembles all governed context (evidence/intake/entity, fetched directly from BAGMAN's canonical services) BEFORE the one bounded, synchronous invocation (`orchestrator.py`), per this same section's own "keep it bounded... simple synchronous request/response... do not build a general autonomous multi-agent platform" instruction.

**Claude Code owns its own authentication/session mechanism entirely** — BAGMAN's own code never reads, writes, transmits, or even knows the shape of any Anthropic credential for this path. `/srv/bagman-secrets/anthropic_api_key` is explicitly **not** a CD-5 blocker and is never provisioned under this corrected architecture — §13's text above describing it is historical, not a live requirement. The operator boundary remains governed exactly as §17 always required: Claude may inspect governed BAGMAN information, explain, summarise, analyse, review AI proposals, identify exceptions, and recommend next actions to Matt — it may not edit BAGMAN source, run shell commands, execute SQL, manipulate Docker, read arbitrary host files, access unrelated secrets, move money, write accounting/tax truth, send email, or alter canonical state; under the corrected design this is enforced structurally (zero tools exist to even attempt any of it), a stronger guarantee than §17's original tool-registry-based enforcement, not a weaker one.

`ai/providers/claude/`, `agent/tools/`, and `agent/bagman/orchestrator.py` are NOT deleted — see the CD-5 evidence file's own classification finding for the architect's ruling on their disposition before any removal.
