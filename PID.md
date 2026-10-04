# BAGMAN PID v5 — AI Foundation, Claude Operator & GUI Integration

# 0. CURRENT PRODUCTION AUTHORITY — READ BEFORE ANY PRODUCTION ACTION

**Sole writable production runtime: Mac mini appliance, `192.168.11.4`.**
Canonical PostgreSQL (`bagman-db`), canonical object store (`bagman-objects`),
and the application (`bagman-api`) all run there. See §100.9 for the full
cutover record (2026-09-17).

**Trinity is a retired, read-only migration archive and backup source only.**
Its `bagman-db`/`bagman-objects` volumes are intentionally preserved as a
controlled fallback — they must NEVER be given a `bagman-api` writer again,
and must never be treated as a deployment target.

**Every future production work order MUST, before any mutation:**
1. fetch canonical `origin/main` fresh (`git fetch origin main`);
2. re-read this section at the head of `origin/main:PID.md` (not a local or
   cached checkout);
3. identify the actual target host from it;
4. prove — by direct inspection (image tag, Alembic revision, row counts)
   — that the host about to be mutated is the one this section names, before
   taking any action.

This section exists because a 2026-09-26 Trinity `bagman-api` rebuild was
mistaken for production throughout a later delivery, despite the correct
authority already being recorded in §100.9 — the earlier record used a
`##`-level heading inconsistent with every other top-level section in this
document, which caused a heading-anchored search to miss it entirely. This
section is a genuine top-level `# 0.` heading, matching the same
`^# [0-9]*\.` pattern any future heading enumeration would use, so it cannot
be silently skipped the way §99–§102 were. Incident record: PID §110.

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

### 101.6 Final fresh Auditor — `CD6_RELIABILITY_DELTA_GREEN_CONFIRMED`

A third fresh Auditor, pinned to `2efd8d9`, independently re-verified the fix rather than trusting the PL's own report. Confirmed by code review that `recover_stale_invocation`'s branch-on-status logic and both repositories' audit-payload fixes match the claimed shape exactly (`git show fb36b76 --stat`). Confirmed the Mac's deployed code is not merely git-state-consistent but **byte-identical** (`md5sum` inside the running container matched the worktree's committed files exactly) — addressing the earlier "deployment hygiene" note directly.

**Independently re-reproduced the exact original defect scenario**, using a fresh `conversation_id` never touched by any prior test: created a genuine `REQUESTED`-only row directly in the running container, confirmed its status, aged it past 600s via a single targeted `UPDATE` (no other row touched), then a real `POST /internal/operator/chat` call to the same subject returned **HTTP 200/`SUCCEEDED`** — the original row correctly recovered to `FAILED`/`STALE_RECOVERY_NEVER_DISPATCHED` with a correct, honest `AI_INVOCATION_STALE_RECOVERED` audit event; an immediate retry on the same conversation also succeeded cleanly. Separately confirmed the original `RUNNING`-stale case remains correct and unbroken (recovers to `TIMED_OUT`/`STALE_RECOVERY_TIMEOUT`, unchanged).

Abbreviated re-check of the rest of the 12-point acceptance: "hi bagman" succeeds (including immediately after a real `bagman-api` restart, combining points 1/11); an evidence-grounded call still records `referenced_evidence_ids` correctly (point 3 — one unrelated, pre-existing test-fixture content issue noted for visibility only, not a state-machine defect: a 73-byte placeholder PNG fixture cleanly `FAILED`/`CLAUDE_CODE_PROCESS_ERROR`, HTTP 200, not a crash); two successive real Claude turns both succeeded (point 10); a live adversarial prompt attempting to list files/run shell commands returned `tool_calls: []`/`permission_denials: []`, matching `tests/security/test_claude_code_operator_containment.py`'s own structural proofs (point 12, zero regression to bounded Claude Code authority). Full regression suite: 804 passed/24 skipped, exact match; gitleaks clean; architecture-memory zero-diff.

**Verdict: `CD6_RELIABILITY_DELTA_GREEN_CONFIRMED`. This reliability delta is closed.** Exact head: `2efd8d9`. CI green throughout. PR #6 remains DRAFT. No merge performed. No Slice 2/Xero work begun.

## 102. Slice 2 — Canonical Company Selector + Xero Reference Data (2026-09-17)

Architect authorised Slice 2: canonical company selector, Xero OAuth connection, Chart-of-Accounts reference-data sync, GUI account dropdown, AI-suggestion constrained to real synced accounts. Explicitly read/reference only — no Xero posting. Per the architect's own §19 instruction ("verify current supported Xero OAuth/accounting API behaviour... before implementation... do not code from stale assumptions"), the PL researched the live Xero Developer documentation before any implementation:

### 102.1 Xero API contract researched (2026-09-17, via official developer.xero.com sources and the published OpenAPI spec)

- **OAuth2 endpoints**: authorization `https://login.xero.com/identity/connect/authorize`; token `https://identity.xero.com/connect/token`; OpenID discovery `https://identity.xero.com/.well-known/openid-configuration`.
- **Tenant/connection discovery**: `GET https://api.xero.com/connections` (returns `id`/connectionId, `tenantId`, `tenantName`, `tenantType` per authorised organisation) — every subsequent Accounting API call requires an `Xero-Tenant-Id` header populated from this, never a browser-supplied value (PID §17's explicit "protect against tenant substitution from browser input").
- **Scopes**: standard Authorization Code flow with `state` (CSRF protection); `openid profile email` (identity) + `offline_access` (refresh tokens) + `accounting.settings.read` (the Chart-of-Accounts/`GET /Accounts` scope — this slice's only data scope, since Accounts live under Xero's "settings" surface, not "transactions"). **Confirmed current and stable**: Xero's own devblog ("Upcoming changes to Xero Accounting API scopes") documents an ongoing migration from broad to granular scopes for `accounting.transactions`/`accounting.reports.read`/`accounting.journals.read`, but explicitly states "scopes for settings, contacts, attachments, and budgets are not changing" — `accounting.settings.read` is unaffected by this migration, confirmed the correct, non-deprecated choice for a read-only Chart-of-Accounts sync.
- **`GET /Accounts` response fields** (from the published OpenAPI spec): `AccountID`, `Code`, `Name`, `Type`, `Class`, `Status`, `TaxType`, `Description`, `EnablePaymentsToAccount`, `ShowInExpenseClaims`, `ShowInWatchlist`, `ReportingCode`, `ReportingCodeName`, `UpdatedDateUTC`, `BankAccountNumber`, `BankAccountType`, `CurrencyCode`, `SystemAccount`. Per this project's own established "open taxonomy, never a hand-typed closed enum for a provider-supplied vocabulary" doctrine (already applied throughout to `entity_type`/`evidence_type`/`item_type` etc.), `Type`/`Class`/`Status`/`TaxType` are captured and stored verbatim, never validated against a hardcoded closed set BAGMAN would have to redeploy to extend.
- **Real architectural constraint found, requiring a topology decision**: Xero's own documented redirect-URI policy requires HTTPS **except** `http://localhost/` specifically for testing — `http://127.0.0.1` is explicitly NOT permitted, and BAGMAN's appliance is deliberately served over plain HTTP at a LAN IP (`http://192.168.11.4:8200`), never HTTPS (a standing CD-6 decision, §98.1/§99.8). A direct OAuth redirect to the Mac's real LAN address is therefore impossible without adding TLS termination — out of scope for a read-only reference-data slice. **Resolution**: register the Xero app's redirect URI as `http://localhost:8200/internal/xero/oauth/callback`; the human OAuth-consent step is performed via an SSH local port-forward (`ssh -L 8200:localhost:8200 matt@192.168.11.4`) from Matt's own machine, so Xero's redirect to `http://localhost/...` resolves correctly through the tunnel to the real `bagman-api` — no new infrastructure, no TLS work, a standard, well-understood technique for testing non-HTTPS OAuth providers locally.

### 102.2 Correction — the `http://localhost` exception does not apply to BAGMAN's OAuth client type (live-found, 2026-09-17)

§102.1's resolution above was **wrong in a way only Xero's own live app-registration UI revealed**: Matt attempted to register `http://localhost:8200/internal/xero/oauth/callback` and Xero's registration form rejected it outright — "must use https". Re-research established the `http://localhost` testing exception is reserved by Xero specifically for its **"Mobile or desktop app"** client type (PKCE-only, never holds a `client_secret`); BAGMAN's OAuth design is server-side, confidential-client ("Web app" registration, holds a `client_secret`, exchanges the authorization code server-side) — the exception simply does not apply to this client shape, full stop, regardless of hostname.

**Real resolution built** (not a redesign of the OAuth client shape, which architect spec §3 requires to stay server-side/confidential): a narrow, loopback-only TLS terminator, additive to and non-disruptive of BAGMAN's deliberately-plain-HTTP LAN-facing GUI/API (§98.1/§99.8 UNCHANGED):

- New Compose service `bagman-xero-oauth-tls`: `caddy@sha256:5f5c8640aae01df9654968d946d8f1a56c497f1dd5c5cda4cf95ab7c14d58648`, bound to `127.0.0.1:8543` only (never `0.0.0.0`, never the LAN interface), reverse-proxying the decrypted request to `bagman-api:8000` over the existing internal `bagman-net` — `bagman-api` itself is completely unaware this proxy exists.
- An explicit, pre-generated self-signed cert (`openssl req -x509 ... -days 3650`, `/opt/bagman/secrets/xero/tls/` on the Mac, 0600) rather than Caddy's own automatic `tls internal` mechanism — the latter's TLS handshake failed unexplained (`LibreSSL "tlsv1 alert internal error"`) on this host despite the local CA installing without any logged error; sidestepped rather than root-caused further, since a narrow one-purpose operator-only listener doesn't need Caddy's on-demand PKI machinery at all.
- Port **8543**, not 8200: a specific-host-IP bind (`192.168.11.4:8200`) was tried first to free up `127.0.0.1:8200` for the new listener, and failed under Colima's own port-forwarding model ("cannot assign requested address" — the VM's own Docker daemon has no visibility into the Mac's physical LAN interface; only `0.0.0.0`-inside-the-VM binds get relayed to real host interfaces at all). Resolved by using a genuinely separate port instead of fighting Colima's networking model. `bagman-api`'s own existing `8200:8000` mapping is UNCHANGED.
- Xero app redirect URI is therefore `https://localhost:8543/internal/xero/oauth/callback`; the one-time human OAuth-consent step is reached via `ssh -L 8543:localhost:8543 matt@192.168.11.4` (**note the port — 8543, not 8200**; an earlier tunnel command shown during setup used 8200, which only reaches the plain-HTTP GUI, not this TLS-terminated OAuth path). A real browser warning (self-signed cert, unknown CA) is expected and safe to proceed past for this one-time step.

A real, live secret-hygiene finding during setup verification, fixed immediately: alongside the real `client_id`/`client_secret` files Matt placed at `/opt/bagman/secrets/xero/` (0700/0600, confirmed), two insecure `644` editor-backup artifacts (`client_id~`, `client_secret~`) were found and removed (`rm -f`, without reading their contents) — narrow, proactively caught, consistent with this project's zero-tolerance secret-handling discipline.

Xero organisations in scope, per Matt (2026-09-17): **Infosecurs Limited** (actively used, real existing chart of accounts — the natural target for the real end-to-end acceptance proof) and **NoustAI Limited** (new/empty; Matt intends to use BAGMAN itself to help populate it going forward — the natural second, genuinely-distinct real organisation for the required cross-company-isolation proof).

### 102.3 Engineer delivery

Full domain/persistence/HTTP/GUI delta dispatched and delivered: new `services/xero/` package (`connection.py` — `XeroConnection` state machine `PENDING↔{CONNECTED,ERROR,DISCONNECTED,REVOKED}` per spec §2; `account.py` — `XeroAccount`, idempotent `upsert_accounts()` keyed on `(tenant_id, account_id)`, atomic batch; `oauth_state.py` — anti-CSRF/replay `state` token, `STATE_TTL_SECONDS=600`; `eligibility.py` — excludes only `ARCHIVED`/`DELETED` status and `BANK` type by default, "expose more when uncertain" per spec §6; `ai_suggestion.py` — `resolve_ai_suggested_account()`, validates strictly against the supplied candidate set, `UNRESOLVED` sentinel, never invents an `AccountID`; `client.py`/`fake_client.py` — `XeroOAuthClient`/`XeroAccountingClient` + `Fake*` pair, stdlib `urllib` only; `secrets.py` — app credentials + per-connection token files under the governed `/opt/bagman/secrets/xero/` root, missing-file-tolerant; `sync.py` — `XeroSyncRun` orchestration, pre-emptive + reactive token refresh, bounded rate-limit backoff, closed `SyncFailureReason` taxonomy, `is_reference_data_stale()`); `persistence/postgres/xero_models.py`/`xero_repository.py` (4 tables: connections/accounts/sync_runs/oauth_states); migration `5e8c1f42b9a7` chained off `712c5a2aab92`; `app/api/routers/xero.py` (8 endpoints — `connect`, `oauth/callback`, `disconnect`, `sync`, `get`, `accounts`, `syncs`, `resolve-suggestion` — `tenant_id` never a request parameter anywhere, per spec §15); GUI: new "Connections" tab (`app/api/static/features/xero/`), `needs-you.js` step 2 gained a genuinely separate real-`AccountID`-valued "Accounting category" select; a real pre-existing violation found and fixed in the same pass — `documents.js`'s upload panel had all three company names hardcoded as literal `<option>` elements, now populated at runtime from `listEntities()`. New contracts under `contracts/xero/` for `XeroConnection`/`XeroAccount`/`XeroSyncRun` (deliberately NOT for `OAuthState` — ephemeral security plumbing, not a durable API-visible record, a documented judgment call). Engineer-reported: 804→891 tests passing (+87), gitleaks clean.

### 102.4 PL reconciliation — real defects found and fixed, not merely a self-report trusted

Per Forge discipline, the PL never trusts an Engineer's self-report — full independent code review performed. Two genuine, security-relevant defects were found and fixed directly by the PL (the highest-stakes class of fix, handled personally rather than delegated):

1. **TOCTOU race in OAuth `state` anti-replay** (`services/xero/oauth_state.py`) — `consume_state()`'s own replay/expiry checks ran via an UNLOCKED `get_state()` read before a separately-locked `mark_consumed()` write; neither `InMemoryOAuthStateRepository` nor `PostgresOAuthStateRepository`'s `mark_consumed` re-validated `consumed_at`/`expires_at` AFTER acquiring their own lock — meaning two near-simultaneous callback requests presenting the identical `state` could both pass the unlocked pre-check before either reached the lock, and the second could still "successfully" overwrite an already-consumed row, defeating the anti-replay guarantee the code's own docstring claimed. Structurally identical to the stale-`REQUESTED`-recovery race already found and fixed once this session in the AIInvocation reliability delta (§101) — same "re-check under the lock, never trust a pre-lock read alone" discipline applied. **Fixed**: `mark_consumed` is now itself the authoritative, lock-guarded gate in both repositories (Postgres: re-validate inside the existing `SELECT ... FOR UPDATE` transaction; in-memory: a real `threading.Lock` added, since this repository's real caller — an HTTP handler — may run across genuine OS threads per the reliability delta's own `run_in_threadpool` precedent). Proven by two new tests per repository, calling `mark_consumed` directly twice (bypassing `consume_state`'s own pre-check entirely) to prove the repository layer itself is race-safe, not merely protected by caller ordering.
2. **Secret-file create/chmod race** (`services/xero/secrets.py`, found during a dedicated deep-review pass) — `_write_secret_file` originally created the file via `Path.write_text` (default mode, typically `0644` under a common umask) and only narrowed permissions via a SEPARATE `chmod` call afterward — a real window in which a freshly-written access/refresh token sat world/group-readable on disk. The same class of exposure the live `client_id~`/`client_secret~` `644` backup-file finding (§102.2) already demonstrated is a real risk on this host, not theoretical. **Fixed**: the file is now opened via `os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)` directly — `_FILE_MODE` baked into the creation call itself, so the file is never observable at any wider mode at any point in its existence; the original `chmod` retained afterward as belt-and-braces for filesystems that apply umask to `os.open` mode too.

Also confirmed, structurally, not just by prose claim: **no Xero write/posting capability exists anywhere in this slice** (architect spec §18 — the single most spec-critical compliance item). `XeroAccountingClient` (the only class in this codebase that ever calls Xero's Accounting API) exposes exactly one public callable, `list_accounts` — proven by introspection (`dir()` over the class) AND at the transport boundary (a `urllib.request.Request` spy proving the one real call this class makes is always `method="GET"`). Every route in `app/api/routers/xero.py` is enumerated and confirmed to be a BAGMAN-internal endpoint (`connect`/`oauth/callback`/`disconnect`/`sync`/`get`/`accounts`/`syncs`/`resolve-suggestion`) — none calls a Xero write endpoint.

Reviewed and confirmed correct, no defect: `connection.py`'s state machine and its two-layer uniqueness enforcement (entity_id: plain unique constraint + `IntegrityError` recovery mirroring `needs_you_repository`'s own established "constraint is the real proof under a race" doctrine; tenant_id: partial unique index, same recovery pattern) — both races correctly backstopped at the database layer, not merely the unlocked application-level pre-check; `account.py`'s atomic batch upsert (proven against real Postgres: a malformed 3rd item in a 3-account batch writes zero rows, not two); archived/inactive accounts retained not deleted across a resync (real-DB proof); cross-company isolation (real-DB proof, two entities upserting the SAME provider `account_id "SAME"` never collide); `eligibility.py`'s default-eligible-unless-known-excluded policy; `ai_suggestion.py`'s strict-candidate-set validation; `client.py`/`fake_client.py`; the Postgres `XeroAccount`/`XeroSyncRun` repositories; the GUI's real-`AccountID`-as-value / honest-disconnected-state behavior.

**`deployment/compose/docker-compose.yml` mount question resolved**: the Engineer had left this file unedited, flagging the `/opt/bagman/secrets/xero` mount as "out of my dispatch, HELM's deployment lane" — on review this framing was a misjudgment (this is the repo's own in-repo dev/CI compose file, not the Mac's live operational compose). No CI path actually depends on it (every existing test uses `InMemoryTokenStore`/`Fake*Client` or a disposable `BAGMAN_XERO_SECRETS_DIR`, and `read_xero_app_credentials()` is missing-file-tolerant), so this was not a functional gap — but the mount was added anyway for consistency with the existing `claude_code_home` precedent, using this repo file's own `/srv/bagman-secrets/` host convention (distinct from the Mac's `/opt/bagman/secrets/` root). The Mac's own live `/opt/bagman/app/compose/bagman.docker-compose.yml` separately gained the real mount (straight passthrough, since host and container paths are identical on the Mac) — narrowly read-write only on the `tokens/` subpath the container must actually write to at OAuth-connect/refresh time, read-only for `client_id`/`client_secret`.

Full regression re-run fresh (`/tmp/bagman-venv`, all Xero-domain/persistence tests plus the complete existing suite): **897 passed, 24 skipped, 0 failed** (804 baseline + 87 Engineer + 4 PL OAuth-state race-proof tests + 2 PL write-capability-proof tests). `gitleaks detect --source . --no-git -v` — clean, no leaks found.

Still pending before Slice 2 can close: deploy to the Mac (secrets mount now included), the real one-time human Xero OAuth-consent step for Infosecurs then NoustAI (via the corrected `ssh -L 8543:localhost:8543` tunnel), full §25 real-acceptance proof list, live browser acceptance at `http://192.168.11.4:8200`, a fresh independent Auditor (Domain/Security/Persistence/Live), and PL adjudication before any push-to-PR#6/CI observation. PR #6 remains DRAFT; no merge; branch `cd-6/gui-operations-foundation` unchanged.

### 102.5 Real two-company live acceptance (2026-09-17) — four further architect-found defects fixed in sequence, all deployed and re-verified

Four additional real defects were found — by the architect, from direct live testing and source inspection, and by the PL during deploy prep — between §102.4's PL reconciliation and a successful real two-company acceptance run. Each was fixed, tested, committed, pushed, CI-verified at exact head, and redeployed to the Mac before the next was attempted, per standing PL discipline:

1. **Stale `_DEFAULT_REDIRECT_URI`** (commit `7a444bd`) — still held the pre-§102.2-correction `http://localhost:8200/...` value in code even though PID.md's own prose had already been corrected to the real `https://localhost:8543/...` topology. Found during Mac deploy prep, before any live OAuth attempt — would have failed with a redirect_uri mismatch.
2. **PENDING operator dead-end + superseded-callback safety** (commit `75ebc0a`) — Matt's live Infosecurs attempt sat at `PENDING`/`Never synced` with no GUI action; `connections.js` deliberately excluded `PENDING` from its actionable states even though `POST /connect` always supported restarting one. Fixed with state-specific GUI actions ("Restart Xero connection" etc.) and a companion fix: `complete_connect` raising `InvalidStateTransitionError` on a superseded (stale, restarted-then-duplicated) callback was uncaught, risking a raw 500 or — had it been handled naively — wrongly flipping an already-good `CONNECTED` row to `ERROR`.
3. **OAuth callback credential-material-in-URL hardening** (commit `1c133b3`) — a real Xero `invalid_request: Invalid redirect_uri` error on Matt's live attempt triggered verification that BAGMAN's own code was correct (confirmed byte-exact against the live running container — the mismatch was on Xero's own Developer Portal registration, not a BAGMAN defect). While investigating, implemented the architect's separately-requested hardening: `GET /oauth/callback` now always 303-redirects to a clean, code/state-free `GET /oauth/result` rather than rendering HTML directly at the callback URL. A real injection risk was found and closed while implementing this — naively carrying the old free-text message through the redirect query string would have made the now-public `/oauth/result` endpoint an XSS vector; closed with a closed set of reason keys mapped server-side to fixed strings.
4. **Unsafe first-element tenant selection** (commit `5cb79f1`) — the real live defect this session's actual two-company run first surfaced: Infosecurs connected successfully (real OAuth, real `GET /Accounts`, 51 accounts), but NoustAI's OAuth callback failed with a tenant conflict — `connections_result.connections[0]` was taken unconditionally, and since the same Xero login has access to both organisations, Xero's own `GET /connections` could return Infosecurs first, attempting to remap its own already-bound tenant onto NoustAI. The existing tenant-uniqueness constraint correctly REJECTED this (nothing was ever corrupted — see the real audit trail below) but left an opaque conflict instead of a resolution. Fixed with deterministic, governed tenant resolution (`services/xero/tenant_selection.py`, new) — see that module's own docstring and the commit message for the full three-way (exactly-one/zero/multiple eligible candidates) design.
5. **Raw-token retention in `PendingTenantSelectionStore`** (commit `01a7ad2`) — architect source review of `5cb79f1` found the multi-candidate broker's `mark_resolved()` replaced a record with a `resolved_at`-stamped copy that STILL held the raw access/refresh tokens, retained indefinitely (no delete path; expired unresolved selections were never purged either) — real process-lifetime raw-token retention, contradicting the module's own short-lived-secret-bridge rationale. Fixed by replacing `mark_resolved()` with an atomic `consume()` (consume-and-REMOVE, not mark-and-retain), plus opportunistic purge-on-access for abandoned expired selections.

**Real two-company acceptance, achieved at exact head `01a7ad2` — independently re-verified by the PL directly against the live Mac (not merely relayed from Matt's report):**

- **Infosecurs Limited**: entity `01a0abdd-3dd0-773e-9b6e-4c79ec5890af` → real Xero tenant `0a6c746c-8916-43df-994f-7b98cc7b19ee` ("Infosecurs Limited"), `CONNECTED`, 51 accounts, real `GET /Accounts` sync `SUCCEEDED`.
- **NoustAI Limited**: entity `01a0abdd-3ddb-7747-98a2-92a94638cb44` → real Xero tenant `e9b0d389-8f12-4a03-a19a-1e981cd68c1a` ("NoustAI Limited"), `CONNECTED`, 87 accounts, real `GET /Accounts` sync `SUCCEEDED` (sync run `01a0b13c-059e-77c5-bf48-c20c08d7a234`, 87 seen/87 created/0 updated, no error).
- **Cross-company isolation, proven directly in Postgres** (`SELECT entity_id, tenant_id, COUNT(*) FROM xero_accounts GROUP BY entity_id, tenant_id`): exactly two groups exist in the entire table — each BAGMAN entity's account rows belong to that entity's own tenant_id alone, 51 and 87 respectively, zero cross-contamination. Real DB constraints confirmed present: `uq_xero_connections_entity_id` (plain unique), `uq_xero_connections_tenant_id` (partial unique, `WHERE tenant_id IS NOT NULL`), `uq_xero_accounts_tenant_account` (unique on `(tenant_id, account_id)`).
- **The real audit trail independently confirms the fix's before/after behaviour**: two `XERO_CONNECTION_CONNECT_FAILED` events (18:56:20, 19:11:23) show the OLD pre-fix code correctly rejecting NoustAI's attempt to bind Infosecurs's tenant ("already connected to a different BAGMAN entity"); after the `5cb79f1` fix was deployed, a fresh `XERO_CONNECTION_CONNECT_INITIATED` → `XERO_CONNECTION_CONNECTED` (20:33:42/20:33:55) shows NoustAI cleanly binding its OWN tenant, `e9b0d389-...`, on the first attempt. No `XERO_CONNECTION_TENANT_SELECTION_REQUIRED` event ever fired — the real consent grant returned exactly one eligible candidate once Infosecurs was excluded, so the single-candidate auto-path handled it; the multi-candidate picker exists and is tested but was not exercised by this real run.
- **Infosecurs non-regression**: byte-identical `xero_connection_id`/`tenant_id`/`tenant_name`/`status`/timestamps/51-account-count checked directly against Postgres before and after every subsequent redeploy in this sequence (four redeploys total, §102.5 items 1-5).
- **Matthew Scott Personal**: `connected: false`, `connection: null`, `account_count: 0` (never `undefined` — the GUI/backend fix from §102.4 holds live).
- **OAuth tunnel independence, proven both behaviourally and architecturally**: Matt shut down his workstation SSH tunnel (`ssh -L 8543:...`), then clicked NoustAI's ordinary "Sync now" — it succeeded (`XERO_SYNC_SUCCEEDED`, 87 accounts, 21:18:16). Confirmed by source inspection too: `redirect_uri` is a parameter ONLY to `build_authorize_url`/`exchange_code` (`services/xero/client.py`) — the connect-initiation and callback-completion steps — never to `.refresh()` (the `refresh_token` grant) or anywhere in `run_sync`'s own orchestration. The port-8543 tunnel is therefore required ONLY for a fresh OAuth connect/reconnect's inbound callback; ordinary token refresh and Xero API sync calls always execute Mac → Xero directly outbound, with no dependency on any inbound tunnel. (PID §102.2/102.4's own existing wording already scoped the tunnel narrowly to "the one-time human OAuth-consent step" — reviewed and confirmed it never claimed permanence; no correction needed, this section only adds the now-real, now-proven evidence.)
- **Secret/credential hygiene**: token files present under the governed root for both entities (`/opt/bagman/secrets/xero/tokens/<entity_id>/{access_token,refresh_token,expires_at}`, 0700 dirs / 0600 files, confirmed via `ls -la`, contents never read); zero token/client_secret-shaped matches across 921 live container log lines; every Xero-prefixed audit payload inspected directly in Postgres contains only entity/tenant ids, names, counts, and hand-written reason strings — never a raw credential.
- **Pending-selection broker lifecycle**: confirmed live, directly against the running container, that `PendingTenantSelectionStore.consume()` exists and `mark_resolved()` does not (`hasattr` check via `docker exec`); confirmed via the audit trail (zero `TENANT_SELECTION_REQUIRED` events ever recorded) that the broker was never populated during this real run at all, so there is no abandoned raw-token record to purge — the strongest possible form of "nothing resident," since nothing was ever created.
- Full regression fresh at exact head `01a7ad2`: **925 passed, 24 skipped, 0 failed**; `gitleaks detect --source . --no-git -v` clean. Git working tree clean, local HEAD/Mac-deployed HEAD/origin HEAD all identical at `01a7ad2f3043d5362e12dec73f27ad920d6a8a89`. PR #6 confirmed OPEN/DRAFT/unmerged throughout.

A fresh, independent Auditor (no inherited Engineer/PL reasoning) is being dispatched next, scoped per architect instruction to: canonical company selector, Xero OAuth lifecycle, deterministic tenant resolution, cross-company tenant isolation, token handling, account projection, sync failure/last-known-good semantics, GUI disconnected/stale/error states, the real two-company live evidence above, no-Xero-write-capability, and exact-head CI. PR #6 remains DRAFT; no merge; Slice 3 not started.

### 102.6 Documentation backfill — bounded Xero-assisted supplier-domain correlation (governance gap found and closed, 2026-09-26)

The independent Slice 2 Auditor (§102.7 below) found that `services/xero/supplier_correlation.py` (805 lines) and `client.py`'s corresponding read-only expansion (`list_contacts`/`list_purchase_invoices`/`list_bank_transactions`, three new read-only OAuth scopes) are real code already in this PR/branch, self-described in their own docstrings and in `services/xero/component.yaml`'s manifest as a "CD-6 architect-authorized addendum" — but this addendum was never given its own narrative entry here in `PID.md`. This is recorded now, as a documentation backfill, not as new work:

**What it is:** a bounded, on-demand, non-AI correlation aid. Phase A historical mailbox discovery produced 90 OPEN `MAILBOX_DOMAIN_REVIEW` Needs You items with no extra context beyond "this domain sent N candidate messages." This module reads Xero's own Contacts/Invoices/BankTransactions transiently (never persisted — no new canonical Xero table), correlates them against each still-OPEN item's own sender domain via `ContactID` only (never fuzzy name matching), and attaches the result to that item's own `metadata` as a review aid — never an auto-decision, never auto-approving anything. Lives in `services/xero/` (not `services/mailbox/`/`services/needs_you/`, both of which explicitly prohibit `direct_xero_access` in their own manifests) for exactly that reason — a real, manifest-documented placement decision, not an arbitrary one.

**Still strictly read-only**: confirmed independently by the Slice 2 Auditor (§102.7) via direct source inspection — this module makes zero direct HTTP calls of its own; it only consumes the same read-only `XeroAccountingClient`, which the Auditor confirmed has exactly one `POST` call site in the entire module (the OAuth token exchange itself, not the Accounting API). `xero_write_endpoints` remains listed under `services/xero/component.yaml`'s own `prohibited:` section, unchanged.

No further action required — this section exists solely so a future reader of this PID does not have to reconstruct an already-real, already-authorized, already-correctly-bounded piece of code from the diff alone.

### 102.7 Slice 2 independent Auditor verdict (2026-09-26) — `SLICE2_GREEN_CONFIRMED`

A fresh, independent Auditor (no inherited Engineer/PL reasoning, own disposable worktree, own fresh test venv) adversarially re-verified Slice 2 end to end, treating every prior claim in §102.1-102.5 as a hypothesis to re-derive, not a fact to repeat:

- **No write/posting capability** — confirmed by direct source inspection: exactly one `POST` call site in `services/xero/client.py` (the OAuth token exchange), every Accounting-API call is `GET`.
- **OAuth state anti-replay + secret-file permissions** — re-confirmed genuinely race-safe by reading the actual locking code (real `threading.Lock`/`SELECT...FOR UPDATE`, re-validated under the lock, not merely asserted), and secret files are `0600` from the `os.open()` creation syscall itself, live-verified on the Mac.
- **Deterministic tenant resolution + cross-company isolation** — all three claimed unique constraints (`uq_xero_connections_entity_id`, `uq_xero_connections_tenant_id` partial-unique, `uq_xero_accounts_tenant_account`) independently confirmed present in the migration AND live against the real production database; the Auditor ran its own `GROUP BY entity_id, tenant_id` query directly (not trusting §102.5's own reported numbers) and reproduced exactly 2 groups, 51/87 rows, zero cross-contamination.
- **Pending-tenant-selection broker** — `mark_resolved()` confirmed absent repo-wide (zero occurrences); `consume()` confirmed to be a genuine atomic removal, not a mark-and-retain.
- **Full test suite, independently run from a clean venv**: **2389 passed, 24 skipped, 0 failed.**
- **`gitleaks detect`**: clean.
- **Live Mac re-verification (read-only only — no write/restart/delete of any kind)**: both real Xero connections confirmed CONNECTED with distinct tenant_ids; token file permissions re-confirmed (0700 dirs/0600 files); CI confirmed green at exact head `91a4e2a2241ccb3b235e4fdc7fe28fe962803a8f`.
- Two non-blocking gaps flagged by the Auditor, both closed by the PL before adjudication: (1) §102.6 above backfills the `supplier_correlation.py` documentation gap; (2) the Auditor could not itself grep live container logs for credential-shaped strings under its own permissions — the PL did so directly afterward (2,089 real, recent `bagman-api` log lines, zero `access_token`/`refresh_token`/`client_secret`/bearer-token/JWT-shaped matches), closing the gap the Auditor's own static-review-only finding had left open.

**Verdict accepted by the PL: `SLICE2_GREEN_CONFIRMED`. No material, unresolved defect found. PR #6's Slice 1 + Slice 2 + CD-6 Slice 5 (evidence classification, WI-1 through WI-6) + the CD-6 §103 inference-architecture ruling documentation are ready for merge to `main`. Slice 3 (TAB 1 mailbox management) was never started and is explicitly deferred to a future delivery — not part of this merge.**

---

# 103. Inference Architecture Ruling — Model Invariance, `bagman-deep` Retirement, Trinity Backlog-Only (Architect ruling, 2026-09-26)

This section records a GREEN-LIT Architect ruling on BAGMAN's LLM backend architecture, reached after four iterative rounds during CD-6 (WI-6-remediation's own live capacity investigation — §102's Xero slice is unrelated and unaffected). **The canonical, durable doctrine itself now lives in `ARCHITECTURE.md`'s new "Inference / LLM backend architecture" section — read that first.** This section records the supersession of prior PID text, the required `bagman-deep` reference inventory, the documentation/config/code delta list, and the bounded CD-5 implementation work order the ruling authorises for a future round. Per the Architect's own explicit instruction, this round is documentation, inspection, and planning ONLY — no application code, `config.yaml`, or Mac-appliance infrastructure was touched.

## 103.1 What is superseded, and what is not

Per this project's standing amendment convention (preserve history, mark superseded, never delete or rewrite):

- **§2C** ("Trinity compute — escalation/deep tier... BAGMAN may escalate difficult cases to Trinity through governed routing only") — **SUPERSEDED.** Trinity is no longer a routine escalation tier reached per-task. It is backlog/overflow-only, reached only via a durable job queue's own operator/maintenance-mode decision, never a live per-request routing choice. See `ARCHITECTURE.md` points 5-6.
- **§4's architecture diagram** (`existing TRINITY LITELLM (sole inference gateway)` fronting both `bagman-fast/core` and `bagman-deep`) — **SUPERSEDED**, on top of §96's own already-recorded topology correction (Mac-local gateway replacing Trinity's as the day-to-day path). §96 remains correct and is RATIFIED, not contradicted, by this ruling — see 103.1a below. What §4 additionally assumed — that `bagman-deep` denotes a genuinely different, heavier model reached through that same gateway as a normal routing outcome — is now superseded by `ARCHITECTURE.md` point 3.
- **§8** ("ONE INFERENCE CONTROL PLANE — the existing Trinity LiteLLM installation... `bagman-deep → Trinity heavier model`") — **SUPERSEDED** on the `bagman-deep` row specifically, and on the premise that Trinity LiteLLM is BAGMAN's inference control plane at all (already corrected by §96; this ruling additionally retires the escalation semantics §8 assumed `bagman-deep` would use). §8's "DO NOT create a second LiteLLM architecture for BAGMAN" instruction is **also superseded** — §96 already recorded that a second, BAGMAN-exclusive Mac-local LiteLLM was in fact built and deployed, and this ruling explicitly RATIFIES retaining it (`ARCHITECTURE.md` point 4). §8's remaining content (semantic alias-only routing, no raw model selection) is unaffected and remains current doctrine.
- **§9** ("`bagman-deep` → Trinity stronger/heavier model") — **SUPERSEDED.** `bagman-deep` no longer denotes a different model at all; see `ARCHITECTURE.md` points 2-3.
- **§11** ("Routing Policy": `COMPLEX / ESCALATED → bagman-deep → Trinity`, and its own "If Trinity is unavailable: the deep/escalated task fails visibly" sub-bullet) — **SUPERSEDED.** There is no live per-task escalation tier to fail visibly; overflow is a queue-level operator decision, not a per-request routing branch. §11's Mac-mini-unavailable / Claude-unavailable sub-bullets are unaffected.
- **§12** ("Existing LiteLLM Remains Authority... BAGMAN must not duplicate these functions") — **SUPERSEDED** as it pertains to Trinity's LiteLLM being the authority BAGMAN must not duplicate; already corrected in substance by §96, and now explicitly ratified in the other direction by `ARCHITECTURE.md` point 4 (the Mac-local duplicate gateway is not merely tolerated, it is affirmatively required to stay).
- **§2A/§2B, §3, §5-§7, §10, §13 (BAGMAN credential structure)** — **NOT superseded.** Claude-as-operator, the Mac mini as primary background inference, the one-BAGMAN-AI-gateway component doctrine, physical-model abstraction, and the credential-scoping doctrine all stand unchanged.

### 103.1a §96/§97 are ratified, not contradicted

§96 (Topology Finalization Addendum, 2026-09-16) already records, as history, that HELM stood up a dedicated Mac-local LiteLLM+Postgres gateway to replace Trinity's shared installation as BAGMAN's day-to-day control plane. This was **not** undiscovered architectural drift needing correction — it was a deliberate, already-executed, already-documented decision. This ruling's own review process initially (incorrectly) treated retiring/simplifying that Mac-local gateway as an open question; the Architect's final GREEN LIGHT explicitly reversed that and ratified §96's real-world outcome as correct standing architecture (`ARCHITECTURE.md` point 4). §96/§97 require no further edits or superseded-markers of their own — they already correctly describe today's reality, which this ruling keeps.

## 103.2 `bagman-deep` reference inventory (required action item 3)

A full repository grep for `bagman-deep`/`bagman_deep`/`BAGMAN_DEEP` was performed. Findings, organised by disposition:

**Confirmed: no live task contract uses it.** `ai/tasks.py`'s `TASK_REGISTRY` has no entry with `preferred_capability="bagman-deep"` — `DOCUMENT_SUMMARY`/`DOCUMENT_TYPE_PROPOSAL` prefer `bagman-fast`, `ENTITY_PROPOSAL` prefers `bagman-core`. This is independently confirmed by `memory/generated/CD5-EVIDENCE-AI-FOUNDATION-CLAUDE-OPERATOR-AND-GUI-INTEGRATION-2026-09-13.md` (lines 157/469, written at CD-5 Gate-1 closure) and by `tests/acceptance/trinity_escalation_live_proof.py`'s own docstring. **This means `bagman-deep`'s retirement requires zero task-contract migration** — it is a config/alias/test/doc-level cleanup only, with no business-logic change anywhere in `services/`.

**Live, deployed, tested infrastructure that currently exists purely as unused capacity:** the Mac's own `bagman-ai-gateway` `config.yaml` (deployment-level, not in this repo) currently maps `bagman-deep` → `openai/bagman-deep`@Trinity's own LiteLLM, and this path was proven live end-to-end at CD-5 closure (`tests/acceptance/trinity_escalation_live_proof.py` — a dedicated live-proof script exercising exactly this path, real HTTP 200, real completion). It is fully working, GREEN infrastructure with zero current production callers.

**Closed alias-governance surface (would need to change if `bagman-deep` is removed rather than repurposed):**
- `ai/invocation.py:291` — `BACKGROUND_CAPABILITY_ALIASES: frozenset[str] = frozenset({"bagman-fast", "bagman-core", "bagman-deep"})`, BAGMAN's own closed alias-governance set.
- `contracts/ai/bagman.ai_invocation.v1.schema.json` (lines 34/37/188) — the same three-value enum constraint at the schema/contract level.
- `tests/security/test_ai_litellm_alias_lockdown.py:110` — asserts this exact closed frozenset.

**Test coverage that currently asserts `bagman-deep`→different-Trinity-model behaviour as correct/expected, and would need updating or retiring once the alias's semantics change:**
- `tests/acceptance/trinity_escalation_live_proof.py` (entire dedicated script — the live escalation proof).
- `tests/acceptance/mac_mini_background_tier_live_proof.py:206`.
- `tests/acceptance/structured_output_20x_proof.py` (lines 27/30/166/175/185/187/193).
- `tests/integration/test_litellm_client.py:279/281`.
- `tests/integration/test_architecture_boundaries.py:634`.
- `tests/contract/test_ai_invocation_contract.py:81`.
- `tests/app_api/test_ai_endpoints.py:233/243`.

**Documentation/comments referencing `bagman-deep` (historical narrative — not code, no functional risk, but reader-facing and would read as stale/misleading once the alias is retired):**
- `PID.md` itself (dozens of references, §2/§8/§9/§11 already addressed above; remaining references at §96/§97/§700ish/§943 etc. are historical narrative describing what was true at the time and do not need correction — they are accurate records of past state).
- `CHANGELOG.md` (lines 130/195/262/285/293) — historical changelog entries, left as-is per this project's own "changelogs are not rewritten" convention.
- `memory/generated/CD5-EVIDENCE-...md` — a frozen historical evidence record; explicitly NOT to be edited (it correctly describes what was true when CD-5 closed).
- `ai/providers/litellm/client.py` (module docstring, lines 12/25/523), `ai/providers/__init__.py` (module docstring), `ai/tasks.py` (module docstring, lines 47/112), `deployment/compose/docker-compose.yml:366` (a comment), `app/api/static/features/ai/ai-api.js:111` (a GUI-facing comment) — all narrative/comment-only, no behavioural effect; would need a wording pass as part of the eventual implementation delta so they stop describing retired architecture as current.

**GUI/status-display consideration (not yet resolved, flagged for the implementation WO, not decided here):** `PID.md` §700/§706-area doctrine and `ai-api.js` currently treat `bagman-deep` as a distinctly-displayed, independently-health-checked tier in the AI status surface. Whether that display collapses to two tiers (Mac profiles + Trinity overflow) or is reworked some other way is an implementation-round design decision, not resolved by this documentation round.

## 103.3 Documentation/config/code delta list (required action item 4)

**Changed THIS round (documentation and planning only):**
- `ARCHITECTURE.md` — new "Inference / LLM backend architecture" section, the durable canonical doctrine.
- `PID.md` — this §103 (supersession markers, inspection report, delta list, implementation work order).

**Deferred to the bounded CD-5 implementation work order (§103.4) — NOT built this round:**
- Deployment-level: the Mac's own `bagman-ai-gateway` `config.yaml` `bagman-deep` entry (repurpose or remove; decide as part of implementation, informed by the GUI-display question above).
- `ai/invocation.py`'s `BACKGROUND_CAPABILITY_ALIASES` frozenset and the matching `contracts/ai/bagman.ai_invocation.v1.schema.json` enum — whether `bagman-deep` is removed outright or repurposed as a Mac-resident profile name (versus introducing a wholly separate `trinity-core` alias reached only through the new backend-selection mechanism, never through `BACKGROUND_CAPABILITY_ALIASES` at all, since that frozenset governs Mac-gateway-routed aliases specifically) is an implementation-round design decision.
- The test files enumerated in §103.2 asserting `bagman-deep`'s current Trinity-escalation semantics — updated, retired, or repurposed to assert the NEW semantics (or deleted if genuinely superseded, per this project's own "delete only what is proven dead" discipline).
- New: a durable Postgres-backed background-inference job table/repository (§103.4).
- New: an explicit `MAC_LOCAL` / `TRINITY_CORE_OVERFLOW` backend-selection function, owned by BAGMAN's own gateway code.
- New/changed: `AIInvocation` provenance fields for `inference_backend` and related metadata (contract + migration + repository).
- New: the one-time `trinity-core` compatibility validation procedure and its evidence artifact.
- Comment/docstring wording passes in the files listed in §103.2's "documentation/comments" bucket, once the real alias decision is made (wording changes should follow the code, not precede it, to avoid describing a decision before it is actually implemented).

## 103.4 Bounded CD-5 implementation work order (specification only — not built this round)

This is a planning artifact. It authorises no code changes by itself; a future round executes it under the normal Forge discipline (dispatch or direct implementation → PL review → tests → commit → push → CI → deploy → live verification → checkpoint).

**Scope:**

1. **Durable background-inference job table.** New Postgres table (reusing `bagman-db`, no new broker/queue technology), minimally: `id`, `task_id`, `task_version`, `generation_profile` (`fast`/`core`, replacing the current alias-as-profile conflation), `input_reference`, `status` (closed enum: `PENDING → CLAIMED → IN_PROGRESS → SUCCEEDED | FAILED_RETRYABLE | FAILED_TERMINAL`), `claimed_by`/`claimed_at` (for atomic claiming, `SELECT ... FOR UPDATE SKIP LOCKED` or equivalent), `attempt_count`, `max_attempts`, `last_error`, `inference_backend`, `created_at`/`updated_at`. Idempotency via a caller-supplied dedupe key (mirroring the existing `AIInvocation` fingerprint-reuse pattern). Restart recovery: a claimed-but-stale row (claimed past a bounded staleness window with no terminal status) is reclaimable, mirroring the existing stale-`REQUESTED`-recovery pattern already built in the AIInvocation reliability delta (§101) — same "re-check under a lock, never trust a pre-lock read alone" discipline.
2. **Backend-selection function.** A single, explicit, BAGMAN-owned function (e.g. `services/ai/backend_selection.py::select_inference_backend(...) -> InferenceBackend`) returning `MAC_LOCAL` or `TRINITY_CORE_OVERFLOW`. For CD-5: default always `MAC_LOCAL`; `TRINITY_CORE_OVERFLOW` selectable only via an explicit, bounded operator/maintenance-mode control (e.g. a config flag or a small operator action), never an automatic threshold-triggered heuristic. No auto-escalation logic is authorised in this round.
3. **Provenance schema addition.** Add `inference_backend` (closed enum, `MAC_LOCAL`/`TRINITY_CORE_OVERFLOW`) to the `AIInvocation` contract/model, migration chained off the current head, backward-compatible (nullable or defaulted for historical rows). Populate alongside the existing model/provider/latency/timestamp/validation/retry fields already recorded. Audit-only — never read by any business-logic branch.
4. **`bagman-deep` retirement delta.** Remove or repurpose `bagman-deep` from `BACKGROUND_CAPABILITY_ALIASES` and the matching JSON-schema enum (final choice made at implementation time per §103.3); update or retire the test files enumerated in §103.2; update the AI-status GUI surface's tier display; a documentation wording pass over the comment/docstring locations in §103.2, once the code decision is made.
5. **`trinity-core` compatibility validation procedure.** Before `trinity-core` may be used for any real overflow traffic: run representative BAGMAN tasks (at minimum `DOCUMENT_TYPE_PROPOSAL`, `DOCUMENT_SUMMARY`, `ENTITY_PROPOSAL`) against `trinity-core` using the exact same task contracts/schemas/validation BAGMAN already uses for the Mac model, and compare output quality/schema-validity against the already-accepted Mac-model baseline. Record the result (pass/fail per task, not just in aggregate) as a durable evidence artifact before authorizing any real traffic. `trinity-core` is the only Trinity alias ever authorised for this — no `trinity-fast`/`trinity-deep`/other Trinity alias may be substituted.
6. **Initial operating procedure.** Document the operator/maintenance-mode procedure for invoking backlog overflow (who decides, what triggers a look, what the rollback/return-to-`MAC_LOCAL` path is) — a short runbook-style addition, not a new automated system.

**Explicitly out of scope for this WO (do not build):** automatic/threshold-based escalation, any change to the accepted Mac model identity, any new distributed queue/broker technology, any GUI redesign beyond the tier-display update in item 4, any Trinity alias other than `trinity-core`.

## 103.5 Branch record

Documentation-only delta, branch `cd-6/gui-operations-foundation`, PR #6 (remains DRAFT/OPEN, unmerged). No application code, `config.yaml`, or Mac-appliance infrastructure changed. Commit reference recorded once pushed (see checkpoint report).

**Update (2026-09-26):** PR #6 (`cd-6/gui-operations-foundation`, containing Slice 1, Slice 2, CD-6 Slice 5 evidence classification, and this §103 architecture-ruling documentation) was subsequently completed through a fresh, independent Slice 2 audit (`SLICE2_GREEN_CONFIRMED`, §102.7) and merged to `main` at merge commit `cae3434bc2ec171ed53f941062eeb8d36acf69aa`. The separately-scoped implementation PR #7 (§103's own bounded CD-5 work order) merged afterward at `114e1140cd48a8199600f2b2194a755fc849b506`, which is canonical production `main` as of that date. See §104 below for the immediately following Slice 3/4/5 governance reconciliation, performed against that exact canonical commit.

---

# 104. Slice 3/4/5 Governance Reconciliation (2026-09-26)

## 104.1 Why this section exists

The Architect authorised "Slice 3" as the next BAGMAN work item, per §98.11's delivery order ("Slice 3 — TAB 1 mailbox management"). Before any implementation began, the PL's own required pre-work (identify the canonical scope, verify the baseline) surfaced a load-bearing fact: **Slice 3 — and substantially Slice 4 ("one real mailbox adapter + sweep engine") and pieces of Slice 5 (email relevance/classification/rules) — were already implemented and merged to `main` inside PR #6**, roughly 15,000 lines across `services/mailbox/`, `app/api/routers/mailboxes_*.py`, and `app/api/static/features/mailbox/`. `services/mailbox/component.yaml`'s own header explicitly self-identified as covering "CD-6 Slice 3: Mailbox Management; CD-6 Slice 4: first real Microsoft Graph mailbox adapter + sweep engine; CD-6 architect amendment: two-stage discovery/domain-gate + governed historical-ingestion boundary." A real production mailbox (`matt@infosecurs.com`) had already been connected and genuinely swept, with 90 real (87 still OPEN, 3 since RESOLVED) `MAILBOX_DOMAIN_REVIEW` Needs You items from a completed historical discovery sweep — plus three further live-connected mailboxes (`matt@noust.ai` via IMAP, two Gmail accounts).

Unlike Slice 1 (§98.14) and Slice 2 (§102, closed with its own dedicated independent Auditor and PID reconciliation), this entire subsystem had **zero dedicated PID.md narrative section and no independent audit of its own** — it rode into PR #6's merge undocumented, the same class of gap the Slice 2 Auditor separately found (at a much smaller scale) for `services/xero/supplier_correlation.py` (§102.6).

Per the Architect's explicit ruling: this section performs a **bounded governance reconciliation** of the existing mailbox subsystem — establish what already exists and whether it is trustworthy, fix only genuine defects required for GREEN, and record the true history — explicitly **not** a redesign, not a rewrite for taste, and not yet the remaining GUI/feature gaps (deferred to a future, separately-authorised work order, §104.6).

**Canonical commit reconciled:** `main` @ `114e1140cd48a8199600f2b2194a755fc849b506` (unchanged from §103.5's own record — this reconciliation is a delta on top of it, not a new base).

## 104.2 Independent audits

Two fresh, independent Auditors (no inherited Engineer/PL reasoning) were dispatched in parallel, each confirmed onto the exact canonical commit above in their own disposable worktree, each running their own fresh test venv:

**Domain/correctness Auditor** — mailbox models/contracts, persistence/migration coherence, the Microsoft Graph/Gmail/IMAP adapters, the sweep engine, historical discovery/ingestion governance, the domain-gate and domain-rule policy engine, the five PID §98.9 idempotency areas, and retries/stale-claim recovery. Verdict: **`MAILBOX_DOMAIN_HOLD`** — one primary blocking defect (§104.3 item 1), four secondary findings, otherwise solid. Own fresh test run: 867/867 mailbox-scoped tests passed (five independent reproductions across four parallel research sub-passes plus its own run, byte-identical every time); `gitleaks` clean.

**Security/isolation/surface/production-safety Auditor** — credential handling across all three adapters, mailbox/company isolation, Needs You integration idempotency, the classification/intake governed boundary, audit-event coverage, CD-5's "AI output is a proposal, never a direct write" invariant, adversarial prompt-injection resistance (§98.12 item 14), the full GUI/API surface, and production safety (no write/mutate capability against any source mailbox). Verdict: **`MAILBOX_SECURITY_HOLD`** — not for a severe vulnerability (credential handling, production read-only safety, and audit-trail engineering were all found genuinely solid), but for the three findings in §104.3 items 2-3 and the false PID.md narrative this section corrects. This Auditor additionally performed real, READ-ONLY live verification directly against the Mac appliance (never a write/restart/mutate of any kind): confirmed all 4 real mailbox connections, the exact 87 OPEN/3 RESOLVED historical Needs You item split, secret-file permissions (0700 dirs/0600 files) matching code discipline exactly, zero cross-mailbox data contamination via a live `GROUP BY` query, and zero credential-shaped strings across 72 hours of real `bagman-api` logs.

Both Auditors independently and unanimously confirmed: no fake/stubbed integration is reachable from production composition for any of the three adapters (Microsoft Graph, Gmail, IMAP); every adapter's real HTTP calls are provably read-only (Microsoft `Mail.Read`-only delegated scope, Gmail `gmail.readonly`-only scope, IMAP protocol-level `EXAMINE`/`BODY.PEEK[...]` with zero mutating IMAP verb anywhere in the codebase); the CD-5 "AI output is a proposal" invariant holds — trivially so, since **no AI/LLM code path exists anywhere in `services/mailbox/` at this commit** (classification/AI is explicitly Slice 5's own still-unbuilt scope, per `component.yaml`'s own text); and the domain-rule/domain-gate policy engine is fully deterministic and auditable, with zero AI involvement in any routing decision.

## 104.3 Genuine defects found and corrected

Per the Architect's explicit "correct only genuine defects required for GREEN — not permission to redesign or rewrite working code merely because an Auditor prefers another shape" instruction, the following were fixed; the PL independently re-derived each one directly from the code before touching anything (never acting on an Auditor's characterization alone — see item 4 below, where doing exactly this caught the Auditor's own claim was imprecise):

1. **`MailboxSweepRun` had no stale-`RUNNING` recovery** (the domain Auditor's primary blocker, unanimous across all four of its own internal research passes). Unlike this exact codebase's own established pattern for an abandoned in-flight record (`ai.invocation.is_stale_running`/`recover_stale_invocation`, `ai.jobs.is_stale_claim`/`recover_stale_claim`), a `MailboxSweepRun` that reached `RUNNING` had no path back to a terminal state if the process running `run_sweep` were killed before calling `complete_run` — `services.mailbox.lock.MailboxSweepLock`'s own 15-minute lease already self-heals so a NEW sweep can proceed, but the OLD run row stayed `RUNNING` forever, permanently corrupting the durable "prove exactly what happened" ledger §98.8 requires, even though sweeping itself was never actually blocked. **Fixed**: `services/mailbox/sweep_run.py` gains `is_stale_running`/`recover_stale_run` (shared predicate/transition, mirroring the existing patterns exactly) and a new `STALE_RUNNING_THRESHOLD_SECONDS` (30 minutes — twice the lock's own lease, so a run is only ever reconciled once its own lease has definitely already expired), plus `MailboxSweepRunRepository.recover_stale_runs(...)` on both concrete repositories (the Postgres one under a real `SELECT ... FOR UPDATE`, re-checking staleness under the lock — mirrors `PostgresBackgroundJobRepository.recover_stale_claims`'s own discipline exactly). Invoked lazily by `run_sweep`, right before it creates a new run for a mailbox — never a scheduled/background poll. A recovered run always targets `FAILED` (never `SUCCEEDED`/`PARTIAL`) with a new `SweepFailureReason.STALE_RECOVERY_TIMEOUT` code. Deliberately does **not** add a new audit-event emission for the recovery itself — the recovered row's own `error_code`/`error_detail` already make the ledger honest again (the actual defect), and a new audit mechanism would require new `audit_repository` composition wiring this bounded delta does not otherwise need.
2. **`reprocess_all_historical_candidates_for_domain` took no `sweep_lock` at all** (security Auditor finding) — nothing prevented it running concurrently with a live `run_sweep` (or another concurrent approval) for the SAME mailbox, which could duplicate `MAILBOX_DOMAIN_REVIEW` Needs You items via `_find_open_domain_review_item`'s own app-level-only dedup scan (no DB constraint exists for that specific item type). **Fixed**: the function now takes and acquires the SAME per-mailbox `MailboxSweepLock` `run_sweep` already uses, wrapping its whole body; all three real call sites (`services/mailbox/review_resolution.py::resolve_domain_review`, called from the Microsoft/IMAP/Gmail routers, both single-item and batch) now thread `composition.mailbox_sweep_lock` through.
3. **Two repositories translated a genuine concurrent-insert race into a raw `PersistenceError` instead of a real, nameable `ConflictError`** (domain Auditor finding, low real-world exposure — the per-mailbox sweep lock forecloses concurrent writers in normal operation, but a genuine defect in the error TYPE under a true race). `persistence/postgres/mailbox_message_repository.py::record_observation` and `persistence/postgres/mailbox_domain_rule_repository.py::upsert_rule` now translate their respective unique-constraint `IntegrityError`s to `ConflictError`, mirroring `persistence.postgres.mailbox_repository.PostgresMailboxSourceRepository.create_mailbox`'s own already-correct pattern exactly.
4. **`services/mailbox/microsoft/graph_client.py::_is_resync_required` read a 410 response body via a bare, unbounded `exc.read()`** before `json.loads()`-parsing it for structured `ResyncRequired` classification — a real memory-exhaustion risk against a hostile/malformed body. **Note on process**: the domain Auditor's own report characterized this as "lacks the Gmail 403-body-classification hardening" — the PL independently re-read the actual code before applying any fix and found this characterization imprecise: Microsoft's 403 handling is unconditional (never re-parses the body for classification at all), so Gmail's SPECIFIC bug (a diagnostic-truncation bound silently doubling as a parsing bound, corrupting JSON before parse) cannot manifest for 403 here. The REAL analogous gap was instead at `_is_resync_required`'s own 410-handling — genuinely unbounded, unlike Gmail's already-hardened `_read_body_bounded`. **Fixed** with the identical proven technique (bounded read via `exc.read(N+1)`, oversized-body detection, never parse a truncated/oversized body) — a new `_MAX_RESYNC_BODY_BYTES` (64 KiB, mirroring Gmail's own identical constant).

**Test suite**: 2474 passed, 24 skipped, 0 failed (was 2456 at the §103 closure checkpoint; +18 new tests covering all four fixes above — stale-recovery domain/Postgres tests, a real lock-contention test, a real 8-thread Postgres race test, and 6 bounded-read regression tests for the resync fix). `gitleaks detect` clean.

## 104.4 Documentation backfill

`services/mailbox/component.yaml` bumped `version: 4` → `version: 5` and updated to describe all three provider adapters (it previously mentioned only Microsoft Graph, despite the IMAP and Gmail adapters having shipped afterward) — `memory/generated/architecture-index.md` regenerated to match, mirroring the exact "regenerate before commit" precedent §98.14 already established for Slice 1.

## 104.5 Reconciliation matrix

| Requirement (PID §98.6/§98.9) | Slice | Implementation | Status |
|---|---|---|---|
| Mailbox models/contracts (MailboxSource, MailSweep→MailboxSweepRun, EmailMessage→MailboxMessage, ProcessingRule→MailboxDomainRule) | 3/4 | `services/mailbox/{mailbox,message,sweep_run,domain_rule}.py` | IMPLEMENTED (substance matches under different names); `EmailDecision` PARTIAL (no standalone typed object); `InvoiceRecord`/`InvoiceCandidate`/`ReferenceDataSnapshot`/`XeroAccountProjection` correctly absent (Slice 6, unbuilt, self-disclaimed) |
| DB-level invariants | 3/4 | `persistence/postgres/mailbox_*_models.py` — real unique/partial-unique constraints, 9-migration linear chain | IMPLEMENTED; the two error-type gaps in §104.3 item 3 fixed |
| Microsoft Graph adapter | 4 | `services/mailbox/microsoft/` — real OAuth/Graph HTTP, 429/pagination/delta-token handling | IMPLEMENTED; the unbounded-read gap in §104.3 item 4 fixed; real-HTTP-path test coverage remains a genuine, disclosed gap (§104.6) |
| Gmail adapter incl. 403-quota defect | 4/5 | `services/mailbox/gmail/` | FIXED-AND-TESTED already (a real prior production incident — a 222-candidate historical backfill broken at candidate 78 — independently re-confirmed genuinely fixed with a real regression test) |
| IMAP adapter | 3/5 | `services/mailbox/imap/` — real `imaplib.IMAP4_SSL`, full cert verification, read-only `EXAMINE`/`BODY.PEEK` | IMPLEMENTED; a real prior defect (non-UTC `Date:` header crash) already fixed with a regression test |
| Sweep engine idempotency + honest provenance | 4 | `services/mailbox/sweep.py::run_sweep` | IMPLEMENTED; stale-run recovery gap fixed (§104.3 item 1) |
| Historical discovery/ingestion governance boundary | Architect amendment | `services/mailbox/bootstrap_policy.py`, `sweep.py` | PARTIAL — the date-floor and missing-config refusal are real and correctly enforced; the bootstrap-vs-incremental distinction is governed only by cursor-row presence, not a structurally separate path (this codebase's own sweep.py docstring already documents one real prior incident of an unintended full historical re-walk, caught only by downstream idempotency) — genuine, disclosed, not fixed in this bounded delta |
| Domain-gate logic | Architect amendment | `services/mailbox/sweep.py`/`domain_rule.py::find_for_sender` | IMPLEMENTED — deterministic, auditable, zero AI involvement, unanimous across both audits |
| Domain-rule policy engine — versioned/auditable/deterministic | Architect amendment | `services/mailbox/domain_rule.py` | Deterministic + auditable satisfied; genuinely NOT "versioned" per §98.6's literal text (no version column/history table, only an audit-event log) — a real, disclosed spec gap, not fixed here (feature work, not defect correction) |
| Domain-rule worked-example fidelity (classification + company + Xero-account suggestion) | Architect amendment vs. Slice 5 | — | Domain-match and company-routing representable and DB-enforced; attachment/content conditions, a classification field, and a Xero-account-suggestion field do not exist — correctly disclaimed as Slice 5's own still-unbuilt scope by `component.yaml` itself, not a defect |
| Idempotency (message ingestion / repeated sweeps / attachment ingestion / invoice creation / rule execution) | 3/4 | various | 1/2/5 solid + tested; attachment ingestion satisfied-by-design (whole raw MIME ingested as one evidence item, keyed by the same message-row uniqueness — no separate mechanism needed); invoice creation correctly N/A (Slice 6 unbuilt) |
| Retries/stale-claim recovery | 4 | `sweep.py` rate-limit backoff; `services/mailbox/lock.py` lease self-heal; `sweep_run.py` (new) | IMPLEMENTED after §104.3 item 1 |
| Credential/secrets handling (3 adapters) | 3/4 | `services/mailbox/{microsoft,gmail,imap}/secrets.py` | CONFIRMED-SAFE — the same TOCTOU-race and create/chmod-race classes the Xero Slice 2 audit found and fixed were independently checked and found ALREADY closed here (atomic `os.open(..., O_CREAT, 0o600)`, row-locked OAuth-state consumption) |
| Mailbox isolation / company boundaries | 3/4 | `mailbox_sources.default_entity_id`/`mailbox_domain_rules.destination_entity_id` | GAP (defense-in-depth only, not exploitable today — no FK constraint exists, mitigated at the application layer by an explicit `entity_repository.get_entity(...)` existence check before acceptance; live query confirmed zero cross-mailbox contamination) — recorded, not fixed in this bounded delta (a real migration touching production FK constraints is a larger, separately-authorised change) |
| Needs You integration | 3/4/5 | `services/mailbox/sweep.py::_find_open_domain_review_item`; `needs_you_repository` | The DB-backed dedup types are race-safe; the app-level-only `MAILBOX_DOMAIN_REVIEW` dedup's own race is closed by §104.3 item 2 |
| Classification/intake governed boundary | 3/4 | `services/mailbox/microsoft/evidence_ingest.py::ingest_email_evidence` | CONFIRMED-SAFE — reuses the exact same `EvidenceSafetyScanner`/`EvidenceRepository.register_evidence()` primitives CD-4's manual-upload path uses, no mailbox-specific bypass |
| Provenance/audit events | 3/4/5 | ~40 spot-checked `record_audit_event` call sites | CONFIRMED-SAFE — no parallel/invented audit mechanism |
| AI boundary compliance (CD-5 invariant) | 5 | — | CONFIRMED-SAFE, vacuously — no AI/LLM code path exists in `services/mailbox/` yet |
| Adversarial prompt-injection proof (§98.12 item 14) | 5 | — | GAP — genuinely unproven, but correctly N/A pending Slice 5's still-unbuilt AI classification path (there is no attack surface to test yet); not treated as silently passed |
| GUI/API surface | 3/4/5 | `app/api/routers/mailboxes_*.py`, `app/api/static/features/mailbox/*.js` | CONFIRMED-SAFE, 1:1 endpoint↔GUI mirroring; a "Test" mailbox-connection action (§98.6's own required mailbox-list action) is confirmed genuinely absent — a real, disclosed GUI gap, not fixed in this bounded delta |
| Production safety (no write/mutate to any source mailbox) | 3/4/5 | all three adapters | CONFIRMED-SAFE, protocol-enforced (read-only OAuth scopes; IMAP `EXAMINE`+`BODY.PEEK`) — the strongest of the findings in this reconciliation |
| PID.md narrative accuracy | — | this section | The prior recorded verdict (§102.7: "Slice 3 ... was never started and is explicitly deferred ... not part of this merge") is **factually superseded** by this section — preserved above as history, not deleted, per this project's own amendment convention; §104 is now the current, accurate record |

## 104.6 Genuine remaining gaps (confirmed, NOT built in this delta)

Recorded for a future, separately-authorised bounded work order — per the Architect's explicit "do not build them in this work order" instruction, none of the following were implemented here:

- **GUI**: an "Email Activity" drill-down view (the backend data model, `MailboxSweepRun`, is essentially complete — zero GUI consumes it); mailbox-list last-error surfacing, relevant-message/needs-review-count columns; a "Test" connection action; a genuine per-message email-triage view (today's `domain-review.js` is domain-level, not message-level).
- **Domain-rule engine**: real versioning (a version column or history table — today only an audit-event log); the fuller worked-example condition shape (attachment/content conditions, an explicit classification field, a Xero-account-suggestion field) — explicitly Slice 5's own scope.
- **Hardening**: real FK constraints on `mailbox_sources.default_entity_id`/`mailbox_domain_rules.destination_entity_id` (defense-in-depth; not exploitable today); test coverage for the Microsoft Graph adapter's real (non-fake) HTTP-handling path (429/pagination/delta-token/token-refresh), which currently has none; a structurally distinct (not merely cursor-presence-inferred) bootstrap-vs-incremental sweep boundary, given this codebase's own history already shows one real unintended full historical re-walk incident.
- **Slice 5 proper**: email classification/AI, operator-correction learned rules beyond the existing deterministic domain-gate, and the adversarial prompt-injection proof that becomes meaningful only once that AI path exists.

## 104.7 Verdict

**`MAILBOX_SLICE_3_4_5_RECONCILIATION_GREEN`.** The existing mailbox subsystem — Slices 3 and 4 in full, and the domain-gate/domain-rule portion of Slice 5 — is confirmed genuinely real (no fake integration reachable from production), fundamentally sound (credential handling, production read-only safety, audit-trail engineering, and the CD-5 AI-boundary invariant all independently confirmed), and now free of the specific defects both Auditors found. Both Auditor HOLDs are resolved by the corrections in §104.3. The remaining gaps in §104.6 are genuine, disclosed, and deliberately deferred — not silently treated as done. No merge without Architect ruling, per §98.12 item 18; see the checkpoint report for the branch/PR/CI record.

## 104.8 Closure (2026-09-27)

Architect ruling accepted this reconciliation GREEN and authorised merge and canonical production deployment. **PR #8 (`mailbox/slice3-4-5-governance-reconciliation`) merged to `main` at merge commit `14de5d1e4f424c0d13629bd77b344d01647bf88f`** (two real parents: prior `main` tip `114e1140cd48a8199600f2b2194a755fc849b506` + PR #8's own head `11d16e7afd825e7de6f8ffa07f8a2a52232a24d9`); post-merge CI green. Canonical production image `bagman-api:main-14de5d1` built from this exact commit and deployed — migration head unchanged (`c7a3f9e1b542`, this delta added no new migration); minimal non-destructive read-only verification performed (container/service health, mailbox API surface, `/internal/ai/health` unchanged, zero `RUNNING` `MailboxSweepRun` rows, zero cross-mailbox message/domain-rule contamination by direct query, zero credential-shaped strings across fresh container logs); full production reconciliation across `MailboxSource`/`MailboxMessage`/`MailboxSweepRun`/`MailboxDomainRule`/`NeedsYouItem`/`EvidenceItem`/`EvidenceClassification`/`AIInvocation`/`BackgroundJob`/`XeroConnection`/`XeroAccount` showed zero unexplained drift against the immediately-prior checkpoint. No historical mailbox sweep was re-run, no domain rule was changed, no credential was rotated, and no new Needs You item was created by this deployment — per the Architect's own explicit "smallest non-destructive verification necessary" instruction, the existing independent test/audit evidence (§104.2/§104.3) stood as sufficient proof of the four corrected defect paths.

**CLOSED GREEN.** Both independent Auditor verdicts (`MAILBOX_DOMAIN_HOLD`→resolved, `MAILBOX_SECURITY_HOLD`→resolved) and this section's own reconciliation matrix (§104.5) stand as the durable record. The §104.6 remaining gaps (Email Activity GUI, mailbox-list columns, a "Test" action, per-message triage, domain-rule versioning, FK hardening, Microsoft adapter test coverage, Slice 5 proper) are NOT started and require separate, future Architect authorisation — this closure does not imply any of them were delivered, nor that these slices were built in their originally-planned sequence (§98.11's own ordering).

---

# 105. Mailbox Operations GUI Completion (2026-09-27)

## 105.1 Scope

Architect-authorised follow-on to §104.6: closes 6 of the "genuine remaining gaps" that section recorded — mailbox-list extension (last-error visibility, `relevant_message_count`, `needs_review_count`, `last_sweep_status`), a governed non-destructive "Test" connection action per provider, an "Email Activity" drill-down view, and a per-message triage view integrated with the existing Needs You model. The richer rule-engine condition expansion (classification + company + Xero-account-suggestion) was explicitly excluded, reserved for a separate future work order.

Doctrine honoured throughout: the mailbox backend was already GREEN and was not redesigned; every feature above was delivered as a thin API/read-model delta over already-existing capability wherever possible. In practice, 3 of 4 major features (Email Activity, per-message triage, and most of the mailbox-list extension) required **zero backend changes** — a precise gap-mapping pass (dispatched before any implementation) confirmed the data already existed across already-shipped endpoints; only the Test action (3 new endpoints) and two small count/aggregation additions were genuinely new backend surface.

## 105.2 Delivery record

- **Backend/API deltas**: `MailboxMessageRepository.count_discovery_candidates()` (a genuine `SELECT COUNT(*)`, never a full-row fetch — structurally proven by a monkeypatch test); `services/mailbox/review_resolution.py::list_mailbox_needs_you_items`/`count_needs_review_items_for_mailbox` (a new shared helper that also replaced 6 previously-duplicated inline Needs-You scans across the three providers' domain-review/security-review endpoints); 3 new `POST /{mailbox_id}/{microsoft,gmail,imap}/test` endpoints (each a pure, read-only identity check — deliberately bypasses every token-refresh/state-mutation path, never sweeps/ingests/creates a Needs You item/touches a domain rule, never returns the raw credential); `GET /internal/mailboxes` extended with the three new list fields.
- **GUI delivered**: mailbox-card extensions (error chip, counts, last-sweep chip, Test button); new `email-activity.js` (zero backend delta, pure consumer of the pre-existing `GET .../sweeps`); new `message-triage.js` (zero backend delta for the data — joins three pre-existing endpoints client-side; every mutating action reuses the pre-existing governed `resolve_domain_review`/`resolve_security_review` endpoints — Microsoft reuses the existing `DomainReview` decision UI wholesale, IMAP/Gmail get a small proportionate inline surface calling the identical governed endpoints — never a new decision mechanism); IMAP/Gmail resolve-wrapper JS parity fixes (the backend endpoints already existed for both providers; only the GUI-side wrappers were missing).
- **PL review**: found and fixed one minor defect during independent review (a dead, unused CSS rule).
- **Independent Auditor** (fresh worktree, no inherited reasoning): traced all three Test-action handlers line by line confirming no mutation/token-refresh/raw-token-leak path exists; confirmed the count method is a genuine COUNT query against real Postgres; confirmed the needs-review-count aggregation can never drift from the per-provider endpoints; confirmed per-message triage's every mutating control resolves to the pre-existing governed endpoints with no parallel path; confirmed Email Activity is fully read-only; confirmed no fabricated/dishonest GUI state (an honest `None`/absent state when a mailbox has genuinely never swept, never a fabricated zero); confirmed zero scope leakage into `ai/`/`services/xero/`/`services/evidence/` and no new IMAP "Domain Review" page built. Verdict: **`MAILBOX_GUI_COMPLETION_GREEN`**.
- **Tests/CI**: 2504 passed, 24 skipped, 0 failed (was 2474 before this delta); `gitleaks` clean. PR #10 (`mailbox/operations-gui-completion`) merged to `main` at merge commit `103088596ac53192d65523146748c693f5df7dd5` (parents: prior tip `299e3b581a50811ec2b18b05bc68efdf720c9861` + PR #10's own head `0fda8cd1b69cc5b6755f67dd2cfedccda29b2c97`).

## 105.3 A real, pre-existing production defect found during live smoke verification (not introduced by this delivery)

Confirming the mailbox-list acceptance items required live verification of the new Email Activity view against real production data. Doing so against the real Microsoft/Infosecurs mailbox's own sweep-run history raised a real `500 INTERNAL_ERROR`: `MailboxSweepRun.to_dict()` crashed with `ValueError: dictionary update sequence element #0 has length 1; 2 is required` when serializing two specific historical sweep-run rows.

**Root cause, independently traced to actual stored data**: these two rows were created before the CD-6 architect amendment that introduced "recursive folder discovery" (§104's own history); `folders_attempted` for them is stored as a bare list of folder-name STRINGS (`["INBOX", "JUNK"]`), the OLD shape, rather than today's `{"folder_id": ..., "display_name": ...}` mapping shape. `to_dict()`'s own `dict(f)` call had always been unable to handle the old shape — this was a genuine, pre-existing latent defect in already-shipped code, not something either PR #8 or PR #10 introduced (neither touched `to_dict()` or the sweep-run write path). It was simply never reachable by any live code path before, since nothing had ever called `.to_dict()` on either of these two specific rows in production until this delivery's own new Email Activity view exercised the pre-existing `GET .../sweeps` endpoint against them for the first time.

**Fixed transparently, in-band, per this WO's own "correct genuine defects found during review" delivery-process instruction**: a new `_folder_entry_to_dict()` helper (`services/mailbox/sweep_run.py`) accepts both shapes — a genuine mapping is copied through unchanged (identical to the original, still-correct behaviour for every row created after the amendment); a bare string is normalised into today's shape using the same value for both `folder_id`/`display_name`, an honest, lossless representation of what the old data actually recorded, never a fabricated value. Read-only serialization fix — no historical row was ever rewritten in the database. 4 new regression tests added (domain-level legacy-shape/mixed-shape/modern-shape-regression-guard tests, plus a real-Postgres round-trip proof). Full suite re-run: 2508 passed, 24 skipped, 0 failed; `gitleaks` clean. PR #11 (`mailbox/sweep-run-folders-attempted-legacy-shape-fix`) merged to `main` at merge commit `882f523c3864d85d8efc342a397f2307efc8126b` (parents: `103088596ac53192d65523146748c693f5df7dd5` + PR #11's own head `47072b897eb344135225a5c7a61f674313cb1443`).

## 105.4 Canonical production deployment and verification

Canonical production image `bagman-api:main-882f523` built from merge commit `882f523c3864d85d8efc342a397f2307efc8126b` and deployed — migration head unchanged (`c7a3f9e1b542`, this whole delivery added no new migration).

Minimal, non-destructive smoke verification performed live, all confirmed GREEN:
- Application/container health: healthy; migrations at head.
- All 4 real mailbox connections visible via `GET /internal/mailboxes`, with the new fields rendering honestly (real, non-trivial `relevant_message_count`/`needs_review_count`/`last_sweep_status` for the 3 swept mailboxes; a correctly-`None`/zero, never-fabricated state for the one mailbox — `matt@noust.ai`, IMAP — that has genuinely never swept).
- Email Activity re-tested specifically against the Infosecurs/Microsoft mailbox that previously crashed — now returns cleanly, with the historical legacy-shaped entries correctly normalised (`["INBOX","JUNK"]` → `[{"folder_id":"INBOX","display_name":"INBOX"},{"folder_id":"JUNK","display_name":"JUNK"}]`).
- Per-message triage's own backing data (`.../messages`, `.../domain-rules`) confirmed returning correctly for a real mailbox.
- All three Test actions invoked live: Microsoft and Gmail both genuinely report `ok:false` — "authentication error, reconnect this mailbox" (a real, honest, operationally-useful finding: both OAuth-based mailboxes' stored credentials currently appear stale/invalid; flagged for Matt's own attention, separate from and not blocking this delivery's own closure, since the Test action correctly and safely detected and reported it rather than failing silently or crashing); IMAP reports `ok:true`. All three calls independently confirmed non-destructive: `MailboxMessage`/`MailboxSweepRun`/`MailboxDomainRule`/`NeedsYouItem` row counts identical before and after every call, and `connection_state`/`status`/`last_error_code` on the Microsoft mailbox confirmed unchanged (still `CONNECTED`/`ACTIVE`/`None`) despite the reported auth failure — proving the deliberate design choice (never taking the token-refresh-and-possibly-mark-`AUTH_REQUIRED` path) holds under a real failure, live.
- Zero credential-shaped strings and zero real `ERROR`-level log lines across a fresh container's full log history.
- `/internal/ai/health` unchanged — inference architecture untouched.
- SeaweedFS mtime unchanged (`Sep 17 09:19:18 2026`).

**Full production reconciliation** (immediately before this deployment sequence began → after): `MailboxSource=4`, `MailboxMessage=25056`, `MailboxSweepRun=9`, `MailboxDomainRule=32`, `NeedsYou 332/395`, `EvidenceItem=689`, `EvidenceClassification=18`, `AIInvocation=375`, `BackgroundJob=4` — every value identical, zero unexplained drift. No historical mailbox sweep was re-run, no domain rule was changed, no credential was rotated, and no new Needs You item was created by any verification action in this whole sequence — every check performed was either a pure read or one of the three explicitly-authorised, independently-proven-non-destructive Test-action calls.

## 105.5 Verdict

**CLOSED GREEN.** Both the PL's own independent review and the fresh independent Auditor's `MAILBOX_GUI_COMPLETION_GREEN` verdict stand. One genuine, pre-existing production defect was found, fixed, tested, and deployed in-band as part of this same closure (§105.3) — reported transparently, not concealed. Two live, real operational findings (Microsoft and Gmail mailbox credentials both currently appear to need reconnection) are flagged for Matt's own attention as a separate matter. The §104.6 rule-engine condition expansion remains untouched and unstarted, reserved for a separate future work order.

---

# 106. OAuth Recovery Diagnostic + Test-Action False-Alarm Fix (2026-09-27)

## 106.1 Scope

Architect-authorised bounded operational-recovery task, opened directly off §105.4's two flagged findings (Microsoft/Gmail Test actions reporting `ok:false`/"reconnect required"). Objective: determine the true root cause — without assuming re-consent was needed — and restore working OAuth connectivity if the failure proved to be safely repairable within existing authorised configuration/code.

## 106.2 Diagnosis

Read-only checks first: all 3 OAuth-backed mailboxes' token files present, correctly permissioned (`0600`/`0700`), not missing. Non-secret `expires_at` metadata showed all 3 access tokens genuinely expired (short-lived by design, after several days of idle time) — proving nothing on its own about refresh-token validity.

Key insight: the §105 "Test" action was deliberately built non-mutating — it reads the raw stored access token and never refreshes, specifically so the action itself could never trigger a write. That design choice means an ordinary, harmless, expected access-token expiry is structurally indistinguishable from a genuinely dead refresh token when judged by Test's own output alone.

Architect authorised a narrowly-scoped diagnostic: invoke each provider adapter's own existing, already-governed `_ensure_fresh_access_token` (never a new refresh mechanism) directly, with all credential material redacted from every diagnostic output. Result, live against real production credentials:

- **Microsoft** (Infosecurs): access token expired since 2026-09-19T09:40:47Z; refresh attempted → **succeeded**; new expiry 2026-09-27T16:03:56Z.
- **Gmail** (matt.george.scott@gmail.com): expired since 2026-09-24T11:31:08Z; refresh → **succeeded**; new expiry 2026-09-27T15:41:43Z.
- **Gmail** (mgs241171@gmail.com): expired since 2026-09-24T11:28:15Z; refresh → **succeeded**; new expiry 2026-09-27T15:41:43Z.

All three refresh tokens are valid; no human re-consent was ever required for any of the three mailboxes. Re-invoking the real `/microsoft/test`/`/gmail/test` endpoints immediately after confirmed all three now report `ok:true`. Full before/after production reconciliation showed zero deltas (`MailboxSource=4`, `MailboxMessage=25056`, `MailboxSweepRun=9`, `MailboxDomainRule=32`, `NeedsYou 332/395`, `EvidenceItem=689`, `EvidenceClassification=18`); `connection_state` remained `CONNECTED` throughout (the Test action had never persisted its own read, confirming its non-mutating design held even for the false-alarm case).

**This was never an OAuth credential incident.** No credential was ever revoked or invalid. §105.4's "reconnect required" finding was a false alarm produced entirely by the Test action's own design choice not to refresh before checking.

## 106.3 Defect and fix

The false alarm itself was ruled a real product defect, not acceptable for an operator-facing connection check: "can BAGMAN authenticate to and reach this mailbox using the credentials it normally uses in production" is the question Test should answer, and for OAuth-backed mailboxes that requires going through the same refresh path a real sweep already uses before concluding failure.

**Fix** (branch `mailbox/test-action-refresh-fix`): added a one-line public pass-through `ensure_fresh_access_token(mailbox_id)` on both `MicrosoftGraphMailboxAdapter` and `GmailMailboxAdapter`, wrapping the existing private `_ensure_fresh_access_token` — no second refresh mechanism. Both `/microsoft/test` and `/gmail/test` routes now call this wrapper before the identity-check call, instead of reading the raw stored token directly.

Behaviour, all confirmed by new regression tests:
- Unexpired token → unchanged (no refresh call made; proven structurally — the fake OAuth clients raise `AssertionError` if `refresh()` is called with nothing queued).
- Expired token + valid refresh → refresh runs through the existing governed path, new token persisted via the existing `token_store.write`, `ok:true`, no "reconnect" wording.
- Expired token + refresh genuinely fails (revoked/invalid grant) → `ok:false`, reconnect/authentication wording, `connection_state → AUTH_REQUIRED` (the correct, already-governed transition — `mark_microsoft_auth_required` is only invoked on an *actual* refresh failure, never merely because the access token itself was expired; this was already true of the underlying adapter method and required no change).
- Refresh succeeds but the subsequent identity check fails for an unrelated reason (e.g. transport error) → reported accurately, never collapsed into "reconnect required", no state mutation.
- No sweep/message/domain-rule/Needs-You side effects in any path (proven by before/after repository-snapshot tests, mirroring §105's own discipline).
- No access token, refresh token, or client secret ever appears in any response body or audit-event payload.
- IMAP untouched (out of scope; no defect found there).

10 new regression tests (5 per provider). Full suite: 2518 passed, 24 skipped, 0 failed (was 2508 before this delta); `gitleaks` clean. Independent Auditor (fresh context) confirmed all 8 architect-required behaviours against the actual code and re-ran the tests directly — verdict **GREEN**. PR #13 (`mailbox/test-action-refresh-fix`) merged to `main` manually by the architect at merge commit `670c11cbfff032d4c90fe7cc20bc59fcbd6d1d4b` — verified two-parent merge topology (parent 1: prior tip `9b2c837521e8e27c2c4653b6640d29f5f141b895`; parent 2: audited PR head `bc875b31c81fe3901fec813b6594928cfe10faf7`). Post-merge CI on the exact canonical merge SHA independently reconfirmed GREEN (`Security` workflow, `conclusion:success`) before any deployment proceeded.

## 106.4 Canonical production deployment and verification (first pass, pre-manual-merge-confirmation)

Canonical production image `bagman-api:main-670c11c` built and deployed — migration head unchanged, no new migration in this delta.

Live verification (genuinely expired tokens, not synthetic — all 3 real access tokens had naturally expired again by the time of this check, ~30-40 minutes after the §106.2 diagnostic's own refresh):
- Microsoft Test → `ok:true` ("Microsoft Graph identity check succeeded for matt@infosecurs.com").
- Gmail Test (both mailboxes) → `ok:true`.
- Token-store `expires_at` files confirmed advanced to new future timestamps for all three (proving a real refresh occurred, not a cached/stale result) — no token value inspected.
- `docker logs` across the verification window: zero credential-shaped lines.

**Full production reconciliation** (immediately before this deployment sequence → after): `MailboxSource=4`, `MailboxMessage=25056`, `MailboxSweepRun=9`, `MailboxDomainRule=32`, `NeedsYou 332/395`, `EvidenceItem=689`, `EvidenceClassification=18` — every value identical, zero unexplained drift. `connection_state` unchanged (`CONNECTED`) for all 4 mailboxes throughout. No historical sweep was run, no domain rule changed, no Needs You item created by any action in this whole sequence.

## 106.5 Canonical post-merge re-verification (architect-witnessed manual merge)

Matt merged PR #13 manually and issued a formal post-merge gate WO: confirm CI on the exact canonical merge SHA before any deploy, then re-verify.

- **Post-merge CI gate**: `Security` workflow on `670c11cbfff032d4c90fe7cc20bc59fcbd6d1d4b` — `status:completed, conclusion:success` (confirmed via both the check-runs and workflow-runs APIs before proceeding).
- **Canonical image provenance re-confirmed**: the Mac's `/opt/bagman/app/src` checkout was at `670c11cbfff032d4c90fe7cc20bc59fcbd6d1d4b` (matching `origin/main` exactly) at build time; the running `bagman-api` container's image tag (`bagman-api:main-670c11c`) traces to that exact checkout — no rebuild was needed since the §106.4 deployment was already from this identical commit.
- **Live re-verification** (again against genuinely, naturally expired tokens — ~75 minutes after §106.4's own refresh): Microsoft Test → `ok:true`; both Gmail Tests → `ok:true`; all three `expires_at` files advanced to new future timestamps (Microsoft → `2026-09-27T18:42:24Z`, Gmail → `2026-09-27T18:29:05Z`/`18:29:05Z`), proving a genuine refresh on each call, not a cached result.
- **Reconciliation** (before this re-verification pass → after, now also including `AIInvocation`/`BackgroundJob`): `MailboxSource=4`, `MailboxMessage=25056`, `MailboxSweepRun=9`, `MailboxDomainRule=32`, `NeedsYou 332/395`, `EvidenceItem=689`, `EvidenceClassification=18`, `AIInvocation=375`, `BackgroundJob=4` — every value identical, zero drift. `connection_state=CONNECTED` unchanged for all 4 mailboxes. No new sweep run, no new message, no new Needs You item, no domain-rule change. `docker logs` over the window: zero credential-shaped lines.

## 106.6 Verdict

**CLOSED GREEN** for both mailboxes, confirmed twice — once at initial deployment (§106.4) and once under the architect's own formal post-merge re-verification gate against the exact canonical two-parent merge SHA (§106.5). Root cause for Microsoft and Gmail was identical and is recorded accurately here: the original Test implementation was intentionally non-mutating and therefore did not refresh expired OAuth access tokens, which caused healthy, idle OAuth mailboxes to appear disconnected. The production refresh credentials themselves were valid throughout — this is not, and must not be read as, an OAuth credential incident. The rule-engine condition expansion (§104.6/§105.1) remains untouched and unstarted.

---

# 107. Mailbox Rule-Engine Condition Expansion — Discovery-Only, Superseded by Architecture Ruling (2026-09-27)

## 107.1 Scope

Architect-authorised bounded delivery: extend the existing governed mailbox domain-rule engine (§104.6/§105.1's deferred item) so rules could additionally condition on classification, company/entity, and Xero-account suggestion. Per the WO's own explicit "discovery before implementation" gate, a complete, read-only mapping of the current rule engine was produced (branch `mailbox/rule-engine-condition-expansion`, starting SHA `f594a24`) **before any schema, model, or runtime code was written.**

## 107.2 Discovery findings

The existing rule engine (`services/mailbox/domain_rule.py`, `persistence/postgres/mailbox_domain_rule_models.py`, `contracts/mailbox/bagman.mailbox_domain_rule.v1.schema.json`) is confirmed sound, provider-neutral, and well-tested (3 parallel suites — in-memory/Postgres/contract). Its precedence, matching semantics, and audit trail all extend cleanly for a like-for-like new condition. But the mapping established, with concrete file:line evidence, that the rule engine's live evaluation point is structurally a **pre-intake gate**:

- **Evaluation ordering, confirmed**: `services/mailbox/sweep.py:1264` calls `find_for_sender` against a bare `MailboxMessage` (no classification field exists on this dataclass — confirmed at `services/mailbox/message.py:115-197`) *before* any MIME fetch or `EvidenceItem` creation, except where an existing `MUST_READ` outcome deliberately continues processing.
- **Classification unavailable at that point, structurally**: `services/evidence/classification.py`'s `EvidenceClassification` requires a real `evidence_id` (`app/api/routers/evidence_classification.py:260,318`), which does not exist until *after* a message has already passed the pre-intake gate and been MIME-fetched. A message resolved BLACKLIST/GRAYLIST/no-rule today never reaches evidence at all, so it can never acquire a classification. This is not intermittent absence — it is unconditionally true for every message at the point the existing gate runs, every time.
- **Canonical company/entity unavailable as an independent pre-rule fact**: `EvidenceItem.entity_id` (`persistence/postgres/models.py:44-45,170-171`) — the one genuine, FK-backed, canonical entity attachment in this codebase — is populated *from* `MailboxDomainRule.destination_entity_id`, i.e. entity is today an **output** of the rule, not an input available before it runs. The only pre-rule candidate, `MailboxSource.default_entity_id` (`services/mailbox/mailbox.py:314-346`), is explicitly documented in this codebase's own doctrine as a non-authoritative hint — "`mailbox == company` must never be encoded anywhere as a canonical invariant."
- **No Xero-account-suggestion producer exists at all**: `services/xero/ai_suggestion.py::resolve_ai_suggested_account` (`:60-100`) is a stateless *validator* of a caller-supplied candidate ID — its own docstring confirms no AI task producing a real suggestion has ever been wired. `services/needs_you/needs_you.py:152`'s `ITEM_TYPE_XERO_ACCOUNT_REQUIRED` is likewise declared "without a live producer wired." No table/column resembling a stored suggestion exists anywhere in the schema.

## 107.3 Architecture ruling (Architect, 2026-09-27)

Accepted in full, verbatim as issued: the existing mailbox domain-rule engine's responsibility is confirmed as **"a deterministic pre-intake/domain gate operating only on facts available before evidence enrichment"** — this responsibility is not changed by this delivery. §104.6/§105.1's original specification of the three condition dimensions was written against the wrong processing phase; the architecture is not to be forced to satisfy that premise. Specifically ruled:

- **Classification**: a legitimate condition only in a *future* post-enrichment policy layer, once governed classification exists for the message in question — never added to the current pre-intake engine, and never given "sometimes unavailable" semantics merely to accommodate a live gate it structurally cannot satisfy. No condition that always evaluates absent is to be built.
- **Company/entity**: a legitimate condition only once canonical entity resolution has genuinely occurred pre-rule. `MailboxSource.default_entity_id` must **not** be promoted into a security/isolation boundary — doing so would weaken this codebase's existing entity-isolation doctrine (the real FK-backed `entity_id` pattern used by every Xero table) for negligible discriminative value, since rules are already mailbox-scoped.
- **Xero-account suggestion**: marked **`BLOCKED — NO GOVERNED PRODUCER EXISTS`**. Not to be built as a dead/always-absent condition field, and its prerequisite producer is explicitly not to be built under mailbox-rule-engine scope — it belongs to a separate, future accounting/enrichment work item.

This is recorded as **a successful discovery outcome, not a failed implementation** — the architecture was correctly protected from an incorrect premise before any runtime change was made.

## 107.4 PID reconciliation

| Requested condition | Discovery result | Ruling |
|---|---|---|
| Classification | Fact does not exist at pre-intake evaluation phase (structural, not intermittent) | Moved to future post-enrichment policy scope |
| Company/entity | Authoritative entity not available as an independent pre-rule fact; `default_entity_id` is hint-only by this codebase's own doctrine | Moved to future post-enrichment policy scope |
| Xero-account suggestion | No producer currently exists anywhere in the codebase | Blocked pending a separately authorised producer capability |

§104.6's original three-dimension rule-engine-expansion item is superseded by this ruling and must not be re-attempted in its original form.

## 107.5 No implementation change

**Confirmed: zero runtime/schema/rule-engine code was changed under this work order.** `git status` on the discovery branch (`mailbox/rule-engine-condition-expansion`, off `f594a24`) shows a clean working tree — no model, migration, contract-schema, API, or test file was modified. The existing rule model, schema, matching semantics, precedence, production rules, provider behaviour, and ingestion boundary are all preserved exactly as they were. No production deployment is required or was performed for this discovery ruling.

## 107.6 Future processing model (recorded, not authorised for implementation)

```text
Mailbox/message discovery
        ↓
Existing pre-intake domain gate   (THIS delivery's scope — unchanged)
        ↓
MIME/content acquisition
        ↓
Evidence creation
        ↓
Classification
        ↓
Canonical entity/company resolution
        ↓
Xero-account suggestion (only once a real producer exists)
        ↓
Future post-enrichment policy evaluation   (NOT authorised by this ruling)
```

## 107.7 Proposed follow-up work items (proposed only — not begun)

**A. Post-Enrichment Policy Foundation** — a bounded design/delivery for policy evaluation only after evidence exists, classification exists where applicable, and canonical entity/company is resolved. Must determine whether the existing rule abstractions (`domain_rule.py`'s model/precedence/matching) can be safely reused as shared primitives for this later stage, or whether a distinct policy-stage contract is required — that determination is not prejudged here, and no second engine is created by this ruling.

**B. Xero Account Suggestion Producer** — a separate accounting/Xero work item establishing a real governed producer of account suggestions: inputs, provenance, confidence, company/Xero-connection isolation, human-review semantics, fail-closed behaviour. Only once this fact genuinely exists does a Xero-account-suggestion condition become eligible for the future post-enrichment policy layer.

## 107.8 Verdict

**§104.6 AS ORIGINALLY SPECIFIED — SUPERSEDED BY DISCOVERY, NO IMPLEMENTATION DEFECT.** The rule-engine expansion as originally scoped (classification + company + Xero-suggestion conditions on the existing live pre-intake gate) is not implementable honestly against real governed facts, and is not attempted. The existing rule engine is fully preserved, untouched, and remains CLOSED GREEN from §104/§105. Two future work items are proposed (§107.7) but not begun, pending separate architect authorisation.

---

# 108. Post-Enrichment Policy Foundation — Discovery & Design (2026-09-27)

## 108.1 Scope

Architect-authorised bounded discovery/design WO, following directly from §107's finding that classification, company/entity, and Xero-account-suggestion cannot be honestly evaluated at the existing pre-intake gate. Objective: establish the real post-enrichment lifecycle, the governed facts that genuinely exist at each stage, and whether a new "post-enrichment policy" abstraction is actually warranted — grounded in the real codebase, not desired future architecture. **Discovery/design only — no runtime, schema, API, GUI, or policy-evaluation code was written; no migration created; no historical processing, AI invocation, or Xero-suggestion producer built.**

Three independent read-only discovery passes were run in parallel against branch `mailbox/post-enrichment-policy-foundation-discovery` (off canonical `main` at `b5119c7`): (A) the exact post-intake→classification runtime lifecycle and the Evidence/Classification/Entity fact matrix; (B) BAGMAN's AI-invocation architecture and the full Needs You mechanism; (C) the real Xero/accounting fact inventory and an existing-rule-abstraction reuse analysis. `git status` on the discovery branch is clean throughout — confirmed zero code changes.

## 108.2 A — Actual current lifecycle (post-intake gate → classification)

Verified against real code, file:line, not docstrings:

1. **Message discovery** — `services/mailbox/sweep.py`, per-folder delta/next_link paging.
2. **Domain-rule evaluation** — `sweep.py:1264`, `find_for_sender`. `BLACKLIST` short-circuits (`:1274-1296`, message row recorded, no MIME fetch, ever). No-rule/`GRAYLIST` fall to the bounded discovery heuristic (`:1298-1328`, never a MIME fetch). Only `POLICY_MUST_READ` continues.
3. **Per-message auth check** (`MUST_READ` only) — `:1344-1358`; a FAIL escalates to `SECURITY_REVIEW` + a Needs You item and stops (no MIME fetch on failure).
4. **MIME/content acquisition** — `:1360-1387`. Only a real `OK` result proceeds.
5. **Evidence creation** — `ingest_email_evidence` (`services/mailbox/microsoft/evidence_ingest.py:129`) → `EvidenceRepository.register_evidence` (`services/evidence/evidence.py:134/265`).
6. **Entity assignment — same call, not a later step, when the rule's destination is FIXED**: `sweep.py:1412-1414` computes `entity_id_for_evidence = rule.destination_entity_id if FIXED else None`, threaded directly into `register_evidence`. `assign_entity`/`assign_evidence_entity` is **never** called at intake time — it exists solely as the separate, later, human-triggered `COMPANY_REQUIRED` resolution path.
7. **Needs You (`COMPANY_REQUIRED`)** — only when the rule's destination is `REVIEW_REQUIRED`: `sweep.py:1499-1502`, evidence already created with `entity_id=None`.
8. **Classification — not part of this pipeline, at all.** Zero classification calls exist anywhere in `services/mailbox/*.py` or `evidence_ingest.py`. Classification is reachable only via three separate, explicitly-triggered HTTP endpoints (`app/api/routers/evidence_classification.py`): `.../classifications/deterministic` (rule-based, zero AI, can yield `CLASSIFIED` directly), `.../classifications/ai-preview` (dry-run, not persisted as a classification), `.../classifications/orchestrated` (persists an `AI_PROPOSAL` row — **always** `REVIEW_REQUIRED`/`UNCLASSIFIABLE`, never `CLASSIFIED` directly on first pass). The orchestrated endpoint's own docstring states it has **never been invoked against real production evidence**. No scheduler/sweep/cron path calls any of these three today.
9. **AI invocation record** — `AIInvocation` is created only inside the orchestrated-classification call path, never during mailbox sweep itself.
10. **Needs You (`CLASSIFICATION_REVIEW`)** — raised only when an `AI_PROPOSAL` is `REVIEW_REQUIRED`/`UNCLASSIFIABLE`; resolved via `resolve_classification_review` (`services/evidence/classification_review.py:398`), which creates a new, superseding `OPERATOR_ASSIGNED` classification (append-only chain, never an overwrite) and can "teach" a new deterministic `EvidenceClassificationRule` for future automatic matching.
11. **Document-type-specific downstream processing** — none exists anywhere outside the classification module family itself.
12. **Automatic Xero-related processing from evidence** — none exists. Zero files under `services/xero/*.py` reference `evidence_id` at all.

**The central finding**: classification is not an intermittently-available fact — it is, for the overwhelming majority of real evidence today, a fact that **nothing in the system ever computes at all**, because nothing triggers it automatically. Entity resolution is the opposite case: a genuinely common, expected, actively-managed workflow state (`entity_id=None` pending `COMPANY_REQUIRED` resolution), not an edge case.

## 108.3 B — Fact availability matrix

`Fact → producer → persisted? → first available stage → authoritative? → mutable? → isolation boundary → provenance`

| Fact | Producer | Persisted? | First available | Authoritative? | Mutable? | Isolation | Provenance |
|---|---|---|---|---|---|---|---|
| `EvidenceItem.evidence_id` | `register_evidence` | Yes (PK) | Evidence creation | Yes | Immutable | n/a | `created_at`, `source_id` |
| `EvidenceItem.entity_id` | `register_evidence` (FIXED rule) **or** later `assign_evidence_entity` via `COMPANY_REQUIRED` resolution | Yes | Creation, or resolution time | Yes once set | **Write-once from `None`→a value only** — a different value on an already-resolved item raises `ConflictError`; reassignment is an explicit, not-yet-built governed correction workflow | Real FK to `governed_entities.entity_id` | `EVIDENCE_ENTITY_ASSIGNED` audit event |
| `MailboxSource.default_entity_id` | Operator, mailbox setup | Yes | Before any message exists | **No — explicitly non-authoritative hint** | Mutable, mailbox-level | None (not an isolation mechanism) | n/a |
| `EvidenceClassification` (any) | Only via one of 3 explicit HTTP endpoints — never automatic | Yes, separate append-only table | Whenever (if ever) explicitly triggered — **not guaranteed for any message** | The *current* classification is a query (`get_current_classification`), not a stored flag | Individual rows immutable; "current" answer changes via a new superseding row | Scoped to one `evidence_id` | Exactly one of `rule_id` / `ai_invocation_id` / `operator_action_id` per row, `reason_codes`, nullable `confidence` |
| `EvidenceClassification.status`/`document_type` | Same | Yes | Same | `DETERMINISTIC_RULE`→can be `CLASSIFIED` directly; `AI_PROPOSAL`→never `CLASSIFIED` on first pass; `OPERATOR_ASSIGNED`→the only source that produces durable `CLASSIFIED` truth | Superseded, not mutated | Same | Same |
| `AIInvocation` | Only inside orchestrated-classification | Yes | Only if/when called | Yes | Terminal state machine | n/a | Own status; referenced by `ai_invocation_id` |
| `XeroConnection`/`XeroAccount` | Xero OAuth connect / sync | Yes, `entity_id`-scoped, real unique constraints | Independent of any evidence | Yes | `XeroAccount` is a synced mirror, not itself a suggestion | Real `entity_id` FK/unique-per-entity | Standard audit events (`record_audit_event`, same shape as mailbox's) |
| Xero supplier correlation | `services/xero/supplier_correlation.py` | **No** — transient, written only into an existing OPEN `MAILBOX_DOMAIN_REVIEW` item's own metadata; explicitly documented "no new persisted canonical domain model" | Pre-intake-adjacent (enriches a domain-review item, not evidence) | No — never auto-approves, never resolves anything | n/a (not a durable fact) | Domain-scoped only; never touches account codes | n/a |
| Xero-account suggestion | **No producer exists** | No | Never | n/a | n/a | n/a | n/a |

**Absence semantics** (item 9 of the WO): `entity_id=None` means "not yet resolved" — an expected, actively-managed state. Classification-absent means "nothing has ever asked for this to be classified" — the default state for effectively all evidence today, not a transient gap. Xero-suggestion-absent means "the producer does not exist" — a capability gap, not a data-availability gap. These three absences are NOT the same kind of thing and must never be collapsed into one "unknown" semantic.

## 108.4 AI outputs and Needs You mechanism (supporting C, F, H, I)

- **AI-invocation architecture** (`ai_invocations` table, `persistence/postgres/ai_invocation_models.py:131-183`): a partial unique index enforces at most one non-terminal invocation per `(task_id, task_version, primary_input_reference)`; terminal rows accumulate as a real audit trail. Classification is the only AI task touching evidence today, and its output is a **SUGGESTION by construction** — `AI_PROPOSAL` classifications are structurally barred from `status=CLASSIFIED`.
- **A second deterministic rule engine already exists**, separate from the mailbox domain-rule engine: `services/evidence/classification_rule.py` (`EvidenceClassificationRule`), consumed by `.../classifications/deterministic` and "taught" new rules via `resolve_classification_review`'s correction path. This matters directly for §108.7's reuse decision below — a classification-condition "policy" already has its own home, at the evidence layer, and does not need the mailbox engine (or a new one) to serve it.
- **Needs You** (`services/needs_you/needs_you.py:127-217`): status vocabulary `{OPEN, RESOLVED, DISMISSED}`, both terminal states having **zero** onward transitions — the uniform, explicit doctrine is "never reopen; a producer that still has a live question raises a fresh item instead." Dedup is a generic `(item_type, source_object_reference)` idempotency guard (one documented metadata-scan exception for `MAILBOX_DOMAIN_REVIEW`, which has no single canonical reference).
- **`COMPANY_REQUIRED` resolution already IS the entity-assignment decision mechanism** (`app/api/routers/needs_you.py:130-270`): assigns the entity via `assign_evidence_entity` **before** persisting the item RESOLVED (so a failed assignment can never falsely appear answered); idempotent; validates the target entity is real. Shared by both the manual-upload flow and the mailbox flow — one producer contract, two triggers.
- **`CLASSIFICATION_REVIEW` resolution already IS the classification-confirmation/correction decision mechanism** (`services/evidence/classification_review.py`): confirm-or-correct, always a new superseding row (even a plain confirmation is a new row — explicit named doctrine), optional rule-teaching. `DISMISSED` is explicitly disallowed for this item type (would leave a permanently ambiguous proposal with no path forward).
- **`XERO_ACCOUNT_REQUIRED`/`XERO_REFERENCE_DATA_STALE`**: declared, zero live producer for either, confirmed by repo-wide grep.

## 108.5 Xero/accounting facts and rule-abstraction reuse (supporting C, F, G, J)

Real persisted, authoritative Xero facts (`XeroConnection`, `XeroAccount`) are entity-scoped with genuine unique constraints — a sound isolation pattern to mirror. `resolve_ai_suggested_account` is confirmed **entirely dead in production** — called only by its own tests, no real caller anywhere. Supplier correlation is real but transient, metadata-only, domain-scoped (never account-scoped), and structurally pre-evidence — not itself a post-enrichment fact. No automatic mailbox→Xero pipeline exists anywhere.

**Reuse analysis of `services/mailbox/domain_rule.py`'s abstractions**, against a hypothetical future policy needing independent, simultaneously-checkable, heterogeneous-typed conditions (classification, entity, account):
- Condition representation: **not reusable** — `match_mode` is a closed 4-member enum of mutually-exclusive identity-space *shapes*, each with its own dedicated DB index, not a composable AND-able condition set.
- Matching primitives (`core/text_matching.py`): **not reusable** — string-domain normalisation/prefix matching has no bearing on exact-value/FK/enum equality checks.
- Precedence model (four-tier most-specific-wins): **not reusable** — tied to domain/address/subject specificity; no natural "more specific" ordering exists between orthogonal facts like classification and entity.
- Action/result representation (`MUST_READ`/`GRAYLIST`/`BLACKLIST`): **not reusable** — meaningless once evidence already exists.
- Audit structure: **fully reusable, already generic** — `record_audit_event(...)` is called with the identical shape from Xero's own routers as from mailbox's; this is already BAGMAN's one common house-style audit primitive, not something to extract, just something to reuse verbatim.

## 108.6 Earliest safe post-enrichment decision point (E)

Given §108.2/§108.3, there is **no fixed synchronous pipeline stage** at which "evidence exists AND entity is resolved AND classification is persisted" reliably converges — the example sequence in the WO's own §5 template does not hold as a guaranteed linear flow. Entity resolution and classification are each **independently event-driven**, arriving at unpredictable, uncorrelated times (immediately, much later, or never) via two already-separate, already-working mechanisms (§108.4). A "policy point" premised on a synchronous convergence of all three facts would, for classification specifically, almost never fire against real production data today, since nothing yet triggers classification automatically for the bulk of evidence.

## 108.7 One-stage vs multi-stage ruling (F)

**Option B is what the evidence supports — not Option A.** The system already expresses multiple, explicit, per-fact lifecycle-stage decision points (`COMPANY_REQUIRED` for entity; `CLASSIFICATION_REVIEW` for classification), each correct and working at the stage its own fact naturally becomes available, rather than one common convergence point. Forcing Option A would require either fabricating a synchronisation barrier that doesn't exist, or leaving evidence indefinitely unprocessed waiting for a fact (classification) that may never arrive.

## 108.8 Reuse decision (D/G)

**Option 4 — no new policy abstraction is justified at this time**, refined from Fork C's Option-3 fallback once combined with the full fact/mechanism picture: every fact this WO was asked to condition on already has a correct, working, fact-specific decision mechanism at the layer where that fact naturally lives — entity via `COMPANY_REQUIRED` + `assign_evidence_entity`; classification via `CLASSIFICATION_REVIEW` + `resolve_classification_review` + its own existing deterministic rule engine (`EvidenceClassificationRule`, itself a rule-teaching mechanism, already in production). The only literal reusable primitive across all of this is the generic audit-event call, which needs no extraction — it is already shared. There is no evidence-grounded gap that a new general-purpose "post-enrichment policy engine" would close.

## 108.9 Proposed policy input/outcome contracts, re-evaluation, security, audit, historical semantics (E/H/I/J — answered against the DO-NOT-BUILD verdict)

Per item 19's own explicit allowance, most of §§8–16 of the WO become **not applicable as a new engine's design**, because no new engine is recommended. Answered honestly against what actually exists instead of manufacturing a fictional contract:

- **Input/outcome contract**: already exists, twice, narrowly — `COMPANY_REQUIRED`'s resolution contract (`resolution.entity_id` + a real evidence anchor → `assign_evidence_entity`) and `CLASSIFICATION_REVIEW`'s (`CONFIRMED`/`CORRECTED` decision → new superseding classification row, optional rule-teaching). No third, general contract is proposed.
- **Missing/unknown semantics**: already correctly fail-closed in both existing mechanisms — a failed entity assignment never falsely marks `COMPANY_REQUIRED` resolved; `DISMISSED` is structurally disallowed for `CLASSIFICATION_REVIEW` to prevent a permanently-ambiguous dead end.
- **Re-evaluation/idempotency**: already a clean, uniform, codebase-wide doctrine — Needs You items are never reopened; a still-live question raises a fresh item instead. This applies without modification to any future fact-specific decision (including a future Xero-account-suggestion review).
- **Security/isolation**: already sound at every real fact source found — `EvidenceItem.entity_id` is a genuine FK, `XeroConnection`/`XeroAccount` are entity-scoped with real unique constraints, supplier correlation never crosses into account codes. Nothing found here needs new isolation design.
- **Audit/provenance**: already a single, generic, already-reused house-style primitive (`record_audit_event`) — any future fact-specific decision (including a future Xero-suggestion review) should call it exactly as `COMPANY_REQUIRED`/classification-review/mailbox-rule mutations already do.
- **Historical/backfill semantics**: not applicable to a policy engine that is not being built. The existing "never reopen, raise a fresh item" Needs You doctrine already gives a consistent, non-special-cased answer for re-running any fact-specific decision historically, should that ever be needed.

## 108.10 Xero Account Suggestion Producer prerequisite contract (I)

The one genuine, evidence-grounded gap found across all three forks is **not** a missing policy engine — it is a missing **producer** for the Xero-account-suggestion fact itself. Candidate output fields for that future, separately-authorised WO, classified per the WO's own instruction (include only what the evidence genuinely requires):

| Field | Classification | Basis |
|---|---|---|
| `entity_id` | REQUIRED NOW | Every real Xero fact found (`XeroConnection`, `XeroAccount`) is entity-scoped; a suggestion not scoped identically would break the isolation pattern used everywhere else. |
| `xero_connection_id`/`tenant_id` | REQUIRED NOW | Mirrors `XeroAccount`'s own real unique-constraint scoping. |
| `suggested_account_id` | REQUIRED NOW | The fact itself. |
| `confidence` | REQUIRED NOW | Mirrors `EvidenceClassification.confidence` (already nullable, already an established pattern for AI-produced facts in this codebase). |
| `rationale`/`reason_code` | OPTIONAL NOW | Mirrors `EvidenceClassification.reason_codes`. |
| `ai_invocation_id` (provenance) | REQUIRED NOW | Mirrors classification's own provenance discipline exactly — every AI-produced fact in this codebase carries this back-reference. |
| `candidate_set` | OPTIONAL NOW | Only if the producer considers multiple accounts; `resolve_ai_suggested_account`'s existing eligible-set validation shape is the natural fit if reused. |
| `review status` | REQUIRED NOW | Must follow the SAME "never `CLASSIFIED`/confirmed on first pass from AI alone" doctrine already proven for classification — an AI-produced suggestion must default to requiring human confirmation, mirroring `AI_PROPOSAL`'s own structural bar against direct authoritative status. |
| `timestamp`/`version` | REQUIRED NOW | Universal in every other governed record found (`created_at` on every table inspected). |
| Automatic account-selection/posting | REJECT | No evidence anywhere in this codebase of automatic Xero posting from a suggestion — would contradict the existing "AI output is a suggestion, human confirms" doctrine found everywhere else. |

Its human-review/resolution step should reuse `ITEM_TYPE_XERO_ACCOUNT_REQUIRED` (already declared) via a resolution handler mirroring `COMPANY_REQUIRED`'s exact pattern (assign-then-persist, idempotent, fail-closed) — not a new policy engine, not a new Needs You mechanism.

## 108.11 Proposed implementation slices (J)

Given the §108.8 verdict, no Post-Enrichment Policy Foundation implementation is proposed. The one concrete, bounded, evidence-grounded next slice is the previously-proposed **Xero Account Suggestion Producer** (§107.7.B), now with a real candidate contract (§108.10) to design against instead of speculation — still not begun, pending separate architect authorisation.

## 108.12 Verdict

**DO NOT BUILD** a new general-purpose Post-Enrichment Policy engine. The existing, separate, working, fact-specific decision mechanisms (`COMPANY_REQUIRED` + `assign_evidence_entity` for entity; `CLASSIFICATION_REVIEW` + `resolve_classification_review` + `EvidenceClassificationRule` for classification) are already the correct abstraction for every fact this WO investigated, at the lifecycle stage each fact actually becomes available — which is event-driven per fact, not a synchronous convergence point. The only genuine gap found is a missing Xero-account-suggestion **producer** (a data problem), not a policy-engine gap; its prerequisite contract is recorded at §108.10 for a future, separate, bounded WO. Zero runtime/schema/API/GUI/policy-evaluation code was written under this WO.

---

# 109. Xero Account Suggestion Producer — Delivery + Production Closure (2026-09-28)

## 109.1 Scope

The bounded producer proposed at §108.10/§108.11: given eligible governed evidence (already classified `SUPPLIER_INVOICE`/`RECEIPT`, resolved entity), produces an AI-generated suggestion of which synced Xero account it should be coded to, validated against the real eligible-account set for the correct entity/Xero-connection, persisted as a non-authoritative proposal, and routed through the existing `XERO_ACCOUNT_REQUIRED` Needs You item type (declared at §107/§108's own discovery, never previously wired) for human confirmation or correction. Not a policy engine, not automatic posting, not a mailbox-rule extension, not a new Needs You mechanism — reuses BAGMAN's existing AI-task gateway, existing eligible-account validator, and existing human-review queue throughout.

## 109.2 Delivery record — three rounds of independent review, all findings fixed, none concealed

This delivery went through three real, independent review passes. Recording the full history accurately, per explicit architect instruction not to smooth it out of the record — it demonstrates the assurance process working correctly, not a defect in the delivery process:

1. **Initial implementation** (Forge Engineer, full spec from PL discovery): new AI task `XERO_ACCOUNT_SUGGESTION` v1 (`ai/tasks.py`), context builder (`services/xero/account_suggestion_context.py`), orchestrator (`services/xero/account_suggestion.py::produce_account_suggestion`), write-once authoritative assignment record (`services/xero/account_assignment.py`), resolution handler (`services/xero/account_suggestion_resolution.py`), migration `e8c4a1f97b23` (creates `xero_account_suggestions`/`xero_account_assignments`), API endpoints, minimal GUI (`xero-account-suggestion-review.js`). PL independent review caught a stale `architecture-index.md`/`component.yaml` (drift-check failure) — fixed before audit.
2. **First independent Auditor found a real, live-reproduced defect**: cross-entity information disclosure. `GET /internal/xero/{entity_id}/evidence/{evidence_id}/suggestion` returned a *different* entity's suggestion/assignment (including its real Xero tenant ID and account ID) when queried through a mismatched entity's own URL — reproduced live against the dev composition. Fixed with an explicit ownership check; a regression test was written and proven to genuinely fail before the fix and pass after (not merely added post-hoc).
3. **Architect identified a real suggestion-persistence race and a real stale-reuse gap**, neither found by the first audit:
   - **Concurrency race**: `xero_account_suggestions` was PK-only, no uniqueness constraint on `evidence_id`. Reproduced *deterministically* against real, disposable PostgreSQL using genuine OS threads synchronised on a `threading.Barrier` at the exact post-AI-invocation/pre-persist window (sequential calls were explicitly ruled insufficient proof) — two committed rows for one `evidence_id`, confirmed by direct table read, not by the app's own claim.
   - **Stale prior-SUCCEEDED-invocation reuse**: the crash-recovery reuse path searched only by `(task_id, task_version, evidence_id)`, never validating the reused invocation was computed under the *current* governed context (classification, eligible-account set). The account-id *validity* check was always freshly re-run (no unsafe account could slip through), but the suggestion's own *content* could reflect stale reasoning.
4. **Both fixed**: a real DB unique constraint (migration `f1a2b3c4d5e6`, `uq_xero_account_suggestions_evidence_id`) plus `IntegrityError`-to-idempotent-return translation, mirroring `xero_account_assignments`'s own already-correct pattern; a required `context_fingerprint` field added to the task's input schema (mirrors `DOCUMENT_TYPE_PROPOSAL` v2's own `classifier_fingerprint` pattern), reuse now scoped to an exact fingerprint match.
5. **Fresh independent Auditor on the final head** (`8b2539d`, no inherited context from either prior round): **GREEN** — independently re-verified the cross-entity leak remained fixed (and checked for equivalent leaks across every other new endpoint — none found), confirmed the concurrency fix is DB-level and N-caller-safe (not merely 2-caller-safe, since correctness rests on the constraint-violation branch, not the count of callers the test used), and confirmed the stale-reuse fix is both correct and *genuinely necessary* — not theatre, since the step-10 validity check alone never covered the content-staleness gap.

**Test/CI record**: 43 tests for this feature (integration, resolution, endpoints, repository, real-Postgres concurrency). Full suite: 2561 passed, 24 skipped, 0 failed (was 2518 before this delta). `gitleaks` clean throughout. `scripts/generate_architecture_memory.py --check` clean.

## 109.3 Merge

PR #17 (`xero/account-suggestion-producer`) merged to `main` via a normal true merge commit at `4a49f2825bf594744a6108daeebe32894373d0f1` — verified two-parent topology: parent 1 `99ebe7fc3a07f9aafeeccfc2ccfc0ad9cc112029` (prior `main` tip), parent 2 `8b2539d39539c6319bb3c7b2f5e665114ac7b4ef` (PR #17's own final, fully-audited head). Post-merge `Security` CI on the exact canonical merge SHA: GREEN (run #137, conclusion `SUCCESS`).

## 109.4 Production deployment

**Pre-deploy baseline** (verified, not assumed): running image `bagman-api:main-670c11c`, container healthy; Alembic `current` = `c7a3f9e1b542` (matched the expected prior head exactly); `origin/main` on the Mac checkout fetched and confirmed at `4a49f282...`. Xero state: 2 `XeroConnection` rows, both `CONNECTED` (Infosecurs Limited, tenant `0a6c746c-...`; NoustAI Limited, tenant `e9b0d389-...`), 138 synced `XeroAccount` rows. Full row-count baseline captured across every governed table (see §109.5).

**Backup**: real `pg_dump` custom-format snapshot taken before any schema change — `/opt/bagman/backups/pre-xero-suggestion-deploy-20260928T114901Z.dump` (3.4MB), following this appliance's own established `pre-deploy-<timestamp>` convention.

**Canonical image**: built from an explicitly-verified checkout — `cd /opt/bagman/app/src && git checkout 4a49f282... ` confirmed `HEAD` at the exact canonical SHA with a clean working tree before `docker build` — tagged `bagman-api:main-4a49f28`.

**Migration sequencing — a genuine deviation from the planned gate, disclosed transparently**: the image's own `entrypoint.sh` unconditionally runs `alembic upgrade head` before starting the application, ignoring any command override passed to the container ("Migration is an explicit, VISIBLE step before the application starts" — see that file's own header comment). A first attempt to run a *read-only* `alembic heads -v` pre-check via `docker compose run --rm --no-deps bagman-api alembic heads -v` was silently overridden by this entrypoint — the container instead ran the full migration-then-serve sequence, briefly exposing a second, unintended `bagman-api:main-4a49f28` instance answering only `/health` on the Docker network (never wired to receive real traffic; the actual serving `bagman-api` container remained on the old image throughout). This was noticed and the stray container was stopped within approximately 4 minutes, by which point the migration itself had already completed successfully. **The command that ran was the identical governed `alembic upgrade head`** the WO itself required — the deviation was in *when* it ran (as a side effect of a mistaken read-only-check attempt) and *sequencing* (before the intended separate, explicit gate), not in *what* ran or *how*. Both new migrations are purely additive (two new tables plus one new constraint on one of those new tables) — the still-serving old application code had, and used, zero knowledge of these tables, so this posed no live-application risk. Full schema verification (below) was performed before proceeding, exactly as the WO's own post-migration gate required, regardless of the sequencing accident. The correct override syntax for a genuine read-only pre-check (`--entrypoint alembic` rather than a trailing command) was identified and used for all subsequent verification.

**Post-migration schema verification**: Alembic `current` confirmed `f1a2b3c4d5e6` (head) via `docker compose run --rm --no-deps --entrypoint alembic bagman-api current`, and a prior `alembic heads -v` confirmed exactly one head (`f1a2b3c4d5e6`) — no branch conflicts. Both tables (`xero_account_suggestions`, `xero_account_assignments`) confirmed present. All expected constraints confirmed present via direct `pg_catalog` query: on `xero_account_suggestions` — FKs on `evidence_id`/`entity_id`/`ai_invocation_id`, plus `uq_xero_account_suggestions_evidence_id`; on `xero_account_assignments` — FKs on `evidence_id`/`entity_id`/`suggestion_id`, plus `uq_xero_account_assignments_evidence_id`. Both tables genuinely empty (0/0 rows) — no test rows created in production, matching the WO's explicit instruction; the real-Postgres acceptance tests already established behavioural enforcement.

**Application deployment**: `docker compose up -d --no-deps bagman-api` recreated the real serving container from `bagman-api:main-4a49f28`; confirmed healthy within 8 seconds. Startup logs clean — migration re-ran idempotently (already at head, no-op), `Application startup complete`, zero exceptions/tracebacks in the startup window. `GET /internal/ai/health` confirmed `bagman_core`/`bagman_fast`/`claude_code` all `ok` — structural proof the new `XERO_ACCOUNT_SUGGESTION` task contract (whose `TaskContract.__post_init__` validates at module-import time) loaded without error, since application startup would otherwise have failed loudly.

## 109.5 Non-mutating production verification

- **Existing Xero state unchanged**: both `XeroConnection` rows re-read post-deploy — identical `status`/`tenant_id`/`tenant_name`/`last_successful_sync_at` to the pre-deploy baseline, byte-for-byte (the unchanged `last_successful_sync_at` is itself proof no sync/mutation occurred).
- **New persistence starts empty**: `xero_account_suggestions=0`, `xero_account_assignments=0`, `XERO_ACCOUNT_REQUIRED` Needs You items = 0, `AIInvocation` rows for `task_id=XERO_ACCOUNT_SUGGESTION` = 0 — the deployment itself never invoked the producer.
- **Read-only endpoint smoke**, both real BAGMAN companies, each against a deliberately-random nonexistent `evidence_id` (never a real cross-company evidence ID, per the WO's own explicit instruction that boundary is already independently proven in tests/audit): `GET /internal/xero/{entity_id}/evidence/{random_id}/suggestion` → `200`, `{"suggestion": null, "assignment": null}` for both Infosecurs and NoustAI. No mutation, no AI call, no Xero contact.
- **GUI smoke**: root GUI page `200`; `GET /internal/needs-you` `200`, existing `COMPANY_REQUIRED` items render correctly (real, pre-existing item content returned, not fabricated).
- **Reconciliation** (pre-deploy → post-deploy, full governed-table set): `MailboxSource=4`, `MailboxMessage=25056`, `MailboxSweepRun=9`, `MailboxDomainRule=32`, `NeedsYou 332 open/395 total`, `EvidenceItem=689`, `EvidenceClassification=18`, `AIInvocation=375`, `BackgroundJob=4`, `XeroConnection=2`, `XeroAccount=138` — every value identical, zero drift. `XeroAccountSuggestion=0`, `XeroAccountAssignment=0` — the only deltas from this deployment are the new, empty schema objects themselves.
- **Xero write-safety**: `docker logs` across the full deployment window grepped for any Xero write-shaped signal (invoice/bill/journal/payment creation, POST-shaped Xero calls) and any credential-shaped string — zero matches on both.

## 109.6 Verdict

**PRODUCTION DEPLOYMENT: CLOSED GREEN.** Canonical code `4a49f282...` (PR #17, correct two-parent merge topology) built, migrated (both `e8c4a1f97b23` and `f1a2b3c4d5e6` applied cleanly, final head confirmed `f1a2b3c4d5e6`), and deployed as `bagman-api:main-4a49f28` — healthy, zero exceptions, zero drift on every pre-existing governed table, zero Xero mutation, zero producer-triggered rows, zero AI invocations, zero new Needs You items. One real, transparently-disclosed sequencing deviation occurred during the migration gate (the entrypoint's own hardcoded migrate-then-serve behaviour caused the governed `alembic upgrade head` to run earlier, and via a briefly-live stray container, than the WO's intended separate step) — the migration itself was correct, additive-only, and fully re-verified before application deployment proceeded; no data was created, no live traffic was ever served by the stray container, and the real application container was never affected.

**Not authorised or attempted in this gate** (per explicit architect instruction): no real production evidence was run through the producer; no `XERO_ACCOUNT_REQUIRED` item was resolved; no automatic suggestion triggering was begun; the failed-invocation retry-cooldown backlog item remains open, disclosed, not addressed here.

# 110. Incident — Trinity Canonical-Authority Violation (2026-10-01)

## 110.1 What happened

During the preflight for the evidence-classification production rollout (the delivery that produced PR #19/#20), the PL inspected what was believed to be BAGMAN production — a `bagman-api`/`bagman-db` docker-compose stack running on Trinity — and found its schema/data materially behind what PID §109 recorded as the post-Xero-rollout production state (Alembic `b4d8f1a92c65` vs. the expected `f1a2b3c4d5e6`; 296 `EvidenceItem` vs. 689; zero mailbox/Xero activity ever recorded).

Read-only investigation (Docker metadata, PostgreSQL system catalogs/lifetime statistics, filesystem timestamps, checksum-verified backup content, and direct read-only inspection of the Mac mini appliance) established:

- Real BAGMAN production has run on the Mac mini appliance (`192.168.11.4`) since the Phase B cutover recorded in §99–§100 (2026-09-17). It was found healthy, current, and completely unaffected throughout this incident: `bagman-api:main-4a49f28`, Alembic `f1a2b3c4d5e6`, `EvidenceItem=689`, `MailboxMessage=25056`, `MailboxSource=4`, `AIInvocation=375`, `NeedsYou=395/332 open`, `XeroAccount=138`, `XeroConnection=2` — exactly matching §109.
- Trinity's `bagman-db` is the retired post-cutover archive §100.8/§100.9 explicitly preserved, never deleted, never reinitialized (its Postgres cluster's own `PG_VERSION` file mtime and the Docker volume's `CreatedAt` both read `2026-09-16T04:34:47Z`, unchanged since cluster creation). Its lifetime `pg_stat_user_tables` counters (never reset since cluster init) prove it never held mailbox or Xero data at any point in its existence — it is not a rolled-back or restored copy of the real production data, simply a different, much earlier, mailbox/Xero-naive database.
- A `bagman-api` container was found running on Trinity against this archive, created 2026-09-26T11:55:39Z, built with `GIT_COMMIT=unknown` from worktree `cd-6-pid`, image tag `bagman-api:dev` (the compose file's own default fallback tag) — re-establishing exactly the writer §100.8's split-brain-prevention step explicitly removed, against exactly the database §100.9 designated archive-only.
- Every "production rollout"/health-check/verification this delivery's own sessions performed prior to this incident's discovery targeted Trinity, not the Mac mini. Real production was never touched by, and remains entirely unaffected by, any of that work.

## 110.2 Why the existing canonical-authority record (§100.9) was missed

§100.9 was correctly committed to `origin/main:PID.md` the entire time, with content byte-for-byte consistent with the Mac mini's own `/opt/bagman/README-CANONICAL-AUTHORITY.md` marker. It was not stale, not missing, and not in a different repository.

The miss was a search-methodology defect: sections 99–102 are the only four top-level sections in this document (of 110) that use a `##` (second-level) Markdown heading for their own section number, where every other section (1–98, 103+) uses a single `#`. A heading-anchored search for top-level sections (`^# [0-9]*\.`) structurally cannot match a line beginning `##`, so it silently skipped from `# 98.` straight to `# 103.` without ever surfacing §99–§102 — even against a freshly-fetched, fully up-to-date `origin/main`. This was reproduced and confirmed directly, not inferred.

## 110.3 Containment action taken (Trinity only; Mac production untouched throughout)

Read-only evidence captured before any mutation (`docker inspect` of both containers and the `bagman-api:dev` image, compose labels, network membership, full container logs, Trinity's Alembic revision and full lifetime table counters) — archived at `/srv/backup/bagman/incident-trinity-canonical-authority-2026-10-01/`.

Trinity's `bagman-api` container stopped and removed (`docker stop` + `docker rm`, container id `39a50335cae8...`, image `bagman-api:dev`). `bagman-db`/`bagman-objects`/`bagman-scan` left running untouched. No volume deleted, no schema altered, no Alembic command run, no object-store data touched, no backup restored.

Post-removal, verified directly: `bagman-api` absent from `docker ps -a`; `bagman-db` still running and healthy; `bagman-postgres-data` volume's `CreatedAt`/mountpoint unchanged; no host-published PostgreSQL port; only `bagman-scan`/`bagman-objects`/`bagman-db` remain on `bagman-net` (no application container capable of writing); Alembic revision and every table's lifetime insert/update/delete counters identical to the pre-removal baseline.

## 110.4 Governance hardening

A prominent, uniformly-`#`-heading-formatted **CURRENT PRODUCTION AUTHORITY** block was added at the very top of this document (before §1), restating what §100.9 already established — the Mac mini as sole writable production runtime, Trinity as retired archive forbidden as a deployment target — and requiring every future production work order to fetch canonical `origin/main`, re-read that block, and prove the actual target host matches it before any mutation. Deliberately documentation-only: no new application/runtime machinery was introduced to solve what was a documentation-prominence and search-methodology failure, not a system-design gap.

## 110.5 Verdict

**INCIDENT CONTAINED GREEN.** Root cause identified with no remaining speculation (§110.2). Trinity restored to its intended §100.8 post-cutover state (no canonical writer). Real production (Mac mini, `bagman-api:main-4a49f28`, Alembic `f1a2b3c4d5e6`) independently confirmed untouched and intact throughout. The evidence-classification delivery (PR #19/#20) has not been deployed to either host and remains pending a separate, correctly-targeted production rollout gate.

# 111. BAGMAN Delivery Governance Doctrine (Architect ruling, 2026-10-02)

Standing, permanent doctrine governing how every future BAGMAN delivery is authorised and executed. Applies from this ruling forward; prior deliveries recorded elsewhere in this document (§1-§110) are not retroactively invalidated, but any NEW work must follow this chain.

## 111.1 The governed delivery chain

> **Architecture decision → PID/Amendment → Git-tracked Work Order → Delivery Controller → Implementer → Independent Audit → PR → Architect Acceptance → Merge → Closure**

- The **Architect** owns architecture, PIDs, amendments, sequencing, and acceptance.
- The **Delivery Controller** owns bounded execution and may dispatch an Implementer (FORGE) only against an approved, Git-tracked Work Order.
- The **Implementer** works only within that Work Order and must not invent architecture or widen scope.
- Every Work Order must identify its parent PID section/amendment, applicable amendments, the exact base SHA it is issued against, scope, exclusions, required tests, and acceptance criteria.
- If implementation exposes an architectural ambiguity, the correct response is to STOP and return it to the Architect — never improvise a resolution.
- Corrections and follow-up work require the same governed Work Order process; there is no "small enough to skip" exception.
- Git/GitHub is the durable authority. Chat is only the control surface — a decision that exists only in chat, and not in a committed Git record, is not yet durable project authority.

## 111.2 Hard invariants

**NO PID → NO WORK ORDER.**
**NO WORK ORDER → NO IMPLEMENTATION.**
**NO INDEPENDENT AUDIT + ARCHITECT ACCEPTANCE → NO MERGE.**
**NO GIT RECORD → NOT DURABLE PROJECT AUTHORITY.**

## 111.3 Binding on prompts, not just on memory

Every future BAGMAN Delivery Controller prompt and every FORGE Implementer prompt MUST reproduce this doctrine (§111.1's chain and §111.2's four invariants) explicitly, in full, inline — never by reference alone, and never left to be recalled from prior chat context. Agents must not rely on chat memory for governance rules: a fresh Delivery Controller or Implementer session has no memory of this section unless the prompt that starts it restates the doctrine directly. This is itself a hard requirement of this ruling, not a style preference.

# 112. Automatic Evidence Classification — Production Activation Amendment (Architect ruling, 2026-10-02)

Records, durably, the architecture now governing automatic evidence classification in production, the activation boundary established during the controlled canary, the production deployment facts, the canary result, an unrelated operational finding, and an explicit governance-process deviation this amendment exists to close out.

## 112.A Automatic classification architecture (authoritative model)

The authoritative automatic-classification model, superseding every earlier cursor/sweep/lap/target design (see `services/evidence/classification_job.py`'s own module docstring, "Simplified design" section, and PR #22):

```text
Evidence ingestion → durable EvidenceItem only

(independently, later)

classification worker → bounded PostgreSQL missing-work discovery
                       → EvidenceClassificationJob
                       → existing governed classify_evidence()
```

The worker discovers `EvidenceItem`s where:
- `EvidenceItem.created_at >= activation_boundary`;
- no `EvidenceClassificationJob` exists for it;
- no current `DOCUMENT_TYPE` `EvidenceClassification` exists for it;

ordered oldest-`created_at`-first, bounded per call (`--discovery-limit`).

There is, by design:
- no ingestion-side classification enqueue (evidence registration never creates a job);
- no reconciliation cursor;
- no sweep/lap/target state machine;
- no Redis/Celery/Kafka requirement;
- no historical automatic backfill (the activation boundary is a hard floor, never crossed).

PostgreSQL itself is the durable source of work state — there is nothing else to persist between worker invocations.

## 112.B Activation boundary

The production activation boundary established during the controlled canary (§112.D):

```text
T_ACT = 2026-10-02T09:58:51Z
```

Eligibility is governed exclusively by `EvidenceItem.created_at >= T_ACT` — never by the underlying message's own `received_at`. A historic email imported (registered) after `T_ACT` is eligible; an `EvidenceItem` whose own `created_at` predates `T_ACT` must never be automatically discovered, regardless of its `received_at`.

This boundary must remain stable for all future recurring worker executions. It must never be moved backwards merely to manufacture work. Any change to it requires an explicit Architect amendment to this section.

## 112.C Production state

The facts below are the **reported** results of the 2026-10-02 rollout, recorded provisionally pending the governance-recovery independent audit required by §112.F.

Canonical code deployed:

```text
493af0c3923bf6f1bdd308521da8f3aca3fafac2
```

Production image:

```text
bagman-api:main-493af0c
```

Production Alembic revision:

```text
ae936a444eae
```

Migration path actually applied:

```text
f1a2b3c4d5e6 → a7f34c9e2d18 → ae936a444eae
```

The obsolete reconciliation-cursor migration, `143b86b2ab44`, was never deployed to any real or persistent environment anywhere (not this production host, not Trinity's retired archive — confirmed independently, repeatedly, before PR #22 removed it) and is not part of canonical migration history. No `evidence_classification_reconciliation_cursors` table exists, or is intended to exist, in production schema.

## 112.D Canary result

The facts below are, likewise, the **reported** results of the 2026-10-02 canary, recorded provisionally pending the governance-recovery independent audit required by §112.F — not yet independently re-verified under §111.

Recorded factually, not interpretively:

- `T_ACT`: `2026-10-02T09:58:51Z`.
- Four legitimate post-activation `EvidenceItem` rows were created by one ordinary, bounded Microsoft Graph mailbox sweep (the existing production sweep mechanism) — not manufactured, not historical replay.
- Worker bounds for the canary: discovery limit `1`, processing limit `1`.
- Exactly one `EvidenceClassificationJob` was created and processed.
- Job final status: `SUCCEEDED`.
- `classification_outcome`: `AI_PROPOSAL_REVIEW_REQUIRED`.
- Deterministic classification returned `NO_MATCH`; the AI fallback proposed `NON_ACCOUNTING_DOCUMENT` (`DOCUMENT_TYPE_PROPOSAL` v2, `bagman-core`, `SUCCEEDED`).
- A `CLASSIFICATION_REVIEW` Needs You item was created and left `OPEN` — the human review this outcome requires has **not** been performed or resolved by this amendment, and this section makes no claim that it has.
- Zero pre-`T_ACT` `EvidenceClassificationJob` rows exist (verified directly against production).
- Zero automatic Xero Account Suggestion activity: zero `XeroAccountSuggestion`, zero `XeroAccountAssignment`, zero `XERO_ACCOUNT_REQUIRED`, zero Xero ledger mutation of any kind.
- No scheduler, cron, timer, or daemon was installed as part of this canary — confirmed absent, both before and after.

## 112.E Gmail operational finding (unrelated to classification; not remediated here)

During the canary's search for legitimate post-activation evidence, two Gmail mailbox sweep attempts failed with a pre-existing `TOKEN_REFRESH_FAILED` condition (expired/revoked OAuth credentials), unrelated to the classification architecture or this delivery. This is recorded here as an operational finding for separate attention — **not** remediated as part of this amendment, and this amendment does not widen its own scope to fix Gmail authentication.

## 112.F Governance deviation — recorded honestly

The 2026-10-02 production deployment and one-job canary (§112.C/§112.D) were **reported technically GREEN by the executing delivery stream** — the rollout report was returned through the ordinary BAGMAN execution stream, not through a separately-dispatched, fresh Independent Auditor with no inherited conclusions. They were, in addition, initiated from a chat-issued rollout instruction **before** the governed delivery doctrine now recorded at §111 had been embodied as a Git-tracked `PID/Amendment → Work Order → Delivery Controller → Implementer` chain.

**Reported technical GREEN is not the same thing as governed independent audit closure.** This section does not claim the reported result is wrong, nor that the deployment failed — only that the distinct, separate step of independent re-verification under §111 has not yet happened.

Therefore:
- the Architect has **provisionally accepted the reported technical result** (§112.C/§112.D) — provisional acceptance of what was reported, not a finding that it has been independently confirmed;
- it has **not yet been independently re-verified under §111**, and must **not** be represented, in any future record, as having followed the full governed chain established at §111 — it did not;
- durable closure of this deployment requires a **separate governance-recovery Work Order**, created AFTER this amendment is merged, issued through the full §111 chain;
- that Work Order's specific purpose is to perform the fresh, independent live-state audit of production that has not yet occurred (re-verifying, under the governed process, everything §112.C/§112.D currently record only as reported) and produce its own Git-tracked closure record;
- **no further classification-production mutation, and no scheduler installation, may occur before that governance-recovery closure lands.**

## 112.G Future scheduler — not authorised here

Recurring classification scheduling (cron, systemd timer, daemon, loop container, Kubernetes CronJob, or any other recurring invocation of the classification worker) is **NOT authorised** by this amendment.

A scheduler requires its own future delivery, through the full chain: `Architecture decision → PID/Amendment if required → Git-tracked Work Order → Delivery Controller → Implementer → Independent Audit → PR → Architect Acceptance → Merge → Closure`.

When that future scheduler delivery occurs, it must reuse the established activation boundary:

```text
T_ACT = 2026-10-02T09:58:51Z
```

unless the Architect explicitly amends §112.B first.
