# BAGMAN PID v4 — Evidence Intake & Manual Upload Foundation

## 1. Product Identity

**Product:** BAGMAN
**Repository:** `github.com/maff0000/bagman`
**Authoritative working tree:** `/srv/bagman`
**Canonical CD-3 merge:** `82472516b7df00b1b0f9319b4ef95546543edcf8`
**Primary branch:** `main`

This PID authorises:

> **CD-4 — Evidence Intake & Manual Upload Foundation**

CD-4 establishes the single governed boundary through which external bytes may become canonical BAGMAN evidence.

Manual upload is the first authorised producer of that intake boundary.

No mailbox, bank, Xero, Chargebee or other live external provider may yet feed evidence into BAGMAN.

---

# 2. Delivery Objective

The objective is to prove:

> BAGMAN can safely accept an untrusted user-supplied file, inspect and validate it, preserve the original bytes, reject or quarantine unsafe material, create canonical evidence exactly once, retain complete provenance and audit history, and make the resulting evidence visible and downloadable through a controlled operator surface.

CD-4 shall establish:

* canonical evidence intake workflow
* intake identity and state
* manual upload producer
* upload limits
* filename safety
* MIME/content inspection
* evidence quarantine
* content-integrity validation
* duplicate/retry semantics
* intake audit trail
* intake provenance
* failure/recovery behaviour
* evidence listing and retrieval
* first BAGMAN Documents GUI surface
* runtime/CI acceptance proof

It shall **not** perform invoice extraction, OCR, financial classification or accounting.

---

# 3. Architectural Principle

All future evidence producers shall converge on one governed intake service.

Future:

```text
Manual Upload ─────┐
Microsoft Graph ───┤
Gmail ─────────────┤
IMAP ──────────────┤
Bank statement ────┤
External API ──────┤
                   ▼
          BAGMAN EVIDENCE INTAKE
                   │
                   ▼
        Validation / Quarantine
                   │
                   ▼
       Canonical Evidence Store
```

Not:

```text
Microsoft adapter → database
Gmail adapter     → object store
GUI upload        → evidence repository
```

The intake boundary is mandatory.

---

# 4. New Bounded Component

Introduce a bounded evidence-intake component.

Suggested identity:

```text
BAGMAN.EVIDENCE.INTAKE
```

Suggested placement:

```text
services/evidence/intake/
```

Responsibility:

> Accept untrusted evidence candidates and govern their transition into canonical BAGMAN evidence.

It shall not own:

* invoice extraction
* supplier classification
* reconciliation
* tax treatment
* accounting
* mailbox connectivity

---

# 5. Intake vs Canonical Evidence

An upload attempt is not automatically evidence.

Introduce an `IntakeRecord` or equivalent intake-domain object.

Conceptually:

```text
UNTRUSTED INPUT
      ↓
IntakeRecord
      ↓
validation / inspection / storage
      ↓
canonical EvidenceItem
```

This distinction is mandatory.

A rejected upload must not accidentally become a canonical `EvidenceItem`.

---

# 6. Intake Identity

Every intake attempt shall receive its own opaque BAGMAN identifier.

Example:

```text
intake_id
```

It is separate from:

```text
evidence_id
```

because:

* intake may fail
* intake may be quarantined
* intake may resolve to existing evidence
* intake may never produce evidence

Do not overload `EvidenceItem.evidence_id`.

---

# 7. Intake State Machine

CD-4 shall establish an explicit state machine.

Recommended states:

```text
RECEIVED
VALIDATING
QUARANTINED
REJECTED
ACCEPTED
REGISTERED
FAILED
```

A refined equivalent is acceptable.

State transitions must be deterministic and tested.

Example successful path:

```text
RECEIVED
   ↓
VALIDATING
   ↓
ACCEPTED
   ↓
REGISTERED
```

Potential hostile/unsupported path:

```text
RECEIVED
   ↓
VALIDATING
   ↓
QUARANTINED
```

Invalid request:

```text
RECEIVED
   ↓
REJECTED
```

Unexpected infrastructure failure:

```text
RECEIVED
   ↓
FAILED
```

Do not encode financial workflow states here.

---

# 8. Intake Persistence

Intake state must be durable in PostgreSQL.

A restart must not erase whether an upload:

* succeeded
* failed
* was rejected
* was quarantined
* produced evidence

Introduce migrations and a durable repository.

Suggested conceptual fields:

```text
intake_id
source_id
entity_hint
status
received_at
completed_at
original_filename
reported_mime_type
detected_mime_type
size_bytes
content_hash
evidence_id
failure_code
quarantine_reason
correlation_id
metadata
```

Avoid storing secret/sensitive content bodies in metadata.

---

# 9. Source Doctrine

Manual upload shall be represented through the existing canonical Source model.

Source type:

```text
MANUAL_UPLOAD
```

The implementation should not create a new Source for every file unless there is a genuine identity reason.

A stable manual-upload source for the BAGMAN operator/runtime is preferable.

Exact source lifecycle may be determined during implementation but must remain explicit and idempotent.

---

# 10. Entity Ownership

Manual upload may specify:

```text
NOUSTAI_LIMITED
INFOSECURS_LIMITED
MATTHEW_SCOTT_PERSONAL
```

or:

```text
UNRESOLVED
```

The upload interface must allow unresolved ownership.

BAGMAN must never force a guessed entity simply because the user uploaded from a particular screen.

If an entity is explicitly selected, that selection must be attributable to the actor.

---

# 11. Original Bytes

The exact original bytes received shall be preserved if the upload proceeds beyond initial rejection.

Do not:

* re-encode
* optimise
* rewrite PDFs
* rotate images
* modify metadata
* alter text encoding

before the original evidence is safely retained.

Derived/rendered versions may be created in later deliveries.

---

# 12. File Size Limits

CD-4 shall enforce configurable upload-size limits.

There must be:

* a request-level limit
* a per-file limit

The system must reject oversized uploads before allowing uncontrolled memory/disk consumption.

Do not rely solely on browser-side checks.

The backend is authoritative.

Reasonable defaults may be selected by FORGE and externalised through safe configuration.

---

# 13. Streaming Requirement

The current CD-3 route reads an entire uploaded file into memory.

That pattern is not acceptable as the long-term governed intake boundary.

CD-4 should use bounded streaming/spooling so an upload cannot consume arbitrary application memory.

The implementation may use:

* chunked hashing
* bounded temporary spool
* streaming object-store upload

provided integrity semantics remain correct.

Temporary material must be:

* bounded
* BAGMAN-owned
* non-canonical
* cleaned on success/failure
* never committed
* never silently treated as durable evidence

---

# 14. Filename Safety

Original filenames are untrusted metadata.

They may be retained for display, but must never control filesystem/object paths.

Reject or neutralise:

* `../`
* absolute paths
* NUL/control characters
* dangerous path separators
* pathological length
* ambiguous Unicode where materially unsafe

Canonical storage remains based on BAGMAN identity/hash, not filenames.

---

# 15. MIME Doctrine

Client-provided MIME type is a hint only.

BAGMAN shall distinguish:

```text
reported_mime_type
detected_mime_type
```

Detected content type should be derived from bytes where practical.

A mismatch must be observable.

Example:

```text
filename: invoice.pdf
reported: application/pdf
detected: application/x-executable
```

must not be quietly accepted as a PDF.

---

# 16. Initial Accepted Content Types

CD-4 should deliberately support a narrow initial set.

Recommended:

```text
application/pdf
image/jpeg
image/png
text/plain
text/csv
```

Potential office formats may be deferred.

Unknown binary formats should default to:

```text
QUARANTINED
```

or:

```text
REJECTED
```

according to policy.

Do not accept arbitrary formats simply because object storage can hold them.

---

# 17. Archive Doctrine

Compressed archives materially increase intake risk.

For CD-4:

```text
.zip
.rar
.7z
.tar
.gz
```

should not be recursively unpacked.

Preferred initial behaviour:

> reject or quarantine archives as unsupported evidence containers.

Archive extraction can be a future governed capability with explicit bomb/path-traversal protections.

---

# 18. Executable Content

Executable/script/binary-program content shall not become ordinary available evidence.

Examples:

```text
ELF
PE/EXE
shell scripts
JavaScript executables
installer packages
```

Where confidently detected, reject or quarantine according to policy.

The original filename extension must not override detected content.

---

# 19. Content Safety Scanner

CD-4 shall introduce a content-safety scanning abstraction.

Suggested:

```text
EvidenceSafetyScanner
```

Result concept:

```text
CLEAN
SUSPICIOUS
MALICIOUS
UNSUPPORTED
SCAN_ERROR
```

The intake service depends on the abstraction.

It must not hardcode antivirus implementation details into canonical evidence logic.

---

# 20. Malware Scanning

A practical initial malware scanning implementation is strongly preferred.

ClamAV is acceptable if FORGE can integrate it cleanly as a BAGMAN-owned dependency, e.g.:

```text
bagman-scan
```

However:

* scanning implementation is infrastructure
* scan verdict is not canonical financial truth
* scanner failure must not silently mean CLEAN

If the scanner cannot complete:

> intake must fail closed into `QUARANTINED` or equivalent.

Do not mark content safe because the scanner is unavailable.

---

# 21. Quarantine Doctrine

Quarantine is first-class.

Quarantined material:

* must not be presented as normal available evidence
* must not enter downstream extraction/classification workflows
* must retain intake/audit information
* may retain original bytes in a quarantine storage area if required for investigation
* must not be silently deleted without policy

Quarantine storage shall be logically distinct from normal evidence availability.

---

# 22. Storage Layout

Canonical available evidence continues using the CD-3 evidence store.

Quarantined objects should be separable, for example:

```text
quarantine/<intake_id>/<hash>
```

versus:

```text
evidence/<canonical-or-storage-id>/<hash>
```

Exact naming may differ.

Path safety and immutability requirements remain binding.

---

# 23. Content Hashing

SHA-256 remains canonical content hashing.

Hash must be computed from the exact original bytes.

Prefer computing incrementally during streaming.

The same computed hash must be used consistently for:

* intake record
* evidence registration
* object integrity
* duplicate detection

Do not hash a transformed representation.

---

# 24. Duplicate Content

Duplicate content is not automatically duplicate evidence.

CD-2 doctrine remains binding.

Two separate uploads of identical bytes may represent two distinct observations.

Therefore:

```text
same SHA-256
≠ automatically same EvidenceItem
```

However the UI/service should make duplicate content observable.

Example:

> "This content hash has been seen in 2 existing evidence records."

No automatic collapse without explicit policy.

---

# 25. Idempotent Upload Retry

A network/client retry of the **same intake request** must not accidentally create repeated canonical evidence.

Introduce an intake idempotency mechanism.

Examples:

```text
Idempotency-Key
```

or an equivalent canonical request key.

Scope should bind enough context to prevent accidental collision.

The same valid key replay must resolve to the same `IntakeRecord` and final result.

A conflicting reuse of a key with different content must fail loudly.

---

# 26. HTTP Intake API

Introduce a governed intake API.

Preferred route:

```text
POST /internal/intake/evidence
```

rather than continuing to make:

```text
POST /internal/evidence
```

the primary upload mechanism.

The legacy CD-3 direct route should either:

* be deprecated/removed, or
* internally delegate to the new intake service

It must not remain a bypass around governance.

---

# 27. Direct Evidence Write Bypass

This is a hard boundary.

Once CD-4 is complete:

> external/user-originated bytes may not directly invoke canonical evidence registration without passing through Evidence Intake.

`BagmanCanonicalAPI.register_evidence()` remains a canonical internal operation.

It is not itself the untrusted upload boundary.

---

# 28. Intake API Metadata

Manual upload should support safe metadata such as:

```text
entity_id | unresolved
evidence_type hint
original_filename
operator note
source
idempotency key
```

`evidence_type` supplied by the user is a hint/declared type.

CD-4 does not perform intelligent document classification.

---

# 29. Evidence Type

Manual upload may select/document:

```text
DOCUMENT
INVOICE
RECEIPT
STATEMENT
CONTRACT
TAX_DOCUMENT
CORRESPONDENCE
UNKNOWN
```

but this is an operator assertion.

It must be attributable and auditable.

Later classifiers may propose a different derived classification without rewriting original intake history.

---

# 30. Audit Events

CD-4 shall add structured audit events.

Expected events include equivalents of:

```text
INTAKE_RECEIVED
INTAKE_VALIDATION_STARTED
INTAKE_REJECTED
INTAKE_QUARANTINED
INTAKE_ACCEPTED
EVIDENCE_STORED
EVIDENCE_REGISTERED
INTAKE_COMPLETED
INTAKE_FAILED
```

Do not flood audit with meaningless implementation noise.

Audit should tell the operational story.

---

# 31. Causality

A single manual-upload workflow should share one correlation.

Example:

```text
INTAKE_RECEIVED
       ↓
INTAKE_VALIDATION_STARTED
       ↓
INTAKE_ACCEPTED
       ↓
EVIDENCE_STORED
       ↓
EVIDENCE_REGISTERED
       ↓
INTAKE_COMPLETED
```

with explicit `causation_id` links.

The resulting EvidenceItem must be traceable back to its IntakeRecord.

---

# 32. Provenance

CD-4 shall preserve:

```text
EvidenceItem
    ↓
Provenance / intake linkage
    ↓
Manual upload source
```

It must be possible to answer:

* who uploaded this?
* when?
* under what source?
* what filename was supplied?
* what content type was reported?
* what content type was detected?
* what validation occurred?
* was it scanned?
* which intake produced this evidence?

---

# 33. Actor Attribution

Manual uploads must identify the actor explicitly.

For current single-user/local operation, a governed actor identity may be used.

Do not hardcode every upload as generic:

```text
SYSTEM
```

Use actor semantics such as:

```text
USER / matt
```

or the appropriate canonical operator identity.

Authentication itself remains separately scoped unless required to safely prove the interface.

---

# 34. Intake Policy

Introduce explicit intake policy/config rather than scattered constants.

Policy should govern at minimum:

* max file size
* accepted MIME types
* quarantine MIME types
* rejected MIME types
* archive treatment
* scanner required/not optional
* scan failure treatment

Policy must be versionable or at least identifiable in audit.

---

# 35. Error Vocabulary

Extend canonical errors where needed.

Examples:

```text
INTAKE_REJECTED
INTAKE_QUARANTINED
FILE_TOO_LARGE
UNSUPPORTED_CONTENT_TYPE
CONTENT_TYPE_MISMATCH
MALWARE_DETECTED
SCAN_FAILED
IDEMPOTENCY_CONFLICT
INTAKE_PERSISTENCE_ERROR
```

Do not leak raw antivirus/object-store/framework exceptions to clients.

---

# 36. First Documents GUI

CD-4 shall introduce the first real BAGMAN GUI evidence surface.

This is **not** the complete BAGMAN GUI.

The purpose is to exercise the real intake workflow through a human-facing client.

Initial top-level shell should support at minimum:

```text
Overview
Documents
```

Other future tabs may be visible only if clearly marked unavailable, but avoid fake functionality.

---

# 37. Documents Screen

The Documents surface should provide:

* upload button/drop zone
* entity selection
* evidence-type hint
* upload status
* document list
* filters
* evidence ID
* entity/unresolved state
* type
* original filename
* received time
* status
* size
* hash/integrity indicator
* source
* download
* intake details

It should already feel like BAGMAN, not a developer demo.

---

# 38. Upload UX

Upload UX should show real workflow state.

Example:

```text
Uploading…
Validating…
Scanning…
Storing…
Registering…
Complete
```

or:

```text
Quarantined — unsupported executable content
```

Do not display an optimistic "Uploaded" before the backend has actually completed canonical registration.

---

# 39. Document Detail

A document detail view/panel should expose:

* canonical evidence ID
* intake ID
* entity
* original filename
* reported/detected MIME
* content hash
* size
* source
* intake state
* evidence state
* audit timeline
* provenance/source link
* download action

Raw object-store paths should not be primary operator-facing concepts.

---

# 40. GUI Business Logic Boundary

The GUI must contain no evidence business logic.

The browser shall not decide whether:

* content is safe
* file is accepted
* file is duplicate
* entity is valid
* upload is quarantined

It calls BAGMAN service APIs and renders authoritative results.

---

# 41. BAGMAN Terminal

The excellent BAGMAN terminal pattern may be represented in the initial shell if it already exists in the intended design reference.

However CD-4 must not implement a raw system shell.

If included, terminal commands must route through governed BAGMAN API/tool surfaces.

Acceptable early commands:

```text
documents
documents recent
intake status
evidence <id>
health
```

No unrestricted bash/host shell.

The Documents workflow remains the mandatory CD-4 GUI capability; terminal work must not derail it.

---

# 42. UI Runtime

If a separate UI runtime is introduced:

```text
bagman-ui
```

it must be BAGMAN-owned and Dockerised.

Do not introduce a Node development server as the production runtime architecture without justification.

The final technical framework may be selected by FORGE, but the application structure must remain feature-bounded and maintainable.

---

# 43. Authentication Scope

BAGMAN is currently a local/private development runtime.

A full production authentication/identity system is not required in CD-4.

However:

* UI/API must remain loopback/private by default
* no new public exposure
* actor identity must not be falsely represented as cryptographically authenticated if it is not

Document the distinction explicitly.

---

# 44. Intake Listing API

Provide governed read APIs sufficient for the GUI.

Potential examples:

```text
GET /internal/intake
GET /internal/intake/{intake_id}
GET /internal/evidence
GET /internal/evidence/{evidence_id}
GET /internal/evidence/{evidence_id}/content
```

Support useful filtering/pagination.

Do not design an unbounded "return entire evidence database" endpoint.

---

# 45. Pagination

Evidence and intake listings must be paginated from the outset.

Avoid a future rewrite caused by:

```text
SELECT * then send everything
```

Define stable ordering.

Recommended default:

```text
received_at DESC
```

with canonical ID as deterministic tie-breaker.

---

# 46. Search Scope

CD-4 does not need semantic search.

Basic metadata filtering is enough:

* entity
* intake status
* evidence type
* date
* filename text
* evidence ID

No vector database.

---

# 47. Download Semantics

Evidence download must continue to:

1. resolve canonical EvidenceItem
2. resolve storage reference
3. retrieve bytes
4. verify integrity
5. return original bytes

Set safe response headers.

User-supplied filenames must be safely encoded.

Do not allow header injection.

---

# 48. Browser Rendering Safety

Do not automatically render arbitrary uploaded HTML/SVG/script-capable content inline.

For supported documents:

* PDF/image preview may be deferred or handled conservatively
* downloads should prefer attachment semantics for risky types

CD-4 must not turn evidence viewing into an XSS/content-execution path.

---

# 49. Quarantine UI

Quarantined intake records must be visible in a clear operator state.

Show:

* quarantine reason
* detected type
* scan result
* timestamp

Do not provide "force accept" in CD-4 unless a governed override policy is explicitly designed and audited.

Prefer no override initially.

---

# 50. Intake Recovery

A failed infrastructure operation must be recoverable.

For example:

```text
bytes stored
metadata registration failed
```

CD-3 already acknowledges orphan storage possibility.

CD-4 should improve this by making such incomplete intake state explicitly discoverable.

At minimum:

* retain failed IntakeRecord
* identify whether object was stored
* allow safe retry/reconciliation
* do not silently create a second copy

A full garbage collector is not required.

---

# 51. Orphan Detection

Provide a narrow operational check/report for:

* intake record without evidence
* storage reference without completed intake where detectable
* evidence without intake linkage for CD-4 manual uploads

Legacy CD-3 synthetic evidence may legitimately predate the intake model and must not be falsely flagged as corruption.

---

# 52. Idempotency Across Restart

Idempotency must remain durable across restart.

Mandatory proof:

```text
submit upload with Idempotency-Key X
complete successfully
restart bagman-api
submit same bytes + metadata + key X
```

must resolve to the same intake/evidence result.

No duplicate evidence/audit story.

---

# 53. Idempotency Conflict

Mandatory proof:

```text
Idempotency-Key X + file A
then
Idempotency-Key X + file B
```

must fail with:

```text
IDEMPOTENCY_CONFLICT
```

or equivalent.

Never silently attach a reused request key to different content.

---

# 54. Concurrent Upload Race

Test at least one realistic concurrency race:

two simultaneous requests using the same idempotency key.

Expected:

* one canonical intake outcome
* one EvidenceItem
* deterministic replay response
* no duplicate observation audit

Database constraints must back the application logic.

---

# 55. Scanner Failure Proof

If malware/content scanning is implemented as a required service:

stop `bagman-scan` or otherwise make the scanner unavailable.

Upload must:

* not become normal AVAILABLE evidence
* fail closed
* produce explicit quarantine/failure state
* remain observable

No silent bypass.

---

# 56. Storage Failure Proof

Stop object storage during intake.

Upload must:

* fail visibly
* retain appropriate IntakeRecord state
* not create an EvidenceItem falsely claiming available bytes
* recover cleanly after dependency restoration

---

# 57. Database Failure Proof

Stop PostgreSQL during intake.

The service must fail loudly.

No local/in-memory fallback.

CD-3 invariant remains absolute.

---

# 58. Audit Failure Doctrine

Canonical audit and state changes that are part of one database transaction should not leave avoidable inconsistent state.

If a mandatory audit event cannot be recorded as part of a canonical transition, that transition should not silently be reported as successful.

Exact transaction boundaries must be documented.

---

# 59. Observability

Structured logs should add intake context:

```text
intake_id
evidence_id
correlation_id
component
state
size_bytes
detected_mime_type
```

Do not log:

* evidence bodies
* full document text
* secrets
* scanner signatures containing sensitive content

---

# 60. Runtime Health

If a scanner becomes mandatory, `/ready` must include it.

Production readiness should then represent:

```text
postgres
object_store
scanner
```

If scanner is optional by architecture, policy must state why.

For this delivery I prefer:

> scanner required for manual upload readiness.

---

# 61. Component Manifests

Create/update manifests for actual implemented components.

Likely:

```text
BAGMAN.EVIDENCE.INTAKE
BAGMAN.RUNTIME.UI
BAGMAN.RUNTIME.SCANNER
```

only if each actually exists.

Architecture-memory projection must remain drift-checked.

---

# 62. Security Doctrine

All previous security doctrine remains binding.

Additionally:

* files are hostile input
* filenames are hostile input
* MIME headers are hostile input
* multipart metadata is hostile input
* scanner output is advisory infrastructure output
* browser rendering is a security boundary

No production documents in Git.

Synthetic fixtures only.

---

# 63. Test Fixtures

Create synthetic files covering:

* valid PDF
* valid JPEG/PNG
* valid text
* empty file
* oversized synthetic stream
* extension/MIME mismatch
* suspicious unsupported binary
* archive
* filename path traversal attempt
* duplicate content
* idempotent retry
* idempotency conflict

Do not include real malware samples in the public repository.

For scanner behaviour, use safe standard test mechanisms such as scanner-supported synthetic signatures/fixtures if appropriate.

---

# 64. CI

Existing CI must remain green.

CI shall continue to run:

* gitleaks
* architecture-memory drift check
* security tests
* contract tests
* domain/integration tests
* PostgreSQL persistence tests
* app API runtime tests

Add CD-4 intake tests.

Resource-heavy Docker suites should remain isolated into separate pytest processes/jobs where needed, carrying forward the CD-3 lesson.

Do not assume local green means CI green.

Live CI must be observed.

---

# 65. GUI Testing

The Documents UI requires real browser-level acceptance evidence.

At minimum prove:

* app loads
* Documents screen renders
* upload valid document
* workflow state visibly progresses/completes
* evidence appears in list
* detail view opens
* download returns identical bytes
* quarantined/rejected file displays correct state

Unit tests alone are insufficient for the user-facing workflow.

---

# 66. Required End-to-End Acceptance Proof

FORGE must demonstrate against the real Docker runtime:

1. start BAGMAN runtime
2. health/readiness green
3. open Documents UI
4. upload synthetic valid PDF
5. create durable IntakeRecord
6. validate size/name/content
7. perform safety scan
8. compute SHA-256
9. store original bytes
10. register canonical EvidenceItem
11. complete provenance/audit chain
12. show evidence in GUI
13. download byte-identical original
14. restart application
15. show same intake/evidence still present
16. replay same idempotency key
17. prove same canonical outcome
18. upload conflicting same key/different bytes
19. prove conflict
20. upload unsupported/suspicious file
21. prove quarantine/rejection
22. stop scanner/storage/database in dedicated failure proofs
23. prove fail-closed behaviour
24. return runtime to healthy state
25. clean shutdown

---

# 67. Required Architecture Proof

Independently verify:

* GUI does not write DB directly
* GUI does not access object storage directly
* intake service owns intake workflow
* adapters do not exist yet for live email
* canonical core does not import scanner/UI/infrastructure code
* scanner implementation remains behind abstraction
* direct upload route cannot bypass intake
* Redis remains absent
* no provider-specific mailbox SDK introduced

---

# 68. FORGE Work Decomposition

Recommended:

### WI-1 — Intake contracts/domain/persistence

Implement:

* IntakeRecord
* state machine
* policy
* PostgreSQL persistence/migration
* intake audit semantics
* durable idempotency

### WI-2 — Content validation & quarantine

Implement:

* streaming/bounded ingest
* filename validation
* MIME detection
* file-size enforcement
* scanner abstraction
* scanner implementation
* quarantine path

### WI-3 — Intake API & canonical integration

Implement:

* governed intake endpoint
* evidence registration orchestration
* direct-route bypass closure
* listing/detail/download APIs
* recovery/error handling

### WI-4 — Documents GUI

Implement:

* initial BAGMAN shell
* Documents surface
* upload
* statuses
* list/detail
* download
* quarantine presentation

### WI-5 — Acceptance/hardening

Implement:

* concurrency/idempotency proof
* restart proof
* dependency failure proof
* browser acceptance
* CI
* evidence trail

Parallelisation only where dependencies genuinely permit it.

---

# 69. Explicitly Out of Scope

CD-4 SHALL NOT implement:

* Microsoft Graph
* Gmail
* IMAP
* mailbox polling
* automatic email attachment intake
* OCR
* PDF text extraction
* image OCR
* invoice field extraction
* supplier recognition
* accounting classification
* bank connectivity
* reconciliation
* Xero
* Chargebee
* customer billing
* tax calculations
* R&D classification
* HMRC submission
* AI agent reasoning
* semantic/vector search
* automatic document deletion
* user-driven quarantine override
* public internet exposure
* production SSO/authentication
* raw host shell terminal

---

# 70. Acceptance Criteria

CD-4 is complete only when independently proven:

## Intake

* intake has independent canonical identity
* state machine durable and valid
* manual upload enters only through intake
* retry/idempotency semantics durable
* concurrency race protected

## Validation

* size limits enforced server-side
* filenames treated as untrusted
* MIME detected from content where practical
* reported/detected mismatch visible
* archives treated according to policy
* executable/unsafe content not normally accepted

## Safety

* scanner abstraction exists
* required scanner fails closed
* quarantine durable and visible
* quarantined bytes cannot enter downstream normal evidence workflow

## Evidence

* original bytes preserved exactly
* SHA-256 verified
* canonical EvidenceItem created only after successful intake requirements
* provenance connects evidence to intake/source
* audit chain complete

## Recovery

* partial failure visible
* restart safe
* storage/database/scanner failure does not create false success

## API

* intake/list/detail/download governed
* pagination implemented
* direct upload bypass removed/delegated

## GUI

* Documents screen functional
* real upload works
* real statuses visible
* evidence list/detail works
* download byte-identical
* quarantine/rejection visible

## Architecture

* component boundaries respected
* no mailbox integration
* no Redis
* no canonical domain dependency on scanner/UI implementation

## Security

* gitleaks green
* no secrets
* no production data
* prior security controls unchanged

## CI

* all mandatory checks visibly green on live PR run
* architecture memory drift check visibly green
* intake suites genuinely executed, not skipped

---

# 71. Independent Audit

A fresh zero-context Auditor shall independently:

* read this PID
* inspect new schema/domain/migrations
* inspect intake state machine
* inspect API bypass controls
* inspect MIME/filename/size enforcement
* inspect scanner failure behaviour
* inspect quarantine semantics
* run all tests
* run gitleaks
* run architecture-memory drift check
* stand up real Docker runtime
* execute the end-to-end upload workflow
* independently retry/replay/conflict
* independently trigger scanner/storage/database failures
* inspect GUI in a browser
* download and hash-compare evidence
* verify no mailbox/provider integration exists
* inspect live CI result
* walk every §70 acceptance bullet individually

The Auditor shall not inherit Engineer reasoning.

---

# 72. Verdict Vocabulary

Use:

### INTAKE_FOUNDATION_GREEN

All mandatory CD-4 acceptance criteria independently proven.

### INTAKE_FOUNDATION_RED

One or more mandatory criteria failed.

### BLOCKED

External conditions prevent required proof.

No generic `GREEN`.

---

# 73. Exit Gate

No live mailbox integration may begin until CD-4 reaches:

> **INTAKE_FOUNDATION_GREEN**

Once green, every future mailbox adapter should have one simple responsibility:

> transform provider content into a governed BAGMAN intake request.

It shall not own evidence storage, scanning, classification, canonical IDs or accounting logic.

---

# 74. Expected Next Delivery

After CD-4, the likely next delivery is:

> **CD-5 — Mail Intake Adapters**

Expected providers:

* Microsoft Graph / Exchange
* Gmail
* IMAP

All feeding the proven CD-4 intake boundary.

CD-5 should not redesign evidence intake.

---

# 75. Product Principle

CD-2 established what evidence means.

CD-3 proved BAGMAN can keep it.

CD-4 establishes the front door.

The invariant is:

> **No untrusted byte becomes trusted BAGMAN evidence merely because it arrived. It must pass one governed, observable, fail-closed intake process first.**
