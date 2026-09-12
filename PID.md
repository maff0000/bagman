# BAGMAN PID v2 — Canonical Entity, Evidence, Provenance & Audit Foundation

## 1. Product Identity

**Product:** BAGMAN
**Repository:** `github.com/maff0000/bagman`
**Authoritative working tree:** `/srv/bagman`
**Canonical CD-1 merge:** `19d3f66688a3ad2bf178a230b34ff5b6122f0d4a`
**Primary branch:** `main`

This PID authorises CD-2:

> **Canonical Entity, Evidence, Provenance & Audit Foundation**

CD-2 establishes the internal identity, evidence, lineage and audit model on which all later BAGMAN capabilities depend.

It does **not** connect BAGMAN to any live mailbox, bank, accounting platform, billing platform or SaaS provider.

---

# 2. Delivery Objective

CD-2 shall establish a durable canonical model for:

* governed entities
* evidence identity
* evidence provenance
* evidence relationships
* source identity
* immutable audit events
* correlation and causality
* component manifests
* canonical contracts
* deterministic validation
* synthetic fixtures
* persistence-ready domain models

The desired end state is:

> Any future document, email, bank transaction, invoice, receipt, tax record or external event can enter BAGMAN through an adapter and be represented using stable internal identities without the rest of BAGMAN needing to understand the originating provider.

CD-2 is therefore a **foundation of meaning**, not an integration delivery.

---

# 3. Core Architectural Principle

External systems speak provider-specific language.

BAGMAN must not.

Future examples include:

```text
Microsoft message ID
Gmail message ID
IMAP UID
Starling transaction ID
Revolut transaction ID
Xero invoice ID
Chargebee subscription ID
```

These are external identifiers.

BAGMAN must establish its own canonical identities.

Conceptually:

```text
EXTERNAL SYSTEM
      ↓
ADAPTER
      ↓
CANONICAL BAGMAN IDENTITY
      ↓
BAGMAN SERVICES
```

No core BAGMAN component should require knowledge of Microsoft, Google, Starling, Revolut, Xero, Chargebee, uSecure, Huntress or other provider-specific identifiers except through explicit source references.

---

# 4. Canonical Governed Entity Model

CD-2 shall introduce a canonical `GovernedEntity` model.

Initial governed entities:

```text
NOUSTAI_LIMITED
INFOSECURS_LIMITED
MATTHEW_SCOTT_PERSONAL
```

These identities are stable BAGMAN identities.

They must not depend on:

* email address
* bank account
* Xero tenant
* legal registration number
* display name
* current jurisdiction

Those attributes may be associated with the entity later.

## Required conceptual fields

A governed entity shall support at minimum:

```text
entity_id
entity_type
canonical_name
display_name
status
created_at
metadata
```

Suggested entity types:

```text
COMPANY
PERSON
```

Future extension must remain possible for:

```text
TRUST
PARTNERSHIP
OTHER
```

without schema redesign.

## Entity isolation invariant

Every canonical financial or evidential record must ultimately be:

* associated with exactly one governed entity, or
* explicitly unresolved

Never silently infer ownership and persist it as truth without provenance.

The unresolved state must be first-class.

---

# 5. Canonical Identifier Doctrine

BAGMAN identities must not be derived directly from mutable business values.

Do not use:

```text
invoice number
email address
supplier name
filename
bank reference
customer name
```

as primary canonical identifiers.

Use opaque immutable BAGMAN-generated identifiers.

Preferred form:

```text
UUIDv7
```

or an equivalently justified monotonic globally unique identifier.

The exact implementation may be selected by FORGE if objectively justified and documented.

Identifiers must be:

* globally unique within BAGMAN
* immutable
* non-secret
* safe for logs
* safe for URLs where applicable
* independent from external-provider identifiers

---

# 6. Evidence Model

CD-2 shall introduce a first-class `EvidenceItem`.

An EvidenceItem represents something BAGMAN has observed or received.

Examples later include:

```text
email
email attachment
uploaded PDF
receipt image
invoice
bank statement
HMRC letter
contract
CSV export
JSON API payload
generated tax pack
```

CD-2 itself shall use synthetic examples only.

## Evidence identity

Each EvidenceItem shall receive its own canonical BAGMAN ID.

Suggested fields:

```text
evidence_id
entity_id or unresolved
evidence_type
source_id
observed_at
received_at
content_hash
mime_type
original_name
size_bytes
storage_reference
status
created_at
metadata
```

Not every field must apply to every evidence type.

Avoid forcing provider-specific fields into the canonical base model.

---

# 7. Evidence Immutability

Original received evidence is immutable.

Once BAGMAN records an original evidence artifact, it must not be silently modified.

Corrections occur by:

* adding new derived records
* superseding interpretation
* adding classifications
* linking replacement evidence
* recording explicit corrective actions

They do not occur by rewriting history.

Example:

```text
Original PDF
    ↓
EvidenceItem A
    ↓
Extraction v1
    ↓
Extraction v2
```

not:

```text
EvidenceItem A overwritten
```

This distinction is mandatory.

---

# 8. Content Hashing

Evidence content should use cryptographic content hashing.

Default:

```text
SHA-256
```

The content hash supports:

* duplicate detection
* integrity validation
* provenance
* evidence identity checks

The hash is not necessarily the canonical evidence ID.

Two observations of identical bytes may still represent separate source observations.

Therefore BAGMAN must distinguish:

```text
evidence identity
```

from:

```text
content identity
```

This is important.

Example:

Two mailboxes may receive the same invoice PDF.

That can result in:

```text
two EvidenceItem observations
same SHA-256 content hash
```

Later deduplication logic may associate them.

CD-2 must not prematurely collapse those observations into one record.

---

# 9. Source Model

Introduce a canonical `Source`.

A source describes where information originated.

Examples for future use:

```text
MAILBOX
MANUAL_UPLOAD
BANK
ACCOUNTING_PLATFORM
BILLING_PLATFORM
GENERATED
API
```

Provider-specific identity belongs underneath the source model.

Suggested conceptual structure:

```text
source_id
source_type
provider
external_source_ref
governed_entity_hint
status
metadata
```

Example later:

```text
source_type: MAILBOX
provider: MICROSOFT_GRAPH
external_source_ref: matt@infosecurs.com
```

This does not mean all records from that mailbox automatically belong to Infosecurs.

A source may provide an entity hint without asserting canonical ownership.

---

# 10. External Reference Model

External identifiers must be represented separately from canonical IDs.

Introduce an `ExternalReference`.

Suggested fields:

```text
external_reference_id
provider
resource_type
external_id
canonical_object_type
canonical_object_id
source_id
first_observed_at
metadata
```

This lets BAGMAN say:

```text
BAGMAN evidence EV-...
corresponds to
Microsoft Graph message AAMk...
```

without contaminating BAGMAN's internal identity model.

Uniqueness rules must be explicit.

For example, the same external ID may only be meaningful within:

```text
provider + account/source + resource type
```

Do not assume provider IDs are globally unique.

---

# 11. Provenance Model

BAGMAN must always be able to answer:

> Where did this information come from?

Introduce explicit provenance.

For a derived fact, provenance should be able to trace back to one or more EvidenceItems.

Conceptually:

```text
Canonical fact
      ↓
Provenance edge
      ↓
EvidenceItem
      ↓
Source
```

Provenance may also include transformation information.

Suggested fields:

```text
provenance_id
subject_type
subject_id
evidence_id
relationship
transform_id
created_at
metadata
```

Relationships may include:

```text
OBSERVED_FROM
EXTRACTED_FROM
DERIVED_FROM
GENERATED_FROM
SUPERSEDES
SUPPORTS
CONTRADICTS
```

Do not hardcode only invoice-oriented provenance.

This is a platform-level primitive.

---

# 12. Derived Data Doctrine

BAGMAN must distinguish:

```text
original evidence
```

from:

```text
derived interpretation
```

Examples later:

Original:

```text
PDF invoice
email
bank transaction payload
```

Derived:

```text
supplier name
invoice total
tax classification
R&D candidate status
reconciliation proposal
```

A derived fact may change.

Original evidence does not.

This must be obvious in both contracts and persistence design.

---

# 13. Audit Event Model

CD-2 shall establish an append-oriented canonical `AuditEvent`.

An audit event records something meaningful that happened inside BAGMAN.

Examples later:

```text
EVIDENCE_OBSERVED
EVIDENCE_STORED
ENTITY_ASSIGNED
CLASSIFICATION_PROPOSED
CLASSIFICATION_APPROVED
RECONCILIATION_PROPOSED
RECONCILIATION_CONFIRMED
DOCUMENT_DOWNLOADED
TAX_PACK_GENERATED
CUSTOMER_SUSPENSION_REQUESTED
```

CD-2 need only implement foundation events.

## Required audit characteristics

Audit events must be:

* immutable after creation
* UTC timestamped
* actor-attributed
* correlation-aware
* causally traceable
* structured
* queryable
* safe to log

Suggested fields:

```text
audit_event_id
event_type
occurred_at
actor_type
actor_id
subject_type
subject_id
correlation_id
causation_id
payload
schema_version
```

---

# 14. Actor Model

BAGMAN must distinguish who or what caused an action.

Suggested actor types:

```text
SYSTEM
USER
AGENT
SERVICE
EXTERNAL_SYSTEM
```

Examples later:

```text
Matt
BAGMAN Agent
mail-ingest worker
reconciler
Chargebee
```

Do not represent all activity as "system".

That destroys accountability.

---

# 15. Correlation and Causality

Every multi-step workflow must be traceable.

Introduce:

```text
correlation_id
causation_id
```

Example later:

```text
incoming email
 correlation=A

attachment discovered
 correlation=A
 causation=email-event-id

invoice extracted
 correlation=A
 causation=attachment-event-id

classification proposed
 correlation=A
 causation=extraction-event-id
```

This allows BAGMAN to reconstruct complete operational stories.

---

# 16. Time Doctrine

All canonical timestamps shall be stored and processed in UTC.

Use timezone-aware timestamps.

No naive datetime values are permitted in canonical BAGMAN state.

Original local/provider timestamps may be preserved separately when required for evidential reasons.

Canonical event time remains UTC.

---

# 17. Schema Versioning

Canonical contracts must include schema/version identity.

Once later components consume these contracts, silent incompatible changes become dangerous.

Therefore contracts shall support explicit versioning.

Example:

```text
bagman.evidence.v1
bagman.audit_event.v1
bagman.entity.v1
```

The exact naming convention may be refined during implementation.

Contract changes must distinguish:

* backward-compatible additions
* breaking semantic changes

Breaking changes require a new contract version or documented migration.

---

# 18. Contract Format

FORGE should use a machine-validatable contract representation.

Preferred:

```text
JSON Schema
```

where appropriate.

Pydantic/domain models may be generated from or validated against those contracts, but no Python-only model should silently become the sole cross-component authority if a language-neutral schema is practical.

Contracts belong under:

```text
contracts/
```

Suggested layout:

```text
contracts/
├── entity/
├── evidence/
├── source/
├── provenance/
├── audit/
└── common/
```

---

# 19. Component Manifests

CD-2 shall introduce the component-manifest pattern discussed in the BAGMAN architecture.

Each bounded component implemented now or later should be capable of declaring:

```text
id
version
responsibility
owns
consumes
produces
dependencies
external_access
prohibited
```

Preferred filename:

```text
component.yaml
```

Example conceptual form:

```yaml
id: BAGMAN.CORE.EVIDENCE
version: 1

responsibility:
  "Own canonical evidence identity and provenance."

owns:
  - EvidenceItem
  - Provenance

consumes: []

produces:
  - EvidenceObserved

external_access: false

prohibited:
  - direct_email_access
  - direct_bank_access
  - direct_xero_access
```

CD-2 should create manifests only for components that genuinely exist.

Do not fabricate future runtime components as though implemented.

---

# 20. Core Domain Placement

Expected CD-2 implementation areas:

```text
core/
contracts/
services/evidence/
tests/
memory/
```

Potential supporting configuration:

```text
config/
```

FORGE may introduce a small shared Python package if required to implement canonical types and validation cleanly.

Avoid generic dumping grounds such as:

```text
utils.py
common.py
helpers.py
```

unless their responsibility is genuinely well-defined.

---

# 21. Persistence Boundary

CD-2 must design models so they are persistence-ready.

However CD-2 should avoid prematurely building the complete production database platform.

Two acceptable approaches:

### Option A

Implement domain/contract models and a narrow in-memory/reference repository abstraction.

### Option B

Introduce a minimal PostgreSQL-backed persistence proof if it materially increases confidence in constraints and immutability semantics.

If PostgreSQL is introduced, it must be:

```text
bagman-db
```

and BAGMAN-specific.

No shared host PostgreSQL.

No Redis.

The choice must be justified in the delivery evidence.

The priority is correct domain semantics, not infrastructure volume.

---

# 22. Repository Interfaces

Where persistence interfaces are introduced, BAGMAN should use explicit repository/service contracts.

For example:

```text
EvidenceRepository
AuditRepository
EntityRepository
```

Core domain logic must not scatter SQL throughout unrelated services.

Likewise, future adapters must not write directly into database tables.

---

# 23. Initial Entity Registry

CD-2 shall establish canonical definitions for:

```text
NOUSTAI_LIMITED
INFOSECURS_LIMITED
MATTHEW_SCOTT_PERSONAL
```

These may be represented through safe committed configuration because their identities are not credentials.

Do not add sensitive company/account data merely because the entities exist.

Initial metadata should remain minimal.

---

# 24. Evidence Type Taxonomy

CD-2 should define an extensible evidence taxonomy.

Initial safe values may include:

```text
EMAIL
EMAIL_ATTACHMENT
DOCUMENT
INVOICE
RECEIPT
STATEMENT
CONTRACT
TAX_DOCUMENT
CORRESPONDENCE
API_PAYLOAD
USER_UPLOAD
GENERATED_DOCUMENT
UNKNOWN
```

Do not assume classification is always known when evidence enters BAGMAN.

`UNKNOWN` is legitimate.

Likewise, a generic `DOCUMENT` may later be reclassified.

Original observation history must remain visible.

---

# 25. Evidence Status

Evidence workflow state should remain distinct from evidence identity/type.

Potential statuses:

```text
OBSERVED
REGISTERED
AVAILABLE
SUPERSEDED
QUARANTINED
ERROR
```

Do not introduce financial statuses such as:

```text
PAID
RECONCILED
POSTED_TO_XERO
```

into the evidence object.

Those belong to later finance domains.

---

# 26. Upload Readiness

Because the future GUI will support document uploads, CD-2 models should support:

```text
source_type = MANUAL_UPLOAD
```

without requiring a future schema change.

CD-2 does not need to implement the production upload GUI.

Synthetic API/unit proof is sufficient.

---

# 27. Agent and Memory Fabric Interaction

The BAGMAN AI agent must not use unstructured memory as canonical truth.

CD-2 shall establish that:

```text
canonical domain state
        ↓
structured memory projection
        ↓
BAGMAN Agent
```

not:

```text
agent memory
        ↓
canonical financial truth
```

Memory Fabric may retain architectural context and summaries, but EvidenceItem, Entity and AuditEvent identities remain authoritative in canonical services/contracts.

CD-2 should document this boundary explicitly.

---

# 28. Generated Memory Projection

Where practical, CD-2 should provide a lightweight deterministic way to generate or update architecture-memory material from:

* component manifests
* contracts
* canonical entity definitions

This need not be sophisticated.

The goal is to prevent BAGMAN's architecture memory from drifting away from the source tree.

Do not introduce a vector database.

---

# 29. Security Doctrine

CD-1 security doctrine remains fully binding.

Specifically:

* repository remains treated as public
* no live credentials
* no mailbox passwords
* no OAuth tokens
* no bank credentials
* no real financial documents
* no customer-sensitive data
* no production database
* no production mailbox contents

Synthetic fixtures only.

Gitleaks and existing security tests remain mandatory.

CD-2 must not weaken any CD-1 control.

---

# 30. Synthetic Fixture Doctrine

CD-2 shall create realistic but obviously fabricated fixtures demonstrating:

* multiple governed entities
* unresolved entity ownership
* duplicate content from different sources
* external-provider references
* evidence provenance
* derived relationships
* audit causality
* schema/version validation

Suggested fictional parties:

```text
Example Systems Ltd
Synthetic Cloud Services Ltd
Test Customer Ltd
```

Do not reuse real BAGMAN suppliers/customers for convenience.

---

# 31. Required Contract Tests

At minimum, prove:

### Entity identity

* valid canonical entity accepted
* malformed entity rejected
* unsupported type rejected where appropriate
* immutable canonical identifier semantics

### Evidence

* evidence requires canonical identity
* unresolved entity is supported
* valid content hash accepted
* malformed hash rejected
* provider-specific metadata does not become canonical identity
* duplicate bytes can exist as distinct observations

### External references

* provider/source scope enforced
* canonical/external identity separation proven

### Provenance

* derived record can trace to evidence
* invalid/orphan provenance rejected
* multiple evidence sources supported

### Audit

* UTC-aware timestamps mandatory
* actor required
* correlation supported
* causation supported
* immutable/event-oriented semantics demonstrated

### Versioning

* contracts carry schema version
* incompatible structures fail validation

---

# 32. Required Architectural Tests

CD-2 should add tests proving boundaries where practical.

Examples:

* core domain code does not import provider adapters
* evidence service does not import banking/accounting/billing adapters
* agent code is not used as canonical domain authority
* contracts do not reference provider-specific implementation classes
* no Redis dependency introduced
* no direct third-party SDK dependency introduced in core

These tests may be static/import-based if appropriate.

---

# 33. Required API Surface

CD-2 may introduce a minimal internal API or service facade if useful.

Acceptable operations include:

```text
register_entity()
register_source()
register_evidence()
link_external_reference()
record_provenance()
record_audit_event()
get_evidence()
trace_provenance()
```

These are internal canonical operations.

Do not introduce external-provider endpoints.

A production HTTP API is not required unless FORGE can justify that it materially proves the architecture.

---

# 34. Idempotency

Future adapters will retry.

Therefore CD-2 must establish idempotency semantics.

Example:

The same:

```text
provider
source
resource_type
external_id
```

observed repeatedly must not blindly produce duplicate canonical records unless the domain explicitly allows separate observations.

Exact semantics should be documented per contract.

Evidence content duplication and event replay are not the same concept and must not be conflated.

---

# 35. Error Model

Introduce a small canonical error vocabulary where needed.

Examples:

```text
VALIDATION_ERROR
CONFLICT
NOT_FOUND
DUPLICATE_EXTERNAL_REFERENCE
INVALID_PROVENANCE
IMMUTABILITY_VIOLATION
```

Do not leak raw database/third-party exceptions as canonical behaviour.

---

# 36. Observability

Even at foundation level, operations should produce structured logs.

Requirements:

* UTC timestamps
* component identity
* correlation ID where available
* canonical object IDs
* no secrets
* no evidence content bodies by default

Do not log complete document contents or sensitive payloads.

CD-2 need not implement a full observability platform.

---

# 37. No Monolithic Core

`core/` must not become a miscellaneous BAGMAN dumping ground.

The intended meaning is:

> shared canonical primitives and rules that genuinely span domains.

If logic belongs to evidence, finance, tax, billing or another bounded service, put it there.

---

# 38. Explicitly Out of Scope

CD-2 SHALL NOT implement:

* Microsoft Graph connectivity
* Gmail connectivity
* IMAP connectivity
* mailbox polling
* mailbox credentials
* document OCR
* LLM invoice extraction
* supplier classification
* bank connectivity
* Revolut connectivity
* Starling connectivity
* bank reconciliation
* Xero connectivity
* Chargebee connectivity
* customer collections
* uSecure connectivity
* Huntress connectivity
* service suspension
* R&D qualification logic
* VAT logic
* corporation tax logic
* personal tax calculations
* HMRC submission
* production document upload GUI
* final visual GUI
* BAGMAN conversational AI
* autonomous financial action
* production secrets
* production data

References to these future capabilities are permitted only where required to prove extensibility.

---

# 39. FORGE Delivery Decomposition

FORGE should decompose CD-2 into small work items.

Recommended structure:

### WI-1 — Canonical contracts

Implement:

* common identity primitives
* entity schema
* source schema
* external-reference schema
* evidence schema
* provenance schema
* audit schema

### WI-2 — Domain implementation

Implement validation/domain models and repository interfaces.

### WI-3 — Component manifests and memory projection

Implement:

* component manifest schema
* manifests for actual CD-2 components
* deterministic architecture-memory projection

### WI-4 — Tests and evidence

Implement:

* contract tests
* architectural-boundary tests
* synthetic fixtures
* CD-1 regression/security tests
* delivery evidence

Parallelisation is permitted only where work items are genuinely independent.

---

# 40. Git Doctrine

All CD-1 Git discipline remains binding.

FORGE Engineers shall not independently alter repository history outside the established FORGE delivery workflow.

Required:

* dedicated CD-2 branch
* controlled worktrees
* PL review
* clean commits
* independent Auditor
* true integration evidence
* clean working tree
* no hidden/generated runtime material

---

# 41. CI

Existing Security workflow must remain green.

CD-2 shall extend CI to include:

```text
security tests
contract tests
domain tests
architectural-boundary tests
```

No live external dependency is permitted in CI.

No secret is required.

No Docker Hub/private registry credential is required merely to run tests.

---

# 42. Acceptance Criteria

CD-2 is complete only when the following are independently proven.

## Identity

* canonical BAGMAN IDs implemented
* governed entities implemented
* initial three entity identities represented
* unresolved ownership supported

## Evidence

* EvidenceItem contract implemented
* immutable-evidence semantics documented and tested
* content hashing implemented
* evidence identity separated from content identity
* duplicate-content/multiple-observation case proven

## Sources

* canonical Source implemented
* external references implemented
* provider identity isolated from canonical identity

## Provenance

* provenance relationships implemented
* evidence lineage query/proof exists
* derived-versus-original distinction tested

## Audit

* AuditEvent implemented
* UTC enforced
* actors represented
* correlation and causality represented
* audit events append-oriented/immutable by contract

## Contracts

* versioned machine-validatable schemas
* contract tests green
* malformed structures rejected

## Architecture

* component manifest schema implemented
* actual CD-2 components have manifests
* provider adapters are not imported into canonical core
* no Redis introduced
* no live third-party connectivity introduced

## Memory Fabric

* architecture memory can be deterministically generated/projected from authoritative manifests/contracts
* memory is explicitly non-authoritative for canonical finance/evidence state

## Security

* all CD-1 security tests remain green
* gitleaks remains green
* no real production data
* no secret required

## CI

* full CI green on delivery PR

## Git

* `git diff --check` clean
* delivery branch clean
* independent audit complete

---

# 43. Required Runtime Proof

Source inspection alone is insufficient.

FORGE must provide executable proof demonstrating at minimum:

```text
1. create synthetic governed entity
2. register synthetic source
3. register synthetic evidence
4. hash evidence
5. associate external reference
6. create derived provenance relationship
7. record audit events
8. trace evidence lineage end-to-end
9. reject malformed/invalid cases
10. repeat an idempotent observation safely
```

This may be through tests or a dedicated acceptance harness.

The output must be deterministic and retained in delivery evidence.

---

# 44. Audit Requirements

A fresh Auditor must independently review CD-2.

The Auditor must not rely solely on Engineer or PL statements.

It must independently:

* read this PID
* inspect contracts
* run full tests
* run gitleaks
* verify no forbidden integration exists
* verify no production data exists
* inspect import/dependency boundaries
* reproduce lineage proof
* reproduce immutability/idempotency checks
* inspect component manifests
* verify Memory Fabric projection is derived from authoritative metadata
* walk each §42 acceptance criterion individually

---

# 45. Verdict Vocabulary

The Auditor and PL shall use:

### FOUNDATION_MODEL_GREEN

All mandatory CD-2 acceptance criteria proven.

### FOUNDATION_MODEL_RED

One or more mandatory criteria failed.

### BLOCKED

Required proof cannot be completed because of an external condition.

Do not use generic "looks good" language as final acceptance.

---

# 46. Exit Gate

No live mailbox integration may begin until CD-2 reaches:

> **FOUNDATION_MODEL_GREEN**

When CD-2 is green, BAGMAN should possess a trustworthy internal representation for:

```text
WHO
what governed entity is involved

WHAT
what evidence or canonical object exists

WHERE FROM
what source produced it

HOW KNOWN
what provenance supports it

WHAT HAPPENED
what audit event occurred

WHY CONNECTED
what correlation/causation relationship exists
```

Only then should live evidence begin entering the platform.

---

# 47. Expected Next Delivery

Subject to successful CD-2 completion, the likely next delivery is:

> **CD-3 — BAGMAN Runtime & Evidence Store**

Expected scope would include:

* `bagman-db`
* BAGMAN-specific PostgreSQL
* object storage
* Docker Compose runtime
* migrations
* durable evidence persistence
* immutable document storage
* health/readiness
* backup foundation

Still with no live mailbox connectivity unless explicitly authorised by the next PID.

---

# 48. Product Principle

BAGMAN will eventually make consequential decisions about money, tax, customers and service access.

Before it can make those decisions, it must first know with certainty:

> **what exists, who it belongs to, where it came from, what happened to it, and why BAGMAN believes it.**

CD-2 exists to establish that truth foundation.
