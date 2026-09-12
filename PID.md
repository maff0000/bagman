# BAGMAN PID v1 — Foundation Security Scaffold

## 1. Product Identity

**Product:** BAGMAN
**Repository:** `github.com/maff0000/bagman`
**Authoritative working tree:** `/srv/bagman`
**Primary branch:** `main`

BAGMAN is a modular AI-assisted financial operations platform intended to manage financial evidence, accounting workflows, reconciliation, tax preparation, SaaS revenue operations, customer billing state, and personal financial administration.

This PID does **not** authorise implementation of those capabilities.

This PID authorises only the secure repository and configuration foundation required before any external system, mailbox, bank, accounting platform, billing platform, or production data may connect to BAGMAN.

---

## 2. Delivery Objective

Establish BAGMAN as a clean, secure, Docker-ready repository with:

1. authoritative repository structure
2. public-repository security controls
3. configuration doctrine
4. external secret handling contract
5. `.gitignore` protections
6. automated secret scanning
7. component/deployment directory skeleton
8. documentation explaining the secure operating model
9. acceptance tests proving the repository cannot trivially admit secrets

No functional BAGMAN business capability is in scope.

The desired end state is:

> BAGMAN can safely begin development without any credential, production financial evidence, customer information, mailbox content, tax data, or other sensitive runtime material entering Git.

---

## 3. Architectural Doctrine

### 3.1 Modular architecture

BAGMAN shall be constructed from bounded components with clear responsibilities and explicit contracts.

Repository structure shall anticipate domains including:

* core
* evidence
* finance
* billing
* tax
* notifications
* adapters
* agent
* memory fabric
* GUI
* deployment
* tests
* operations

Creating these directory boundaries in CD-1 does not authorise implementation within them.

### 3.2 Docker-first

BAGMAN shall be Docker-first.

All future BAGMAN runtime dependencies shall be BAGMAN-specific.

BAGMAN shall not silently consume shared application infrastructure belonging to another product.

Canonical container naming shall follow:

`bagman-<operational-role>`

Examples for future use include:

* `bagman-api`
* `bagman-ui`
* `bagman-worker`
* `bagman-agent`
* `bagman-db`
* `bagman-objects`
* `bagman-notify`

CD-1 does not require working application containers.

### 3.3 PostgreSQL doctrine

PostgreSQL is the intended canonical structured datastore.

CD-1 shall not deploy PostgreSQL unless required solely to prove deployment/config structure.

### 3.4 Redis doctrine

Redis is **not part of BAGMAN v1 architecture**.

It shall not be introduced without a later documented architectural requirement demonstrating why PostgreSQL and normal worker patterns are insufficient.

### 3.5 Object evidence doctrine

Future immutable financial evidence shall use BAGMAN-controlled object storage or equivalent durable evidence storage.

No production evidence is permitted in Git.

---

## 4. Required Repository Structure

FORGE shall create a clear initial structure under `/srv/bagman`.

At minimum:

```text
/srv/bagman/
├── PID.md
├── README.md
├── ARCHITECTURE.md
├── CHANGELOG.md
├── .gitignore
├── .gitattributes
│
├── config/
│   ├── README.md
│   ├── base/
│   └── examples/
│
├── contracts/
│   └── README.md
│
├── core/
│   └── README.md
│
├── services/
│   ├── evidence/
│   ├── finance/
│   ├── billing/
│   ├── tax/
│   └── notifications/
│
├── adapters/
│   ├── email/
│   ├── banking/
│   ├── accounting/
│   ├── billing/
│   └── services/
│
├── agent/
│   ├── bagman/
│   ├── tools/
│   ├── policies/
│   └── memory/
│
├── memory/
│   ├── architecture/
│   ├── operating-model/
│   ├── companies/
│   ├── products/
│   ├── tax/
│   └── generated/
│
├── ui/
│   └── README.md
│
├── tests/
│   ├── security/
│   ├── contract/
│   ├── integration/
│   └── fixtures/
│
├── deployment/
│   ├── docker/
│   ├── compose/
│   └── environments/
│
├── ops/
│
├── scripts/
│
└── .github/
    └── workflows/
```

Empty directories may use `.gitkeep` only where genuinely required.

Prefer concise README files describing ownership and purpose over meaningless empty directory proliferation.

---

## 5. Configuration Contract

BAGMAN shall use three configuration layers.

### Layer 1 — committed configuration structure

Committed configuration may contain:

* schemas
* safe defaults
* entity identifiers
* service definitions
* secret reference names
* example configuration
* development-safe placeholders

It must not contain real secrets.

Example:

```yaml
mailboxes:
  noust_matt:
    provider: imap
    address: matt@noust.ai
    credential_ref: BAGMAN_MAIL_NOUSTAI
```

### Layer 2 — runtime environment configuration

Environment-specific, non-secret runtime configuration may be supplied externally.

Examples:

* runtime environment
* service endpoints
* log level
* database hostname
* feature flags
* timezone

Environment configuration must not become a backdoor for committing credentials.

### Layer 3 — external secrets

Secrets must live outside `/srv/bagman`.

The intended local/development pattern is:

```text
/srv/bagman-secrets/
```

This path shall not be created as part of the Git repository.

Example future secret identities include:

```text
noust_imap_password
microsoft_graph_credentials
gmail_primary_credentials
gmail_secondary_credentials
postgres_password
xero_noustai_credentials
xero_infosecurs_credentials
revolut_credentials
starling_credentials
chargebee_credentials
usecure_credentials
huntress_credentials
```

Secret values must never appear in committed configuration.

---

## 6. Secret Consumption Doctrine

Where practical, BAGMAN runtime components should consume secrets through mounted files such as:

```text
/run/secrets/<secret_name>
```

rather than exposing broad secret values through environment variables.

Environment variables may contain references to secret locations.

Example:

```text
BAGMAN_NOUST_IMAP_PASSWORD_FILE=/run/secrets/noust_imap_password
```

rather than:

```text
BAGMAN_NOUST_IMAP_PASSWORD=<secret>
```

No service may log secret values.

No debugging output may expose secret contents.

No exception path may intentionally echo credentials.

---

## 7. Email Credential Doctrine

Future mailbox integrations shall prefer:

1. OAuth
2. provider application credentials
3. provider-specific app passwords
4. static username/password only where unavoidable

Primary mailbox passwords should not be used where a safer provider-supported mechanism exists.

Expected future providers include:

* Microsoft Graph / Exchange
* Gmail / Google APIs
* IMAP

CD-1 must not connect to any mailbox.

---

## 8. Production Data Doctrine

The BAGMAN repository must contain no real:

* invoices
* receipts
* bank statements
* bank transactions
* emails
* mailbox exports
* customer names or records
* Xero exports
* Chargebee exports
* tax returns
* HMRC correspondence
* R&D evidence
* account numbers
* production database dumps
* OAuth tokens
* API tokens
* private keys

Tests must use synthetic fixtures.

Synthetic fixtures must clearly identify themselves as fabricated test data.

---

## 9. Public Repository Security Invariant

**BAGMAN source control SHALL be treated as publicly readable regardless of current GitHub visibility. No runtime secret, credential, token, private key, bank identifier requiring protection, customer-sensitive record, personal financial document, production database content or production email content may be committed. Runtime secrets SHALL be supplied externally through governed secret mounts or equivalent protected mechanisms. Configuration committed to Git SHALL contain only non-secret structure, defaults and secret references. Automated secret scanning SHALL block violations.**

This invariant remains binding even if the repository later becomes private.

Private Git is not a secrets manager.

### Mandatory implementation controls

FORGE shall implement:

* `.gitignore`
* secret scanning
* CI secret scanning
* documented secret handling
* synthetic-only test fixture doctrine
* safe configuration examples

At minimum, `.gitignore` shall protect patterns covering:

```text
.env
.env.*
!.env.example

secrets/
*.secret
*.pem
*.key
*.p12
*.pfx

credentials.json
token.json

runtime/
data/
backups/

*.sqlite
*.db

config/local/
```

FORGE may extend this list where justified.

---

## 10. Secret Scanning

`gitleaks` is the mandatory first-line secret scanner.

The repository shall contain a repeatable command to scan the repository.

Example intended developer operation:

```text
gitleaks detect --source . --verbose
```

Exact invocation may differ if required by the installed version.

A GitHub Actions workflow shall run secret scanning on:

* pull requests
* pushes to `main`

A secret-scanning failure must fail CI.

FORGE shall not weaken scanner rules merely to obtain a green build.

False positives must be resolved explicitly and minimally.

Any allow-list entry must document why it is safe.

---

## 11. Optional Local Guard

FORGE may introduce a local pre-commit or pre-push check if lightweight and reliable.

However:

> CI is authoritative.

CD-1 must not depend solely on developers remembering to run a local hook.

---

## 12. Git Doctrine

Git history must remain clean and comprehensible.

Requirements:

* no credentials committed and later removed
* no secret-containing commits
* no generated runtime data
* no database files
* no unnecessary binaries
* no production documents
* meaningful commit messages
* clean working tree at delivery

If a genuine secret is ever committed, normal deletion is insufficient.

The event must be treated as credential compromise requiring:

1. credential revocation/rotation
2. history remediation where appropriate
3. audit of exposure
4. documented incident response

---

## 13. Documentation Requirements

### README.md

Must explain:

* what BAGMAN is
* current delivery status
* public-repository warning
* basic repository structure
* how to run security checks
* where secrets must not live

### ARCHITECTURE.md

For CD-1 this is intentionally high level.

It should capture:

* modular architecture doctrine
* Docker-first doctrine
* BAGMAN-owned runtime dependencies
* PostgreSQL canonical datastore intention
* no Redis by default
* external secret handling
* adapter/service separation
* GUI and agent as governed clients of BAGMAN services

It must not fabricate implementation that does not yet exist.

### config/README.md

Must explain the three-layer configuration model.

### tests/fixtures

Must clearly state:

> synthetic data only

---

## 14. CI Requirements

Create an initial GitHub Actions workflow that performs at least:

1. checkout
2. repository hygiene checks
3. secret scanning
4. lightweight structural/security tests

No production credentials may be required by CI.

No CI secret should be necessary to validate CD-1.

---

## 15. Security Tests

FORGE shall create deterministic tests proving at minimum:

### Test A — repository hygiene

Fail if prohibited obvious secret/runtime filenames become tracked.

### Test B — configuration examples

Verify committed example configuration contains secret references/placeholders rather than expected real values.

### Test C — production evidence prohibition

Verify known production-data directories are ignored/not tracked.

### Test D — Gitleaks

Repository passes the configured Gitleaks scan.

### Test E — synthetic fixtures

Fixtures included in CD-1 are demonstrably synthetic.

---

## 16. Explicitly Out of Scope

CD-1 SHALL NOT implement:

* Microsoft Graph integration
* Gmail integration
* IMAP integration
* invoice extraction
* receipt extraction
* document processing
* PostgreSQL financial schemas
* bank connectivity
* Revolut connectivity
* Starling connectivity
* Xero connectivity
* Chargebee connectivity
* uSecure connectivity
* Huntress connectivity
* R&D classification
* tax logic
* personal tax
* HMRC submission
* customer billing
* customer suspension
* BAGMAN AI reasoning
* BAGMAN autonomous actions
* production GUI
* production authentication
* real secrets
* real financial evidence

No mailbox may be connected during this delivery.

No live external API token may be introduced.

---

## 17. FORGE Directory Authority

FORGE may modify:

```text
/srv/bagman/PID.md
/srv/bagman/README.md
/srv/bagman/ARCHITECTURE.md
/srv/bagman/CHANGELOG.md
/srv/bagman/.gitignore
/srv/bagman/.gitattributes
/srv/bagman/config/
/srv/bagman/contracts/
/srv/bagman/core/
/srv/bagman/services/
/srv/bagman/adapters/
/srv/bagman/agent/
/srv/bagman/memory/
/srv/bagman/ui/
/srv/bagman/tests/
/srv/bagman/deployment/
/srv/bagman/ops/
/srv/bagman/scripts/
/srv/bagman/.github/
```

FORGE must not modify:

```text
/srv/bagman-secrets/
```

or any unrelated repository, home-directory credential store, host-global secret store, external mailbox, bank, Xero tenant, Chargebee tenant, or SaaS provider.

---

## 18. Acceptance Evidence

CD-1 is complete only when FORGE presents evidence for all of the following:

### Repository

* correct GitHub remote
* `main` branch relationship understood
* clean working tree
* no unexpected untracked runtime material

### Structure

* required BAGMAN repository structure exists
* responsibilities are documented
* no unnecessary implementation has leaked into CD-1

### Security

* `.gitignore` passes review
* no secrets tracked
* no production data tracked
* Gitleaks installed and successfully run
* CI secret scanner exists
* CI configuration contains no secrets

### Configuration

* three-layer configuration model documented
* safe examples present
* secret references used
* external secrets path documented
* runtime secret file pattern documented

### Tests

* security tests pass
* repository secret scan passes
* `git diff --check` passes

### GitHub

* CI runs successfully against the delivery branch/PR
* no GitHub Actions workflow requires production credentials

---

## 19. Delivery State

FORGE shall report one of:

### FOUNDATION_GREEN

All acceptance criteria proven.

### FOUNDATION_RED

A mandatory criterion failed.

### BLOCKED

External condition prevents proof.

FORGE must not report GREEN based solely on code inspection.

Runtime/tool evidence is required.

---

## 20. Exit Gate

No BAGMAN external integration delivery may begin until CD-1 reaches:

> **FOUNDATION_GREEN**

Specifically, before FOUNDATION_GREEN:

* no mailbox credentials
* no bank credentials
* no Xero credentials
* no Chargebee credentials
* no SaaS provider credentials
* no production documents
* no customer data

may enter the BAGMAN runtime or repository.

The next authorised delivery after FOUNDATION_GREEN should focus on the canonical BAGMAN foundation and evidence architecture, not immediately on broad external integration.

---

## 21. Guiding Principle

BAGMAN will ultimately hold extremely sensitive financial and operational authority.

Therefore the first capability BAGMAN must prove is not email ingestion, accounting, AI or reconciliation.

It is:

> **the ability to be developed safely.**
