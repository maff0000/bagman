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

### `BAGMAN.PERSISTENCE.POSTGRES` (v1)

Own durable, PostgreSQL-backed implementations of every CD-2/CD-4 repository interface (GovernedEntity, Source, ExternalReference, EvidenceItem, Provenance, AuditEvent, IntakeRecord) plus the SQLAlchemy engine/session factory and Alembic migration schema — preserving exactly the same canonical behaviour (immutability, idempotent external-reference/evidence-observation/intake replay, append-only audit) as the in-memory reference implementations core/ and services/evidence/ ship, durably.

- **Owns:** _(none)_
- **Consumes:** `BAGMAN.CORE`, `BAGMAN.SERVICES.EVIDENCE`, `BAGMAN.EVIDENCE.INTAKE`
- **Produces:** _(none)_
- **Dependencies:** `SQLAlchemy`, `psycopg`, `alembic`
- **External access:** `false`
- **Prohibited:** `direct_email_access`, `direct_bank_access`, `direct_xero_access`, `direct_chargebee_access`

### `BAGMAN.RUNTIME.API` (v3)

Own the FastAPI/Uvicorn HTTP-facing application layer that exposes BagmanCanonicalAPI as a runnable, containerised service: health/ readiness (with a hard no-fallback invariant on readiness failure, and — since CD-4 WI-3 — a mandatory content-safety scanner reachability check alongside PostgreSQL/object-store), version metadata, structured logging, thin internal HTTP wrappers around register_entity/register_source/get_evidence/list_evidence/ trace_provenance, and — CD-4 WI-3 — the governed Evidence Intake HTTP API (POST /internal/intake/evidence, GET /internal/intake[/{id}]): evidence-registration orchestration (staging bytes into canonical storage, registering the EvidenceItem, and the full intake audit causation chain) once WI-2's content-validation pipeline reaches ACCEPTED, plus closure of the CD-3 direct-upload bypass (the old byte-accepting POST /internal/evidence has been removed entirely). Also owns the PID §14 composition root — the one place BAGMAN_RUNTIME_ENV is read to choose between in-memory and PostgreSQL+MinIO+ClamAV-backed repositories/object-store/scanner — and the stable MANUAL_UPLOAD Source resolve-or-create lifecycle (PID §9). CD-4 WI-4 additionally owns serving the first BAGMAN Documents GUI (PID §36-42) as plain static HTML/CSS/vanilla-JS assets (app/api/static/), mounted at "/" via Starlette's StaticFiles — no separate `bagman-ui` runtime/container was introduced (PID §42's "may be selected by FORGE" framework decision: the simplest option that needs zero new infrastructure), so this manifest is deliberately NOT split into a distinct BAGMAN.RUNTIME.UI component; the GUI is judged to genuinely be part of bagman-api's own delivery boundary, not a separate bounded component, since it introduces no new process, dependency surface, or deployment unit of its own. The GUI itself owns/decides no evidence business logic (PID §40) — it is a pure client of the very API routes this same manifest already describes.

- **Owns:** _(none)_
- **Consumes:** `BAGMAN.CORE`, `BAGMAN.SERVICES.EVIDENCE`, `BAGMAN.EVIDENCE.INTAKE`, `BAGMAN.PERSISTENCE.POSTGRES`, `BAGMAN.PERSISTENCE.OBJECTS`
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
