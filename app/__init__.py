"""``app/`` — BAGMAN's HTTP-facing application layer (CD-3 WI-3).

Distinct from ``core/`` (canonical domain models), ``services/``
(domain services composing ``core/``), and ``persistence/`` (durable
repository/object-store implementations): everything under
``app/`` is *application*, not *domain*, code — it wires the
canonical facade to an ASGI HTTP server and to real infrastructure
(PostgreSQL, MinIO) via configuration, and never itself defines a
canonical type or business rule.

See ``app/api/component.yaml`` (``BAGMAN.RUNTIME.API``) for this
component's manifest. Named ``app/`` (not ``runtime/``, the prior
dispatch's original choice) because a tracked path with a directory
component literally named ``runtime`` collides with CD-1's
``tests/security/test_repo_hygiene.py`` ``PROHIBITED_DIRECTORY_NAMES``
guard against committed production/runtime *data* — see this
component's own manifest for further detail. The component id
``BAGMAN.RUNTIME.API`` is unaffected: it names a conceptual component,
not a literal path.
"""
