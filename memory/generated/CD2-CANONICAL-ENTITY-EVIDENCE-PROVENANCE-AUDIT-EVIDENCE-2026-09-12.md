# BAGMAN CD-2 — Canonical Entity, Evidence, Provenance & Audit Foundation — Delivery Evidence

**PID:** `PID.md` v2 ("BAGMAN PID v2 — Canonical Entity, Evidence, Provenance & Audit Foundation")
**Delivery branch:** `cd-2/canonical-entity-evidence-provenance-audit-foundation`
**Commit under audit:** `890c58b2b1e4695aa6e29d4b141aac0785f51d08`
**PL:** Bagman persona (Trinity ecosystem), operating under Forge doctrine (`/srv/forge`) in hub-model mode.
**Date:** 2026-09-12

Per Forge doctrine (`docs/FORGE-NORTH-STAR.md#durable-truth`, `.claude/skills/forge/SKILL.md#8-deliver`) this evidence trail lives in the repository, not only in session/fabric state.

---

## 1. Delivery summary

Four PID-prescribed work items (§39), dispatched sequentially — each genuinely depends on the previous one's output (domain models need the contracts; manifests need the real components; tests need everything):

| Work item | Commit | Scope |
|---|---|---|
| WI-1 | `0cf97ea` | Six JSON Schema contracts (entity, evidence, source, external_reference, provenance, audit_event) + three common primitives (identifier/UUIDv7, UTC timestamp, schema version) |
| WI-2 | `78696fc` | Domain models + in-memory repositories (`core/`, `services/evidence/`) + `BagmanCanonicalAPI` facade |
| WI-3 | `de28a1b` | Component manifest schema + two real manifests + deterministic memory projection (`scripts/generate_architecture_memory.py`, `memory/generated/architecture-index.md`) |
| WI-4 | `9d4233a` | Contract tests, architectural-boundary tests, synthetic fixtures, domain/lineage integration tests, the PID §43 runtime proof (102 tests total) |
| CI fix | `890c58b` | Extended `.github/workflows/security.yml` to run the full suite (PID §41); added `pyproject.toml` fixing the underlying pytest rootpath issue generally, not just in CI |

Two additional PL decisions of note, both made because PID §5/§21 explicitly delegated the choice to FORGE and documented the requirement to justify it:

- **Persistence: Option A** (in-memory/reference repositories) — Postgres (`bagman-db`) deferred to CD-3, which already expects it (PID §47).
- **Identifier scheme: UUIDv7, hand-implemented** in `core/identity.py` (RFC 9562 §5.2) rather than a new dependency — a monotonic counter seeds `rand_a` per-millisecond so ordering holds under real collisions, proven with a 20,000+ and later 5,000-ID generation/sort-order check.

No Engineer ever ran `git commit`/`push`; every diff was reviewed file-by-file by the PL and committed onto the integration branch. WI-1+related work proceeded on isolated worktrees created manually against `/srv/bagman` (hub-model dispatch — the PL's own session root is `/srv`, not this repository), each verified via `git cat-file -t <known commit>` before dispatch.

## 2. A defect caught and repaired before commit (PL reconciliation, not audit)

WI-2's first delivery had `register_evidence`'s `external_reference` idempotency check as **read-only** — it never wrote the `ExternalReference` row itself, so a bare caller retry (the exact scenario PID §34 exists to protect against) created a **second, duplicate** `EvidenceItem` instead of returning the existing one. The PL found this via its own smoke-test reconciliation (not the Engineer's self-report, which had tested a different call sequence) before ever committing the WI-2 diff, and required a scoped repair: `register_evidence` now writes the external-reference tuple through on first creation, making a single call self-sufficient for retries. Independently re-verified by the PL after the fix, again independently re-verified by the fresh Auditor later, and again by the PL as a final spot-check on the merged branch (§5 below) — the guarantee holds at every checkpoint.

## 3. A CI gap caught and repaired before audit

`.github/workflows/security.yml` (carried over from CD-1) only ever installed `requirements-dev.txt` and ran `tests/security/` — none of CD-2's 79 new tests, and none of CD-2's own runtime dependencies (`requirements.txt`), were ever exercised in CI, silently missing PID §41's explicit requirement. Fixed to install both requirement files and run the full `pytest tests/`. A related latent issue surfaced during that fix — `tests/contract/`/`tests/integration/` failed to import `core` without `PYTHONPATH` set — was fixed generally via `pyproject.toml`'s native `pythonpath = ["."]` option rather than papering over it with a CI-only environment variable, so the same fix benefits any local developer or future Engineer/Auditor dispatch, not just CI.

## 4. PL reconciliation (before audit)

Run against the fully-integrated tree at `890c58b`, in a disposable venv built strictly from the repo's own `requirements.txt`/`requirements-dev.txt`, no manual `PYTHONPATH`:

```
$ gitleaks detect --source . -v --redact
11 commits scanned. no leaks found. exit 0

$ pytest tests/ -q
102 passed in 0.37s
```

## 5. Auditor dispatch and report

A **fresh** Agent dispatch (general-purpose subagent under the Forge Auditor contract), zero inherited context from any Engineer session, into its own worktree `/srv/bagman-worktrees/cd2-audit` (branch `wt/cd2-audit`), verified via `git cat-file -t 890c58b2b1e4695aa6e29d4b141aac0785f51d08` before dispatch. Full instructions covered: repository state, the full 102-test suite reproduced independently, gitleaks, every contract file read and its open/closed field design verified, eight live domain-behavior reproductions against the running code (not source reading), component-manifest validation plus a deliberate corrupt-then-restore non-vacuousness proof on the memory-projection `--check` flag, an independently re-derived (not test-suite-trusted) import-boundary and zero-Redis check, the CI workflow read in full, the synthetic-data/security doctrine, every PID §42 acceptance bullet individually, and the full PID §43 runtime proof.

**Verdict: FOUNDATION_MODEL_GREEN.**

Key reproduced findings (full detail in the Auditor's report, condensed here):
- 102/102 tests pass in the Auditor's own disposable venv, no `PYTHONPATH` override needed.
- All six schemas: `additionalProperties: false`; open fields (`entity_type`, `source_type`, `provider`, `evidence_type`, evidence `status`, `event_type`) are NOT closed enums; closed fields (`provenance.relationship`, `audit_event.actor_type`) ARE closed enums; `evidence.entity_id` and `audit_event.causation_id` are required-but-nullable; identifier pattern enforces UUIDv7 (version nibble 7, variant 8-b); UTC timestamp requires `format: date-time`.
- Live reproduction, against the running code: unresolved evidence round-trips as explicit `None`; a second `assign_entity` call raises `ImmutabilityViolationError`; identical `content_hash` across different external-reference tuples remains two distinct records; **the specific idempotency guarantee from §2 above was independently re-verified to genuinely hold** — a bare retry with no separate `link_external_reference` call returns the same `evidence_id` and exactly one `EVIDENCE_OBSERVED` audit event; a conflicting external reference raises `DuplicateExternalReferenceError`; an orphan provenance edge raises `InvalidProvenanceError`; 5,000 generated UUIDv7 IDs are unique and generation-order-sorted; a malformed `entity_type` raises the canonical `ValidationError`, never a raw `jsonschema` exception.
- Both component manifests independently validated against the manifest schema; `--check` clean on the intact tree; a deliberately corrupted `id` field made `--check` fail loudly with a specific message, then clean again after restoration — proving the drift check is not vacuous.
- Independently re-derived (via the Auditor's own grep/import-parsing, not trusting `test_architecture_boundaries.py`'s own claim): the only `core/`→`services/` import crossing anywhere is `core/api.py`; zero `redis` references anywhere in code or requirements files.
- CI workflow read in full: installs both requirement files, runs the full suite, triggers on PR/push-to-main, references no `secrets.*`. The one honestly-flagged scope limitation: a live GitHub Actions run cannot be observed from a local audit — its two substantive steps were reproduced locally with identical green results instead, the same limitation CD-1 carried and resolved by observing the live run post-merge.
- Minor observation (not an acceptance failure): `referencing` was imported directly by `core/contract_validation.py` but only pinned transitively via `jsonschema`. **Fixed by the PL directly after the audit** (§6 below), verified not to change any test outcome.

## 6. PL adjudication

Per Forge doctrine (`SKILL.md` §7), the PL spot-checked several Auditor claims directly against the real integration branch (not the Auditor's own worktree) before accepting the verdict:

```
$ grep -rniE "import redis|from redis" core/ services/ contracts/ scripts/
(no match)
$ grep -ni redis requirements.txt requirements-dev.txt
(no match)
$ grep -rn "^from services\|^import services" core/*.py
core/api.py:69:from services.evidence.evidence import (...)
$ python3 scripts/generate_architecture_memory.py --check
architecture-index.md is up to date.
```

Plus a final, independent live reproduction of the idempotency guarantee specifically (the one defect this delivery required a repair for), on the actual merged branch, in a fresh venv:

```
PL final spot-check PASS: idempotent bare-retry holds on merged branch, exactly 1 EVIDENCE_OBSERVED
```

All spot-checks corroborate the Auditor's claims. The one minor observation (`referencing` not directly pinned) was acted on directly — added `referencing==0.37.0` to `requirements.txt` with its rationale, re-verified 102/102 tests and gitleaks clean afterward, no behavior change.

**Adjudicated verdict: FOUNDATION_MODEL_GREEN.**

## 7. Exit-gate statement (PID §46)

Per PID §46, no live mailbox integration may begin until CD-2 reaches FOUNDATION_MODEL_GREEN. That condition is met as of this commit. BAGMAN now has a trustworthy internal representation for WHO (governed entity, including explicit unresolved state), WHAT (evidence/canonical object identity, separate from content identity), WHERE FROM (source + external reference, provider identity isolated from canonical identity), HOW KNOWN (provenance lineage), WHAT HAPPENED (append-only, UTC, actor-attributed audit events), and WHY CONNECTED (correlation/causation). No mailbox credential, bank credential, Xero/Chargebee/SaaS credential, production document, or customer data has entered this repository or runtime at any point in CD-2. Per PID §47, the expected next delivery is CD-3 — BAGMAN Runtime & Evidence Store (`bagman-db`, object storage, Docker Compose runtime) — still with no live mailbox connectivity unless explicitly authorised by that PID.
