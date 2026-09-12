# Changelog

All notable changes to BAGMAN will be documented in this file.

## 2026-09-12 — CD-1 — Foundation Security Scaffold

- Established repository structure (component and directory skeleton).
- Established security doctrine (public-repository invariant, external
  secret handling, secret consumption model).
- Established configuration contract (three-layer model).
- Added security test suite (`tests/security/`, PID §15 Test A-E) and
  CI secret-scanning gate (`.github/workflows/security.yml`).
- No business capability implemented.
- **Verdict: FOUNDATION_GREEN** (independent Auditor review + PL
  spot-check, commit `7f60a6c`). See
  `memory/generated/CD1-FOUNDATION-SECURITY-SCAFFOLD-EVIDENCE-2026-09-12.md`
  for the full evidence trail.

## 2026-09-12 — CD-2 — Canonical Entity, Evidence, Provenance & Audit Foundation

- Canonical JSON Schema contracts (entity, evidence, source,
  external_reference, provenance, audit_event, plus common identity/
  timestamp/schema-version primitives).
- Domain models + in-memory repositories (`core/`, `services/evidence/`)
  and the `BagmanCanonicalAPI` facade — UUIDv7 identity, immutable
  evidence with two narrow controlled mutations, idempotent external-
  reference/evidence-observation handling, append-only audit events
  with correlation/causation.
- Component manifest schema + manifests for `core/` and
  `services/evidence/` + a deterministic architecture-memory projection
  (`scripts/generate_architecture_memory.py`, with drift detection).
- Contract, architectural-boundary, and domain/lineage integration
  tests, synthetic fixtures, and the PID §43 10-step runtime proof
  (102 tests total).
- CI extended to run the full suite; pytest rootpath fixed generally
  via `pyproject.toml`.
- No mailbox, bank, accounting, or billing connectivity implemented.
- **Verdict: FOUNDATION_MODEL_GREEN** (independent Auditor review + PL
  spot-check, commit `890c58b`). See
  `memory/generated/CD2-CANONICAL-ENTITY-EVIDENCE-PROVENANCE-AUDIT-EVIDENCE-2026-09-12.md`
  for the full evidence trail.
