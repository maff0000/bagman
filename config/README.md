# BAGMAN Configuration Model

BAGMAN's repository is **public**. This directory documents and implements a
strict three-layer configuration model whose entire purpose is to make it
structurally hard for a secret to ever land in Git — not just discouraged by
policy, but never a required or convenient path.

The three layers are, in order of increasing sensitivity:

1. Committed configuration structure (this directory)
2. Runtime environment configuration (supplied externally, per environment)
3. External secrets (never in this repository, in any form)

---

## Layer 1 — Committed configuration structure

**Location:** `config/base/`, `config/examples/`

This is the only layer committed to Git. It may contain:

- schemas and structural definitions
- safe defaults
- entity identifiers (e.g. the legal entities BAGMAN operates across)
- service and integration definitions
- **secret reference names only** — e.g. `credential_ref: BAGMAN_MAIL_NOUSTAI`
  — never the secret itself
- example / placeholder configuration
- development-safe defaults

It must **never** contain a real secret, credential, token, password,
private key, tenant ID that would need protecting, or any other sensitive
value. Every placeholder in this layer must be *obviously* a placeholder
(e.g. `<SET_VIA_SECRET_STORE>`, a `credential_ref` name, or a clearly fake
example value) — not a realistic-looking fake that could be mistaken for a
real credential.

Files in this layer:

- `config/base/bagman.yaml` — top-level application configuration skeleton
- `config/base/entities.yaml` — the canonical BAGMAN legal entities
- `config/base/mailboxes.example.yaml` — mailbox identity definitions, by
  `credential_ref` only
- `config/base/integrations.example.yaml` — placeholder entries for future
  external integrations, by `credential_ref` only
- `config/examples/dev.example.env` — example Layer 2 configuration for a
  development environment
- `config/examples/prod.example.env` — example Layer 2 configuration for a
  production environment

## Layer 2 — Runtime environment configuration

**Location:** supplied externally per environment — never committed as a
real, in-use file (only `*.example.env` templates live in Git).

This layer carries configuration that is environment-specific but **not
secret**:

- runtime environment name (`development`, `production`, …)
- service endpoints and hostnames
- log level
- database hostname (not credentials)
- feature flags
- timezone

Rule: this layer must never become a backdoor for committing credentials.
An environment variable in this layer may point *at* a secret (a file path),
but must never *carry* a secret value. See the examples in
`config/examples/`.

## Layer 3 — External secrets

**Location:** entirely outside `/srv/bagman`.

- **Local / development:** `/srv/bagman-secrets/` — this path must never be
  created inside the Git repository, and nothing under it is ever committed.
- **Container runtime:** mounted files at `/run/secrets/<secret_name>`
  (the standard Docker/Compose secrets mount pattern).

Rules for this layer:

- Secret values must **never** appear in committed configuration, under any
  filename, in any layer, at any time — not even temporarily, since removing
  a value from a later commit does not remove it from Git history.
- Where practical, runtime components should consume secrets by **reading a
  mounted file** (`/run/secrets/<name>`) rather than reading the secret
  value directly from an environment variable.
- An environment variable may carry the *path* to a secret file, e.g.:

  ```
  BAGMAN_NOUST_IMAP_PASSWORD_FILE=/run/secrets/noust_imap_password
  ```

  It must never carry the value itself, e.g. this pattern is forbidden:

  ```
  BAGMAN_NOUST_IMAP_PASSWORD=<the actual password>
  ```

---

## Summary

| Layer | Committed to Git? | Contains secrets? | Example |
|-------|--------------------|--------------------|---------|
| 1. Committed configuration structure | Yes | Never | `config/base/mailboxes.example.yaml` |
| 2. Runtime environment configuration | No (only `*.example.env` templates) | Never | `BAGMAN_DB_HOST=bagman-db` |
| 3. External secrets | No, never | Yes | `/srv/bagman-secrets/` or `/run/secrets/<name>` |

If you are ever unsure which layer a value belongs in, ask: *"Would this
value cause harm if this public repository's full history were read by a
stranger?"* If yes, it belongs in Layer 3, and only a `credential_ref` name
or file-path reference belongs in Git.
