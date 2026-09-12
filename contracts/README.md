# contracts/

Cross-component interface contracts for BAGMAN — the explicit boundaries
that let `services/`, `adapters/`, `agent/`, and `ui/` interoperate without
depending on each other's internals.

As of CD-2 / WI-1, this directory holds the canonical JSON Schema
(draft 2020-12) contracts for BAGMAN's foundation model: governed
entities, sources, external references, evidence, provenance, and audit
events (PID §4-18). These are contracts only — no domain/Python
implementation lives here yet (that is WI-2).

## Directory layout

```
contracts/
├── common/       reusable primitives ($ref'd by every domain schema below)
├── entity/       GovernedEntity
├── source/       Source, ExternalReference
├── evidence/     EvidenceItem
├── provenance/   Provenance
└── audit/        AuditEvent
```

This layout is a deliberate interpretation of the PID §18 suggested
layout (`entity/ evidence/ source/ provenance/ audit/ common/`).

### Why `ExternalReference` lives under `source/`

The PID's suggested top-level layout (§18) does not list a separate
folder for `ExternalReference` — only `entity/`, `evidence/`, `source/`,
`provenance/`, `audit/`, and `common/` are named. But PID §9 (`Source`)
and §10 (`ExternalReference`) are introduced back-to-back, and §10 opens
by saying external identifiers "must be represented separately from
canonical IDs" and that "provider-specific identity belongs underneath
the source model" (§9) — i.e. `ExternalReference` is conceptually the
mechanism by which provider-specific identity attaches to a `Source`.
Rather than inventing an unlisted seventh top-level folder for a single
schema, `bagman.external_reference.v1.schema.json` is placed alongside
`bagman.source.v1.schema.json` under `contracts/source/`, since both
schemas exist to keep provider/external identity isolated from BAGMAN's
canonical identity model, and neither can be understood without the
other. This placement decision is documented here, in the
`ExternalReference` schema's own top-level `description`, and in the
WI-1 delivery report.

## Versioning convention

Every contract file follows `bagman.<domain>.v<N>.schema.json`, matching
the `schema_version` string convention from PID §17
(e.g. `bagman.evidence.v1`, `bagman.audit_event.v1`, `bagman.entity.v1`).

Rules:

- One JSON file per version. A shipped version is never edited in place.
- A backward-compatible addition (e.g. a new optional field) may be made
  within the current version file only if it does not change the meaning
  of any existing field or make a previously-valid instance invalid.
- Any breaking/incompatible semantic change (removing/renaming a
  required field, narrowing a type, changing a pattern to reject
  previously-valid values, etc.) ships as a new version file
  (`bagman.<domain>.v2.schema.json`) alongside the prior version, or is
  accompanied by a documented migration. The prior version file is left
  untouched.
- `common/` primitives follow the same rule: `bagman.identifier.v1`,
  `bagman.utc_timestamp.v1`, `bagman.schema_version.v1` would each gain a
  `v2` sibling rather than being changed in place.

## `common/` primitives and `$ref`

Every domain schema below composes the shared primitives in
`contracts/common/` via `$ref` (identifier shape, UTC timestamp shape,
schema-version shape) rather than redefining `type`/`pattern`/`format`
locally. See `contracts/common/README.md` for the full list of
primitives and the exact `$ref`/nullable-`$ref` conventions used.

## Schema index

| File | Canonical object | One-line description |
|------|-------------------|------------------------|
| `common/bagman.identifier.v1.schema.json` | — | Canonical opaque BAGMAN identifier (lowercase UUIDv7). |
| `common/bagman.utc_timestamp.v1.schema.json` | — | Canonical UTC, offset-aware timestamp. |
| `common/bagman.schema_version.v1.schema.json` | — | `bagman.<domain>.v<N>` contract-version identifier string. |
| `entity/bagman.entity.v1.schema.json` | `GovernedEntity` | A stable BAGMAN identity (company/person/etc) that financial and evidential records are ultimately associated with, or explicitly left unresolved. |
| `source/bagman.source.v1.schema.json` | `Source` | Where information originated (mailbox, bank, upload, platform, ...), isolating provider identity from canonical identity. |
| `source/bagman.external_reference.v1.schema.json` | `ExternalReference` | Maps a raw provider-native ID to a BAGMAN canonical object, with composite (`provider`+`source_id`+`resource_type`+`external_id`) uniqueness, never global uniqueness. |
| `evidence/bagman.evidence.v1.schema.json` | `EvidenceItem` | Something BAGMAN has observed or received; immutable original evidence, content-hash separate from identity, explicit unresolved-entity support. |
| `provenance/bagman.provenance.v1.schema.json` | `Provenance` | A single-evidence lineage edge from a canonical subject back to the one `EvidenceItem` it traces to. |
| `audit/bagman.audit_event.v1.schema.json` | `AuditEvent` | An immutable, actor-attributed, correlation/causation-aware record of something that happened inside BAGMAN. |
