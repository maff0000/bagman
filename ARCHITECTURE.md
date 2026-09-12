# BAGMAN Architecture

This document describes the high-level architectural doctrine for BAGMAN as
established by CD-1 (Foundation Security Scaffold). It captures intent and
doctrine only — it must not be read as describing implementation that
exists yet. As of CD-1, no business logic, no working containers, and no
deployed datastore exist. See `PID.md` §16 for the full out-of-scope list.

## Modular architecture doctrine

BAGMAN is constructed from bounded components, each with a clear
responsibility and an explicit contract for how other components may
interact with it. The repository anticipates the following domains:

- **core** — shared core domain logic used across BAGMAN.
- **evidence** — immutable financial evidence handling.
- **finance** — accounting, reconciliation, and financial workflows.
- **billing** — customer billing state and revenue operations.
- **tax** — tax preparation support.
- **notifications** — outbound notification handling.
- **adapters** — integration points with external systems (email, banking,
  accounting, billing, and other services), isolating the rest of BAGMAN
  from third-party APIs.
- **agent** — the BAGMAN AI agent, its tools, policies, and memory
  interface.
- **memory** — durable institutional memory fabric (architecture,
  operating model, companies, products, tax, generated artifacts).
- **GUI** — the user-facing client (`ui/`).
- **deployment** — Docker, Compose, and environment definitions.
- **tests** — automated test suites.
- **ops** — operational runbooks and procedures.

Creating these directory boundaries in CD-1 does not authorise
implementation within them. Each component directory is scaffolding only.

## Docker-first doctrine

BAGMAN is Docker-first. All future BAGMAN runtime dependencies shall be
BAGMAN-owned — BAGMAN shall not silently consume shared application
infrastructure belonging to another product on the host.

Canonical container naming follows `bagman-<operational-role>`, for example:

- `bagman-api`
- `bagman-ui`
- `bagman-worker`
- `bagman-agent`
- `bagman-db`
- `bagman-objects`
- `bagman-notify`

CD-1 does not require any working application containers.

## Datastore doctrine

**PostgreSQL** is the intended canonical structured datastore for BAGMAN.
CD-1 does not deploy PostgreSQL, except where strictly necessary to prove
deployment/configuration structure.

**Redis is not part of BAGMAN v1 architecture.** It shall not be introduced
without a later, explicitly documented architectural requirement
demonstrating why PostgreSQL and normal worker patterns are insufficient.

## External secret handling

Secrets never live inside this repository. BAGMAN follows a three-layer
configuration model — committed structure, runtime environment
configuration, and external secrets — with runtime secrets consumed via
mounted files (e.g. `/run/secrets/<name>`) rather than broad environment
variable exposure. The full three-layer model is documented in
`config/README.md` (introduced by a parallel work item); the doctrine
itself is defined in `PID.md` §5 and §6.

## Adapter / service separation

Adapters (`adapters/`) are the only components that speak to external
systems (email providers, banks, accounting platforms, billing platforms,
and other third-party services). Services (`services/`) implement BAGMAN's
own bounded business domains and depend on adapters through explicit
contracts rather than reaching into external systems directly. This keeps
external integration surface area isolated and replaceable without
disturbing core business logic.

## GUI and agent as governed clients

Both the GUI (`ui/`) and the BAGMAN AI agent (`agent/`) are governed clients
of BAGMAN's own services. Neither holds broad credentials directly, and
neither is permitted to reach external systems or data stores on its own
authority — all access is mediated through BAGMAN's services and adapters
under their respective contracts. This preserves a single, auditable point
of control over sensitive financial operations regardless of which client
is acting.
