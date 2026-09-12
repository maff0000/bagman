# BAGMAN PID v3 — Runtime & Evidence Store

## 1. Product Identity

**Product:** BAGMAN
**Repository:** `github.com/maff0000/bagman`
**Authoritative working tree:** `/srv/bagman`
**Canonical CD-2 merge:** `2dd4714131bcee0062ea929ecaea2c5d253025bb`
**Primary branch:** `main`

This PID authorises:

> **CD-3 — BAGMAN Runtime & Evidence Store**

CD-3 turns the canonical in-memory foundation delivered in CD-2 into a durable BAGMAN-owned runtime.

It establishes:

* PostgreSQL persistence
* durable immutable evidence storage
* Docker Compose runtime
* migrations
* repository implementations
* health/readiness
* backup/restore foundations
* runtime observability
* durable restart/recovery proof

It does **not** authorise any live mailbox, banking, accounting, billing or SaaS-provider integration.

---

# 2. Delivery Objective

The objective is to prove:

> BAGMAN can persist canonical state and immutable evidence durably across container restarts, recover that state deterministically, and operate entirely inside BAGMAN-owned Docker infrastructure.

At the end of CD-3, BAGMAN should be capable of receiving a synthetic evidence item through its canonical API, persisting its metadata and binary content, shutting down completely, restarting, and proving that the same canonical evidence, provenance, audit trail and content remain intact.

---

# 3. Runtime Doctrine

BAGMAN is Docker-first.

All CD-3 runtime dependencies shall be BAGMAN-specific.

No shared runtime database, cache, object store or application service belonging to another project may be consumed.

Canonical operational naming shall follow:

```text
bagman-<role>
```

Initial runtime is expected to include:

```text
bagman-db
bagman-objects
bagman-api
```

A worker container may be introduced only if genuinely required by CD-3.

Do not create containers merely to mirror code directories.

---

# 4. Redis Doctrine

Redis remains out of scope.

CD-3 shall not introduce Redis.

PostgreSQL and normal application/runtime patterns are sufficient for this delivery.

Any future Redis introduction requires a separate architectural decision demonstrating need.

---

# 5. Required Docker Runtime

CD-3 shall provide a BAGMAN-controlled Compose runtime.

Preferred high-level topology:

```text
                 ┌─────────────────┐
                 │   bagman-api    │
                 └────────┬────────┘
                          │
             ┌────────────┴────────────┐
             │                         │
      ┌──────▼──────┐          ┌───────▼────────┐
      │  bagman-db  │          │ bagman-objects │
      │ PostgreSQL  │          │ object storage │
      └─────────────┘          └────────────────┘
```

All services must run on a BAGMAN-specific Docker network.

No database or object-storage port should be publicly exposed unless required for explicit development use.

Internal service-to-service networking is preferred.

---

# 6. Container Naming

Canonical Compose/service/container naming shall be clear and operational.

Use:

```text
bagman-db
bagman-objects
bagman-api
```

Future names may include:

```text
bagman-worker
bagman-ui
bagman-agent
bagman-notify
```

Do not use whimsical or ambiguous names.

---

# 7. Image Doctrine

Application images must be reproducible and versionable.

Do not use `latest` as canonical promotion identity.

The same BAGMAN application image shall eventually be promoted between environments, with only external configuration/secrets differing.

CD-3 should establish this pattern now.

The runtime should expose build/version metadata sufficient to associate a running container with:

* Git commit
* image identity
* application version where applicable

---

# 8. PostgreSQL

CD-3 shall introduce BAGMAN-owned PostgreSQL.

Container:

```text
bagman-db
```

PostgreSQL becomes the durable canonical structured datastore.

It must persist at minimum:

* governed entities
* sources
* external references
* evidence metadata
* provenance
* audit events

The persistence layer must preserve CD-2 semantics.

---

# 9. Database Ownership

Only BAGMAN persistence components may write BAGMAN canonical tables.

Future adapters must not directly write SQL.

Future GUI and AI agent components must not directly write SQL.

All canonical mutations must continue through BAGMAN domain/service/repository contracts.

---

# 10. Database Schema

The database schema must reflect the canonical contracts rather than redefine them arbitrarily.

Expected tables include concepts equivalent to:

```text
governed_entities
sources
external_references
evidence_items
provenance
audit_events
```

Exact names may differ if justified.

Provider-specific data should remain in bounded metadata or external-reference structures and must not contaminate canonical identity.

---

# 11. Database Constraints

Important domain invariants must be enforced in persistence where practical.

Examples include:

* primary canonical IDs unique
* external-reference tuple uniqueness
* foreign-key integrity
* append-only audit identity
* valid evidence/source relationships
* no accidental duplicate canonical records from replay
* entity ownership nullable only where unresolved is legitimate

Database constraints should complement, not replace, domain validation.

---

# 12. Migrations

CD-3 must introduce a formal migration mechanism.

Recommended:

```text
Alembic
```

or an equivalently justified migration system.

Requirements:

* migrations committed to Git
* ordered and deterministic
* no manual schema editing as normal operation
* migration version queryable
* clean database can reach current schema automatically
* existing database can upgrade without destructive reset

No schema creation hidden inside random application startup logic.

---

# 13. Persistence Repository Implementations

CD-2 repository interfaces must gain durable PostgreSQL implementations.

Expected examples:

```text
PostgresEntityRepository
PostgresSourceRepository
PostgresExternalReferenceRepository
PostgresEvidenceRepository
PostgresProvenanceRepository
PostgresAuditRepository
```

The in-memory implementations remain useful for fast tests.

Canonical service behaviour must not fork between in-memory and PostgreSQL variants.

---

# 14. Composition Root

CD-3 should introduce a clean runtime composition root that chooses repository implementations from configuration.

Conceptually:

```text
DEV/TEST
  -> InMemory repositories

RUNTIME
  -> PostgreSQL repositories
```

The domain layer must not contain environment checks scattered across files.

Do not spread:

```python
if ENV == "prod":
```

through canonical domain code.

---

# 15. Evidence Object Storage

CD-3 shall introduce BAGMAN-owned object storage for original evidence bytes.

Container:

```text
bagman-objects
```

Preferred implementation:

```text
MinIO
```

or another S3-compatible self-hosted store if objectively better.

The purpose is to provide durable immutable evidence storage behind an internal BAGMAN storage contract.

---

# 16. Storage Abstraction

Core/domain code must not depend directly on MinIO APIs.

Introduce an abstraction such as:

```text
EvidenceObjectStore
```

with operations conceptually equivalent to:

```text
put()
get()
exists()
verify_hash()
```

Provider-specific storage implementation lives behind that contract.

This allows later migration to another S3-compatible service or cloud object store without rewriting evidence semantics.

---

# 17. Evidence Immutability

Original evidence bytes are immutable.

Once stored under a canonical evidence record, BAGMAN must not silently overwrite the original object.

If a different object arrives, it is new evidence.

If corrected content arrives, it is new evidence.

Do not implement "replace file contents but keep the same evidence ID."

---

# 18. Content Addressing and Verification

Stored evidence must be verified against the canonical SHA-256 content hash.

Required pattern:

```text
bytes received
   ↓
SHA-256 calculated
   ↓
canonical EvidenceItem
   ↓
bytes stored
   ↓
retrieved bytes can be re-hashed
   ↓
hash equality proven
```

BAGMAN must detect corruption or mismatch.

Object-storage keys must not rely solely on user filenames.

The filename may be retained as metadata, but storage identity must use a safe canonical scheme.

---

# 19. Object Key Doctrine

Preferred structure may resemble:

```text
evidence/<evidence_id>/<content_hash>
```

or equivalent.

The exact form may be refined.

Requirements:

* safe
* deterministic
* no directory traversal
* no reliance on original filename
* no accidental overwrite
* canonical evidence ID visible or recoverable

---

# 20. Evidence Registration Transaction Semantics

CD-3 must explicitly handle the fact that metadata persistence and object storage span two systems.

Avoid creating silent half-valid evidence.

The system must define what happens if:

```text
database succeeds
object storage fails
```

or:

```text
object storage succeeds
database fails
```

A robust staged lifecycle is preferred.

For example:

```text
OBSERVED
  ↓
object stored + hash verified
  ↓
AVAILABLE
```

Failure may result in:

```text
ERROR
```

or:

```text
QUARANTINED
```

with a recoverable workflow.

Do not pretend cross-system atomicity exists where it does not.

---

# 21. Audit Persistence

Audit events must become durable.

They must remain append-oriented.

Normal application code must not expose:

```text
update_audit_event()
delete_audit_event()
```

as routine operations.

Database permissions or repository design should make audit mutation difficult by default.

---

# 22. Provenance Persistence

Provenance must survive restart and remain queryable.

The CD-2 lineage proof must work against PostgreSQL-backed repositories.

BAGMAN must still be able to trace:

```text
Derived object
    ↓
Provenance
    ↓
EvidenceItem
    ↓
Source
```

after a complete runtime restart.

---

# 23. Idempotency Persistence

The CD-2 idempotency guarantee must survive process restart.

This is mandatory.

Example:

```text
register evidence
shutdown BAGMAN
restart BAGMAN
retry same provider/source/resource/external-ID
```

must return the same canonical evidence identity.

It must not create:

* duplicate EvidenceItem
* duplicate ExternalReference
* duplicate `EVIDENCE_OBSERVED` audit event

This is one of the most important CD-3 acceptance proofs.

---

# 24. Runtime API

CD-3 may expose a minimal internal HTTP API around canonical operations.

This is encouraged if it materially proves containerised runtime behaviour.

Possible operations:

```text
GET  /health
GET  /ready
GET  /version

POST /internal/entities
POST /internal/sources
POST /internal/evidence
GET  /internal/evidence/{id}
GET  /internal/provenance/...
```

The exact API may be smaller.

This is not yet the final public/user-facing BAGMAN API.

No external integration endpoints are authorised.

---

# 25. Health and Readiness

Every long-running BAGMAN container must provide meaningful health/readiness behaviour.

For `bagman-api`:

### Liveness

Answers:

> Is the process alive?

### Readiness

Answers:

> Can BAGMAN currently perform its required work?

Readiness should fail if required dependencies such as PostgreSQL or object storage are unavailable.

A process that is alive but cannot reach its required persistence layer is not ready.

---

# 26. Docker Healthchecks

Compose should use actual healthchecks.

Startup ordering should rely on health/readiness where appropriate, not blind sleep statements such as:

```bash
sleep 10
```

Avoid race-condition-driven runtime startup.

---

# 27. Structured Logging

All application containers must use structured logs.

Minimum fields where applicable:

```text
timestamp_utc
level
component
message
correlation_id
canonical_object_id
event_type
```

Do not log:

* secrets
* credentials
* document bodies
* full email bodies
* object contents

Use identifiers rather than sensitive content wherever practical.

---

# 28. UTC

UTC remains mandatory everywhere.

Containers should operate in UTC.

PostgreSQL canonical timestamps must be timezone-aware.

Logs must be UTC.

No local-time assumptions are permitted in persistence.

---

# 29. Configuration

CD-1 three-layer configuration doctrine remains binding.

Committed configuration may include safe runtime structure.

Secrets remain outside `/srv/bagman`.

Examples:

```text
database password
MinIO root credentials
internal runtime credentials
```

must be mounted through external secret files or equivalent governed mechanism.

No secrets in Compose files.

No real `.env` committed.

---

# 30. Database Credentials

Database credentials should use secret-file loading where practical.

Example:

```text
/run/secrets/postgres_password
```

Do not hardcode credentials such as:

```text
postgres/postgres
```

for the canonical BAGMAN runtime merely because it is development.

Tests may use ephemeral credentials generated within isolated CI/runtime contexts if they are clearly non-production.

---

# 31. Object Storage Credentials

Object-store credentials are secrets.

They must follow the same external-secret doctrine.

No MinIO root password or access key may enter Git.

---

# 32. Network Exposure

Database and object storage should not be exposed publicly.

Preferred:

```text
bagman-api
   |
bagman internal Docker network
   |
bagman-db / bagman-objects
```

Host exposure for debugging should be opt-in and documented.

Production doctrine should remain closed by default.

---

# 33. Persistent Volumes

Containers are disposable.

Data is not.

Introduce BAGMAN-owned named volumes.

Expected:

```text
bagman-postgres-data
bagman-object-data
```

Exact naming may vary slightly under Compose project prefixes, but ownership must be explicit.

Destroying/recreating application containers must not destroy evidence or database state.

---

# 34. Backup Foundation

CD-3 must establish a real backup foundation.

At minimum:

### PostgreSQL

Provide a repeatable logical backup procedure.

For example:

```text
pg_dump
```

### Object store

Provide a repeatable evidence backup/snapshot/export procedure.

The exact production backup destination is not required in CD-3.

What is required is:

> deterministic backup and restore capability proven with synthetic data.

---

# 35. Restore Proof

Backup without restore proof is not accepted.

CD-3 must demonstrate:

1. create synthetic BAGMAN state
2. persist synthetic evidence bytes
3. create backup
4. destroy/recreate a clean runtime or clean persistence target
5. restore
6. verify all canonical IDs remain identical
7. verify evidence bytes remain identical
8. verify SHA-256 remains identical
9. verify provenance remains intact
10. verify audit trail remains intact

---

# 36. Database Migrations in Runtime

Runtime startup must not silently run destructive migrations without governance.

For development, automatic safe migration may be acceptable.

The mechanism must still be explicit and visible.

Future production operation should be able to separate:

```text
migrate
```

from:

```text
start application
```

Do not build migration behaviour that cannot later be controlled independently.

---

# 37. Initial Persistence Scope

CD-3 persists only CD-2 canonical domains:

* GovernedEntity
* Source
* ExternalReference
* EvidenceItem
* Provenance
* AuditEvent

Do not introduce finance, invoice, reconciliation, tax or billing tables yet.

Those belong to later domain deliveries.

---

# 38. GUI Scope

The production GUI remains out of scope.

CD-3 may expose a tiny diagnostic runtime page only if useful for health/runtime proof.

Do not start building the polished BAGMAN interface in this delivery.

---

# 39. Agent Scope

The BAGMAN AI agent remains out of scope.

No LLM integration.

No agent reasoning.

No autonomous financial behaviour.

Memory Fabric remains an architecture projection only at this stage.

---

# 40. External Integration Scope

Explicitly forbidden in CD-3:

* Microsoft Graph
* Gmail
* IMAP
* Starling
* Revolut
* Xero
* Chargebee
* uSecure
* Huntress
* HMRC
* any other live external provider

No credentials for those systems may be introduced.

---

# 41. No Production Evidence

All runtime demonstrations use synthetic data.

No real:

* invoice
* receipt
* statement
* email
* customer record
* tax document
* bank transaction

may enter CD-3 tests or runtime evidence.

---

# 42. Required Persistence Tests

At minimum:

### Entity persistence

* create entity
* restart repository/runtime
* same entity retrievable

### Source persistence

* create source
* restart
* source retained

### Evidence persistence

* store metadata
* store bytes
* restart
* same EvidenceItem retrieved

### Hash integrity

* stored object re-hashes correctly
* mismatched content rejected

### External reference

* uniqueness enforced
* replay resolves existing object
* conflicting target rejected

### Provenance

* lineage persists across restart

### Audit

* audit events survive restart
* correlation/causation retained

---

# 43. Required Runtime Restart Proof

FORGE must prove:

```text
docker compose up
        ↓
register synthetic entity/source/evidence
        ↓
persist evidence bytes
        ↓
record provenance/audit
        ↓
docker compose down
        ↓
docker compose up
        ↓
retrieve same canonical IDs
        ↓
verify same evidence hash
        ↓
verify same lineage/audit
```

This is mandatory.

A unit-test-only implementation is insufficient.

---

# 44. Required Container Rebuild Proof

FORGE must also prove data survives application container replacement.

At minimum:

```text
build bagman-api image
run
persist data
remove bagman-api container
recreate bagman-api
verify data remains
```

Persistence must belong to durable volumes/services, not application container filesystem layers.

---

# 45. Required Failure Proof

CD-3 must demonstrate BAGMAN behaves correctly when dependencies fail.

At minimum:

### PostgreSQL unavailable

* API not ready
* no silent fallback to in-memory persistence

### Object storage unavailable

* evidence cannot become falsely AVAILABLE
* failure is visible/audited/logged

### Hash mismatch

* rejected
* evidence not silently accepted

There must be no "best effort" silent data loss.

---

# 46. No Hidden Fallback

This is a hard invariant.

If runtime is configured for PostgreSQL:

> database failure must not cause BAGMAN to silently switch to in-memory repositories.

If runtime is configured for object storage:

> storage failure must not cause BAGMAN to silently store evidence on arbitrary local disk.

Fail loudly.

---

# 47. Object Download Proof

CD-3 shall expose or test a governed evidence retrieval operation.

Given:

```text
evidence_id
```

BAGMAN must be able to:

* locate canonical metadata
* locate stored object
* retrieve bytes
* verify hash

This becomes the foundation for future GUI download functionality.

---

# 48. Manual Upload Readiness

Although the GUI upload surface is out of scope, the runtime should be structurally ready for future:

```text
MANUAL_UPLOAD
```

sources.

A synthetic upload path/harness may be used for acceptance.

No browser UI is required.

---

# 49. Runtime Version Endpoint

If an API is introduced, include:

```text
/version
```

or equivalent.

It should expose safe information such as:

```text
git_commit
build_version
schema_migration_version
runtime_environment
```

No secrets.

This will later be useful in the Operations GUI.

---

# 50. Operational Commands

Provide clear documented commands for:

```text
start
stop
status
logs
migrate
backup
restore
test
```

These may be scripts or Make targets.

Avoid obscure multi-line commands that operators must reconstruct manually.

---

# 51. `/srv/bagman` Organisation

CD-3 must preserve directory clarity.

Expected areas:

```text
deployment/docker/
deployment/compose/
deployment/environments/
ops/
scripts/
services/evidence/
core/
```

Do not scatter Dockerfiles and operational scripts randomly at repository root.

---

# 52. Component Manifests

Update or introduce manifests for actual runtime components only.

Examples may include:

```text
BAGMAN.RUNTIME.API
BAGMAN.RUNTIME.POSTGRES
BAGMAN.RUNTIME.OBJECTS
```

if they are genuinely implemented as governed components.

Memory projection must remain drift-checked.

---

# 53. Architecture Boundary

Persistence implementations may depend on PostgreSQL/object storage clients.

Canonical contracts/domain models must not.

Provider-specific infrastructure code belongs in infrastructure/runtime layers.

Do not import MinIO/PostgreSQL SDKs directly into canonical entity/evidence model modules.

---

# 54. SQL Discipline

Do not scatter handwritten SQL across the project.

Use an explicit persistence layer.

If SQLAlchemy is chosen, use it consistently.

If a lighter database layer is chosen, it must still preserve repository boundaries and migration discipline.

The exact library choice may be made by FORGE if justified.

---

# 55. Transaction Boundaries

Database writes that form one canonical operation should use explicit transactions.

For example:

```text
EvidenceItem
+
ExternalReference
+
AuditEvent
```

where appropriate should not leave avoidable partial database state.

Object-store operations remain separate and require the staged semantics defined earlier.

---

# 56. Audit of Storage Operations

Material evidence storage events should produce audit events.

Examples:

```text
EVIDENCE_STORED
EVIDENCE_AVAILABLE
EVIDENCE_STORAGE_FAILED
EVIDENCE_HASH_VERIFIED
```

Exact taxonomy may be refined.

Keep events structured and safe.

---

# 57. Error Handling

Persistence failures must map into BAGMAN canonical errors.

Examples:

```text
PERSISTENCE_ERROR
STORAGE_ERROR
INTEGRITY_ERROR
CONFLICT
NOT_FOUND
```

Do not leak raw psycopg/SQLAlchemy/MinIO stack details to callers as the public contract.

Detailed internal logs may retain technical cause safely.

---

# 58. Security

All CD-1 and CD-2 security doctrine remains binding.

Existing gitleaks and security tests must continue to pass.

Add security tests where necessary for:

* Compose secret use
* no credentials in committed deployment files
* no default production passwords
* no unintended public database exposure
* no production data fixtures

---

# 59. CI

CI shall continue to run:

* gitleaks
* full existing test suite
* architecture-memory drift check

CD-3 should additionally prove persistence/runtime behaviour.

Where practical, CI may start ephemeral PostgreSQL/object-storage services.

Do not require real secrets.

Synthetic ephemeral credentials are acceptable.

Docker-based acceptance may run in CI if stable.

---

# 60. Required Acceptance Harness

Create a deterministic CD-3 acceptance path that proves:

1. build/start BAGMAN runtime
2. migrations apply successfully
3. health/readiness green
4. register synthetic governed entity
5. register synthetic source
6. store synthetic evidence bytes
7. create canonical EvidenceItem
8. external reference persisted
9. provenance persisted
10. audit persisted
11. evidence downloaded and hash verified
12. stop runtime
13. restart runtime
14. prove same canonical IDs/state
15. repeat same external observation
16. prove idempotency
17. backup state
18. restore into clean target
19. prove same IDs/bytes/hash/lineage
20. clean shutdown

---

# 61. Expected Work Items

Recommended FORGE decomposition:

### WI-1 — PostgreSQL persistence

* database schema
* migrations
* durable repositories
* persistence tests

### WI-2 — Object evidence store

* object-store abstraction
* MinIO/S3 implementation
* hash verification
* immutable storage semantics

### WI-3 — Container runtime

* bagman-api
* bagman-db
* bagman-objects
* Docker/Compose
* health/readiness
* version metadata
* config/secrets

### WI-4 — Backup/restore and runtime acceptance

* backup
* restore
* restart proof
* dependency failure proof
* end-to-end acceptance evidence

Sequence should follow real dependencies.

---

# 62. Explicitly Out of Scope

CD-3 SHALL NOT implement:

* mailbox access
* Microsoft Graph
* Gmail
* IMAP
* invoice extraction
* OCR
* AI document classification
* bank connectivity
* Revolut
* Starling
* reconciliation
* Xero
* Chargebee
* customer management
* dunning
* uSecure
* Huntress
* tax calculation
* R&D logic
* HMRC submission
* production GUI
* BAGMAN terminal
* BAGMAN AI agent
* notification delivery
* real credentials
* real evidence

---

# 63. Acceptance Criteria

CD-3 is complete only when independently proven:

## Runtime

* BAGMAN Docker Compose runtime starts cleanly
* BAGMAN-specific network/services used
* `bagman-db`, `bagman-objects`, and application runtime correctly isolated
* no Redis
* health/readiness meaningful

## PostgreSQL

* migrations reproducible
* CD-2 canonical state durable
* repository contracts preserved
* uniqueness/foreign-key constraints correct

## Evidence storage

* evidence bytes stored durably
* original bytes immutable
* SHA-256 validated
* content retrieved by canonical identity
* no filename-based unsafe storage

## Restart

* canonical IDs survive full runtime restart
* lineage survives
* audit survives
* bytes/hash survive

## Idempotency

* replay after restart returns same EvidenceItem
* no duplicate external reference
* no duplicate observation audit event

## Failure behaviour

* DB failure causes not-ready/fail-loud behaviour
* storage failure does not falsely mark evidence AVAILABLE
* hash mismatch rejected
* no in-memory/local-disk silent fallback

## Backup/restore

* synthetic backup succeeds
* restore into clean environment succeeds
* canonical IDs preserved
* content bytes/hash preserved
* provenance and audit preserved

## Security

* secrets external
* Compose contains no real credentials
* no production data
* gitleaks green
* all previous security tests green

## Architecture

* domain models do not depend directly on PostgreSQL/MinIO
* persistence behind repository/storage interfaces
* memory projection updated and drift-check green

## CI

* full test suite green
* runtime/persistence proof green
* live PR CI green

---

# 64. Audit Requirements

A fresh independent Auditor shall reproduce the acceptance evidence.

The Auditor must independently:

* inspect Docker topology
* inspect network/port exposure
* inspect secret handling
* run migrations from clean state
* start runtime
* run acceptance harness
* restart runtime
* reproduce persistent idempotency
* stop object storage and verify fail-loud behaviour
* stop PostgreSQL and verify readiness failure
* verify hash-mismatch rejection
* independently perform backup/restore
* rerun gitleaks
* run full tests
* check no Redis dependency exists
* walk every §63 acceptance item

---

# 65. Verdict Vocabulary

Use:

### RUNTIME_FOUNDATION_GREEN

All mandatory CD-3 requirements independently proven.

### RUNTIME_FOUNDATION_RED

One or more mandatory requirements failed.

### BLOCKED

External conditions prevent required proof.

No generic "looks good" verdict.

---

# 66. Exit Gate

No live mailbox may connect until CD-3 reaches:

> **RUNTIME_FOUNDATION_GREEN**

At that point BAGMAN must have proven:

> canonical truth survives processes, containers and restarts; original evidence bytes are durably preserved; provenance and audit remain intact; and the entire runtime can be recovered from backup.

---

# 67. Expected Next Delivery

If CD-3 completes cleanly, the likely next delivery is:

> **CD-4 — Evidence Intake & Manual Upload Foundation**

That should likely introduce:

* governed upload API
* document ingestion workflow
* evidence intake state machine
* safe file validation
* quarantine
* first GUI evidence/document surface

Then live mailbox ingestion can follow once the intake path itself is mature.

This is preferable to making Microsoft or IMAP the first real source of evidence.

---

# 68. Product Principle

CD-2 taught BAGMAN what evidence **means**.

CD-3 must prove BAGMAN can **keep it safely**.

The architectural invariant is:

> **BAGMAN must never claim to know something unless the canonical record, original evidence, provenance and audit trail can survive the death and reconstruction of the application runtime.**
