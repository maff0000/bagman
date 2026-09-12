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
