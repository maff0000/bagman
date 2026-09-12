# contracts/manifest/

The JSON Schema contract for BAGMAN's component-manifest pattern
(PID §19): `bagman.component_manifest.v1.schema.json`.

## What a component manifest is

Any bounded BAGMAN component that genuinely exists as a top-level
implementation directory (as of CD-2: `core/`, `services/evidence/`) may
declare a `component.yaml` file at its root, describing:

```text
id                responsibility   owns
consumes          produces         dependencies
external_access   prohibited
```

Manifests are created only for components that genuinely exist —
never for illustrative or future components (PID §19). CD-2 ships
exactly two: `core/component.yaml` and `services/evidence/component.yaml`,
matching the two real top-level component directories WI-2 produced.
`contracts/`, `memory/`, `agent/`, `adapters/`, `ui/`, `deployment/`,
`config/`, `tests/`, and `ops/` remain scaffolding-only as of CD-2 (see
their own `README.md` files) and so do not get a `component.yaml` yet.

## Shape and validation

`bagman.component_manifest.v1.schema.json` validates the shape above.
All eight fields are required; `additionalProperties: false`. `owns`,
`consumes`, `produces`, `dependencies`, and `prohibited` are arrays of
strings and may be empty — a component that owns/consumes/produces/
depends on/prohibits nothing still declares the empty array explicitly,
rather than omitting the key.

Validation is performed by `scripts/generate_architecture_memory.py`,
which discovers every `component.yaml` in the repository by glob (not a
hardcoded list), validates each against this schema, and fails loudly
(non-zero exit) if any manifest is invalid. There is no separate
standalone validator script — the generator *is* the validator, run
either for real (writing `memory/generated/architecture-index.md`) or
with `--check` (drift detection only, no write). See
`scripts/generate_architecture_memory.py`'s module docstring and
`memory/architecture/README.md` for how the generated projection relates
to this manifest data.

## Why `id`/`version` do not `$ref` the `contracts/common/` primitives

`contracts/common/bagman.identifier.v1.schema.json` and
`bagman.schema_version.v1.schema.json` are `$ref`'d by every other
CD-2 domain contract (see `contracts/README.md`), but neither fits this
manifest's `id`/`version` fields, so this schema defines both directly
instead:

- **`id`** is a dotted, uppercase, human-authored, *static* component
  path (e.g. `BAGMAN.CORE`, `BAGMAN.SERVICES.EVIDENCE`) — it names a
  place in the source tree, not a runtime domain-object instance.
  `bagman.identifier.v1` is an opaque, non-business-meaningful lowercase
  UUIDv7 assigned to a runtime instance (an `EvidenceItem`, a
  `GovernedEntity`, ...) — the opposite of what `id` needs to express
  here; forcing a UUID onto a component manifest would make it useless
  as a stable, human-legible cross-reference in code review and in the
  generated architecture memory.
- **`version`** is a small local integer that increments when a
  component's declared shape changes — a plain
  `"type": "integer", "minimum": 1"` is simpler and says exactly what it
  means. `bagman.schema_version.v1` instead validates a *string* of the
  form `bagman.<domain>.v<N>` (e.g. `bagman.evidence.v1`), which is the
  convention for a versioned wire *contract* file name, not a component
  manifest's own revision counter — a manifest is not itself a
  versioned wire contract in that sense, so imposing that string shape
  here would be a category mismatch, not a genuine reuse.

This is a documented judgement call (per this work item's brief), not
an oversight: every other field on this schema that could sensibly
reuse a `contracts/common/` primitive already does not need to (none of
`responsibility`/`owns`/`consumes`/`produces`/`dependencies`/
`external_access`/`prohibited` correspond to an identifier, timestamp,
or schema-version shape), so there was no other `$ref` opportunity to
take here.
