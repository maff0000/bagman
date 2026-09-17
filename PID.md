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

`ai/providers/claude/`, `agent/tools/`, and `agent/bagman/orchestrator.py` were, at the point this classification was first written, NOT deleted — see the CD-5 evidence file's own classification finding for the architect's ruling on their disposition before any removal. **This has since changed — see the correction note immediately below.**

**Correction — final removal, 2026-09-16 (CD-5 Gate-1 and Gate-2 both CLOSED GREEN, `AI_FOUNDATION_GREEN` formally issued at head `6f47901c`):** the architect subsequently authorised, as one final bounded hygiene delta before merge, the removal of this same superseded direct-Anthropic operator implementation — `ai/providers/claude/`, `agent/tools/`, and `agent/bagman/orchestrator.py` — as ONE cohesive obsolete unit, plus every test/manifest/import/configuration line that existed solely to support it. This paragraph's own "NOT deleted" statement above is preserved as history, not rewritten: it accurately describes the state at the time it was written (before final closure). The removal itself is recorded as history, not erased: `ai/providers/claude/`, `agent/tools/`, and `agent/bagman/orchestrator.py` were the original CD-5 implementation; the architecture was superseded (by the bounded headless Claude Code operator documented above) before final closure; the classification finding referenced above proved zero live dependents on the superseded code before any file was touched; the removal itself was executed before merge specifically to prevent dual-authority ambiguity (i.e. to guarantee exactly one operator architecture, `agent/claude_code/`, is ever live in this repository — never two supposedly-authoritative implementations at once). See the CD-5 evidence file's own cleanup-delta section for the full removal record, required-checks verification, and the fresh focused Auditor's independent verdict on this delta.

**CD-5 closed. `AI_FOUNDATION_GREEN` formally issued; PR #5 merged to `main` at merge commit `13c2282f052491cf0783597c326de26f25c9b35d` (2026-09-16T19:01:30Z, merged head `f9399acd90abe52ddabeabaa9c9e2df8c23ce36e`). Post-merge `main` CI confirmed green (run `35138032010`).**

---

# 98. CD-6 — GUI Operations Foundation (Architect build authority, 2026-09-16)

CD-1 through CD-5 are CLOSED GREEN.

From this point forward, BAGMAN product delivery becomes **GUI-led vertical slices**.

The governing UX principle is:

> Simple on the surface, extremely capable underneath.

The operator should normally understand what needs attention within seconds of opening BAGMAN.

## 98.1 Runtime requirement

The BAGMAN GUI/application shall run on the dedicated Mac mini:

`192.168.11.4`

and shall be accessible to Matt from:

`192.168.246.0/24`

HELM owns deployment/network/firewall/startup acceptance.

Do not silently move canonical PostgreSQL/MinIO merely to satisfy GUI placement. Application placement and canonical-data placement are separate decisions.

## 98.2 GUI doctrine

Use one coherent premium BAGMAN application shell.

Design requirements:

* clean and spacious;
* highly readable;
* restrained use of colour;
* excellent desktop usability;
* exceptions first;
* drill-down instead of clutter;
* original evidence beside BAGMAN's interpretation;
* persistent Ask BAGMAN;
* universal Needs You queue;
* every automatic action logged;
* no fake buttons;
* no business logic in browser JavaScript.

Overview should resemble:

```text
Good morning Matt

4 things need your attention

2 invoices need a company
1 email needs classification
1 rule proposal needs approval

Everything else is running normally.
```

## 98.3 Global invoice / receipt / photo upload

Add a prominent global:

`+ Add`

with:

* Upload invoice / receipt
* Upload other document
* Add photo

Also add `Upload invoice` within the Invoices tab.

Supported evidence should include:

* PDF
* JPEG/JPG
* PNG
* HEIC if safely practical
* multiple images/pages where practical

All uploads MUST pass through the existing CD-4 governed evidence-intake pipeline.

There is no special image-upload bypass.

For newly uploaded invoices/receipts, BAGMAN should determine what it safely can and ask Matt only for missing information.

The critical operator questions are:

### Company

Select from canonical BAGMAN entities.

Initial expected entities:

* Infosecurs Limited
* NoustAI Limited
* Matthew Scott Personal

These labels must not be hardcoded as business truth in the UI. They map to canonical entity IDs.

### What

Accounting/business meaning.

This should ultimately be coded using the selected company's real Xero Chart of Accounts.

### Why

Short business-purpose explanation.

Example:

```text
Company: NoustAI Limited
What: <Xero account: Computer Equipment>
Why: GPU hardware for local inference R&D testing
```

The `why` field is first-class provenance and may later contribute to R&D/tax evidence.

## 98.4 Xero reference-data doctrine

BAGMAN SHALL NOT maintain a competing generic accounting-category vocabulary where Xero owns the actual accounting coding.

Each BAGMAN company may map to one Xero organisation.

For each connected Xero organisation, synchronise:

`GET /api.xro/2.0/Accounts`

Persist/cache at least:

* Xero tenant / organisation ID
* AccountID
* Code
* Name
* Type
* Class if supplied
* TaxType
* Status
* ShowInExpenseClaims
* ReportingCode
* ReportingCodeName
* UpdatedDateUTC
* BAGMAN last-sync UTC

The invoice/receipt account dropdown must display real Xero accounts for the currently selected company.

Display should normally be:

```text
[400] Advertising
[404] Bank Fees
[420] Cleaning
[429] General Expenses
...
```

or whatever the organisation's actual Xero Chart of Accounts contains.

Do not assume two companies have identical charts.

BAGMAN AI may propose:

> likely account = Software / Subscriptions

but the actual selected value must resolve to the real Xero `AccountID`.

Prefer active purchase/expense-appropriate accounts in the operator UI.

`ShowInExpenseClaims` is useful metadata but is not automatically the only eligibility rule.

When Xero is unavailable/not yet connected:

```text
Xero chart of accounts not connected
```

Do not fabricate account categories to make the screen look finished.

Tax-rate/reference-data synchronisation should follow the same model.

Do not build new functionality on Xero Classic Expense Claims / Receipts APIs (deprecated/decommissioning).

## 98.5 Universal Needs You queue

Create one cross-BAGMAN operator queue.

Example:

```text
NEEDS YOU

Screwfix receipt
Which company is this for?

AWS invoice
What was this spend for?

HMRC email
Is this important?

Adobe rule
Always classify Adobe invoices as Infosecurs software?
```

Each item contains:

* ID
* type
* source module
* priority
* created UTC
* concise question
* evidence/context
* allowed answers/actions
* resolution
* resolved UTC
* operator provenance

Resolving one item should naturally advance to the next.

## 98.6 TAB 1 — Email Inboxes

This is the first operational tab.

### Mailbox list

Show:

* mailbox address
* provider
* status
* enabled/paused
* last successful sweep
* last error
* relevant messages
* needs-review count

Actions:

* Add
* Edit
* Pause / enable
* Disconnect/delete
* Test
* Sweep now

Initial adapters:

1. Microsoft Graph — `matt@infosecurs.com`
2. IMAP/OAuth-capable — `matt@noust.ai`
3. Gmail adapters later

Prove ONE real mailbox fully before broadening.

Mailbox is a source, not a company/entity.

### Email triage

For each message show:

* received UTC
* sender
* subject
* mailbox
* classification proposal
* company proposal
* confidence
* reason
* attachments
* resulting evidence
* operator action

Governed classification vocabulary should cover at least:

* supplier invoice
* receipt
* supplier statement
* HMRC/tax
* customer billing
* subscription/renewal
* payment failure
* supplier correspondence
* customer correspondence
* irrelevant
* unknown

### Learning

Matt's correction may create an explicit rule.

Example:

```text
IF:
sender domain = adobe.com
AND:
invoice-like PDF exists

THEN:
classification = supplier invoice
company = INFOSECURS_LIMITED
suggest Xero account = <AccountID>

reason:
Adobe software subscription
```

Rules must be:

* visible;
* editable;
* disableable;
* versioned;
* auditable.

Never hide important learned behaviour only inside AI/model memory.

### Email Activity

Every sweep creates a durable record:

```text
Mailbox: matt@infosecurs.com
Messages examined: 47
Relevant: 3
Ignored: 42
Needs review: 2
Attachments ingested: 4
Invoice candidates: 2
Duplicates: 1
Errors: 0
```

Drill-down identifies exactly why each message was processed/ignored.

## 98.7 TAB 2 — Invoices

Primary views:

* Needs Review
* Ready
* Processed
* Exceptions
* Activity

Avoid giant spreadsheet UX.

Primary row/card should expose only essential information:

```text
Adobe                 £118.80
Infosecurs            Ready
Software subscription   99%
```

Open an invoice into a review surface showing:

LEFT: original image/PDF

RIGHT:

* supplier
* invoice number
* date
* due date
* net
* VAT
* gross
* currency
* company
* real Xero account
* why
* confidence
* provenance

Actions:

* Approve
* Correct
* Reject/not invoice
* Mark duplicate where applicable
* Create/update supplier rule

No Xero posting in this first slice unless separately authorised.

The boundary should be:

```text
Evidence
  ↓
Invoice recognised
  ↓
Reviewed/coded
  ↓
Xero-ready
```

not:

```text
AI output → Xero
```

## 98.8 Activity / audit UX

Every tab gets an Activity view.

Simple default:

```text
10:42  Adobe invoice processed automatically
10:38  Matt approved Screwfix receipt
10:21  Mailbox sweep: 47 checked, 3 relevant
09:56  Supplier rule created
```

Clicking an event reveals full forensic detail:

* source evidence ID
* audit event
* AIInvocation
* rule/version
* operator
* timestamps
* old/new values
* reason
* confidence

This allows:

> What happened?

and

> Prove exactly what happened.

without cluttering the normal GUI.

## 98.9 Domain/persistence expectations

Introduce proper durable concepts, approximately:

* MailboxSource
* MailSweep
* EmailMessage
* EmailDecision
* ProcessingRule
* InvoiceRecord / InvoiceCandidate
* NeedsYouItem
* ReferenceDataSnapshot
* XeroAccountProjection

Use existing canonical evidence/provenance/audit infrastructure.

Idempotency is mandatory for:

* mailbox message ingestion;
* repeated mailbox sweeps;
* attachment ingestion;
* invoice creation;
* rule execution.

## 98.10 AI boundaries

CD-5 remains binding.

* AI output = proposal.
* No direct canonical writes merely because a model answered.
* Email/document text is untrusted DATA.
* Use typed `bagman-fast/core/deep` contracts.
* Ask BAGMAN remains bounded Claude Code.
* Consequential AI output requires provenance and validation.

## 98.11 Delivery order

Build in this order:

**Slice 1** — GUI visual refinement + universal Needs You + global receipt/photo upload.

**Slice 2** — Canonical company selector + Xero-reference-data contracts/dropdowns.

**Slice 3** — TAB 1 mailbox management.

**Slice 4** — One real mailbox adapter + sweep engine.

**Slice 5** — Email relevance/classification/operator corrections/rules/activity.

**Slice 6** — TAB 2 invoice workflow consuming uploaded/email evidence.

**Slice 7** — Mac deployment at `192.168.11.4` and live browser proof from `192.168.246.0/24`.

Every slice must remain usable and coherent.

Do not build fake integrations for screenshots.

## 98.12 Acceptance

GREEN requires real proof of:

1. polished GUI running from Mac mini;
2. reachable from `192.168.246.0/24`;
3. universal Needs You queue;
4. PDF/image receipt upload through CD-4 intake;
5. original evidence beside BAGMAN's interpretation;
6. company dropdown backed by canonical entities;
7. Xero dropdown backed by real synced Xero Accounts when connected;
8. visible failure if Xero reference data is unavailable;
9. company / what / why review interaction;
10. correction → explicit reusable rule;
11. full activity/audit record;
12. one real mailbox connected and swept;
13. email attachment → evidence → invoice workflow;
14. adversarial email/document prompt-injection proof;
15. no direct AI canonical accounting writes;
16. fresh independent Auditor;
17. exact-head live CI GREEN;
18. no merge without Architect ruling.

Do not reinterpret ambiguity. Escalate it.

## 98.13 Branch record

Branched from `main` at `13c2282f052491cf0783597c326de26f25c9b35d` (CD-5 merge commit) as `cd-6/gui-operations-foundation`, by the PL under this section's own architect authority — the branch/issue creation the architect attempted directly failed with `403 Resource not accessible by integration` on the architect's own GitHub connector (a permission gap on that connector, not a repository restriction); the PL's own git/GitHub access is a separate credential and was unaffected, so the branch is created here rather than by writing directly to `main`.

## 98.14 Slice 1 delivered and independently confirmed

CD-6 Slice 1 (premium GUI shell, universal Needs You queue, global governed upload) landed at commit `28ee010` (branch `cd-6/gui-operations-foundation`, PR #6 draft). PL reconciliation independently re-ran the full test suite fresh (751 passed, 24 skipped), re-ran gitleaks (clean), and caught+fixed one real gap the Engineer's own commit had missed (a stale `architecture-index.md` — regenerated before commit). A fresh, independent Auditor with no inherited conclusions then adversarially re-verified all of it live against an isolated Docker Compose stack — governed-intake bypass resistance, Needs You idempotency under a real replayed request, double-submit and restart-survival of a resolution, entity/Xero honesty, activity log, no regression, architecture-memory freshness — verdict `CD6_SLICE1_GREEN_CONFIRMED` at the application/code level. One real but unrelated issue surfaced: the Claude Code OAuth credential mounted for Ask BAGMAN is revoked (`401 OAuth access token has been revoked`) — pre-existing on the shared credential, not introduced by this slice, flagged for rotation; the GUI surfaced the failure honestly rather than masking it. Also flagged: `agent/claude_code/runner.py`'s `is_available()` is a binary-on-PATH check only, so it cannot detect this class of failure — the AI-health tile is not currently a reliable signal for Ask BAGMAN's real working state.

Slice 1's product acceptance (PID §98.12) was proven at the application level; the GUI's live Mac-mini reachability point (§98.12.15/§98.1) was explicitly deferred pending the appliance-topology ruling below, since it surfaced a genuine architectural fork (see §99) that was not the PL's to resolve unilaterally.

## 99. Appliance Topology Ruling — Full Mac Mini BAGMAN Appliance (Architect ruling, 2026-09-17, supersedes the deployment-topology question raised against §98.1)

**This section supersedes any earlier "expose Trinity's canonical Postgres/MinIO to the Mac's IP" or "private tunnel to live Trinity storage" proposal** (raised by the PL as an open question after discovering Trinity's `bagman-db`/`bagman-objects`/`bagman-scan` had zero network exposure even to localhost beyond the Docker Compose network, and therefore could not be reached by a Mac-hosted `bagman-api` under §98.1's literal "do not move canonical data" reading without a new network decision). The architect's ruling below resolves that fork explicitly: BAGMAN does not remain split across two hosts. The Mac mini becomes the full BAGMAN appliance — application, canonical database, evidence/object storage, scanner, AI gateway, AI database, and native Ollama, all under one root, with Trinity's canonical Postgres/MinIO retired to backup/archive status once migration is proven complete, never a second live writable copy.

**Design goal (verbatim):** "one dedicated Mac mini, one clearly organised BAGMAN root, one coherent Docker/Colima application stack, one trivial backup surface."

### 99.1 Authoritative topology

```text
Matt / 192.168.246.0/24
        |
        v
Mac mini / 192.168.11.4
+-----------------------------------------------+
| BAGMAN APPLIANCE                               |
|                                                 |
| bagman-api / GUI                               |
| bagman-db            PostgreSQL                |
| bagman-objects       MinIO                     |
| bagman-scan          ClamAV                    |
|                                                 |
| bagman-ai-gateway    LiteLLM                   |
| bagman-ai-db         PostgreSQL                |
| native Ollama        Apple Metal                |
+-----------------------------------------------+
                     |
                     | deep inference only
                     v
                  Trinity

Mac BAGMAN canonical state
        |
        v
   encrypted backup
        |
        v
Trinity / governed backup target
```

Trinity is no longer a live dependency for ordinary BAGMAN canonical storage. It remains: deep-inference escalation (`bagman-deep`); backup/recovery target; infrastructure support if needed.

**Reconciliation with the already-live AI appliance:** HELM's Gate-1 build (`/opt/bagman-ai/` — Colima, `bagman-ai-gateway`/`bagman-ai-db`, pf-anchored to Trinity-only access, its own backup already taken 2026-09-16) is the direct precursor of this section's `bagman-ai-gateway`/`bagman-ai-db` boxes — it is folded into the new unified root (§99.2), not rebuilt from scratch, and its own working configuration/backup discipline is the template this section's canonical-side build follows.

### 99.2 Filesystem doctrine — ONE root

`/opt/bagman` is the one top-level root for everything BAGMAN-owned on the Mac:

```text
/opt/bagman/
├── app/            (compose, config, scripts, runbooks)
├── data/           (postgres, minio, clamav, ai-postgres — host-backed, named paths, not opaque anonymous volumes)
├── secrets/        (app, xero, mail, ai, infrastructure — 0700 dirs / 0600 files, never in Git, never in argv, never printed)
├── backups/        (postgres, minio, ai-postgres, manifests, restore)
├── logs/           (app, backup, maintenance)
├── runtime/generated/
└── README.md
```

No scattered state outside this root except native Ollama's own macOS-native model storage, which must be documented (exact path + inclusion in the backup/rebuild manifest) rather than silently left undocumented. `/srv/bagman-secrets/` (Trinity) is superseded for the Mac deployment by `/opt/bagman/secrets/` — no two authoritative secret roots except during a documented, temporary migration window.

### 99.3 Canonical PostgreSQL / AI PostgreSQL / MinIO / ClamAV

`bagman-db` (canonical BAGMAN state — entities, evidence metadata, intake, audit, provenance, invoice state, mailbox state, Needs You, workflow state, Xero reference projections, rule definitions, later domains) persists under `/opt/bagman/data/postgres/`, kept strictly separate from `bagman-ai-db` (LiteLLM/auth/inference state only) under `/opt/bagman/data/ai-postgres/` — never merged merely because both are PostgreSQL. `bagman-objects` (MinIO, the authoritative evidence store — PDFs, photographs, invoices, receipts, later email attachments/tax evidence, all through the existing governed intake path, no bypass) persists under `/opt/bagman/data/minio/`. `bagman-scan` (ClamAV) persists only genuinely-necessary scanner state under `/opt/bagman/data/clamav/` — never treated as canonical data.

### 99.4 Application definitions, secrets, native Ollama exception

All deployment material (compose files, service definitions, non-secret runtime config, health/backup/restore/bootstrap scripts, README, recovery runbooks) lives under `/opt/bagman/app/` — the Git repository remains the development source, but the deployed runtime definition on the Mac is its own clear, reproducible copy under this root. Secrets live under `/opt/bagman/secrets/` (0700/0600, never in Git/argv/logs/Compose YAML, mounted read-only where possible). Native Ollama stays outside Docker (Apple Metal acceleration requires native execution — an accepted, already-proven exception per `/opt/bagman-ai/README.md`'s own documented finding that a true pre-login system daemon cannot access VZ/Metal); its configuration/manifests/model metadata must still be represented under `/opt/bagman/app/` and/or `/opt/bagman/backups/manifests/`, with its physical model-file location explicitly documented and included in the backup/rebuild plan even though the files themselves stay in Ollama's native location.

### 99.5 Migration from Trinity — mandatory, controlled, no split-brain

Trinity's current canonical Postgres/MinIO hold authoritative BAGMAN state from CD-1 through CD-6. Initialising empty replacements on the Mac and calling that "migrated" is explicitly forbidden. Required sequence: (1) quiesce canonical BAGMAN writes; (2) record source DB/object state; (3) take a verified source backup; (4) restore canonical PostgreSQL to Mac `bagman-db`; (5) transfer/restore MinIO evidence objects; (6) verify object counts/checksums where practical; (7) verify database row counts/invariants; (8) start the Mac BAGMAN stack against restored canonical state; (9) run application acceptance; (10) prove old and new state match; (11) designate Mac state authoritative; (12) prevent accidental split-brain writes to Trinity; (13) retain the Trinity copy only as backup/archive per documented policy. There must never be two simultaneously writable canonical BAGMAN databases.

### 99.6 Backup doctrine

The conceptual backup surface is `/opt/bagman/` plus any unavoidable native-Ollama artifacts documented in one manifest — but a blind filesystem copy of a running PostgreSQL data directory is explicitly NOT an acceptable "backup"; proper application-consistent mechanisms are required. PostgreSQL backups (scheduled, logical and/or physical-consistent) go under `/opt/bagman/backups/postgres/`; MinIO backup manifests/checksums/state under `/opt/bagman/backups/minio/` with actual copies replicated to the remote governed target; AI PostgreSQL backed up separately under `/opt/bagman/backups/ai-postgres/`; recovery-relevant manifests (image tags/digests, model identity/revision/quantisation, container names, ports, filesystem paths, config version, schema/migration version, backup timestamps, restore instructions) under `/opt/bagman/backups/manifests/`. Trinity is the preferred remote/off-box backup target — encrypted where appropriate, scheduled, auditable, not dependent on source Git, restorable onto a fresh Mac; replication is explicitly not the same thing as backup, and enough version history must be kept to recover from accidental deletion, corruption, a bad migration, an operator mistake, or a failed upgrade.

### 99.7 Restore acceptance — mandatory real proof

Not "backup created successfully" — a real restore proof: backup -> destroy/disconnect a disposable restored target -> restore PostgreSQL -> restore MinIO -> start BAGMAN -> verify canonical evidence and state. A disposable isolated restore target is acceptable in place of destructive testing against the live Mac. The result must establish that a replacement Mac can be rebuilt without forensic archaeology.

### 99.8 GUI runtime, network/firewall

Only the GUI/API is normally user-facing, reachable from `192.168.246.0/24`, restricted appropriately; PostgreSQL/MinIO/ClamAV/LiteLLM-DB and other internal services must not be exposed to Matt's LAN unless technically required (none are expected to be). Internal containers communicate over Docker/Colima networking. Any administration endpoint that must exist binds locally or is tightly restricted. The final report to the architect must include the Mac IP, protocol, TCP port, exact browser URL (never assumed — e.g. `http://192.168.11.4:<actual-port>` is illustrative only), bind address, service/container name, health endpoint, autostart/reboot status, firewall restriction, and proof a client on `192.168.246.0/24` can reach it.

### 99.9 Boot behaviour, monitoring

After a real Mac reboot (not merely a service restart): native Ollama, Colima, `bagman-db`, `bagman-objects`, `bagman-scan`, `bagman-ai-db`, `bagman-ai-gateway`, `bagman-api` all return, the GUI becomes reachable, canonical DB/object state remains intact, and no manual command is required — using the already-proven autologin/LaunchAgent reality `/opt/bagman-ai/README.md` documents (a true pre-login system-daemon path was tested and proven incompatible with Apple's VZ framework), not a claim of unsupported true-prelogin behaviour. Basic health/operational monitoring is required for PostgreSQL, MinIO, ClamAV, the BAGMAN API, the AI gateway, free disk capacity, latest backup age, and latest backup success/failure — a low-disk condition must become visible before it threatens canonical evidence.

### 99.10 Scope discipline

This topology change does not alter Slice 1's product acceptance (§98.12) — GUI, Needs You, global Add, upload, evidence-beside-interpretation, company/what/why, activity/audit, and unchanged Ask BAGMAN all remain required. Not yet in scope, this delivery or the appliance migration: Xero posting, mailbox automation, banking, tax filing, unrelated financial automation.

### 99.11 Auditor scope

A fresh, independent Auditor (no inherited conclusions) covers both: **Product** (GUI/Needs You/upload, evidence path, provenance, Ask BAGMAN regression) and **Appliance migration** (no split-brain, canonical state preserved, DB/object-store not exposed unnecessarily, secret hygiene, one-root filesystem discipline, backup correctness, restore proof, reboot/autostart proof, browser reachability).

### 99.12 Required final report

Exact CD-6 head SHA; Mac directory tree beneath `/opt/bagman`; container/service list; canonical Postgres location; canonical MinIO location; AI DB location; backup locations; remote backup target; migration evidence; restore proof; reboot proof; Auditor verdict; live CI run ID/result; exact GUI browser URL; health endpoint; network reachability proof; known limitations; PR #6 state. PR #6 stays DRAFT. No merge. No Slice 2 without architect review.

### 99.13 Two-gate execution — resource cleanup, GUI redesign, and a security investigation (2026-09-17)

Architect ruling paused Phase B a second time to require two gates before canonical-data migration: **Gate A** (make the Mac mini a genuinely dedicated BAGMAN appliance — inventory, backup, remove/disable everything not required, real reboot, real sustained-load resource proof) and **Gate B** (a deliberate GUI visual/design reset — the architect rejected the Phase-A GUI as "functionally reachable but visually unacceptable," a real Slice-1 acceptance failure, not cosmetic backlog).

**Gate A**, executed by a dedicated infrastructure pass: inventoried the whole Mac (installed apps, LaunchAgents/Daemons, cron, Docker/Colima artifacts, Ollama models, disk, memory/swap baseline), classified every non-BAGMAN item, and removed/disabled — Claude Desktop (confirmed distinct from and unrelated to the separate, still-being-resolved headless Claude Code operator credential; `~/.claude/` CLI state verified untouched), a crash-looping unrelated OpenClaw node/gateway + its supporting nginx, stale Dolos NFS/SMB mounts (one of which was found with a plaintext password embedded in a LaunchDaemon plist — backed up at `0600`, the live mount and plist removed), an unused `gemma4:12b` Ollama model (confirmed via the AI gateway's own config that only `bagman:gemma` is used), plus assorted stopped containers/dangling images/one stale pre-migration Docker volume (individually content-verified before removal — no indiscriminate `prune` used anywhere). A real `sudo reboot` was performed; the full BAGMAN stack (native Ollama, Colima, all 6 containers) came back automatically within 58 seconds, zero manual intervention. Pre-cleanup baseline was ~4.0GB swap in active use at idle; post-cleanup, post-reboot, under a 5-minute sustained real-workload test (real LiteLLM completions + real API traffic), swap held at exactly 0.00M throughout — the root cause was unrelated background software, not the appliance stack itself. Honestly flagged, not resolved: Screen Sharing/File Sharing open on all interfaces unrestricted by firewall (left as a possible legitimate remote-access path, Matt's call); RustDesk/virtual-display/BetterDisplay left in place (this headless Mac's only display, genuine ambiguity about boot-chain dependency); a persona's (Gunnar's) unrelated files left untouched. PL independently reconfirmed the container/swap/firewall/secrets-permission state directly via SSH rather than trusting the report alone.

**Gate B**, executed by a dedicated frontend/UI-only design specialist with no backend responsibility (no installed frontend-design skill exists in this environment — confirmed via `ToolSearch` before dispatch, per the architect's own explicit instruction to check first): a full design-system pass (restrained, meaning-carrying colour; one type stack + monospace for ids/hashes; an elevation-scaled radius/shadow system) and a structural rebuild (dark header + persistent left sidebar shell; Overview narrowed to "what needs Matt" with health telemetry demoted; the Needs You review drawer rebuilt as a genuine two-pane split surface with a one-question-at-a-time Company/What/Why flow, still submitting the one existing `resolve()` call — no wire-contract change). Found and fixed, as a byproduct: the evidence-preview panel had been rendering EMPTY since Slice 1 landed — `GET /internal/evidence/{id}/content`'s `Content-Disposition: attachment` header makes a browser download rather than render an `<iframe>`/`<img>` pointed at it directly; fixed frontend-only via a `fetch()` + `blob:` object URL, same one governed call. PL independently reviewed the actual before/after screenshots (not just the report), reran the static-UI pytest suite (62 passed, matching exactly) and reran the full GUI-operations browser acceptance script from a fresh build (all 11 steps independently reproduced, including a real container-restart survival proof).

Both gates committed and pushed (`28ee010`/`278135d` PID record → `5ebf8ac` cleanup+redesign), CI green at each head.

**Security investigation, `FALSE_POSITIVE_CREDENTIAL_LEAK_ALERT`**: while resolving Ask BAGMAN on the Mac (provisioning a bootstrap credential — see below), the platform's own automated classifier flagged the dispatching agent's actions for possible "Credential Leakage." The PL treated this as a real, if unconfirmed, finding and investigated directly (not delegated) rather than dismissing or trusting the agent's own self-report either way. Findings: the `/tmp/claude-0/.../tasks/*.output` paths that displayed `lrwxrwxrwx` are **symlinks** — a cosmetic, kernel-ignored display universal to all symlinks, not a real permission; the actual transcript content lives under a root-restricted path (`/root/.claude/projects/.../subagents/*.jsonl`), and all 114 transcript files inspected across the whole session were genuinely `0600` from creation, under a `0700`-restricted `/root`. A safe field-name sweep (the credential JSON's own key names, e.g. `accessToken`/`refreshToken` — never the values) found **zero occurrences in any transcript**, and gitleaks was clean. **This correction is recorded additively — the earlier suspicion is preserved above as history (the PL's own contemporaneous report to the architect), not rewritten, and is not to be treated as a confirmed incident.** No credential rotation was required, or performed, solely on account of this alert. Root cause of the alarm itself was not conclusively identified (the classifier's exact trigger is external to this repository); no permissive file-creation defect was found in this environment.

**Separately (not a security incident, a design decision)**: the credential actually in use for Ask BAGMAN on the Mac is a copy of the PL's own authenticated Claude Code session — an acceptable bootstrap, explicitly not the desired final state. The architect ruled the Mac appliance must have its own independently-authorised OAuth credential, obtained via `claude auth login` (this CLI version's, `2.1.274`, canonical interactive sign-in command — `claude setup-token` and `claude login`/`claude auth login` were confirmed, by direct test, to require real interactive browser/device authorization the agent cannot complete autonomously; per the architect's explicit instruction, no attempt was made to fake, scrape, or automate around that boundary). This step requires Matt's own direct action and is recorded as in-progress, not yet complete, as of this section.

**Update — dedicated Mac credential provisioned and proven (2026-09-17)**: the arm64 Linux `claude` binary staged for containers cannot run in a native macOS shell (a Mach-O-vs-ELF `exec format error`, corrected live); Matt instead installed a native macOS `claude` CLI via the official installer (`curl -fsSL https://claude.ai/install.sh | bash`) directly on the Mac and ran `claude auth login` himself in a real interactive SSH terminal — a genuinely independent authorization (`claude auth status` on the Mac confirms a distinct Claude Max account/organisation, not derived from any PL session). The PL then, file-to-file only, never echoing contents: backed up the retiring bootstrap credential to `/opt/bagman/backups/manifests/` (`0600`); installed the new credential at the same governed path, `/opt/bagman/secrets/app/claude_code_home/.claude/.credentials.json` (`0700`/`0700`/`0600`, `matthewscott:staff`); restarted only `bagman-api`. Proven live: two real, successive Ask BAGMAN turns both `SUCCEEDED` with real responses and full provenance (session id, per-model token/cost accounting); a controlled failure test (`chmod 000` on the credential, live) produced a clean, visible `FAILED`/`CLAUDE_CODE_PROCESS_ERROR` — no silent fallback, no fake success — restoring access (`chmod 600` + restart) immediately recovered a real successful turn. Final sweep: gitleaks clean; zero `accessToken`/`refreshToken` occurrences in Docker logs, this session's own subagent transcripts, or the Mac's shell history; zero leftover credential files anywhere under `/tmp` on the Mac. Trinity's own `bagman-api` compose (`deployment/compose/docker-compose.yml`) references its own separate, unrelated path (`/srv/bagman-secrets/claude_code_home`) — confirmed structurally untouched by any of this and not relying on the Mac's new credential in any way.

**Final fresh Auditor (Gate A + Gate B + security), commit `8350da4`**: found Gate A and the security remediation hold up under adversarial live testing (one minor, non-blocking discrepancy — swap was ~1.5GB in active use rather than a literal 0.00M at the moment of audit, ~2h post-reboot; confirmed flat, not climbing, under real load — normal macOS compressor behaviour on a tight 16GB host, not a recurrence of the original unrelated-software problem). Gate B's visual reset was confirmed genuinely good. But the Auditor found a real, reproducible **P0**: every upload path was completely non-functional on the actual deployed URL (`http://192.168.11.4:8200`) — `crypto.randomUUID()` is undefined outside a browser secure context (HTTPS or `localhost`), which a plain-HTTP LAN address always is, and neither upload call site nor its caller had a `try`/`catch` wrapping that early a step, so the failure was a silent, indefinitely-hung "Uploading…" with no visible error — the opposite of this delivery's own "no fake buttons" doctrine. The Auditor correctly diagnosed that Gate B's own earlier acceptance run must have tested against `localhost`/a tunnel (a secure context), masking the bug.

**Fixed** (commit `69a941b`): a new `shared/uuid.js` (`generateRequestId()`) using `crypto.getRandomValues()` — not restricted to secure contexts — as the primary path, formatted as a real v4 UUID, with `crypto.randomUUID()` preferred only where actually available; both call sites switched; both the `documents.js` handler and `add-menu.js`'s click handler now wrap their full body in `try`/`catch` so ANY unexpected exception (not just a failed HTTP response) produces a visible error and re-enables the submit control. Redeployed to the Mac (rebuilt `bagman-api` from `69a941b`) and re-verified live, against the real URL, reproducing the Auditor's own exact test conditions: `typeof crypto.randomUUID` confirmed `undefined` on `http://192.168.11.4:8200` (the insecure-context premise genuinely holds); a real Playwright upload through the global `+ Add` modal succeeded (`POST /internal/intake/evidence` → `201`, a real evidence id registered); the Documents tab's own separate upload form (the second, independently-fixed call site) also succeeded; Needs You now shows real open items from these uploads — the downstream review flow the first Auditor could not reach at all (zero evidence existed) is now genuinely exercisable. CI green at `69a941b`.

**Final, fresh Auditor re-verification of the P0 fix, commit `658968b`**: a THIRD fresh Auditor (no inherited conclusions), pinned exactly to this head, re-tested everything live against the real URL (never localhost/a tunnel) and independently confirmed: the deployed Mac image is byte-identical to the fix (`shared/uuid.js` md5-matched against the worktree); `typeof crypto.randomUUID`/`window.isSecureContext` reproduce the real insecure-context condition; both upload paths succeed with real `201`s and real evidence ids, zero console/page errors; the full downstream Needs You flow now works end-to-end (a real item created, the evidence preview genuinely renders via the Gate-B `blob:`-URL fix, Company/What/Why answered and independently re-confirmed persisted via a fresh `GET`); no regression (EICAR still genuinely quarantined by real ClamAV, oversized upload still cleanly rejected, both visible, neither hangs); fresh-venv suite 751 passed/24 skipped (exact match); gitleaks clean; architecture-memory zero-diff; Gate A/security spot-check clean (6/6 containers healthy, swap flat and non-alarming per the prior Auditor's own documented normal-behaviour finding, one fresh Ask BAGMAN turn succeeded with full provenance). **Verdict: `CD6_P0_FIX_CONFIRMED_GATES_AB_SECURITY_GREEN`.** No issues found. CI green at `658968b`.

**Both Gate A and Gate B are now CLOSED GREEN**, independently confirmed by three separate fresh Auditors across this sequence (Gate A/B/security → the P0 finding → the fix's own re-verification). The Mac appliance's Ask BAGMAN credential is Matt's own independently-authorised OAuth login, not a shared/bootstrap copy. PR #6 remains DRAFT. Phase B canonical-data migration remains paused pending the architect's own review of this full package.

**Also flagged, live, by Matt's own use**: the persistent "Ask BAGMAN" affordance in the shell header (PID §98.2's own requirement) can be opened with no document/entity in view, in which case `ask-bagman.js`'s own `hasContext()`/`contextLabel()` correctly detect the absence but do not block submission — a generic message ("hi bagman") reaches the backend with `evidence_id`/`intake_id`/`entity_id` all null and is honestly rejected by PID §29/§73's traceability requirement, but the raw technical `VALIDATION_ERROR` is what renders as the "response," not a helpful prompt to pick a document/company first. This is a real, pre-existing (CD-5-era) tension between "traceable AI input" and "persistent, reachable-from-anywhere Ask BAGMAN," not a CD-6 regression — recorded here for the architect's own decision, not silently resolved.

## 100. Phase B — Canonical Data Migration (2026-09-17)

Architect ruling formally closed Gate A, Gate B, and the security remediation GREEN and approved Phase B resumption: migrate BAGMAN canonical PostgreSQL/MinIO from Trinity to the Mac mini appliance, with the Mac becoming the sole writable canonical authority. Executed directly by the PL (not delegated — the highest-stakes step in this delivery, matching this session's own established discipline of handling secret/data-integrity-critical work personally rather than via a subagent).

### 100.1 Pre-migration inventory (2026-09-17T12:11Z)

Trinity source: PostgreSQL 17.11 (x86_64), Alembic version `9c2f4b1e7a05`. Authoritative `COUNT(*)` per table (captured post-quiesce, not the earlier approximate `pg_stat_user_tables.n_live_tup` reading, which was found to be a stale-statistics artifact off by 1 on two tables — investigated and resolved as a measurement artifact, not a data change, before proceeding, per the architect's own "if source inconsistencies are discovered, STOP and report them" instruction): `ai_invocations=260, audit_events=2486, evidence_items=271, external_references=0, governed_entities=3, intake_records=280, needs_you_items=24, provenance=0, sources=1`. MinIO: bucket `bagman-evidence`, 549 objects, 66KiB, 100% content-hash-verified (BAGMAN's own evidence objects are content-addressed — object key = sha256 of content — a strong, free integrity check exploited throughout this migration).

Mac target (pre-restore): PostgreSQL 17.11 (aarch64), same Alembic version `9c2f4b1e7a05` (no schema drift), but NOT empty — held 18 MinIO objects and a handful of Postgres rows accumulated from Gate A/B/security acceptance testing conducted directly against the live appliance. Treated as disposable test residue (not real canonical data) and cleanly replaced, not merged, matching the architect's own "no split-brain / no ambiguous merge" spirit.

### 100.2 Quiesce (2026-09-17T12:11:30Z Trinity; T12:15:49Z Mac)

Trinity: `docker stop bagman-api` (the ONLY container capable of writing to `bagman-db`/`bagman-objects` — both already had zero host-published ports, confirmed via `docker port`). Mac: `docker stop bagman-api` immediately before restore. Verified no writer remained on either side before proceeding.

### 100.3/100.4 Backup (Trinity source)

`pg_dump --format=custom` (`sha256:e1137ddf...`) + a plain-SQL companion, taken post-quiesce. MinIO: `mc mirror --preserve` of the full bucket, verified 549/549 objects with zero content-hash mismatches before transfer. Both transferred to the Mac via `scp`/piped `docker cp`, checksums reconfirmed identical on arrival.

### 100.5/100.6 Restore (Mac target)

Postgres: `DROP DATABASE`/`CREATE DATABASE` (clean replace, not merge) + `pg_restore --no-owner --no-privileges`. Verified: **exact `COUNT(*)` match on every one of the 9 tables** against Trinity's post-quiesce authoritative counts. MinIO: bucket emptied (`mc rm --recursive --force`) then `mc mirror --preserve` of the 549-object source — verified 549 objects/66KiB (exact match), 100% content-hash integrity re-verified on the Mac's own host filesystem (`shasum -a 256`, 549/549, 0 mismatches), and sample `evidence_items.storage_reference` values confirmed to resolve to real objects via `mc stat`.

### 100.7 Application cutover

Mac `bagman-api`'s configuration already had zero Trinity dependency (confirmed via `docker inspect` env dump — built local-only from Phase A). Restarted; all 6 containers healthy; `GET /health`→`alive`, `GET /ready`→all three checks `ok`; real migrated data confirmed visible via `GET /internal/evidence`.

### 100.8 Split-brain prevention

Trinity's `bagman-api` container **removed** (`docker rm`, not merely stopped — its `restart: unless-stopped` policy would not have auto-restarted it after an explicit stop, but removal is unambiguous and durable against any future accidental `docker start`). `bagman-db`/`bagman-objects` on Trinity remain running (retained as controlled fallback, per instruction, not deleted) but are structurally non-writable: no host-published ports, and the one container on their shared Docker network that could reach them is gone.

### 100.9 Canonical-authority marker

Written to `/opt/bagman/README-CANONICAL-AUTHORITY.md` on the Mac (verbatim marker per the architect's own template) and recorded here: **canonical BAGMAN runtime is the Mac mini (192.168.11.4)**; canonical Postgres is `bagman-db` at `/opt/bagman/data/postgres/`; canonical object store is `bagman-objects` at `/opt/bagman/data/minio/`; Trinity's copy is retired/read-only migration archive + backup source only.

### 100.10 Off-box backup

A **fresh** dump/mirror of the Mac's now-canonical state (not the earlier migration-source copy) pulled back to Trinity: `/srv/bagman-backups/phase-b-canonical/20260917T121549Z/{postgres,minio,manifests}/`, each artifact checksummed, with a full recovery manifest (`manifests/manifest.md`) recording image identities, row/object counts, and restore instructions — genuinely off-box (a separate host from the Mac), not merely a second copy on the same machine, and distinct from the retained pre-migration Trinity source (never conflated with it).

### 100.11 Restore proof — real, isolated, disposable

Restored the off-box backup into throwaway `bagman-restore-proof-pg`/`bagman-restore-proof-minio` containers on Trinity (never touching production Mac or the retained Trinity source): Postgres restore matched the canonical Mac state exactly on every table checked (`ai_invocations=260, audit_events=2486, evidence_items=271, governed_entities=3, intake_records=280, needs_you_items=24, sources=1`); MinIO restore matched exactly (549 objects/66KiB), 100% content-hash-verified again (549/549, 0 mismatches), and a sample `evidence_items.storage_reference` confirmed to resolve to a real restored object. Disposable environment fully torn down afterward.

### 100.12 Reboot proof

Real `sudo reboot` triggered at `2026-09-17T12:17:15Z`. SSH back within ~30s; **all 6 containers reported healthy by `2026-09-17T12:18:29Z`** (~74 seconds total), fully automatic, zero manual intervention. `GET /health`/`GET /ready` both healthy afterward; native Ollama confirmed serving `bagman:gemma`; migrated canonical data confirmed intact post-reboot (271 evidence_items, 549/66KiB objects — unchanged).

### 100.13 Resource check, post-migration

`vm.swapusage` held at **0.00M** throughout — immediately post-reboot, after real repeated `/health`/`/ready`/`/internal/evidence` traffic, and after real Ask BAGMAN completions (2 successive real turns, both `SUCCEEDED`). No regression from Gate A's own resolved swap result. DB size 10198 kB; MinIO 66KiB/549 objects; free disk 125GiB. Native Ollama's resident model remains the dominant fixed memory cost, unchanged, per the architect's own standing "that decision is the architect's, not to be silently worked around" instruction.

### 100.14 Real end-to-end proof

A genuinely new synthetic invoice uploaded through the real GUI (`+ Add` → Invoice/Receipt): (1-2) governed intake accepted it, real `evidence_id` returned; (3) metadata confirmed present in canonical Mac PostgreSQL; (4) content confirmed present in canonical Mac MinIO; (5) retrieved via the real API, **byte-identical** to the uploaded file; (6) the resulting Needs You item resolved (Company/What/Why) via the real API; (7) the full causal audit chain confirmed present end-to-end (`INTAKE_RECEIVED → ... → EVIDENCE_REGISTERED → NEEDS_YOU_ITEM_CREATED → NEEDS_YOU_ITEM_RESOLVED`); (8) a real Ask BAGMAN query succeeded; (9) `bagman-api` restarted, and the evidence + its resolution were both confirmed unchanged afterward.

**One real, honestly-flagged finding during this step, not fixed (out of Phase B's explicit scope, unrelated to data migration mechanics)**: an Ask BAGMAN call whose HTTP client disconnected after its own 60s timeout left the corresponding `AIInvocation` row stuck in `RUNNING` well past that window, which then correctly (per PID §73's own concurrency guard) blocked a second call scoped to the same `evidence_id` subject — worked around here by using a different (entity-scoped) subject for the required proof, not by forcing the stuck one through. This suggests the runner's own subprocess-timeout enforcement may not reliably record a terminal status when the HTTP client itself disconnects mid-request, independent of the subprocess's own fate — flagged for a future delivery to investigate, not resolved here.

### 100.15 Verdict

All 14 of the architect's numbered Phase B steps executed and verified as above. Head at completion: see the commit this section is recorded in. Fresh, independent Auditor dispatched next (§100.16, once landed) — no self-issued GREEN.

### 100.16 Fresh Auditor — `CD6_PHASE_B_MIGRATION_GREEN_WITH_FINDINGS`

A fresh, independent Auditor (no inherited conclusions) re-verified the migration live and adversarially, including redoing the restore proof itself into its own, differently-named disposable containers. Confirmed, independently: exact Trinity row counts; Mac row-count deltas consistent with exactly one post-migration test cycle; 12 randomly-sampled objects content-hash-verified with zero mismatches; 3/3 evidence references resolved; split-brain prevention structurally confirmed (no `bagman-api` at all on Trinity, zero Trinity references in the Mac's config); off-box backup checksums verified and independently restored into a fresh disposable target with exact counts; reboot timing corroborated via `kern.boottime`; GUI and Ask BAGMAN both genuinely working; gitleaks clean; `git show --stat 824d5b0` confirmed pure-documentation (no code change); one-root doctrine confirmed (real bind mounts, not anonymous volumes); the §100.14 stuck-`AIInvocation` finding independently re-confirmed still present, live.

**Two precise corrections to this section's own narrative, found by the Auditor — neither is a migration defect, both are documentation errors, corrected here rather than silently rewritten above:**

1. **§100.14's "271 evidence_items, 549/66KiB objects — unchanged" (§100.12) and any "550 objects after +1 evidence item" arithmetic was wrong.** BAGMAN stores each logical evidence item as **two** physical MinIO objects (one under `evidence/`, one under `intake-staging/`) — confirmed on Trinity itself: 271 `evidence/` + 271 `intake-staging/` + 7 `quarantine/` = 549, exactly matching the already-correct migration verification in §100.5/100.6 (which compared real counts, not predicted ones, so the migration itself was never miscounted). The error was only in predicting the count AFTER the one end-to-end test upload: +1 evidence_item correctly produces +2 physical objects, so the Mac's post-test bucket correctly holds 551 objects, not the 550 this section's §100.14 narrative implied. The Auditor confirmed 551 directly and traced the exact accounting. No unexplained or extra objects exist.
2. **An undocumented second `bagman-api` restart.** The Auditor found `bagman-api` restarted a second time at `12:21:48Z`, ~3.5 minutes after the reboot-triggered bring-up completed, which §100.12's reboot-proof text did not mention. Timing corresponds exactly to §99.13's own already-documented Ask-BAGMAN-credential-rotation restart ("installed the new credential... restarted only bagman-api") — that action happened, chronologically, to fall shortly after this same day's reboot rather than before it, and both events are independently, honestly recorded elsewhere in this PID (§99.13 for the restart's own cause, §100.12 for the reboot) — but §100.12 itself did not cross-reference it, which is the gap being corrected here, not a previously-undisclosed action.

**Also noted, not a migration defect**: `docker logs bagman-api` on the Mac was found to return a stale/truncated view (frozen at an earlier point) while the real underlying log file is current — a Colima/Docker CLI quirk on this specific host, not a data-integrity issue (health checks/GUI/API all independently confirmed live and correct throughout) — flagged for whoever builds out §99.9's monitoring doctrine, since `docker logs` cannot currently be trusted alone for `bagman-api` troubleshooting on this appliance.

**Verdict stands: Phase B migration is sound.** The corrections above are to this record's own arithmetic/cross-referencing, not to the underlying migrated state, which the Auditor re-verified independently and found exact throughout.

## 101. Reliability Delta — AIInvocation Terminal-State Correctness + Ask BAGMAN Traceability (2026-09-17)

Architect ruling closed Phase B GREEN and authorised one tightly bounded reliability delta before Slice 2: fix the two defects this delivery's own §100.14 and live testing surfaced — a stuck-`RUNNING` `AIInvocation`, and Ask BAGMAN's rejection of any message with no `evidence_id`/`intake_id`/`entity_id` (a plain "hi bagman").

### 101.1 Root cause — the stuck-`RUNNING` defect

Investigated live, not guessed (commit `7528eb8`). A controlled reproduction (`curl -m 2` disconnect) proved a client disconnect ALONE does not strand a row — the server-side call, being fully synchronous with no thread offload, runs to completion regardless of the client's presence. The real mechanism: `POST /internal/operator/chat` called the synchronous `handle_operator_message` directly from an `async def` handler, blocking the ENTIRE single-worker process for the call's full duration (up to 90s); if that PROCESS died mid-call (restart/crash/OOM/reboot), nothing was left to ever record a terminal state. The original incident's own stuck row was traced to an unrelated, ~96-second-later `bagman-api` restart (the Ask-BAGMAN-credential rotation, §99.13) landing while that exact call was in flight — independently reproduced with a deliberate `docker kill -9` mid-flight. A third, distinct bug found along the way: `subprocess.Popen` raises `ValueError` ("embedded null byte") when binary evidence content, decoded with `errors="replace"`, embeds a real NUL byte into the prompt — `runner.py` only caught `OSError`, so this escaped uncaught past the `RUNNING` transition, same stuck-row symptom via a different trigger.

Fixed: `run_in_threadpool` offload (the real root-cause fix — frees the event loop); two new terminal states, `TIMED_OUT`/`CANCELLED` (`RUNNING -> {SUCCEEDED, FAILED, TIMED_OUT, CANCELLED}`, `REQUESTED -> {RUNNING, FAILED, REJECTED, CANCELLED}`); the runner's own `TIMEOUT` outcome now maps to `TIMED_OUT`, not `FAILED`; `runner.py`'s except clause widened to `(OSError, ValueError)` (zero change to the bounded subprocess argv/`--tools`/`--restricted`/`--strict-mcp-config`/env security contract — confirmed by the PL and, independently, by the fresh Auditor); and a bounded, deterministic stale-`RUNNING` recovery backstop (no background worker — a lazy check at `create_invocation`/`find_active_invocation`, fixed 600s threshold, identical in both `InMemoryAIInvocationRepository` and `PostgresAIInvocationRepository`, the latter under `SELECT ... FOR UPDATE` row-locking) covering every cause of mid-flight process death, not just this one trigger.

### 101.2 Ask BAGMAN traceability doctrine

Per the architect's ruling ("an operator-originated conversational message is itself a valid traceable input"), added `conversation_id` as a new recognised primary-reference key, deliberately LAST in precedence (a document/entity-grounded question stays keyed on that document, never diluted onto the surrounding conversation) — generated by the GUI per open Ask BAGMAN drawer session. A genuinely contextless "hi bagman" is now keyed on the conversation instead of being refused outright; two turns in the same open conversation still correctly conflict via the unchanged concurrency guard; two different conversations never conflict. A `source` field records the originating UI surface.

Migration: `alembic/versions/712c5a2aab92` (drop/recreate `primary_input_reference`'s STORED GENERATED expression + its partial unique index — PostgreSQL has no in-place `ALTER` for a generated column's expression).

### 101.3 PL reconciliation

Independent of the Engineer's own report: read the full diff for the state machine, the Postgres row-locking implementation, the migration, and confirmed the `runner.py` security contract untouched. Reran the full suite fresh (800 passed, 24 skipped); gitleaks clean; architecture-memory regeneration idempotent. Verified live on the Mac: "hi bagman" succeeded with a real response and a properly threaded `conversation_id`; a second successive turn in the same conversation also succeeded. Pushed to `7528eb8`, CI green.

### 101.4 Fresh Auditor — critical defect found (`CD6_RELIABILITY_DELTA_RED_STALE_REQUESTED_RECOVERY_DEFECT`)

A fresh Auditor, pinned to `7528eb8`, ran the full 12-point live acceptance adversarially and found 11 of 12 points held — but a **critical, live-reproduced defect** in point 8 (stale recovery): `is_stale_running()` correctly treats a stale `REQUESTED` row as recoverable, but `recover_stale_invocation()` unconditionally targeted `TIMED_OUT`, and `ALLOWED_TRANSITIONS["REQUESTED"]` deliberately excludes `TIMED_OUT` (this module's own documented design — a request cannot time out before it was ever dispatched). Every shipped stale-recovery test called `transition_status(..., "RUNNING")` before aging the row, so none of them exercised a genuinely-`REQUESTED`-only stale row — the exact real, reachable gap between `create_invocation` returning `REQUESTED` and a caller's own subsequent `transition_status(..., "RUNNING")` a few lines later (exactly `handle_operator_message`'s own call shape).

Reproduced live, directly against the real Postgres-backed repository: created a genuine `REQUESTED` row, aged it past 600s via a targeted `UPDATE`, then proved a real `POST /internal/operator/chat` call to the same subject returned **HTTP 500**/`INVALID_STATE_TRANSITION`, repeatedly, with the row permanently stuck `REQUESTED` — **worse than the original bug** (that failed with a clean conflict; this crashed, forever, for that subject). The Auditor manually repaired the row via a direct SQL `UPDATE` to unblock the subject and confirmed recovery, then correctly declined to issue a GREEN verdict.

### 101.5 Fix (commit `fb36b76`)

`recover_stale_invocation` now branches on the invocation's status at the moment it went stale: `RUNNING` still targets `TIMED_OUT` (unchanged, correct); `REQUESTED` now targets `FAILED` (a new, distinguishing `STALE_RECOVERY_NEVER_DISPATCHED_ERROR_CODE`), matching this module's own pre-existing `REQUESTED -> FAILED` doctrine. Also fixed a second-order instance of the same class of bug: both repositories' stale-recovery audit-event payloads previously hardcoded `STALE_RECOVERY_ERROR_CODE`/an implicit `TIMED_OUT` assumption — now read `error_code`/`status` from the actual recovered invocation, so a `REQUESTED`-row recovery's audit trail is no longer silently wrong even after the state-machine fix. Added the exact missing test coverage (a genuinely-`REQUESTED`, never-`RUNNING` row aged past threshold, `create_invocation` and `find_active_invocation`, both repositories, the Postgres one against a real disposable database) — 4 new tests, all passing; full suite 804 passed/24 skipped (zero regression); pyflakes/gitleaks clean.

Deployed to the Mac via a clean `git fetch`/`reset --hard` this time (addressing the Auditor's own "deployment hygiene" note about the prior working-tree-edit deployment method), rebuilt, redeployed. Live re-verification, reproducing the Auditor's EXACT failing scenario: created a genuine `REQUESTED` row via the real running container, aged it past threshold via a targeted `UPDATE`, then a real `POST /internal/operator/chat` call to the same subject returned **HTTP 200**/`SUCCEEDED` (not 500) — confirmed the stale row was recovered to `FAILED`/`STALE_RECOVERY_NEVER_DISPATCHED` with a correct `AI_INVOCATION_STALE_RECOVERED` audit event (`recovered_status: "FAILED"`, matching the fixed payload). Appliance left healthy, all 6 containers up.

A further fresh Auditor pass is dispatched next (§101.6, once landed) to close the loop on this fix specifically, re-running the full 12-point acceptance, before this delta is considered closed.
