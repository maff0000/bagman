# BAGMAN Architecture

This document describes the high-level architectural doctrine for BAGMAN as
established by CD-1 (Foundation Security Scaffold). It captures intent and
doctrine only — it must not be read as describing implementation that
exists yet. As of CD-1, no business logic, no working containers, and no
deployed datastore exist. See `PID.md` §16 for the full out-of-scope list.

## Modular architecture doctrine

BAGMAN is constructed from bounded components, each with a clear
responsibility and an explicit contract for how other components may
interact with it. The repository anticipates the following domains:

- **core** — shared core domain logic used across BAGMAN.
- **evidence** — immutable financial evidence handling.
- **finance** — accounting, reconciliation, and financial workflows.
- **billing** — customer billing state and revenue operations.
- **tax** — tax preparation support.
- **notifications** — outbound notification handling.
- **adapters** — integration points with external systems (email, banking,
  accounting, billing, and other services), isolating the rest of BAGMAN
  from third-party APIs.
- **agent** — the BAGMAN AI agent, its tools, policies, and memory
  interface.
- **memory** — durable institutional memory fabric (architecture,
  operating model, companies, products, tax, generated artifacts).
- **GUI** — the user-facing client (`ui/`).
- **deployment** — Docker, Compose, and environment definitions.
- **tests** — automated test suites.
- **ops** — operational runbooks and procedures.

Creating these directory boundaries in CD-1 does not authorise
implementation within them. Each component directory is scaffolding only.

## Docker-first doctrine

BAGMAN is Docker-first. All future BAGMAN runtime dependencies shall be
BAGMAN-owned — BAGMAN shall not silently consume shared application
infrastructure belonging to another product on the host.

Canonical container naming follows `bagman-<operational-role>`, for example:

- `bagman-api`
- `bagman-ui`
- `bagman-worker`
- `bagman-agent`
- `bagman-db`
- `bagman-objects`
- `bagman-notify`

CD-1 does not require any working application containers.

## Datastore doctrine

**PostgreSQL** is the intended canonical structured datastore for BAGMAN.
CD-1 does not deploy PostgreSQL, except where strictly necessary to prove
deployment/configuration structure.

**Redis is not part of BAGMAN v1 architecture.** It shall not be introduced
without a later, explicitly documented architectural requirement
demonstrating why PostgreSQL and normal worker patterns are insufficient.

## External secret handling

Secrets never live inside this repository. BAGMAN follows a three-layer
configuration model — committed structure, runtime environment
configuration, and external secrets — with runtime secrets consumed via
mounted files (e.g. `/run/secrets/<name>`) rather than broad environment
variable exposure. The full three-layer model is documented in
`config/README.md` (introduced by a parallel work item); the doctrine
itself is defined in `PID.md` §5 and §6.

## Adapter / service separation

Adapters (`adapters/`) are the only components that speak to external
systems (email providers, banks, accounting platforms, billing platforms,
and other third-party services). Services (`services/`) implement BAGMAN's
own bounded business domains and depend on adapters through explicit
contracts rather than reaching into external systems directly. This keeps
external integration surface area isolated and replaceable without
disturbing core business logic.

## GUI and agent as governed clients

Both the GUI (`ui/`) and the BAGMAN AI agent (`agent/`) are governed clients
of BAGMAN's own services. Neither holds broad credentials directly, and
neither is permitted to reach external systems or data stores on its own
authority — all access is mediated through BAGMAN's services and adapters
under their respective contracts. This preserves a single, auditable point
of control over sensitive financial operations regardless of which client
is acting.

## Inference / LLM backend architecture (Architect ruling, 2026-09-26)

This section is the durable, canonical record of BAGMAN's LLM backend
architecture, established by an explicit Architect ruling during CD-6 and
recorded here as the standing reference so it does not drift or get
re-litigated. It supersedes, on the specific points below, the CD-5
three-tier topology doctrine in `PID.md` §2/§8/§9/§11/§12 — see `PID.md`
§103 for the full supersession record, the reasoning, and the bounded
CD-5 implementation work order that follows from this ruling. `PID.md`'s
own historical text is preserved, not deleted or rewritten, per this
project's standing amendment convention.

**Model quality is invariant; throughput is elastic.** Production
inference for BAGMAN's normal background workloads runs on exactly ONE
permanently-resident, originally-tested-and-accepted model on the
dedicated Mac mini appliance. That model identity is never swapped for a
smaller, cheaper, or faster substitute merely to solve a capacity or
memory-pressure problem. Capacity problems are solved by capacity-side
means (durable queuing, backlog/overflow handling, operational scheduling)
— never by silently degrading the model every BAGMAN task actually runs
against.

Standing doctrine, in full:

1. **One Mac model, always.** No second local model, no model swapping, no
   quantization/context-window reduction adopted for capacity reasons. This
   was explicitly tested (a Colima memory-ceiling reduction and a reduced-
   context-window model tag, both trialled and reverted during CD-6) and
   found not to help — capacity is a genuine host constraint, not a model
   sizing problem, and is not to be "solved" by re-opening the model choice.
2. **`bagman-fast` / `bagman-core` are generation PROFILES, not separate
   model identities.** Both may carry distinct token budgets, temperature,
   prompt content, and output expectations, but both resolve to the SAME
   physical Mac-resident model. There is no architectural meaning by which
   `bagman-fast` and `bagman-core` are different models.
3. **`bagman-deep` is RETIRED as a permanent tier.** Its current behaviour —
   routing to a different, heavier Trinity-hosted model for "escalated"
   tasks — is no longer authorised as standing architecture. No live CD-5
   task contract ever set `preferred_capability="bagman-deep"`, so retiring
   it requires no task-contract migration — only alias/config/test/doc
   cleanup (see `PID.md` §103 for the full inventory).
4. **The Mac-local `bagman-ai-gateway` LiteLLM layer is explicitly
   RETAINED.** It is not removed, replaced, or "simplified for architectural
   neatness" as part of CD-5. It exists for valid, already-tested reasons —
   correct `response_format`/structured-output translation to Ollama,
   `think:false` handling, local credential/routing isolation, and stable
   BAGMAN-facing API behaviour that has already been proven live. This point
   explicitly reverses an earlier, since-corrected recommendation made
   during this same ruling's own review process to retire it; see `PID.md`
   §103 for that history.
5. **Trinity's own LiteLLM (`192.168.246.202`) is NOT part of normal BAGMAN
   production inference.** It is backlog/overflow capacity only, for when
   the Mac mini's own durable job queue is genuinely backed up beyond
   operationally acceptable wait times — never a routine per-request
   escalation path. The ONLY authorised Trinity model for BAGMAN overflow
   is a single alias, `trinity-core` — never `trinity-fast`, never
   `trinity-deep`, never any other Trinity alias — and `trinity-core` may
   not be used for any real overflow traffic until an explicit, one-time
   compatibility validation has been run and passed: the same task
   contracts, the same schemas, and a real check that output quality does
   not regress relative to the Mac-resident model. BAGMAN's request
   architecture (prompts, contracts, schemas, structured-output validation)
   is never bent around the overflow model — `trinity-core` must conform to
   it, not the reverse.
6. **BAGMAN owns its own explicit backend-selection decision.** A
   `MAC_LOCAL` / `TRINITY_CORE_OVERFLOW` selection is made explicitly by
   BAGMAN's own code, never hidden inside LiteLLM's own routing rules.
   Default is always `MAC_LOCAL`. For CD-5 this stays a simple, explicit,
   bounded operator/maintenance-mode decision for backlog handling — no
   invented automatic thresholds or auto-escalation heuristics are
   authorised yet.
7. **A durable, Postgres-backed job mechanism is required** for background
   inference work — the smallest sensible design reusing the existing
   `bagman-db`, not a new distributed broker — with atomic claiming, retry,
   restart recovery, idempotency, terminal failure handling, audit history,
   and concurrency protection. See `PID.md` §103 for the bounded
   implementation work order.
8. **Provenance must record, per invocation:** `inference_backend`
   (`MAC_LOCAL` / `TRINITY_CORE_OVERFLOW`, or a similarly-named closed
   enum), actual model/provider information where available, task identity
   and version, generation profile, timestamps, latency, validation status,
   and retry/error history. This is audit-only — it must never be used to
   drive business-logic branching.

This architecture is now frozen. It is not to be redesigned, and settled
questions above are not to be reopened, unless real implementation
evidence proves a genuine problem with the ruling itself — capacity
pressure alone is not such evidence; see point 1.
