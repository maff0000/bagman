# BAGMAN

BAGMAN is a modular AI-assisted financial operations platform serving NoustAI
Limited, Infosecurs Limited, and Matthew Scott's personal finances. It is
intended to manage financial evidence, accounting workflows, reconciliation,
tax preparation, SaaS revenue operations, customer billing state, and personal
financial administration through a set of bounded, Docker-first components.

## Delivery status

**CD-1 — Foundation Security Scaffold.**

This delivery establishes the repository structure, security doctrine, and
configuration contract only. **No business capability exists yet.** There is
no mailbox integration, no bank connectivity, no accounting or billing
connectivity, no tax logic, no AI reasoning, and no production data of any
kind. See `PID.md` §16 for the full list of what is explicitly out of scope
for this delivery.

## ⚠️ PUBLIC REPOSITORY WARNING

**This repository shall be treated as publicly readable regardless of its
current GitHub visibility setting.**

No runtime secret, credential, token, private key, bank identifier requiring
protection, customer-sensitive record, personal financial document,
production database content, or production email content may ever be
committed to this repository — at any time, in any commit, including ones
later removed. Private Git is not a secrets manager, and this invariant
remains binding even if the repository later becomes private.

This restates the Public Repository Security Invariant defined in `PID.md`
§9. If a genuine secret is ever committed, it must be treated as a credential
compromise (revoke/rotate, remediate history, audit exposure, document the
incident) — not simply deleted.

## Repository structure

```text
bagman/
├── README.md            — this file
├── ARCHITECTURE.md       — high-level architectural doctrine
├── CHANGELOG.md          — delivery history
├── PID.md                — authoritative Project Initiation Document
├── .gitattributes        — repository-wide text/binary handling
│
├── contracts/            — cross-component interface contracts
├── core/                 — shared core domain logic
├── services/             — bounded business services (evidence, finance, billing, tax, notifications)
├── adapters/             — external system adapters (email, banking, accounting, billing, services)
├── agent/                — the BAGMAN AI agent, its tools, policies, and memory interface
├── memory/               — durable institutional memory (architecture, operating model, companies, products, tax, generated)
├── ui/                   — GUI client
├── deployment/           — Docker, Compose, and environment definitions
├── ops/                  — operational runbooks and procedures
├── scripts/              — developer/operator utility scripts
├── config/               — three-layer configuration contract (added under a parallel work item)
├── tests/                — automated test suites, including tests/security/ (added under a parallel work item)
└── .github/              — CI workflows (added under a later work item)
```

Every component directory carries its own `README.md` stating its purpose and
ownership. As of CD-1, these are scaffolding only — no implementation exists
inside them yet, per `PID.md` §16.

## Running security checks

Two mechanisms are intended to guard this repository against secret
disclosure (introduced by parallel/later work items in this delivery):

- **Secret scanning** — `gitleaks` is the mandatory first-line scanner. The
  intended developer operation is:

  ```bash
  gitleaks detect --source . --verbose
  ```

- **Security test suite** — deterministic tests under `tests/security/` prove
  properties such as: no prohibited secret/runtime filenames are tracked,
  committed configuration examples use secret references rather than real
  values, known production-data directories are ignored, the Gitleaks scan
  passes, and any included fixtures are demonstrably synthetic. See `PID.md`
  §15 for the full test specification.

## Where secrets must NOT live

Secrets **must never** be committed to this repository, in any form — not as
`.env` files, not as example values, not in filenames, not in test fixtures.

Runtime secrets live entirely outside this repository:

- **Local/development:** `/srv/bagman-secrets/` — a path outside this Git
  working tree, never created or tracked as part of it.
- **Runtime containers:** mounted secret files at `/run/secrets/<name>`,
  referenced only by name/path from configuration — never by value.

See `PID.md` §5 (Configuration Contract), §6 (Secret Consumption Doctrine),
and §9 (Public Repository Security Invariant) for the full doctrine.
