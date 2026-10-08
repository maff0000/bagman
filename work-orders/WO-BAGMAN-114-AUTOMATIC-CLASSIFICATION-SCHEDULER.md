# WO-BAGMAN-114-AUTOMATIC-CLASSIFICATION-SCHEDULER
## Add the Git-tracked launchd scheduler definition required by PID §114, then govern its staged production deployment and activation

**Lifecycle at creation:** `PROPOSED`
**Execution gate:** authorised only when exact-head Independent Audit GREEN, Architect Acceptance GREEN, canonical merge complete, and required post-merge CI GREEN all hold for this document's own exact content (PID §113's authoritative lifecycle rule; see §113.4's future-drafting guidance, applied here from creation).

---

## 0. Mandatory BAGMAN delivery doctrine (reproduced inline — binding on every future prompt derived from this WO)

The project operates under this mandatory chain:

> **Architecture decision → PID/Amendment → Git-tracked Work Order → Delivery Controller → Implementer → Independent Audit → PR → Architect Acceptance → Merge → Closure**

Rules:

- The **Architect** owns architecture, PIDs, amendments, sequencing, and acceptance.
- The **Delivery Controller** owns bounded execution and must dispatch implementation only against an approved Work Order.
- The **Implementer / FORGE** works only within that Work Order and must not invent architecture or widen scope.
- Every Work Order must identify: parent PID; applicable amendments; exact base SHA; scope; exclusions; required tests/proofs; acceptance criteria.
- If implementation exposes an architectural ambiguity, STOP and return it to the Architect. Do not improvise.
- Corrections and follow-up work require the same governed Work Order process.
- Git/GitHub is durable authority; chat is only the control surface.

Hard invariants:

**NO PID → NO WORK ORDER.**
**NO WORK ORDER → NO IMPLEMENTATION.**
**NO INDEPENDENT AUDIT + ARCHITECT ACCEPTANCE → NO MERGE.**
**NO GIT RECORD → NOT DURABLE PROJECT AUTHORITY.**

Every future BAGMAN Delivery Controller, FORGE Implementer, and HELM deployment-execution prompt derived from this WO must reproduce this doctrine inline. Agents must never be expected to remember it from another conversation.

---

## 1. Work Order storage convention

Uses the established convention (`work-orders/`, one Markdown file per Work Order, no framework/database/automation — see `WO-BAGMAN-112F-GOVERNANCE-RECOVERY.md` §1). This file: `work-orders/WO-BAGMAN-114-AUTOMATIC-CLASSIFICATION-SCHEDULER.md`.

---

## 2. Identity

| Field | Value |
|---|---|
| **Identifier** | `WO-BAGMAN-114-AUTOMATIC-CLASSIFICATION-SCHEDULER` |
| **Parent PID** | `PID.md §114` (Automatic Evidence Classification Scheduling — Architect ruling, 2026-10-07), confirmed **`CLOSED GREEN`** |
| **Applicable inherited binding PID sections** | §0 (Current Production Authority); §111 (BAGMAN Delivery Governance Doctrine); §112.A (automatic-classification architecture); §112.B (`T_ACT` activation boundary — hard invariant); §112.G (scheduler authority reserved to a future PID, satisfied by §114); §113 (Work Order lifecycle/authorisation semantics); §114.1–§114.14 (the scheduler architecture itself — execution mechanism, cadence, bounds, `T_ACT` safeguard, durable-configuration-authority requirement, concurrency doctrine, retry/crash semantics, kill switch, observability ceiling, staged activation doctrine, exclusions) |
| **Exact canonical architecture base SHA** | `33dab111422492c1c15d6d5e6e43b474d56ae81b` |

This SHA is the canonical baseline against which this Work Order was authored. The eventual Delivery Controller must verify canonical ancestry and the merged state of this WO before any dispatch. Unexpected code/PID drift between this baseline and dispatch-time canonical `main` requires STOP and Architect review — never silent continuation.

---

## 3. Purpose

This Work Order exists to implement and govern the activation of the recurring automatic evidence-classification scheduler authorised, as architecture, by PID §114. It covers **two separate lanes inside one bounded delivery**, which must not be blurred:

1. **Git-tracked repository implementation** — the canonical `launchd` scheduler definition and supporting documentation (Delivery Controller / FORGE / BAGMAN lane).
2. **Governed production deployment and activation** — installing, proving, and enabling that definition on the canonical BAGMAN production Mac (HELM lane), strictly staged and gated.

The desired chain:

```text
BAGMAN Delivery Controller
        ↓
FORGE Implementer — Stage A: Git-tracked scheduler artifacts only
        ↓
Stage B: repository verification (tests, gitleaks, static checks)
        ↓
Fresh Independent Audit (repository content)
        ↓
Git-tracked PR → Architect Acceptance → Merge → post-merge Security GREEN
        ↓
HELM — Stage D: deploy disabled → Stage E: pre-activation safety proof
        ↓
Stage F: explicit enablement (only after all Stage E gates GREEN)
        ↓
Stage G: bounded observation
        ↓
Fresh Independent Audit (production activation evidence)
        ↓
Git-tracked activation-closure PR → Architect Acceptance → Merge → post-merge Security GREEN
        ↓
Recurring automatic classification scheduling CLOSED GREEN
```

**This Work Order document itself, once canonical, authorises the full chain above through to closure.** It does not require a second Work Order for the production-activation half — but every stage gate below is binding, and a STOP at any stage returns to the Architect rather than being worked around.

---

## 4. Roles

### Delivery Controller — BAGMAN

- Re-reads the canonical WO from Git before each dispatch.
- Verifies the exact authorised SHA and that PID §114 remains canonical/unchanged at dispatch time.
- Gives FORGE/HELM only the bounded mandate for the stage being dispatched.
- Does not reinterpret architecture. Stops on ambiguity.
- Does not perform the Independent Auditor's work itself.
- Never authorises Stage D onward until Stage A–C (repository implementation → merge → post-merge CI GREEN) is fully closed.

### Implementer — FORGE (Stage A only)

FORGE performs **only**:

- creation of the canonical Git-tracked `launchd` scheduler artifacts under `deployment/launchd/` (§6.A below);
- the minimal, evidence-justified configuration representation described at §6.A.4, if and only if implementation evidence proves it is required — never assumed;
- static/local verification described at §6.B.

FORGE must **not** touch production, must not create `deployment/launchd/` content that embeds `T_ACT`, and must not modify classification logic, migrations, or any file outside the bounded scope at §6.A. FORGE must receive the full §0 doctrine inline in its dispatch prompt.

### HELM — production deployment/activation (Stages D–G only)

HELM performs the staged, read-mostly production deployment and activation sequence at §6.D–§6.G, strictly in order, strictly gated. HELM owns installation and runtime management of the `launchd` job on the Mac (per PID §114.6) but does not own the canonical scheduler definition — that is this WO's Git-tracked artifact, merged before HELM ever installs it.

### Independent Auditor

A fresh Auditor with **no inherited FORGE/HELM/Delivery-Controller conclusions**, dispatched **twice** under this WO: once against the repository implementation (before the implementation PR), and once against the production-activation evidence (before the activation-closure PR). Each dispatch must receive the full §0 doctrine inline and must inspect the actual evidence itself, never merely review prose.

---

## 5. Authorised production target

Sole target: **Mac mini `192.168.11.4`**, as defined by PID §0.

Trinity is: **retired/read-only migration archive + backup source only**, and is **NOT** a production target. No BAGMAN API writer may be started on Trinity, and no stage of this WO may touch Trinity's runtime state in any way. Production authority must never be inferred from the presence of live containers on any host — it is established solely by canonical PID §0.

---

## 6. Scope — staged delivery

### 6.A — Stage A: Repository implementation (FORGE)

FORGE may create or modify **only** the following, under `deployment/launchd/` (extending the existing `deployment/compose/`/`deployment/docker/` convention named at PID §114.6):

1. **Canonical plist** — a `launchd` property-list template implementing exactly: periodic invocation of the existing, unmodified worker (`scripts/process_evidence_classification_jobs.py`); cadence `StartInterval = 300` (seconds); the bounded command `--process --limit 20 --discovery-limit 50` plus the required `--runtime-dir`/`--worker-id` arguments; stdout/stderr redirected to a documented log path; **no** `T_ACT`/`BAGMAN_EVIDENCE_CLASSIFICATION_ACTIVATION_BOUNDARY` value, reference, or environment override anywhere in the plist. The plist must not define `RunAtLoad`-driven immediate classification execution at install time in a way that bypasses the disabled-by-default requirement of Stage D — it must be installable in a loaded-but-not-yet-triggered or explicitly-unloaded state.
2. **README** (`deployment/launchd/README.md`) documenting: exact installation path on the Mac; exact install/load command; exact disable/unload command; exact cadence and bounded invocation command (must read as functionally equivalent to the already-proven manual canary command); where stdout/stderr land; an explicit statement that `T_ACT` is never carried by the scheduler and remains sourced solely from the BAGMAN application/container environment; an explicit link to PID §114 as the governing architecture.
3. FORGE must inspect the existing `deployment/compose/` README convention and mirror its tone/structure (governed artifact, applied by an explicit command, never auto-generated).
4. **Conditional, evidence-gated config change**: FORGE may touch `deployment/compose/docker-compose.mac-production.yml` or any other existing deployment-configuration file **only if** FORGE's own inspection of production/Git evidence proves `BAGMAN_EVIDENCE_CLASSIFICATION_ACTIVATION_BOUNDARY` is not already correctly exposed to `bagman-api`'s container environment in a Git-tracked way. FORGE must not assume this change is needed. If FORGE finds the value is already correctly represented in Git/production configuration, FORGE must leave it untouched and say so explicitly in its return. If FORGE finds it is genuinely missing or wrong, FORGE must name the exact gap and propose the minimal fix, but must **STOP before touching production** — a config-file change in Git is in-scope; applying it to the live Mac is Stage D/E's job, not Stage A's.

No production action of any kind occurs in Stage A.

### 6.B — Stage B: Repository verification (FORGE, pre-audit)

Before any PR or Independent Audit:

1. Run the relevant existing test suite(s) touched or adjacent to this change (if any exist for deployment artifacts; note if none exist — that is not itself a defect, deployment artifacts are not Python modules).
2. Validate plist syntax (e.g. `plutil -lint` if available, or an equivalent well-formedness check appropriate to the environment FORGE is running in — note explicitly if the exact Apple tool is unavailable in FORGE's own environment and what alternative check was used instead).
3. Validate the exact invocation command/path referenced in the plist against the real, current signature of `scripts/process_evidence_classification_jobs.py` (argument names, required vs. optional flags).
4. Run `gitleaks detect` against the branch.
5. Confirm, via `git diff --stat` against the exact base SHA, that no unintended file changed — the expected diff is `deployment/launchd/` (new) plus, only if §6.A.4's condition was met, the one named existing config file.
6. Confirm no classification-logic file (`services/evidence/classification*.py`, `scripts/process_evidence_classification_jobs.py` itself, any migration) was modified.
7. Confirm the plist/README do not introduce a daemon, broker, queue, or cursor design of any kind — a literal re-read against PID §114.1's prohibition list.
8. Confirm `T_ACT`/`BAGMAN_EVIDENCE_CLASSIFICATION_ACTIVATION_BOUNDARY` is not duplicated, hardcoded, or newly introduced anywhere in the Stage A diff outside its single existing, already-governed source.

No production contact is authorised in Stage B. If verification genuinely cannot be completed without read-only production discovery (e.g. confirming the exact current state of `BAGMAN_EVIDENCE_CLASSIFICATION_ACTIVATION_BOUNDARY` in the live container, if the §6.A.4 condition is in question), FORGE must say so explicitly and request that specific, narrow, read-only discovery from the Delivery Controller rather than silently either skipping the check or contacting production itself — the Delivery Controller must prefer no production contact over Stage C if the question can instead be resolved by inspecting canonical Git evidence (e.g. the existing `docker-compose.mac-production.yml` and the governance-recovery closure document's own prior production findings, which already recorded container environment facts read-only on 2026-10-07).

### 6.C — Stage C: PR / Independent Audit / Architect Acceptance / Merge (repository implementation)

Repository implementation must complete `Implementer → Independent Audit (repository content) → PR → Architect Acceptance → Merge → post-merge Security GREEN` before any production deployment. No production deployment may be sourced from an unmerged branch or an un-accepted SHA — canonical deployment authority is the merged `main` SHA, and only that SHA.

### 6.D — Stage D: Production deploy, disabled (HELM)

After Stage C closes GREEN, HELM may, read-mostly:

1. contact only the canonical production Mac (`192.168.11.4`);
2. install the canonical plist from the merged `main` SHA, leaving it **disabled/unloaded** — no recurring execution yet;
3. verify the exact installed file content matches the canonical Git blob byte-for-byte (e.g. hash comparison);
4. verify production BAGMAN version/configuration is otherwise unchanged by this step.

No recurring execution, and no other production mutation, occurs in Stage D.

### 6.E — Stage E: Pre-activation safety proof (HELM, read-only)

Before enabling `launchd`, HELM must prove, all read-only:

1. **Production identity** — the inspected host is the canonical Mac mini per PID §0; Trinity is not touched.
2. **`T_ACT`** — the production `bagman-api` container environment resolves `BAGMAN_EVIDENCE_CLASSIFICATION_ACTIVATION_BOUNDARY` to exactly `2026-10-02T09:58:51Z`.
3. **Historical gate** — read-only query: count of `EvidenceClassificationJob` rows joined to an `EvidenceItem` with `created_at < T_ACT` must equal exactly `0`. **Any non-zero result is a hard STOP — RED.**
4. **Current health** — PostgreSQL, BAGMAN API, AI gateway, classification-related tables all healthy/reachable; no unexpected scheduler already installed; no conflicting `launchd` job present.
5. **Xero isolation baseline** — record existing Xero-related state (current `XeroAccountSuggestion`/`XeroAccountAssignment`/`XERO_ACCOUNT_REQUIRED` counts) as a pre-activation baseline, for later comparison. No mutation.

Any failed gate: **STOP — RED. Do not proceed to Stage F.**

### 6.F — Stage F: Explicit enablement (HELM)

Only after **every** Stage E gate is GREEN may HELM enable/load the `launchd` scheduler, using the exact command documented in `deployment/launchd/README.md`. Enablement is a deliberate, explicitly-invoked, individually-recorded governed action — never an incidental side effect of installing the plist in Stage D.

### 6.G — Stage G: Bounded observation (HELM + Delivery Controller)

Observe enough real scheduled executions (naturally occurring, bounded in time — not open-ended) to prove:

1. `launchd` fires at the intended ~300-second cadence;
2. the worker is actually invoked each time;
3. each invocation exits with the expected result/report;
4. the JSON run report is created/updated each time;
5. discovery remains bounded (`--discovery-limit 50`);
6. processing remains bounded (`--limit 20`);
7. no duplicate job is created for any `evidence_id`;
8. no `EvidenceItem` with `created_at < T_ACT` ever enters automatic classification;
9. Needs You behaviour remains correct for any genuine classification outcome produced;
10. no Xero side effect occurs beyond the Stage E baseline;
11. no architecture drift (no new file/process/config appears beyond what Stage D installed).

Prefer naturally arriving evidence. **Evidence must not be manufactured merely for convenience.** If no natural eligible evidence arrives during the bounded observation window, this Work Order explicitly permits proving correct scheduler execution and correct no-op behaviour (cadence firing, bounded discovery returning zero candidates, report written, no duplicate/historical job created) as sufficient Stage G evidence on their own — a real eligible item is not required to prove the mechanism works correctly, only to additionally prove full end-to-end classification lineage if one happens to arrive. This WO does **not** authorise fabricating a fake business document or triggering a manufactured mailbox sweep to force one.

---

## 7. Gmail finding is explicitly excluded

PID §112.E records `TOKEN_REFRESH_FAILED` for two Gmail sweep attempts. This Work Order may note that finding if encountered again but must **not**: refresh OAuth; reconnect Gmail; change credentials; edit mailbox configuration; fix the issue. Gmail remediation requires a separate governed Work Order.

---

## 8. Absolute exclusions

This Work Order authorises **NO** production mutation beyond exactly what Stages D/F explicitly name (install-disabled, then explicit enable). It does **not** authorise:

- Gmail OAuth remediation;
- any Xero behavioural change or ledger write;
- historical classification backfill;
- any change to `T_ACT`'s value;
- any change to `classify_evidence` logic;
- any AI-inference redesign;
- a persistent worker daemon;
- an application-internal scheduler;
- Redis, Celery, Kafka, or any generic broker/queue;
- cursor or reconciliation architecture of any kind;
- an ingestion-side classification enqueue;
- a GUI classification/scheduler dashboard;
- any metrics stack, Prometheus, Grafana, or bespoke monitoring/scheduler-state database;
- any new scheduler-level or application-level lock (e.g. `MailboxSweepLock` or equivalent) added merely as belt-and-braces engineering;
- any unrelated production cleanup;
- any Trinity production use.

If any stage's own verification would require one of these actions: **STOP and return to Architect.** Do not work around the exclusion.

If actual production evidence, gathered under this WO, shows that `launchd`-driven overlap produces a material operational or correctness issue not already handled by the existing PostgreSQL-level mechanisms (unique `evidence_id` constraint, idempotent submission, `SKIP LOCKED`, stale-claim recovery): **STOP and return to Architect** — do not silently add a lock.

---

## 9. Read-only query discipline

Any production database query outside the explicitly-authorised Stage D/F mutations must be demonstrably read-only. Prefer `SELECT`; schema inspection; transaction mode `READ ONLY` where supported. No DDL. No DML outside what Stage D/F's own narrow, named actions require (installing/enabling the scheduler itself involves no DDL/DML at all — it is a filesystem/`launchctl` operation only). If a tool or command might implicitly mutate state beyond what a stage explicitly authorises, do not use it without Architect review.

---

## 10. Evidence preservation

Both FORGE and HELM/the Delivery Controller must capture sufficient evidence to support every finding without exposing secrets. Do **not** commit: passwords; OAuth tokens; API keys; raw secret files; personal email bodies unless strictly necessary; sensitive production payloads unrelated to the proof. Use: IDs; timestamps; hashes; counts; redacted excerpts; schema facts; command outputs with secrets removed. Run `gitleaks` before any PR this work produces (both the repository-implementation PR and the activation-closure PR).

---

## 11. Deliverables

### 11.1 Repository implementation (Stage A)

```text
deployment/launchd/<canonical-plist-filename>.plist
deployment/launchd/README.md
```

and, only if §6.A.4's condition is met, the one named existing deployment-configuration file.

### 11.2 Activation closure document (after Stage G)

```text
work-orders/WO-BAGMAN-114-AUTOMATIC-CLASSIFICATION-SCHEDULER-CLOSURE.md
```

containing, at minimum:

1. WO identifier.
2. Canonical repository-implementation merge SHA.
3. Production authority proof (Stage E.1).
4. Exact Stage D/E/F/G commands/checks performed.
5. `T_ACT` verification (Stage E.2) and re-verification at closure.
6. Zero-pre-`T_ACT`-job proof — both the Stage E pre-activation result and a final re-run at closure.
7. Current health (Stage E.4).
8. Xero isolation — baseline (Stage E.5) and post-observation comparison (Stage G.10).
9. Bounded-observation evidence (Stage G, all 11 items).
10. Scheduler health/state at closure (loaded, cadence confirmed, no drift).
11. Gmail finding noted but untouched, if encountered.
12. Deviations.
13. Evidence references.
14. Implementer/HELM verdict.

No secrets in either deliverable.

---

## 12. Independent Audit requirements

### 12.1 Repository-implementation audit (before Stage C's PR)

A fresh Auditor, no inherited conclusions, dispatched against the Stage A/B diff. Must independently verify, at minimum: correct parent PID (§114, `CLOSED GREEN`); exact canonical base SHA; scope limited to `deployment/launchd/` (plus the conditional file, if used, with its necessity independently re-checked); exclusions preserved; `launchd` mechanism and exact 300-second cadence; exact `--limit 20`/`--discovery-limit 50` bounds; `T_ACT` not present/duplicated/overridden anywhere in the new artifacts; no new lock; no daemon/broker/cursor design; `gitleaks` clean; the plist's invocation command genuinely matches the real worker script's current signature.

### 12.2 Production-activation audit (before the activation-closure PR)

A second fresh Auditor, no inherited conclusions (including no inheritance from the Stage A audit), dispatched against the real production evidence after Stage G. Must independently repeat, read-only, wherever possible: the zero-pre-`T_ACT` hard gate; exact `T_ACT` value in the live container; exact installed plist content vs. canonical Git; actual observed cadence/invocation evidence; Xero isolation (baseline vs. post-observation); scheduler health; absence of any other production mutation. Must explicitly answer: does the production evidence support "recurring automatic classification is operating safely, without historical backfill, duplicate jobs, Xero side effects, or architecture drift"?

Both audits must receive the full §0 doctrine inline and must bind their verdict to the exact commit/evidence-set audited.

---

## 13. Acceptance criteria

This Work Order's full delivery is GREEN only if **all** of the following hold:

1. Repository implementation is scoped exactly to `deployment/launchd/` plus, at most, one evidence-justified existing config file.
2. No classification-logic, migration, or application code is modified.
3. No daemon/broker/cursor/ingestion-enqueue design is introduced.
4. The canonical plist implements exactly `launchd`, 300-second cadence, and the existing worker with `--limit 20`/`--discovery-limit 50`.
5. `T_ACT` is never carried, passed, calculated, defaulted, or altered by the scheduler artifacts.
6. Repository-implementation Independent Audit returns GREEN.
7. Repository-implementation PR completes Architect Acceptance → true merge → post-merge Security GREEN.
8. Stage D installs disabled; no recurring execution before Stage F.
9. Stage E's zero-pre-`T_ACT`-job gate returns exactly `0` before enablement.
10. Stage F enablement is an explicit, individually-recorded action.
11. Stage G bounded observation proves correct cadence, bounded discovery/processing, no duplicate jobs, no historical backfill, correct Needs You behaviour (if applicable), and no Xero side effect.
12. No new scheduler/application lock was added.
13. Existing retry/stale-claim/same-fingerprint semantics remain unchanged.
14. The kill switch (disable/unload) is documented and, if exercised during this delivery, proven non-destructive.
15. Only the minimum observability named in PID §114.11 was used — no GUI dashboard, metrics stack, or bespoke monitoring subsystem.
16. Gmail remains untouched.
17. Trinity remains untouched.
18. Production-activation Independent Audit returns GREEN, explicitly answering the §12.2 question affirmatively.
19. The activation-closure PR completes Architect Acceptance → true merge → post-merge Security GREEN.
20. No production mutation occurred beyond exactly the named Stage D (install-disabled) and Stage F (enable) actions.

If any material claim required above cannot be independently supported, do **not** rewrite evidence to make it fit. Return `RED`/`HOLD` with the exact unsupported claim, and return to the Architect.
