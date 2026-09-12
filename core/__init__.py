"""BAGMAN core domain package.

Shared canonical primitives and rules that genuinely span domains
(PID §37): identifier generation, UTC time handling, the canonical
error vocabulary, the actor-type vocabulary, contract validation, and
the canonical domain models (GovernedEntity, Source, ExternalReference,
Provenance, AuditEvent) plus their in-memory reference repositories.

`core/` must not import from `services/`, `adapters/`, `agent/`, `ui/`,
or any provider SDK (PID §32/§37) — it depends only on the Python
standard library, `jsonschema`, and `rfc3339-validator`.
"""
