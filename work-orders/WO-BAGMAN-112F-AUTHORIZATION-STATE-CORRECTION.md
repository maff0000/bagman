# WO-BAGMAN-112F-AUTHORIZATION-STATE-CORRECTION
## Correct stale execution-authorisation state in WO-BAGMAN-112F-GOVERNANCE-RECOVERY

**Status:** `PROPOSED — NOT AUTHORISED FOR EXECUTION`

This document authorises creation of this correction Work Order document only. It does **not** itself perform the correction it specifies, and does **not** authorise dispatch of FORGE to perform that correction. FORGE may be dispatched to make the change specified at §4 only after THIS document has itself been independently audited, Architect-accepted, and merged into canonical `main`.

---

## 0. Mandatory BAGMAN delivery doctrine (reproduced inline — binding on every future prompt derived from this WO)

> **Architecture decision → PID/Amendment → Git-tracked Work Order → Delivery Controller → Implementer → Independent Audit → PR → Architect Acceptance → Merge → Closure**

Rules:

- The **Architect** owns architecture, PIDs, amendments, sequencing, and acceptance.
- The **Delivery Controller** owns bounded execution and must dispatch implementation only against an approved Work Order.
- The **Implementer / FORGE** works only within that Work Order and must not invent architecture or widen scope.
- Every Work Order must identify its parent PID, applicable amendments, exact base SHA, scope, exclusions, required tests/proofs, and acceptance criteria.
- If implementation exposes an architectural ambiguity, STOP and return it to the Architect. Do not improvise.
- Corrections and follow-up work require the same governed Work Order process.
- Git/GitHub is durable authority; chat is only the control surface.

Hard invariants:

**NO PID → NO WORK ORDER.**
**NO WORK ORDER → NO IMPLEMENTATION.**
**NO INDEPENDENT AUDIT + ARCHITECT ACCEPTANCE → NO MERGE.**
**NO GIT RECORD → NOT DURABLE PROJECT AUTHORITY.**

Every Bagman Delivery Controller and FORGE Implementer prompt derived from this WO must reproduce this doctrine inline.

---

## 1. Identity

| Field | Value |
|---|---|
| **Identifier** | `WO-BAGMAN-112F-AUTHORIZATION-STATE-CORRECTION` |
| **Status** | `PROPOSED — NOT AUTHORISED FOR EXECUTION` |
| **Parent PID** | `PID.md §112.F` |
| **Applicable binding sections** | PID §0 (Current Production Authority), PID §111 (BAGMAN Delivery Governance Doctrine), PID §112.F (Governance deviation — recorded honestly), and the canonical document `work-orders/WO-BAGMAN-112F-GOVERNANCE-RECOVERY.md` |
| **Exact base SHA** | `f459dee0d218fd95bdd26e66098547ba81dceea5` |

This SHA is the canonical baseline against which this correction Work Order was authored. Confirmed, before drafting, to be the exact `origin/main` tip, and confirmed to be PR #24's own merge commit.

---

## 2. Finding this WO exists to correct

A fresh FORGE Implementer, dispatched against `work-orders/WO-BAGMAN-112F-GOVERNANCE-RECOVERY.md`, correctly halted before touching production. It read that document directly and found:

- its header states: `Status: PROPOSED — NOT AUTHORISED FOR EXECUTION`;
- its own §14 states FORGE may be dispatched only after the document is "(1) reviewed, (2) independently audited as a WO document, (3) Architect-accepted, and (4) merged into canonical `main`";
- checking canonical Git history directly, FORGE found only one PR (#24) ever touched that file, with no later PR recording items (1)–(3) as a separate event.

**This WO's own finding, independently re-verified against the real Git/GitHub record before drafting this document:**

Conditions (1)–(4) were in fact all durably satisfied — on PR #24 itself, not via any later, separate PR:

- **(1) reviewed / (2) independently audited as a WO document**: a fresh Independent Auditor, with no inherited Delivery Controller conclusions, explicitly reviewed `WO-BAGMAN-112F-GOVERNANCE-RECOVERY.md` against PID §0/§111/§112 and the Architect's own 18-item mandate, and posted the verdict `INDEPENDENT AUDIT — GREEN` as a durable PR review on PR #24 — review id **`5425424908`**.
- **(3) Architect-accepted**: the Project Architect's acceptance, `ARCHITECT ACCEPTANCE — GREEN`, was posted as a durable PR review on PR #24 — review id **`5427495956`**.
- **(4) merged into canonical `main`**: PR #24 was merged via a true merge commit, SHA **`f459dee0d218fd95bdd26e66098547ba81dceea5`** — the exact current `origin/main` tip, independently re-confirmed in §1 above.
- **Post-merge CI**, additionally (not one of the WO's own four stated conditions, but further corroborating evidence the state transition is genuine and complete): Security workflow run **`37455123778`**, `completed` / `success`, on that exact merge SHA — independently re-confirmed before drafting this document.

**Root cause**: the governance-recovery WO's own textual `Status:` header and §14 prose were written at document-creation time, *before* its own audit/acceptance/merge occurred, and were never mechanically updated afterward to record that the state transition had completed. A fresh reader (human or FORGE) encountering only the static text, with no cross-reference to the actual PR #24 review/merge history, reasonably concludes the document is still unauthorised — even though the real, durable Git/GitHub record shows otherwise.

This is a **governance-document state-recording defect**, not an architecture defect and not a production defect. **FORGE's stop was correct** — it is exactly the behaviour PID §111 requires ("if implementation exposes an architectural ambiguity, STOP and return it to the Architect — do not improvise").

---

## 3. Purpose

This Work Order exists **solely** to authorise a narrow, Git-tracked correction to `work-orders/WO-BAGMAN-112F-GOVERNANCE-RECOVERY.md`'s own authorisation-state text, so that document becomes internally self-consistent with the real, durable PR #24 record — **without** rewriting history to suggest the document was ever authorised before it actually was.

This correction WO does not perform that edit itself. It specifies exactly what the future edit must say and must not say, for a later, separately-authorised FORGE dispatch to carry out — per §111's own "corrections and follow-up work require the same governed Work Order process."

---

## 4. Exact future implementation authorised by this correction WO (NOT authorised yet — only specified)

Once THIS correction WO is independently audited, Architect-accepted, and merged, Bagman may dispatch FORGE to modify **only**:

```text
work-orders/WO-BAGMAN-112F-GOVERNANCE-RECOVERY.md
```

No other file. The implementation must make that existing document self-consistent, in three places:

### 4.1 Header status

Replace the stale status with a durable historical/current form such as:

```text
Status: CANONICAL — AUTHORISED FOR DELIVERY CONTROLLER DISPATCH

Originally created as PROPOSED — NOT AUTHORISED FOR EXECUTION.
Execution authorisation became effective only after:
- Independent Audit review 5425424908 — GREEN;
- Architect Acceptance review 5427495956 — GREEN;
- PR #24 merge f459dee0d218fd95bdd26e66098547ba81dceea5;
- post-merge Security run 37455123778 — SUCCESS.
```

Exact prose may be tightened by the eventual Implementer, but all four durable facts (the two review ids, the merge SHA, the CI run id) must remain present, verbatim-identifiable. The correction must **not** imply FORGE was authorised to execute before those four events occurred.

### 4.2 Opening paragraph

Correct the opening paragraph (currently: "This document authorises creation of the Work Order document only... FORGE may be dispatched only after this document itself has been reviewed, independently audited, Architect-accepted, and merged...") so it explicitly distinguishes:

- the **original, pre-merge state** (proposed, not yet authorised — preserved as history, not deleted); from
- the **current, canonical, post-approval state** (now authorised for Delivery Controller dispatch, because its own stated approval conditions were durably fulfilled, per §4.1's evidence).

### 4.3 §14

Replace the stale, purely-prospective wording ("Until this Work Order is (1) reviewed, (2) independently audited..., (3) Architect-accepted, and (4) merged..., BAGMAN must NOT dispatch FORGE against it") with an explicit **state-transition record** that makes clear:

- the four conditions were requirements for authorisation (unchanged from the original text's own intent);
- each is now satisfied, with the same four durable evidence references as §4.1 (audit `5425424908`; Architect Acceptance `5427495956`; merge `f459dee0...`; post-merge CI `37455123778`);
- therefore Bagman **may** dispatch FORGE against this WO;
- all scope, exclusions, and acceptance criteria elsewhere in the governance-recovery WO (its §§5–13) remain **fully and unconditionally binding**, unchanged by this correction.

---

## 5. What MUST NOT change (binding on the eventual correction implementation)

The later FORGE implementation of §4 must **not** alter: the governance-recovery WO's own stated purpose; its named production target; its read-only nature; any of its §6.x scope items; its Gmail exclusion (§7); its absolute exclusions list (§8); its read-only query discipline (§9); its evidence-preservation rules (§10); its closure-document requirements (§11); its Independent Audit requirements for the *future* governance-recovery delivery (§12); its 16 acceptance criteria (§13); `T_ACT`; any production identifier (job/classification/AI-invocation/Needs-You IDs, image tags, Alembic revisions, backup hashes); `PID.md`; any application code; any migration; any runtime configuration.

**This is an authorisation-state correction only.**

---

## 6. No retroactive fiction

The corrected governance-recovery WO must preserve chronology. The eventual correction must **not** rewrite history to make it appear the document was authorised at creation. The durable record must explicitly show the real sequence:

```text
PROPOSED → Independent Audit (5425424908) → Architect Acceptance (5427495956)
  → merge (f459dee0...) → GREEN post-merge CI (37455123778) → AUTHORISED FOR DISPATCH
```

That sequence being visible, honestly, in the corrected text is the entire point of this correction — not merely flipping a status word.

---

## 7. Acceptance criteria for this correction delivery

The correction implementation (§4, performed under a later, separate dispatch once THIS WO is itself approved) will be GREEN only if **all** of the following hold:

1. Only the canonical governance-recovery WO (`work-orders/WO-BAGMAN-112F-GOVERNANCE-RECOVERY.md`) changes.
2. The stale `PROPOSED — NOT AUTHORISED FOR EXECUTION` text is no longer presented as the *current* state.
3. The original proposed state remains historically recorded (not deleted, not hidden).
4. Independent Audit review `5425424908` is cited correctly.
5. Architect Acceptance review `5427495956` is cited correctly.
6. Merge SHA `f459dee0d218fd95bdd26e66098547ba81dceea5` is cited correctly.
7. Post-merge Security run `37455123778` is cited correctly.
8. The current state is unambiguously `AUTHORISED FOR DELIVERY CONTROLLER DISPATCH`.
9. No production scope/technical requirement changes anywhere in the governance-recovery WO.
10. No `PID.md`/code/config/migration changes anywhere.
11. A fresh Independent Auditor returns GREEN on the correction PR.
12. The Architect accepts the correction PR before merge.

---

## 8. What this document does NOT yet authorise

This document, by itself, authorises nothing beyond its own creation and review. It does **not** authorise:

- performing the §4 edit to the governance-recovery WO;
- dispatching FORGE for any purpose;
- any production action of any kind.

Until THIS document is (1) reviewed, (2) independently audited, (3) Architect-accepted, and (4) merged into canonical `main`, Bagman must **not** dispatch FORGE to perform the §4 correction.
