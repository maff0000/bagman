# WO-BAGMAN-112F-GOVERNANCE-RECOVERY
## Automatic Evidence Classification Production Governance Recovery

**Lifecycle at creation:** `PROPOSED — NOT AUTHORISED FOR EXECUTION`
**Current execution state:** `CANONICAL — AUTHORISED FOR DELIVERY CONTROLLER DISPATCH`

Execution authorisation became effective only after the following durable gate evidence, per PID §113:

- Independent Audit review `5425424908` — GREEN;
- Architect Acceptance review `5427495956` — GREEN;
- PR #24 merge `f459dee0d218fd95bdd26e66098547ba81dceea5`;
- post-merge Security CI run `37455123778` — SUCCESS.

At creation, this document authorised only the creation and review of this Git-tracked Work Order — it did **not**, at that time, authorise dispatch of FORGE against it. That original, pre-approval state is preserved here as history and is not erased. This Work Order's required gates — review, Independent Audit, Architect Acceptance, and merge into canonical `main` — have since been durably completed, per the evidence cited above. Under PID §113's authoritative lifecycle rule, this Work Order is now `CANONICAL — AUTHORISED FOR DELIVERY CONTROLLER DISPATCH`. All scope, exclusions, and acceptance criteria stated elsewhere in this document (§§5–13) remain fully and unconditionally binding and unchanged by this correction.

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

Every future BAGMAN Delivery Controller and FORGE Implementer prompt derived from this WO must reproduce this doctrine inline. Agents must never be expected to remember it from another conversation.

---

## 1. Work Order storage convention (established by this document)

No pre-existing Git-tracked Work Order directory/file convention existed in BAGMAN before this document. The Architect establishes the following convention, effective from this WO forward:

```text
/work-orders/
```

at repository root. One authorised Work Order = one Markdown file. No generic workflow framework, database, schema, or automation is introduced around Work Orders — a plain Git-tracked Markdown document is sufficient.

This first Work Order is this file: `work-orders/WO-BAGMAN-112F-GOVERNANCE-RECOVERY.md`.

---

## 2. Identity

| Field | Value |
|---|---|
| **Identifier** | `WO-BAGMAN-112F-GOVERNANCE-RECOVERY` |
| **Status** | `PROPOSED — NOT AUTHORISED FOR EXECUTION` |
| **Parent PID** | `PID.md §112.F` |
| **Applicable binding PID sections** | §0 (Current Production Authority), §111 (BAGMAN Delivery Governance Doctrine), §112.A (classification architecture), §112.B (activation boundary), §112.C (reported production state), §112.D (reported canary result), §112.E (Gmail finding — explicitly separate), §112.F (governance deviation — the section this WO exists to close), §112.G (scheduler prohibition) |
| **Exact technical baseline SHA** | `7288b29303571a4dddefbc75b9dfd91fa91d47f6` |

This SHA is the canonical baseline against which this Work Order was authored. The eventual Delivery Controller must verify canonical ancestry and the merged state of this WO before any dispatch. Unexpected code drift between this baseline and the dispatch-time canonical `main` requires STOP and Architect review — never silent continuation.

---

## 3. Purpose

This Work Order exists **solely** to close the governance deviation recorded at §112.F.

It must cause the previously-reported 2026-10-02 production rollout/canary to be **independently re-verified through the governed §111 process**.

This is primarily a **read-only production evidence and assurance delivery** — not a new implementation.

The desired chain once this WO itself becomes approved/canonical:

```text
BAGMAN Delivery Controller
        ↓
FORGE Implementer — bounded read-only evidence collection
        ↓
Fresh Independent Auditor — independently repeats/verifies evidence
        ↓
Git-tracked closure PR
        ↓
Architect Acceptance
        ↓
Merge
        ↓
Governance deviation CLOSED GREEN
```

---

## 4. Roles

### Delivery Controller — BAGMAN

- Re-reads the canonical WO from Git before dispatch.
- Verifies the exact authorised SHA.
- Gives FORGE only the bounded WO mandate below.
- Does not reinterpret architecture.
- Stops on ambiguity.
- Does not perform the Independent Auditor's work itself.

### Implementer — FORGE

FORGE performs **only**:

- read-only production inspection;
- evidence capture;
- reconciliation;
- creation of the Git-tracked closure/evidence document (§12).

FORGE must **not** make production changes. FORGE must receive the full §111 doctrine inline in its dispatch prompt (reproduced at §0 above — the Delivery Controller must carry it forward verbatim).

### Independent Auditor

A fresh Auditor with **no inherited FORGE conclusions** must independently inspect the real evidence and, where safe/read-only, repeat the critical live checks. The Auditor must not merely review FORGE's prose — it must go to the actual evidence itself.

---

## 5. Authorised production target

Sole target: **Mac mini `192.168.11.4`**, as defined by PID §0.

Trinity is: **retired/read-only migration archive + backup source only**, and is **NOT** a production target. No BAGMAN API writer may be started on Trinity.

The Auditor/Implementer may inspect Git evidence concerning Trinity if needed to confirm authority, but must not mutate Trinity in any way.

---

## 6. Scope — FORGE read-only evidence collection

The eventual FORGE execution must independently establish the real current/live and historical evidence supporting or contradicting §112.C/D. **No mutation is authorised anywhere in this scope.**

### 6.1 Production authority

Directly prove: inspected host is Mac mini `192.168.11.4`; canonical BAGMAN application/database/object store reside there; Trinity is not acting as a BAGMAN writer.

### 6.2 Deployed application

Verify production image identity and embedded `BAGMAN_GIT_COMMIT`. Expected reported deployment:

```text
bagman-api:main-493af0c
493af0c3923bf6f1bdd308521da8f3aca3fafac2
```

If production has legitimately moved since the canary, do **not** automatically call RED. Instead establish through durable evidence what changed, when, and under what authorised Git record. If no authorised explanation exists, STOP RED.

### 6.3 Database migration state

Verify current Alembic state. Reported state to re-verify: `ae936a444eae`. Verify migration ancestry:

```text
f1a2b3c4d5e6 → a7f34c9e2d18 → ae936a444eae
```

Prove: `143b86b2ab44` is not part of canonical migration history; `evidence_classification_reconciliation_cursors` does not exist in the intended production schema; `evidence_classification_jobs` has the expected durable schema and unique `evidence_id` protection.

### 6.4 Backup evidence

Verify, read-only, the reported pre-rollout backup if still present:

```text
/opt/bagman/backups/pre-classification-simplification-deploy-20261002T095317Z.dump
```

Reported properties: size `3,420,026` bytes; SHA-256 `5bb3509c3ad0288aba28a608b5d54645489f1c46c8344e72d49bdc8a8a59310b`. Do **not** restore it.

### 6.5 Activation boundary

Canonical `T_ACT = 2026-10-02T09:58:51Z`. Prove: PID §112.B contains this exact value; no scheduler has silently substituted another value; no recurring worker configuration currently carries a different activation boundary; no historical automatic job exists with `EvidenceItem.created_at < T_ACT`.

**This last query is a HARD acceptance gate. Expected result: `0`.**

### 6.6 Canary worker evidence

Locate and inspect the preserved canary runtime report if available, including:

```text
/opt/bagman/runtime/evidence-classification-canary
```

Cross-check it with database state. Reported worker bounds: discovery limit `1`, process limit `1`. Reported outcome: `created_count = 1`, `claimed_count = 1`, `SUCCEEDED = 1`. **Do not execute the worker again.**

### 6.7 Exact canary job

Re-verify job `01a0fc10-69f4-78f0-b4e1-f13661e8583d`. Reported: status `SUCCEEDED`; attempt count `1/3`; actor `bagman-evidence-classification-discovery`; outcome `AI_PROPOSAL_REVIEW_REQUIRED`; no last error. Cross-check the associated `EvidenceItem` and its `created_at >= T_ACT`.

### 6.8 Classification

Re-verify the corresponding classification lineage. Reported canary result: deterministic classification `NO_MATCH`; AI proposal `NON_ACCOUNTING_DOCUMENT`; status `REVIEW_REQUIRED`; source `AI_PROPOSAL`; reason `AI_PROPOSAL_PENDING_REVIEW`. Do **not** modify it.

### 6.9 AI invocation

Re-verify `01a0fc10-6a0e-7b09-b343-e441534db9d8`. Reported: task `DOCUMENT_TYPE_PROPOSAL`; version `v2`; provider `LITELLM`; capability alias `bagman-core`; status `SUCCEEDED`. Correlate it to the canary evidence.

### 6.10 Needs You

Re-verify item `01a0fc10-affd-75da-b9b9-5af3283a8491`. Reported type `CLASSIFICATION_REVIEW`; reported original status `OPEN`. Do **not** change its status. If its current state has legitimately changed since 2026-10-02, reconstruct the audit trail and report it accurately rather than overwriting or assuming failure.

### 6.11 Prospective evidence / mailbox sweep

Reconstruct the reported Microsoft Graph sweep sufficiently to verify: four legitimate `EvidenceItem`s were created after `T_ACT`; they were produced through the ordinary production mailbox mechanism; they were not synthetic backfill manufactured for the canary. **Do not run another sweep** merely to recreate evidence.

### 6.12 Xero isolation

Verify specifically for the canary evidence and canary execution window: no `XeroAccountSuggestion`; no `XeroAccountAssignment`; no `XERO_ACCOUNT_REQUIRED`; no Xero ledger mutation attributable to the canary. Do **not** require globally-zero Xero activity if legitimate unrelated accounting activity has occurred since — scope the proof causally to the canary.

### 6.13 Inert deployment

Use historical/current evidence to verify the deployment itself did not classify evidence before the explicitly-invoked canary worker. Confirm, as far as durable evidence supports: job table began at zero; deployment itself did not create classification jobs; worker invocation was the event that created the canary job.

### 6.14 Scheduler

Prove current state: `NOT INSTALLED`. Check, read-only: cron; launchd/system scheduler applicable to the Mac; Docker/Compose containers; persistent worker loops; other BAGMAN scheduling configuration. Also verify `T_ACT` is not silently persisted into an unauthorised recurring runner.

### 6.15 Current health

Read-only health inspection: application; database; object store; scanner; AI gateway; GUI/HTTP surface. This is assurance only — do **not** restart unhealthy components under this WO. If anything is unhealthy, report and STOP rather than repair.

---

## 7. Gmail finding is explicitly excluded

PID §112.E records `TOKEN_REFRESH_FAILED` for two Gmail sweep attempts. This Work Order may confirm the finding exists in logs/evidence. It must **not**: refresh OAuth; reconnect Gmail; change credentials; edit mailbox configuration; fix the issue. Gmail remediation requires a separate governed Work Order.

---

## 8. Absolute exclusions

This Work Order authorises **NO production mutation**. FORGE and the Auditor must **not**:

- restart `bagman-api`;
- rebuild or redeploy containers;
- run Alembic upgrade/downgrade;
- insert/update/delete production DB rows;
- resolve Needs You;
- rerun the classification worker;
- run a new classification canary;
- alter `T_ACT`;
- install cron/timer/scheduler;
- perform historical backfill;
- invoke Xero Account Suggestion;
- write to Xero;
- alter OAuth credentials;
- fix Gmail;
- modify object-store evidence;
- restore the backup;
- touch Trinity runtime state.

If verification requires any of these actions: **STOP and return to Architect.**

---

## 9. Read-only query discipline

Any production database query must be demonstrably read-only. Prefer `SELECT`; schema inspection; transaction mode `READ ONLY` where supported. No DDL. No DML. If a tool or command might implicitly mutate state, do not use it without Architect review.

---

## 10. Evidence preservation

FORGE must capture sufficient evidence to support every finding without exposing secrets. Do **not** commit: passwords; OAuth tokens; API keys; raw secret files; personal email bodies unless strictly necessary; sensitive production payloads unrelated to the proof. Use: IDs; timestamps; hashes; counts; redacted excerpts; schema facts; command outputs with secrets removed. Run `gitleaks` before any PR this work produces.

---

## 11. Closure deliverable

The eventual FORGE execution must create:

```text
work-orders/WO-BAGMAN-112F-GOVERNANCE-RECOVERY-CLOSURE.md
```

containing:

1. WO identifier.
2. Canonical execution SHA.
3. Production authority proof.
4. Commands/checks performed.
5. Production image/commit.
6. Alembic state.
7. Backup verification.
8. `T_ACT` verification.
9. Zero-pre-`T_ACT`-job proof.
10. Canary report verification.
11. Exact job verification.
12. Classification verification.
13. AI invocation verification.
14. Needs You verification/current state.
15. Mailbox evidence reconstruction.
16. Xero isolation proof.
17. Scheduler absence proof.
18. Current health.
19. Gmail finding noted but untouched.
20. Deviations.
21. Evidence references.
22. Implementer verdict.

No secrets in this document.

---

## 12. Independent Audit requirements (for the eventual governance-recovery delivery)

After FORGE completes its read-only evidence collection and closure document, BAGMAN must dispatch a **fresh** Independent Auditor.

The Auditor must receive this doctrine inline (§0 above, reproduced verbatim at dispatch time).

The Auditor must:

- read canonical PID §0/§111/§112;
- read this canonical WO;
- inspect the actual FORGE diff/evidence;
- independently repeat the critical live read-only checks;
- not inherit FORGE conclusions;
- explicitly determine whether §112.C/D are now independently substantiated.

Required Auditor verdict: `GREEN` or `RED`. The audit itself must be made durable on the eventual closure PR.

---

## 13. Acceptance criteria for the governance-recovery delivery

The future delivery is GREEN only if **all** of the following hold:

1. Mac mini authority is independently confirmed.
2. Production deployment/migration lineage is explained and authorised.
3. No reconciliation cursor schema exists.
4. `T_ACT` remains exactly `2026-10-02T09:58:51Z`.
5. Zero classification jobs exist for evidence with `created_at < T_ACT`.
6. Canary report/database evidence reconcile.
7. Exact canary job is verified.
8. Classification/AI/Needs You lineage reconciles.
9. Xero isolation is independently supported.
10. Scheduler is absent.
11. No historical backfill occurred.
12. No production mutation occurs during recovery.
13. Gmail issue remains untouched.
14. Independent Auditor returns GREEN.
15. Closure evidence is Git-tracked.
16. Architect explicitly accepts the closure PR before merge.

If any material historical claim in §112.C/D cannot be independently supported, do **not** rewrite history to make it fit. Return `RED`/`HOLD` with the exact unsupported claim.

---

## 14. Work Order lifecycle state — authorisation complete (per PID §113)

This Work Order's original four authorisation conditions, stated at creation, were: (1) reviewed; (2) independently audited as a WO document; (3) Architect-accepted; (4) merged into canonical `main`. Per PID §113's authoritative lifecycle rule, required post-merge CI on the merge SHA is also now a binding gate for execution authority.

All conditions are satisfied by the durable evidence cited in the header above: Independent Audit review `5425424908` — GREEN; Architect Acceptance review `5427495956` — GREEN; merge `f459dee0d218fd95bdd26e66098547ba81dceea5`; post-merge Security CI run `37455123778` — SUCCESS.

Therefore, per PID §113, this Work Order is `CANONICAL — AUTHORISED FOR DELIVERY CONTROLLER DISPATCH`, and BAGMAN **may** dispatch FORGE against it.

This record changes lifecycle state only. It does not reopen, reinterpret, or alter any other content of this document. §§5–13 above remain fully and unconditionally binding and unchanged.

Any later edit to this canonical Work Order's substantive content requires the same governed correction process (Independent Audit → Architect Acceptance → merge → required post-merge CI) and does not automatically inherit this existing execution authorisation — a changed head would require its own fresh audit, acceptance, merge, and CI before it, too, is authorised for dispatch.
