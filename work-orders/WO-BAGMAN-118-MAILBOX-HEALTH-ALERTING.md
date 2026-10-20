# WO-BAGMAN-118-MAILBOX-HEALTH-ALERTING
## Mailbox connection health, episode-aware incidents, and durable operator notification

**Lifecycle at creation:** `PROPOSED`
**Execution gate:** authorised only when exact-head Independent Audit GREEN, Architect Acceptance GREEN, canonical merge complete, and required post-merge CI GREEN all hold for this document's own exact content (PID §113's authoritative lifecycle rule).

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

Every future BAGMAN Delivery Controller and FORGE Implementer prompt derived from this WO must reproduce this doctrine inline.

---

## 1. Work Order storage convention

`work-orders/WO-BAGMAN-118-MAILBOX-HEALTH-ALERTING.md`, one Markdown file, no framework/database/automation — matching every prior WO in this repository.

---

## 2. Identity

| Field | Value |
|---|---|
| **Identifier** | `WO-BAGMAN-118-MAILBOX-HEALTH-ALERTING` |
| **Parent PID** | `PID.md §118` (Mailbox Connection Health and Operator Alerting — Architect ruling, 2026-10-19) |
| **Applicable inherited binding PID sections** | §0 (Current Production Authority); §111 (Delivery Governance Doctrine); §113 (WO lifecycle/authorisation semantics); §114 (scheduler — explicitly unaffected); §117.E (audit-event reuse precedent); §118.A–K (this amendment's full scope) |
| **Exact canonical architecture base SHA** | `14ab2c49eb1b5ebf5da87bd510af8d700a70e4a3` (canonical `main`, PID §118 merged) |

### 2.1 Base SHA discipline

This WO is authored against canonical `main` at the SHA above. The eventual Delivery Controller must independently re-verify canonical ancestry and that PID §118 remains unchanged before any FORGE dispatch. Unexpected drift requires STOP and Architect review.

---

## 3. Purpose

Implement the repository-side mechanics of PID §118: episode-aware mailbox-health incident detection (Gmail and IMAP wired to the pattern Microsoft already proves out), a durable notification outbox with fencing-protected claim recovery, and the transactional-consistency design §118.F requires — **repository implementation only**. Production activation (installing the new evaluator plist, provisioning the real Pushover credential) is explicitly a separate, later-gated stage, not authorised by this WO's own merge, mirroring WO-117's Stage A–C / Stage D–G split.

---

## 4. Roles

### Delivery Controller — BAGMAN
Re-reads this WO from Git before dispatch; verifies the exact SHA and that PID §118 remains canonical; gives FORGE only the bounded Stage-A mandate; stops on ambiguity; never authorises production activation until Stage A–C (implementation → merge → post-merge CI GREEN) is fully closed.

### Implementer — FORGE (Stage A only)
Performs **only** the bounded repository implementation at §6.A. Must not touch production, must not modify WO-114's scheduler, WO-117's authentication/session code, classification code, Xero write behaviour, or any file outside the bounded scope. Must receive the full §0 doctrine inline.

### Independent Auditor
A fresh Auditor with no inherited conclusions, dispatched against the repository implementation before the Stage A/B→C PR. Must independently reproduce the concurrency/fencing/transaction-consistency proofs required at §12 — not merely read the code and assert it looks correct.

---

## 5. Authorised production target (for the later, separately-gated activation stage only)

Sole eventual target: **Mac mini `192.168.11.4`** (`mac-prod-01`), per PID §0. Not touched by this WO's own Stage A–C.

---

## 6. Scope — staged delivery

### 6.A — Stage A: Repository implementation (FORGE)

FORGE may create or modify **only** the following:

1. **One new Alembic migration** — `mailbox_health_incidents` table (per PID §118.C: `incident_id` UUID PK, `mailbox_id` FK, `failure_class`, `status`, `first_observed_at`, `last_observed_at`, `resolved_at`, `recovery_evidence` jsonb, `metadata` jsonb) **with the partial unique index** `(mailbox_id, failure_class) WHERE status = 'OPEN'` created in the same migration — and `mailbox_health_notifications` table (per PID §118.E, **extended by this WO's own §1.A correction below**: `notification_id` UUID PK, `incident_id` FK, `purpose` (`INITIAL`/`REMINDER_24H`/`RECOVERY`), `state` (`PENDING`/`IN_PROGRESS`/`ACCEPTED`/`RETRYABLE_FAILURE`/`TERMINAL_FAILURE`/**`CANCELLED`**), `attempt_count`, `max_attempts`, `next_attempt_at`, `claimed_by`, `claimed_at`, `claim_generation` int default 0, `created_at`, `completed_at`, `provider_response_metadata` jsonb, **`cancellation_reason` jsonb nullable**) **with a real PostgreSQL unique constraint/index `(incident_id, purpose)`** — each purpose occurs **at most once, ever, per incident** (§1.A). No existing table is altered.

#### 6.A.1.A — Mandatory correction: notification uniqueness and idempotent concurrent creation

A real PostgreSQL unique constraint on `(incident_id, purpose)` (not merely application-level deduplication) — `INITIAL`, `RECOVERY`, and `REMINDER_24H` are each singular per incident: **exactly one reminder total per incident, ever** (not a recurring daily nag — this is a deliberate, explicit clarification of PID §118.I's "one controlled reminder" language, resolved here as "one," not "one per day"). Creation must use `INSERT ... ON CONFLICT (incident_id, purpose) DO NOTHING RETURNING *`; when the `INSERT` returns no row (a concurrent creator already won), the caller must `SELECT` and return the already-existing row — **never** treat the conflict as an error, and **never** attempt a second dispatch. This must be proven by an executable concurrent-creation test (§12).

#### 6.A.1.B — Mandatory correction: reclaim, retry exhaustion, and dual-predicate fencing

The claim predicate (PID §118.E.1, refined here) must:
- Match due `PENDING`/`RETRYABLE_FAILURE` work (`next_attempt_at <= now()`) **and** expired `IN_PROGRESS` work (`claimed_at < now() - interval '<claim_timeout>'`) in one predicate, exactly as PID §118.E.1 specifies.
- **Increment `attempt_count` atomically in the same claim `UPDATE`, every time a claim succeeds — including a reclaim of an expired `IN_PROGRESS` row.** A reclaimed attempt counts toward `max_attempts`; it is never a free/reset attempt. When the claiming `UPDATE` would bring `attempt_count` to or past `max_attempts`, the claim must instead transition the row directly to `TERMINAL_FAILURE` (never claim it as `IN_PROGRESS` again) — bounded exhaustion is enforced at claim time, not only at completion time.
- Apply bounded backoff via `next_attempt_at` exactly as PID §118.E already specifies.
- **Completion writes must be fenced by both `claim_generation` and current state**: `UPDATE ... WHERE notification_id=$id AND claim_generation=$my_generation AND state='IN_PROGRESS'` — the added `state='IN_PROGRESS'` predicate is defense-in-depth alongside the generation check, so a stale worker's write is rejected even in an edge case where generation alone might not have been bumped yet. A write matching zero rows is a correct, silent no-op — the stale worker must not treat this as an error requiring its own further action.
- This must be proven by executable crash/reclaim/exhaustion tests (§12) — not code inspection alone.

#### 6.A.1.C — Mandatory correction: pre-dispatch incident-state recheck and durable suppression

Before a claimed `INITIAL` or `REMINDER_24H` notification is actually sent to Pushover, the worker must re-check the parent incident's **current** `status`. If the incident has since transitioned to `RESOLVED` (recovered between notification creation/claim and actual send), the notification must be written directly to the new **`CANCELLED`** state (fenced by the same `claim_generation AND state='IN_PROGRESS'` predicate as any other completion write), with `cancellation_reason` recording the incident's resolution evidence, and a corresponding `AuditEvent` (e.g. `MAILBOX_HEALTH_NOTIFICATION_CANCELLED`) for a durable, auditable record — the external send to Pushover must **not** occur for a cancelled notification. **`RECOVERY` notifications are never subject to this check** — they exist specifically to announce the resolution and must always be sent once created, per PID §118.H/I. **This design does not, and must not pretend to, recall a notification whose state is already `ACCEPTED`** — Pushover's own API offers no retraction mechanism, and this WO does not invent one; the suppression check only ever prevents a **not-yet-sent** notification from going out after the fact. This must be proven by executable tests covering incident resolution while a notification sits in each of `PENDING`, `RETRYABLE_FAILURE`, and `IN_PROGRESS` (§12).
2. **`services/mailbox/health/` (new module)** — the incident/notification domain logic: `MailboxHealthIncidentRepository` (ABC + in-memory + Postgres, mirroring `core/entity.py`'s existing shape), `MailboxHealthNotificationRepository` (same shape), and the pure functions implementing PID §118.C/D's episode-detection and §118.D's three-consecutive-failure/expiry-based triggering logic for `AUTH_REQUIRED`/`PERSISTENT_PROVIDER_ERROR`/`OAUTH_RECONNECT_INCOMPLETE`. `STALE_INGESTION` is defined in the taxonomy but its detection logic is explicitly **not implemented** in this WO (PID §118.D/G: reserved, inactive, gated behind the not-yet-built recurring-sweep workstream) — a stub/`NotImplementedError` or equivalent explicit no-op is acceptable; a real check is not.
3. **Shared-transaction interface extension (PID §118.F's primary design)** — add an optional `session: Optional[Session] = None` parameter to: `MailboxHealthIncidentRepository`'s create/resolve methods (new, from item 2); `NeedsYouRepository.create_needs_you_item` and `.resolve_needs_you_item` (existing, `services/needs_you/needs_you.py` and its Postgres implementation); `MailboxHealthNotificationRepository`'s create method (new, from item 2). Every existing call site of the two modified `NeedsYouRepository` methods must continue to work unchanged (the parameter defaults to `None`, opening its own `session_scope()` exactly as today when omitted) — **FORGE must prove this with the full existing Needs-You test suite passing unmodified**, not merely assert it by inspection. If this interface extension proves genuinely unsafe or disproportionate for a reason not anticipated in PID §118.F, FORGE must STOP and return to the Architect — the §118.F fallback (idempotent reconciliation) is **not** authorised to be silently substituted by FORGE's own judgment.
4. **Provider-neutral Needs-You wiring** — generalise `app/api/routers/mailboxes_microsoft.py`'s existing `_sync_needs_you_for_connection_state` into a shared helper (exact location FORGE's own choice, e.g. `services/mailbox/health/needs_you_sync.py`), and call it from `app/api/routers/mailboxes_gmail.py` and `app/api/routers/mailboxes_imap.py` at the same four lifecycle points (connect, callback, disconnect, sweep) Microsoft already calls it from. **Microsoft's existing call sites and behaviour must not change** — this is additive wiring for the two providers that currently lack it, not a Microsoft rewrite.
5. **Health evaluator script** — `scripts/evaluate_mailbox_health.py` (or equivalent name), a bounded, single-invocation CLI script (mirroring `scripts/process_evidence_classification_jobs.py`'s own existing shape and JSON-per-run logging convention exactly) implementing: querying all `ACTIVE` mailboxes' `connection_state`/`mailbox_sweep_runs` history; creating/updating incidents per PID §118.C's transactional, fencing-safe design; creating notification intent per §6.A.1.A's idempotent-conflict design; claiming and attempting due/reclaimable notifications per §6.A.1.B's exact claim predicate, attempt-budget, and dual-predicate fencing; performing §6.A.1.C's pre-dispatch incident-state recheck and suppression before any actual Pushover call; recording `AuditEvent` outcomes (including cancellation). **Never** fetches email, advances a cursor, or invokes ingestion — read-only against mailbox/sweep state, write-only against the two new tables, `NeedsYouItem`, and `AuditEvent`.
6. **Notification client** — a narrow Pushover HTTP client (`services/notifications/pushover.py` or equivalent), reading its credential via the existing `BAGMAN_*_FILE` secret-file convention (new env vars, e.g. `BAGMAN_PUSHOVER_API_TOKEN_FILE`/`BAGMAN_PUSHOVER_USER_KEY_FILE`), used only by the evaluator script. Must never log or persist the credential value itself; must report provider HTTP status/response accurately, never claim device-delivery confirmation.
7. **A new, inert launchd plist** (`deployment/launchd/` or wherever WO-114's own plist lives in this repo — FORGE must locate and mirror that existing convention exactly) for the health-evaluator script, at a 15-minute `StartInterval`, structurally separate from WO-114's own plist file — **not installed on any host by this WO**; the file exists in the repository only, exactly mirroring WO-114's own "installed but inert until a separate production stage" discipline.
8. **Tests**: full domain/unit coverage for the new repositories and evaluator logic, plus the full concurrency/fencing/transaction-consistency matrix at §12 acceptance criteria.

No production action of any kind occurs in Stage A. FORGE must not modify: WO-114's scheduler artifacts or plist, WO-117's `core/auth.py`/`core/entity_context.py`/session middleware, classification code, Xero write behaviour, any existing migration, or any file outside the list above.

### 6.B — Stage B: Repository verification (FORGE, pre-audit)

1. Run the new test suite plus the full existing relevant suite (prove zero regression to the existing Microsoft mailbox-health wiring, the full Needs-You test suite with the new optional `session` parameter present, and WO-117's own auth/session test suite — none of this WO's changes may touch that code, but the full suite must still be re-run to prove it).
2. Run `gitleaks detect`.
3. Confirm via `git diff --stat` that only the authorised files/new files changed.
4. Confirm the new migration applies cleanly against a disposable Postgres instance and that both new tables' structural invariants (the partial unique index; the fencing column) are present exactly as specified.
5. Confirm no change to WO-114's scheduler artifacts, WO-117's authentication code, or Xero write-behaviour.

No production contact is authorised in Stage B.

### 6.C — Stage C: PR / Independent Audit / Architect Acceptance / Merge

Repository implementation must complete `Implementer → Independent Audit (repository content) → PR → Architect Acceptance → Merge → post-merge Security CI GREEN` before any production deployment. No production deployment may be sourced from an unmerged branch or an un-accepted SHA.

### 6.D — Stage D: Production deploy, disabled/inert (separately gated — not authorised by this WO alone)

After Stage C closes GREEN, a **separate** Architect authorisation (mirroring WO-117's own Stage D gate exactly) is required before: deploying the merged image containing this code (the evaluator script/plist are inert until installed); installing the new launchd plist; provisioning the real Pushover credential. This WO's own merge does **not** itself authorise any of Stage D onward.

### 6.E–6.G — Reserved

Pre-activation safety proof, explicit enablement, and bounded observation — structure to be defined in a future Architect directive once Stage C closes, mirroring WO-117's own Stage E–G discipline. Not specified further here to avoid pre-committing production-stage detail ahead of Stage C's own real evidence.

---

## 7. Explicit exclusions

This Work Order authorises **NO**: changes to WO-114's scheduler or classification code; changes to WO-117's authentication/session/CSRF/Origin code; a recurring mailbox-sweep scheduler of any kind (a separately governed, later workstream per PID §118.D/G); `STALE_INGESTION` detection logic; Gmail/Microsoft/IMAP OAuth token-handling changes or provider-connection-flow rewrites beyond the Needs-You-sync wiring at §6.A.4; any accounting-record mutation; any production credential provisioning or plist installation (reserved to the separately-gated Stage D); a general-purpose monitoring platform; any new PID or parallel Work Order.

If any stage's own verification would require one of these actions: **STOP and return to Architect.**

---

## 8. Read-only query discipline

Any production database query outside this WO's own authorised migration must be demonstrably read-only (`SELECT`/schema inspection/`READ ONLY` transaction mode). No DDL/DML against production is authorised by Stage A–C.

---

## 9. Evidence preservation

Never commit: Pushover credential values, any mailbox OAuth token/secret, or financial/message content in any test fixture or log. Use synthetic test data throughout. Run `gitleaks` before any PR this work produces.

---

## 10. Deliverables

### 10.1 Repository implementation (Stage A)
Exactly the files enumerated at §6.A — new migration, new domain module, provider-neutral Needs-You wiring, evaluator script, notification client, inert plist, full test coverage.

### 10.2 Closure document (after Stage C merge)
A brief closure note (not a full activation-closure document, since production activation is a separate, later-gated stage) recording: exact merge SHA; confirmation Stage A/B/C's own acceptance criteria all independently held; explicit statement that Stage D onward requires its own, separate Architect authorisation before any production action.

No secrets in any deliverable.

---

## 11. Independent Audit requirements

### 11.1 Repository-implementation audit (before Stage C's PR)

A fresh Auditor, no inherited conclusions, must independently verify: correct parent PID (§118, canonical); exact base SHA; scope limited to the enumerated files; the partial unique index (`mailbox_health_incidents`) and the real `(incident_id, purpose)` unique constraint (`mailbox_health_notifications`) and `claim_generation` fencing column genuinely exist in the migration exactly as specified; the fencing protocol genuinely prevents a stale worker's write from overwriting a newer claim's result (reproduce the race, don't just read the code — claim as worker A, force-expire, reclaim as worker B bumping the generation, attempt A's stale completion write, confirm it affects zero rows against the dual `claim_generation AND state='IN_PROGRESS'` predicate); reclaimed attempts genuinely count toward `max_attempts` and genuinely terminate at exhaustion (reproduce, don't assume); concurrent notification creation genuinely resolves idempotently via the real unique constraint, not merely by application-level convention (reproduce two simultaneous creators, confirm one row, both callers receive it); the pre-dispatch suppression check genuinely prevents a Pushover call for a notification whose incident has since resolved, in each of the `PENDING`/`RETRYABLE_FAILURE`/`IN_PROGRESS` states, while a `RECOVERY` notification is genuinely unaffected by this check; the shared-transaction design genuinely composes incident+NeedsYou+notification creation into one atomic commit-or-rollback unit (reproduce a forced mid-transaction failure and confirm no partial row survives in any of the three tables); the existing Needs-You test suite passes unmodified with the new optional parameter present; no change to WO-114/WO-117/Xero-write code; `gitleaks` clean; full test suite passes including the required concurrency matrix.

---

## 12. Acceptance criteria

GREEN only if **all** of the following hold:

1. Repository implementation scoped exactly to the enumerated files; no WO-114/WO-117/Xero-write code touched.
2. `mailbox_health_incidents`' partial unique index `(mailbox_id, failure_class) WHERE status='OPEN'` exists and is independently proven to prevent a duplicate `OPEN` row for the same mailbox+class under concurrent insertion.
3. A resolved incident's `(mailbox_id, failure_class)` pair is independently proven to admit a genuinely new `incident_id` on the next real failure — the episode mechanism, not merely asserted.
4. `mailbox_health_notifications`' claim predicate is independently proven to reclaim both due `PENDING`/`RETRYABLE_FAILURE` work and expired `IN_PROGRESS` work.
5. The `claim_generation` fencing token is independently proven, via a reproduced race (not code inspection alone), to make a stale/superseded worker's completion write a no-op.
6. The shared-transaction design (§6.A.3) is independently proven, via a reproduced forced mid-transaction failure, to leave no partial incident/NeedsYouItem/notification row in any of the three tables — true all-or-nothing commit.
7. Gmail and IMAP mailboxes going `AUTH_REQUIRED` now produce exactly one `NeedsYouItem` each, via the same mechanism already proven for Microsoft, with zero change to Microsoft's own existing behaviour.
8. `STALE_INGESTION` is defined in the taxonomy but has no active detection logic — confirmed by test, not merely by absence of code.
9. Repeated outage episodes (fail → incident → recover → resolve → fail again) produce two distinct `incident_id`s and two distinct `NeedsYouItem`s, the first provably untouched by the second.
10. Concurrent evaluator runs against the same new failure produce exactly one incident and one `INITIAL` notification — reproduced, not assumed.
11. A notification's abandoned `IN_PROGRESS` claim becomes reclaimable after the configured timeout, without duplicate delivery when the original worker's stale completion write is correctly rejected by the fencing check.
12. Bounded retry/backoff and eventual `TERMINAL_FAILURE` are proven for a simulated persistent provider failure, without the underlying incident ever silently closing as a result.
13. `REMINDER_24H` is proven, via the real `(incident_id, purpose)` unique constraint, to occur **at most once ever per incident** — not per 24-hour window, exactly once total, and never on every evaluator tick.
14. Full recovery round-trip closes the incident only per PID §118.H's exact per-class proof (never on bare `connection_state=CONNECTED` alone), with exactly one `RECOVERY` notification created.
15. Paused/retired mailboxes are proven structurally excluded from incident creation.
16. No financial evidence, mailbox content, or OAuth token material appears in any new table row, log line, or outbound payload — proven via the same kind of adversarial-marker test already proven effective in WO-117's own delivery history.
17. **Concurrent notification creation is idempotent**: two simultaneous attempts to create the same `(incident_id, purpose)` notification are independently proven to resolve to exactly one row via the real unique constraint, with both callers correctly receiving the same existing row — neither an unexplained error nor a duplicate dispatch.
18. **Reclaimed attempts count toward `max_attempts`**: a notification reclaimed after an abandoned `IN_PROGRESS` claim is independently proven to have its `attempt_count` incremented at reclaim time (not reset), and to transition to `TERMINAL_FAILURE` once the budget is exhausted — including via reclaim, not only via a normal failure response.
19. **Dual-predicate completion fencing** (`claim_generation` AND `state='IN_PROGRESS'`) is independently proven, via a reproduced stale-worker race, to make a superseded worker's completion write affect zero rows.
20. **Pre-dispatch suppression is independently proven**: an incident that resolves while its `INITIAL`/`REMINDER_24H` notification sits in each of `PENDING`, `RETRYABLE_FAILURE`, and `IN_PROGRESS` is independently proven, in each case, to result in that notification transitioning to `CANCELLED` (with a durable `cancellation_reason` and corresponding `AuditEvent`) **without** an actual Pushover send occurring — and a `RECOVERY` notification for the same incident is independently proven to be sent regardless (never subject to this suppression).
21. **No retraction of already-`ACCEPTED`** notifications is attempted or claimed anywhere in the implementation — confirmed by code review finding no such code path exists.
22. Repository-implementation Independent Audit GREEN; PR Architect-Accepted; true merge; post-merge Security CI GREEN.
23. WO-114's scheduler and WO-117's authentication/session behaviour independently confirmed completely unaffected throughout.
24. No production mutation of any kind occurred during Stage A–C.

If any material claim cannot be independently supported, return `RED`/`HOLD` with the exact unsupported claim, and return to the Architect — never rewrite evidence to make it fit.
