# tests/integration/

Integration tests for BAGMAN — end-to-end tests exercising real component
wiring (services, adapters, datastore) rather than mocked boundaries.

Empty scaffolding as of CD-1 (no business capability or connectivity existed
yet). As of CD-2 / WI-4 this directory holds:

- `test_architecture_boundaries.py` — static/import-based architectural
  boundary tests (PID §32): `core/`/`services/evidence/` never import
  `adapters`/`agent`/`ui`; the one documented `core/api.py` ->
  `services.evidence` exception is exactly and only that one file/import;
  no Redis dependency anywhere; no SDK-shaped third-party import under
  `core/`; WI-1's open-vs-closed contract field design is locked in.
- `test_domain_and_lineage.py` — domain-behavior tests through
  `core.api.BagmanCanonicalAPI` (multiple governed entities, unresolved
  entity ownership + resolution + reassignment rejection, duplicate content
  from different sources, external-reference idempotency/conflict, evidence
  provenance/lineage tracing, orphan-provenance rejection, audit causality,
  domain-layer contract-validation wiring).
- `test_runtime_proof.py` — the PID §43 required runtime proof, as one
  ordered, printed story (`pytest -s tests/integration/test_runtime_proof.py -v`).
- `conftest.py` — shared `api` (a fresh `BagmanCanonicalAPI`), `utc_now`,
  and synthetic-invoice-fixture helpers used by the two files above.
