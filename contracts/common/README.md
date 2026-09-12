# contracts/common/

Reusable JSON Schema primitives shared across every BAGMAN domain contract.
These are not domain objects in their own right — they are the building
blocks every domain schema `$ref`s so that identity, time, and versioning
rules are defined exactly once and stay consistent across `entity/`,
`source/`, `evidence/`, `provenance/`, and `audit/`.

## Primitives

| File | Purpose |
|------|---------|
| `bagman.identifier.v1.schema.json` | BAGMAN's canonical opaque identifier: a lowercase UUIDv7 string (RFC 9562). Used for every `*_id` field on every canonical object (`entity_id`, `evidence_id`, `source_id`, `provenance_id`, `audit_event_id`, etc). Non-secret, immutable once assigned, safe for logs and URLs, independent of any external-provider identifier (PID §5). |
| `bagman.utc_timestamp.v1.schema.json` | BAGMAN's canonical timestamp: `type: string`, `format: date-time`. The `date-time` format already requires an RFC 3339 offset or `Z`, so a naive (timezone-unaware) datetime cannot be expressed by this schema at all — UTC is canonical everywhere a timestamp appears (PID §16). |
| `bagman.schema_version.v1.schema.json` | Pattern for a contract version identifier, `bagman.<domain>.v<N>` (e.g. `bagman.evidence.v1`). Used by any schema that needs to self-declare its own version, e.g. `AuditEvent.schema_version` (PID §17). |

## How other schemas reference these

Every domain schema pulls a primitive in with a plain `$ref` to that
primitive's `$id`, e.g.:

```json
"entity_id": {
  "$ref": "https://bagman.internal/contracts/common/bagman.identifier.v1.schema.json",
  "description": "..."
}
```

Under JSON Schema draft 2020-12, `$ref` may be combined with sibling
keywords (such as an object-specific `description`) in the same schema
object — both are applied. This lets each domain schema add
field-specific documentation on top of the shared primitive definition
without needing to redefine the primitive's `type`/`pattern`/`format`.

Where a field is a *nullable* reference to a primitive (e.g. an
unresolved `entity_id`, or a `causation_id` with no prior cause), the
domain schema composes the primitive with an explicit null branch using
`oneOf`, e.g.:

```json
"entity_id": {
  "oneOf": [
    { "$ref": "https://bagman.internal/contracts/common/bagman.identifier.v1.schema.json" },
    { "type": "null" }
  ]
}
```

This preserves the identifier's pattern validation for any non-null
value while still allowing an explicit, first-class `null`.
