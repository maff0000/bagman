# memory/architecture/

Owns durable institutional memory about BAGMAN's own architecture —
decisions, doctrine, and rationale that should outlive any single delivery.

## Authority boundary: canonical state vs. memory projection (PID §27)

This is CD-2's explicit statement of the boundary PID §27 requires:

```text
canonical domain state
        ↓
structured memory projection
        ↓
BAGMAN Agent
```

**Canonical domain state** — the authoritative truth for
`GovernedEntity`, `Source`, `ExternalReference`, `EvidenceItem`,
`Provenance`, and `AuditEvent` identities — lives only in:

* the JSON Schema contracts under `contracts/` (the wire shape), and
* the domain implementation and repositories under `core/` and
  `services/evidence/`, reached through the single
  `core.api.BagmanCanonicalAPI` facade.

**Everything under `memory/`** — including the generated
`memory/generated/architecture-index.md` produced by
`scripts/generate_architecture_memory.py` from the `component.yaml`
manifests, the `contracts/` schemas, and `config/base/entities.yaml`
(PID §28) — is a **projection**: a human/agent-readable summary
*derived from* the authoritative source, regenerated deterministically
from it, and never itself a system of record. A generated projection
can go stale relative to the tree the moment either side changes
without the other being regenerated; `--check` mode exists precisely so
this can be caught mechanically rather than trusted blindly.

The reverse direction — an agent treating its own memory (Memory
Fabric, this directory, or the generated index) as canonical financial
or evidential fact — is explicitly not permitted:

```text
agent memory
        ↓
canonical financial truth
```

A future BAGMAN AI agent must read canonical entity/evidence/audit
state through `core.api.BagmanCanonicalAPI` (or the repositories it
composes), never by treating anything under `memory/` — generated or
hand-authored — as authoritative for those identities. Memory Fabric
and this directory may retain architectural context, doctrine, and
summaries, but not `EvidenceItem`, `GovernedEntity`, or `AuditEvent`
identity itself.

No further implementation beyond this generated-projection mechanism
exists yet as of CD-2 (see `PID.md` §16, §27-28).
