# BAGMAN CD-3 — Runtime & Evidence Store — Delivery Evidence

**PID:** `PID.md` v3 ("BAGMAN PID v3 — Runtime & Evidence Store")
**Delivery branch:** `cd-3/runtime-and-evidence-store`
**Commit under audit:** `5d0319896bc056b9778427bfe41042fce60c9b1c`
**PL:** Bagman persona (Trinity ecosystem), operating under Forge doctrine (`/srv/forge`) in hub-model mode.
**Date:** 2026-09-12

Per Forge doctrine this evidence trail lives in the repository, not only in session/fabric state.

---

## 1. Delivery summary

Four PID-prescribed work items, dispatched per real dependency (WI-1/WI-2 in parallel, WI-3/WI-4 sequential):

| Work item | Commit(s) | Scope |
|---|---|---|
| WI-1 | `b6ad97b` | SQLAlchemy models, Alembic migrations, six PostgreSQL-backed repository implementations (`persistence/postgres/`) |
| WI-2 | `2bf5ee4` (amended pre-push) | `EvidenceObjectStore` abstraction + boto3/MinIO implementation (`persistence/objects/`) |
| merge | `9e45fdf` | WI-1+WI-2 merged; real conflict (both independently created `persistence/__init__.py` and `tests/persistence/conftest.py`) resolved by hand — combined fixture sets |
| WI-3 | `3ee3e85` | `bagman-api` FastAPI runtime, Docker Compose (`bagman-db`/`bagman-objects`/`bagman-api`), composition root, health/readiness with a hard no-fallback invariant |
| WI-4 | `5d03198` | Backup/restore tooling, and the three required acceptance proofs (restart, container-rebuild, restore-into-clean-target) |

Environment check before any work began: confirmed Docker daemon reachable, image pulls working, no conflicting `bagman-*` infrastructure already running, 3.3T free on `/srv`. `/srv/bagman-secrets/` (outside the repository, chmod 700/600) provisioned with synthetic dev credentials for Postgres and MinIO — the first CD-3 delivery to actually need it.

FORGE decisions made and documented: persistence Option A superseded here by the real Postgres/MinIO backends this PID specifically authorises; SQLAlchemy 2.x + psycopg3 + Alembic; boto3 (not the `minio` package) for portability; a new top-level `persistence/` directory keeping domain models free of storage-technology imports; FastAPI + Uvicorn for `bagman-api`, under a new top-level `app/api/` directory (not `runtime/api/` — see §3 below).

## 2. Defects and gaps caught and fixed during delivery (all before this PR, none discovered only at audit)

1. **Idempotency (carried forward from CD-2, re-verified durable):** the CD-2 idempotency guarantee (a bare retry of the same external-reference tuple returns the same `EvidenceItem`) had to hold across a real process/container restart in CD-3, not just in-process. `persistence/postgres/evidence_repository.py` writes both rows in one transaction and re-resolves against the real unique-constraint winner on a race, rather than assuming — proven durable by `tests/acceptance/restart_proof.py`.
2. **A hardcoded test credential (gitleaks, WI-2):** the first version of `tests/persistence/conftest.py` hardcoded a throwaway MinIO access/secret key as a string literal. Caught via gitleaks (surfaced by a sibling worktree sharing the same git object store) before the branch was ever pushed. Fixed by generating the secret at import time (`secrets.token_urlsafe()`) instead of a literal, and the still-local commit was amended rather than left as permanent flagged history. Recorded as a standing lesson: rerun gitleaks explicitly for every work item, not only the test suite.
3. **A directory-naming collision (WI-3):** the container runtime was first built at `runtime/api/`, which collided with CD-1's `tests/security/test_repo_hygiene.py` — a check that forbids any tracked directory literally named `runtime` (meant to stop production *data* from being committed, not to forbid application code). Fixed by renaming to `app/api/` rather than weakening CD-1's security doctrine.
4. **A malformed-ID error-mapping bug (WI-3):** a syntactically-invalid (non-UUID) id passed to any of the six `persistence/postgres/*_repository.py` lookup methods raised the database's own format error, mapped to the generic `PersistenceError` (HTTP 503) instead of `NotFoundError` (HTTP 404) — a malformed id cannot correspond to an existing row, so 404 is the honest answer. Fixed consistently across all six repositories.
5. **Two undeclared dependencies (`httpx2`, then `requests`):** `fastapi.testclient.TestClient` (WI-3's own tests) and the WI-4 acceptance scripts each imported a package that happened to already be present on this host for unrelated reasons, but was never declared in `requirements.txt`/`requirements-dev.txt`. Both caught by the PL building a genuinely clean venv from the repo's own pinned files before accepting each work item, and fixed by declaring the dependency explicitly with its rationale.

## 3. PL reconciliation (before audit)

Run against the fully-integrated tree at `5d03198`, in a disposable venv built strictly from the repo's own `requirements.txt`/`requirements-dev.txt`, with a freshly-started disposable MinIO instance and no manual `PYTHONPATH`:

```
$ pytest tests/ -q
164 passed in ~14-24s (varies by run)

$ gitleaks detect --source . -v --redact
17 commits scanned. no leaks found. exit 0

$ python3 scripts/generate_architecture_memory.py --check
architecture-index.md is up to date.
```

The PL additionally stood up the real Docker Compose stack directly (build, up, health/ready/version, register+retrieve real evidence bytes byte-identical, stop/restart `bagman-db` and `bagman-objects` individually confirming 503-no-fallback then recovery, malformed-ID 404 confirmed, full teardown confirmed clean) — twice: once after WI-3 merged, and again independently reproducing all three `tests/acceptance/` scripts (restart, container-rebuild, restore-into-clean-target) end-to-end after WI-4 merged, before ever dispatching the Auditor.

## 4. Auditor dispatch and report

A **fresh** Agent dispatch, zero inherited context, into its own worktree `/srv/bagman-worktrees/cd3-audit` (branch `wt/cd3-audit`), verified via `git cat-file -t 5d0319896bc056b9778427bfe41042fce60c9b1c` before dispatch. Given real Docker access and instructed to reproduce everything itself rather than trust any prior claim, including build the stack, run all three acceptance scripts for real, independently re-derive the import-boundary and no-Redis checks via its own AST scan, and walk every PID §63 acceptance bullet individually.

**Verdict: RUNTIME_FOUNDATION_GREEN.**

Key reproduced findings (condensed; full detail in the Auditor's own report):
- 164/164 tests, live MinIO genuinely exercised (not skipped).
- Gitleaks clean over full history (17 commits); confirmed both documented near-misses are genuinely fixed, not merely papered over.
- Docker topology independently inspected: `bagman-db`/`bagman-objects` publish no host port; `bagman-api` bound to `127.0.0.1` only; named volumes correctly mounted; secrets wired from the real `/srv/bagman-secrets/*` files, never a literal; `postgres:17` and a pinned MinIO `RELEASE.*` tag, never `:latest`; migrations run as a visible pre-start step.
- Built and ran the real stack itself: all three services healthy, `/health`/`/ready`/`/version` correct, evidence round-tripped byte-identical, no-fallback proven live against both dependencies (stop/restart cycle each), malformed-ID fix confirmed.
- **All three `tests/acceptance/` scripts executed for real, end to end, exit 0** — including the restore-into-clean-target proof's sharpest assertion: after `down -v`, the evidence was confirmed genuinely **absent** (a live 404) before restoring, proving the target really was clean rather than silently retaining state.
- Failure proof: confirmed hash-mismatch is genuinely unreachable via the HTTP API (server computes the hash itself) and that the object-store layer's own corruption/mismatch tests pass live.
- Architecture boundaries independently re-derived via the Auditor's own AST import scan (not trusting the repo's own test) — zero `core`/`services` → `persistence`/`app` violations; zero Redis references anywhere.
- One honest, non-blocking gap noted: the Docker/Postgres/MinIO-dependent acceptance proofs are not part of the automated CI pipeline — explicitly optional per PID §59, and stated plainly by the delivery's own `tests/acceptance/README.md`, not silently omitted.
- Confirmed its own Docker usage left nothing `bagman-*` behind on the host.

## 5. PL adjudication

Per Forge doctrine, the PL spot-checked several Auditor claims directly on the real integration branch (not the Auditor's own worktree) before accepting the verdict:

```
$ grep -A3 "ports:" deployment/compose/docker-compose.yml
    ports:
      - "127.0.0.1:8000:8000"
    (only bagman-api publishes a port; bagman-db/bagman-objects have none)

$ git ls-files | grep -E "(^|/)runtime(/|$)"
(no match — confirmed clean of the earlier naming collision)

$ grep -E "image: " deployment/compose/docker-compose.yml
    image: postgres:17
    image: quay.io/minio/minio:RELEASE.2025-09-07T16-13-09Z.hotfix.7aa24e772
    image: bagman-api:${BAGMAN_IMAGE_TAG:-dev}
    (no :latest anywhere)

$ gitleaks detect --source . -v --redact
17 commits scanned. no leaks found.
```

All spot-checks corroborate the Auditor's claims.

**Adjudicated verdict: RUNTIME_FOUNDATION_GREEN.**

## 6. Exit-gate statement (PID §66)

Per PID §66, no live mailbox integration may begin until CD-3 reaches RUNTIME_FOUNDATION_GREEN. That condition is met as of this commit. BAGMAN has proven: canonical truth survives full container restart (with idempotency intact); canonical truth survives application-container replacement while the database/object-store containers are untouched; canonical truth and original evidence bytes both survive complete destruction and restoration from backup into a provably clean target; and dependency failure produces an honest, loud, non-silent readiness failure rather than any fallback. No mailbox credential, bank credential, Xero/Chargebee/SaaS credential, production document, or customer data has entered this repository or runtime at any point in CD-3. Per PID §67, the expected next delivery is CD-4 — Evidence Intake & Manual Upload Foundation — still with no live mailbox connectivity unless explicitly authorised by that PID.
