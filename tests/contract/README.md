# tests/contract/

Contract tests for BAGMAN — verifying the interface boundaries defined in
`contracts/` are honoured by the components that implement them.

Empty scaffolding as of CD-1 (no contracts existed yet). As of CD-2 / WI-4,
this directory validates every `contracts/**/*.schema.json` file directly
(PID §31) — via `core.contract_validation.validate_against_contract`, never
through `core/`/`services/evidence/`'s domain classes — proving valid
instances are accepted, malformed instances are rejected, the deliberately
open taxonomy fields (`entity_type`, `source_type`, `provider`,
`evidence_type`, evidence `status`, audit `event_type`) remain open, the
deliberately closed fields (`provenance.relationship`, `audit_event.actor_type`)
remain closed, and schema/version identity holds (`test_schema_versioning.py`).
Domain-layer behaviour (immutability, idempotent replay, orphan-provenance
rejection, etc.) is proven separately in `tests/integration/`, not here.

`conftest.py` provides one instance-factory fixture per contract
(`make_entity`, `make_evidence`, `make_source`, `make_external_reference`,
`make_provenance`, `make_audit_event`) plus `new_id`/`now_str` value
generators.
