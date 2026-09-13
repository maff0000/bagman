# BAGMAN CD-3 — Runtime & Evidence Store — Delivery Evidence

**PID:** `PID.md` v3 ("BAGMAN PID v3 — Runtime & Evidence Store")
**Delivery branch:** `cd-3/runtime-and-evidence-store`
**Commit under audit (Auditor's dispatch):** `5d0319896bc056b9778427bfe41042fce60c9b1c`
**Architect-required CI fix commit:** `2708918` (adds the missing drift-check step; live CI confirmed green there, run `34744470069`) — see §6b below. This evidence file itself, and any subsequent docs-only commit describing it, necessarily lands on top of that fix commit; CI was independently reconfirmed green on each such follow-up commit as it was pushed (most recently the PR's actual current tip).
**PL:** Bagman persona (Trinity ecosystem), operating under Forge doctrine (`/srv/forge`) in hub-model mode.
**Date:** 2026-09-12 (initial delivery); 2026-09-13 (post-audit CI fixes and architect delta review)

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

## 6a. Post-audit finding: live CI genuinely failed twice, then was fixed and observed green

The Auditor's report (§4 above) was explicit and honest that it could not observe a live GitHub Actions run from its sandboxed environment, and the PL's own PR description likewise left "confirm this PR's CI run goes green" as an open checkbox rather than asserting it. When the PL actually checked the live run after opening PR #3, **it had genuinely failed** — twice, for two different real reasons, neither reproducible on this development host:

1. **First failure** (`88d408d`'s predecessor state, commit `5d03198`/`9d59beb`): `pytest tests/ -v` run as a single process left `tests/persistence/`'s and `tests/app_api/`'s independent session-scoped Postgres/MinIO container fixtures alive concurrently for the life of the whole process. Fixed (commit `88d408d`) by splitting the CI test step into three separate pytest invocations (non-Docker suites; `tests/persistence`; `tests/app_api`) so each Docker-backed suite's containers are guaranteed torn down (interpreter exit) before the next starts.
2. **Second failure** (after the above fix, commit `88d408d`, run `34738637679`): the split did not fix it — every single `tests/persistence` test failed from the very first one, running with no other Postgres container present at all, ruling out container overlap. The actual root cause, found by reading the CI traceback closely: the official `postgres` Docker image's first-run entrypoint briefly starts a *temporary* server (during `initdb`'s init-script phase), stops it, then starts the *final* long-lived server. Both `tests/persistence/conftest.py`'s and `tests/app_api/conftest.py`'s `_wait_for_postgres()` helpers only checked `pg_isready` once — which can report success during that fleeting temporary-server window — so the very first real query (Alembic's own migration) could land exactly as the temporary server shut down, producing `psycopg.OperationalError: ... server closed the connection unexpectedly`. A slower/more loaded CI runner makes this race far likelier to actually land inside that narrow window than this fast, idle development host — which is why it was never reproduced here despite extensive PL and Auditor verification. Fixed (commit `9da6235`) by requiring an actual `SELECT 1` to succeed on 3 consecutive attempts before declaring readiness, in both conftest files.

After both fixes, the live GitHub Actions run (`34738774690`) passed in full — all three test steps (non-Docker suites, `tests/persistence`, `tests/app_api`) genuinely green, confirmed via `gh run view --job` showing every step checked, not skipped. **This is the actual, observed confirmation of PID §63's "Live PR CI green" acceptance criterion** — stronger evidence than either the PL's or the Auditor's local reproduction, neither of which could have caught this specific runner-resource-profile-dependent race.

This is recorded here in full, not silently smoothed over, because it is exactly the kind of gap Forge's evidence doctrine exists to surface: both the PL and a fresh, thorough Auditor verified this delivery extensively on a capable host and both, correctly and honestly, flagged that live CI was the one thing neither could observe — and when it was finally observed, it did not simply pass, it required two more rounds of genuine debugging. The lesson carried forward: "PL/Auditor verification passed locally" and "live CI is green" are different claims, and the second must actually be checked, not assumed once the first holds.

## 6b. Architect delta review — one mandatory CI control missing, fixed

Matt (architect) reviewed PR #3 directly against PID §59 and found one concrete, correctly-scoped gap: **PID §59 requires CI to continue running the architecture-memory drift check** (`scripts/generate_architecture_memory.py --check`, established in CD-2). `.github/workflows/security.yml` ran gitleaks and the three pytest steps (non-Docker suites, `tests/persistence`, `tests/app_api`) but never actually invoked the drift check — it had only ever been run and confirmed locally (by every WI's own Engineer report, by the PL's own reconciliation, and by the Auditor), never enforced in the live pipeline itself. The architect explicitly confirmed the rest of the delivery — Docker topology, secrets, no-fallback composition, evidence integrity (hash recomputed before storage, immutable, hash re-verified on read) — matched doctrine, and scoped this as a single, mandatory, narrow fix rather than reopening the wider runtime design.

**Ruling:** "CD-3 remains RUNTIME_FOUNDATION_GREEN in substance, but PR #3 is NOT YET APPROVED FOR MERGE because one mandatory CI control from PID §59 is absent."

**Fix** (commit `2708918`): added a `Check architecture memory projection` step to `.github/workflows/security.yml`, running `python3 scripts/generate_architecture_memory.py --check` right after dependency install, before the test suites — so a future `component.yaml`/contract/entity-registry change that drifts the committed `memory/generated/architecture-index.md` out of sync now fails CI loudly, not only locally.

**Verification performed, in the order the architect specified:**
1. `python3 scripts/generate_architecture_memory.py --check` locally → `architecture-index.md is up to date.`
2. `gitleaks detect --source . -v --redact` → 21 commits scanned, no leaks found.
3. The relevant regression suite (`tests/security tests/contract tests/integration`, the suite this step sits directly beside) in a fresh venv → 102 passed.
4. Pushed (commit `2708918`).
5. **New live GitHub Actions run observed at the new head**, run `34744470069` — confirmed via `gh run view --job` that `Check architecture memory projection` now runs as its own named, visibly green step, distinct from and preceding all three test steps, not silently folded into or skipped by any of them.
6. This evidence file updated with the final head SHA and run — this section.

Live CI green in full at the fix commit, including the previously-missing drift check, confirmed by direct inspection of the run's step list, not inferred — and reconfirmed green again at this evidence commit's own predecessor (the docs-only commit recording §6a/§6b), since a docs change cannot itself un-green a passing pipeline but was checked anyway rather than assumed.

This is recorded in full for the same reason as §6a: a Human reviewer catching a real, narrow compliance gap that both the PL and the Auditor missed is exactly Forge's independent-scrutiny doctrine working as intended, not a failure to smooth over.

## 6c. Exit-gate statement (PID §66)

Per PID §66, no live mailbox integration may begin until CD-3 reaches RUNTIME_FOUNDATION_GREEN. That condition is met as of this commit. BAGMAN has proven: canonical truth survives full container restart (with idempotency intact); canonical truth survives application-container replacement while the database/object-store containers are untouched; canonical truth and original evidence bytes both survive complete destruction and restoration from backup into a provably clean target; and dependency failure produces an honest, loud, non-silent readiness failure rather than any fallback. No mailbox credential, bank credential, Xero/Chargebee/SaaS credential, production document, or customer data has entered this repository or runtime at any point in CD-3. Per PID §67, the expected next delivery is CD-4 — Evidence Intake & Manual Upload Foundation — still with no live mailbox connectivity unless explicitly authorised by that PID.
