# WO-BAGMAN-117-SINGLE-OPERATOR-AUTH
## Single-operator authentication, private HTTPS transport, and shared Entity Context

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

Every future BAGMAN Delivery Controller, FORGE Implementer, and BAGMAN-authorised-production-executor prompt derived from this WO must reproduce this doctrine inline. Agents must never be expected to remember it from another conversation.

---

## 1. Work Order storage convention

Uses the established convention (`work-orders/`, one Markdown file per Work Order, no framework/database/automation). This file: `work-orders/WO-BAGMAN-117-SINGLE-OPERATOR-AUTH.md`.

---

## 2. Identity

| Field | Value |
|---|---|
| **Identifier** | `WO-BAGMAN-117-SINGLE-OPERATOR-AUTH` |
| **Parent PID** | `PID.md §117` (Single-Operator Authentication, Private HTTPS, and Entity Context — Architect ruling, 2026-10-13) |
| **Applicable inherited binding PID sections** | §0 (Current Production Authority); §111 (BAGMAN Delivery Governance Doctrine); §113 (Work Order lifecycle/authorisation semantics); §114 (scheduler — explicitly unaffected, must not be interrupted); §117.A–J (this amendment's full scope) |
| **Exact canonical architecture base SHA** | *(set to the exact SHA this WO is merged against — see §2.1)* |

### 2.1 Base SHA discipline

This WO is authored against canonical `main` at the SHA recorded in its own creation-PR description and durable Git history. The eventual Delivery Controller must independently verify canonical ancestry and the merged state of this WO, and that PID §117 remains canonical/unchanged, before any dispatch. Unexpected code/PID drift requires STOP and Architect review — never silent continuation.

---

## 3. Purpose

Implement and govern the activation of: (a) single-operator authentication (login/logout/session), (b) a minimal private HTTPS entry point preserving the existing Xero-OAuth Caddy leg untouched, and (c) the **shared, reusable Entity Context contract** (`core/entity_context.py`'s `resolve_entity_context(...)` — the server-side scoping/validation function) — per PID §117. This WO covers **two separate lanes inside one bounded delivery**, mirroring WO-114's own proven structure:

**Explicit scope boundary (per PID §117.H's clarified relationship to this WO):** this WO delivers authentication, HTTPS, and the Entity Context *contract* only — the server-side function every entity-scoped route can call. It does **not** deliver the global GUI entity selector, per-browser-tab entity persistence, the stale-response epoch guard, or the dirty-state-before-switch warning described in PID §117.H's GUI paragraph — those are explicitly deferred to a **separate, immediately-following governed Work Order** (dependent on this WO's own Stage C merge, since the selector needs `resolve_entity_context` to already exist), which this WO's own closure must name as the next required artifact. §6.A.5 below is correspondingly narrow: the one `shell.js` edit in this WO is only an authentication-redirect check, never an entity-selector implementation.

1. **Git-tracked repository implementation** (Stages A–C: code, migration, routes, login page, CLI script, Independent Audit, PR, Architect Acceptance, Merge).
2. **Governed production deployment and activation** (Stages D–G: deploy disabled → pre-activation safety proof with a tested rollback plan → explicit enablement → bounded observation), performed by the BAGMAN-authorised production executor under Delivery Controller governance, strictly staged and gated.

**This Work Order document itself, once canonical, authorises the full chain through closure** — no second Work Order is required for the production-activation half — but every stage gate below is binding, and a STOP at any stage returns to the Architect rather than being worked around.

---

## 4. Roles

### Delivery Controller — BAGMAN
Re-reads the canonical WO from Git before each dispatch; verifies the exact authorised SHA and that PID §117 remains canonical/unchanged at dispatch time; gives FORGE or the production executor only the bounded mandate for the stage being dispatched; does not reinterpret architecture; stops on ambiguity; never authorises Stage D onward until Stage A–C (repository implementation → merge → post-merge CI GREEN) is fully closed.

### Implementer — FORGE (Stage A only)
Performs **only** the bounded repository implementation at §6.A. Must not touch production, must not modify WO-114's scheduler artifacts, classification code, Xero write behaviour, migrations unrelated to this WO's own new tables, or any file outside the bounded scope. Must receive the full §0 doctrine inline in its dispatch prompt.

### BAGMAN-authorised production executor (Stages D–G only)
Operates under BAGMAN Delivery Controller governance — not a separate infrastructure platform, new service, or new architectural component. Performs the staged, read-mostly production deployment/activation sequence at §6.D–§6.G, strictly in order, strictly gated.

### Independent Auditor
A fresh Auditor with **no inherited FORGE/production-executor/Delivery-Controller conclusions**, dispatched **twice**: once against the repository implementation (before the Stage C PR), once against the production-activation evidence (before the activation-closure PR). Each dispatch must receive the full §0 doctrine inline and must inspect the actual evidence itself, never merely review prose.

---

## 5. Authorised production target

Sole target: **Mac mini `192.168.11.4`** (`mac-prod-01`), as defined by PID §0. Trinity is **retired/read-only migration archive + backup source only**, and is **NOT** a production target under any circumstance in this delivery.

---

## 6. Scope — staged delivery

### 6.A — Stage A: Repository implementation (FORGE)

FORGE may create or modify **only** the following:

1. **`core/auth.py`** — `verify_operator_password`, `SessionRecord`, `SessionRepository` (ABC) + `InMemorySessionRepository` + wiring for a Postgres-backed implementation (new file in `persistence/postgres/`), mirroring `core/entity.py`'s existing ABC + in-memory + Postgres repository shape exactly. **Argon2id only** — no bcrypt, per PID §117.C.
2. **`core/entity_context.py`** — `EntityContext` dataclass + `resolve_entity_context(...)`, generalising the already-proven `_require_entity` (`app/api/routers/xero.py`) and the redundant cross-entity ownership check (`xero.py:1007`) into one reusable function. Must not alter either existing call site's behaviour — only extract/generalise.
3. **`app/api/routers/auth.py`** — `POST /auth/login`, `POST /auth/logout`, `GET /auth/status`.
4. **`app/api/static/login.html`** — a new, separate static page (the static mount already serves multiple files via `html=True`).
5. **`app/api/static/shell/shell.js`** (minimal edit only) — an on-load `GET /auth/status` check and redirect-to-login if unauthenticated. This is a UX convenience only — FORGE must not treat it as the real access-control boundary (that is the server-side middleware at §6.A.6). **This item is authentication-only.** It must not include, and FORGE must not add, any entity-selector UI, header dropdown, per-tab entity persistence, or stale-response epoch guard — those belong entirely to the separate, immediately-following Work Order named in §3's scope boundary above.
6. **New middleware in `app/api/main.py`**, registered alongside (never replacing) the existing `correlation_id_middleware` — enforces session validity on every request **except** a narrowly, explicitly justified public-route allow-list. Each exemption below must be named individually, with its reason — never a blanket "everything under this router" grant:
   - `/health`, `/version`, `/ready`-equivalent — container healthchecks, run intra-container, never through a browser session.
   - `POST /auth/login`, `GET /auth/status` — the login mechanism itself cannot require a session to reach.
   - `GET /login.html` **and only the specific static assets the login page itself requires to render** (e.g. its own CSS/JS, not the whole static mount) — enumerated exactly, not a wildcard under `/`.
   - **The full provider OAuth browser lifecycle**, enumerated by exact real route, never a pattern-match: `POST /{entity_id}/disconnect` stays protected (it's an operator-initiated action), but every route an external provider's redirect or the operator's own browser hits *during* an OAuth round-trip must stay exempt exactly as today: `GET /oauth/result` (Xero), `GET /oauth/callback` (Xero), `GET /oauth/pending-selection/{selection_id}` (Xero multi-tenant candidate-selection page) and `POST /oauth/pending-selection/{selection_id}/resolve` (Xero candidate-selection submission) — all in `app/api/routers/xero.py`; `GET /microsoft/oauth/result` and `GET /microsoft/oauth/callback` in `mailboxes_microsoft.py`; `GET /gmail/oauth/result` and `GET /gmail/oauth/callback` in `mailboxes_gmail.py`. `POST /{mailbox_id}/{provider}/connect` and `/disconnect` remain protected (operator-initiated, not part of the external redirect chain).
   - FORGE must confirm, before merge, that each of these routes' **existing own security properties are untouched** by the new exemption: Xero's pending-tenant-selection flow already carries its own `selection_id`-scoped state/expiry/replay protection and the `_eligible_candidates` multi-tenant-consent disambiguation logic (`app/api/routers/xero.py`, the real architect-found defect this already fixed) — the new auth middleware must not weaken, bypass, or duplicate any of this; it only decides whether the *session* layer applies, never touches the OAuth state/CSRF-equivalent protections each provider flow already has.
   - FORGE must list every exempted path explicitly in its return, with the one-line justification above restated per path — an omission, or a broadened exemption beyond what's listed here, is a defect, not a minor detail, since either could silently break provider reconnection or silently widen the unauthenticated surface.
7. **One new Alembic migration** — `operator_sessions` table (session-token SHA-256 digest, created_at, expires_at, revoked_at) and, if needed, a minimal `operator_login_attempts` throttling table. No existing table is altered.
8. **`scripts/set_operator_password.py`** — governed CLI for initial credential creation and reset; writes only the Argon2id hash to a secret-file path; never logs/echoes plaintext; reset must revoke all existing sessions (calling into `core/auth.py`, not reimplementing revocation logic).
9. **Dependency addition**: Argon2id library (e.g. `argon2-cffi`), pinned, added to `requirements.txt` with the same annotation-comment discipline every existing dependency already carries. FORGE must confirm compatibility with the production container's base image/architecture (arm64 Mac mini) before finalising — a compiled-extension dependency that doesn't build or run on that architecture is a hard blocker, not a detail to paper over.
10. **CSRF protection**: a session-bound double-submit (or equivalent) token mechanism for state-changing requests, plus Origin-header verification as an independent second safeguard — per PID §117.E, `SameSite=Lax` alone is explicitly insufficient.
11. **Tests**: new, independent domain tests for `core/auth.py`/`core/entity_context.py`, plus the full access-control and OAuth-regression test matrix at §12 (acceptance criteria 19–21).

No production action of any kind occurs in Stage A. FORGE must not modify: WO-114's scheduler, classification code, Xero write behaviour, any existing migration, or any file outside the list above (as extended by §6.A′ below).

### 6.A′ — Architect Correction (Stage A/B, 2026-10-14)

Issued after direct Architect inspection of PR #35 (exact head `a951efc302ea27f7c162938f9debd269b080d5fe`), following a GREEN Round-2 Independent Audit. **This is a narrow correction to Stage A/B scope, not a new architecture decision** — the original §6.A/§6.B above, and all PID §117 architecture, remain binding and are not reopened.

**Extended Stage A file authorisation** — FORGE may additionally create or modify:

12. **`app/api/static/shared/api.js`**, **`app/api/static/features/mailbox/mailbox-api.js`**, **`app/api/static/shell/intake-upload.js`**, **`app/api/static/features/documents/documents.js`** — the real, independently-enumerated set of every GUI code path that issues a state-changing (`POST`/`PUT`/`PATCH`) browser request (confirmed exhaustive by direct grep of `app/api/static/` for `fetch(...)` calls carrying a non-GET method; no `DELETE` verb exists anywhere in the current GUI). Each must be updated to supply the session-bound CSRF header (`X-Bagman-Csrf-Token`, matching `app/api/main.py`'s existing `CSRF_HEADER_NAME`) on every authenticated state-changing request, reading the CSRF value from the same-origin, JS-readable `__Host-bagman_csrf` cookie already set at login (§6.A.10) — a **shared, minimal helper** (e.g. a small addition to `app/api/static/shared/api.js`, reused by the other three files, consistent with this codebase's existing vanilla-JS, no-build-step convention) is preferred over four independent implementations. Existing content types, multipart boundaries, idempotency headers, and all established business logic of every affected service (Needs You, Xero, AI, mailbox, Documents, intake) must be preserved exactly — this is a transport-header addition only, never a request-shape or business-logic change. The CSRF cookie value must never be sent to any non-same-origin destination.
13. **`scripts/set_operator_password.py`** (already-authorised file, extended requirement) — credential rotation must be failure-safe: the new password-hash file must be written securely and atomically (temp-file-plus-rename or equivalent within the same filesystem, restrictive permissions from creation, never a window where the file is world-readable or missing), and the script must **not report a successful reset** unless session invalidation has actually completed; session-store unavailability, file-write failure, or an incomplete reset must each be distinctly surfaced as failure, not silently swallowed into a false "success."
14. **`app/api/main.py`** (already-authorised file, extended requirement) — `_origin_is_trusted` must validate the **full** expected origin (scheme, hostname, and effective port), not netloc/host alone; it must continue to read the request's own `Host` header directly (never a client-supplied `X-Forwarded-Host`/`Forwarded` value) per PID §117.D's existing proxy-trust instruction, while correctly accounting for the narrowly-trusted HTTPS reverse-proxy topology (Caddy) once that topology exists. The independent CSRF-token requirement is retained unchanged alongside this correction — Origin verification is strengthened, not substituted for CSRF.

**Previously accepted dependency, now formally documented**: `app/api/composition.py` (the `operator_session_repository` DI wiring, +43 lines, first introduced in commit `6534da7860b80303c7dd88379d524e556a02040a`) is recorded here as a **justified, in-scope dependency** of §6.A.1's `SessionRepository` delivery — required by `composition.py`'s own pre-existing "no environment branching outside this file" invariant and by PID §117.G's instruction to extend the existing composition/DI pattern, consistent with how every other durable repository in this codebase is wired. Independently judged justified by both Round 1 and Round 2 Independent Audits. No further action on this item is required; it is not a new scope item, only a formal record.

**Required tests** (additive to §6.A.11 and §12):
- An end-to-end GUI-write-workflow test (a real logged-in session via `TestClient`, exercising at least one representative write through each of the four files above — an evidence/Documents multipart upload, an intake multipart upload, a Needs-You or Xero write through `apiPost`, and a mailbox write through the PUT helper) proving the request is **not blocked** by the auth-enforcement middleware when the correct CSRF header is supplied by the real client-side code path, and **is** rejected when it is withheld. A test that only supplies the CSRF header directly through a Python HTTP client, bypassing the actual JavaScript helper, does **not** satisfy this requirement — the real static JS must be exercised (e.g. via a headless-browser/DOM-executing test harness, or by asserting the shipped JS source itself constructs the header correctly against a live server, whichever this codebase's existing test conventions best support).
- Negative Origin-verification tests: HTTP `Origin` against an HTTPS-configured application; wrong hostname; wrong port; missing Origin; malformed Origin; an attempted untrusted-forwarded-header substitution — each independently proven rejected.
- A password-reset failure-safety test matrix: session-store unavailable during reset; password-hash file-write failure; any other incomplete-reset condition — each proven to **never** report success, and to leave no inconsistent half-rotated state.
- Re-confirmation that the existing sabotage-tested HTTP-level fail-closed tests (commit `a951efc302ea27f7c162938f9debd269b080d5fe`) remain passing and effective unchanged.
- Full login/logout/cookie/session-expiry/CSRF-rejection/complete-OAuth-compatibility re-confirmation under the corrected code.

No production access, deployment, database mutation, or authentication activation is authorised by this correction. `BAGMAN_AUTH_ENABLED` remains disabled by default throughout. Xero domain logic, classification logic, banking records, provider credentials, and production configuration remain untouched. This correction does not reopen any settled WO-117 architecture decision, and does not authorise a new PID or a parallel Work Order.

### 6.A″ — Architect Correction (Stage A/B, 2026-10-15)

Issued after direct Architect inspection of PR #35 (exact head `0e327c470d06e60d5e03d682f2203c024adf5d3a`), following a GREEN Independent Audit of the §6.A′ correction. The Independent Auditor's own observation of a middleware-ordering defect (correctly identified as pre-existing, not introduced by §6.A′) is confirmed by the Architect as requiring correction before merge. **This is a further narrow correction to Stage A/B scope, not a new architecture decision** — §6.A, §6.A′, and all PID §117 architecture remain binding and are not reopened. The GUI CSRF, Origin-validation, and password-reset corrections already accepted under §6.A′ are not reopened.

**Extended Stage A file authorisation** — FORGE may additionally modify:

15. **`app/api/main.py`** (already-authorised file, extended requirement) — **middleware registration order**: Starlette's `@app.middleware("http")` registers each decorated function such that the most-recently-registered becomes outermost. The current registration order (`correlation_id_middleware` first, `auth_enforcement_middleware` second) therefore makes the auth-enforcement layer outermost in practice, contradicting this file's own docstring claim and causing every `401`/`403`/`503` the auth layer returns to bypass correlation-ID tagging and the sanitised completion log entirely. Correct the registration (or otherwise restructure, e.g. via explicit `add_middleware` ordering or a single combined middleware) so that `correlation_id_middleware` genuinely wraps `auth_enforcement_middleware` — every response, including every authentication/CSRF/Origin/session-store-failure rejection, must carry `X-Correlation-Id` and produce the existing sanitised, path-only completion log line. Must preserve unchanged: existing sanitised path-only request logging (never the query string); no OAuth code/state/credential logging; existing correlation-ID behaviour for already-passing requests; authentication enforcement and fail-closed semantics (no weakening); the full public-route/OAuth allow-list behaviour.
16. **`app/api/routers/auth.py`** (already-authorised file, extended requirement) and **`app/api/main.py`** — **sanitise authentication 5xx responses**: `PostgresSessionRepository`'s `PersistenceError` messages may carry underlying SQLAlchemy exception detail (SQL text, driver exceptions, connection strings). These must never reach a browser response. Session-store failures during authentication enforcement, session-creation failures at login, and missing/unreadable operator-credential configuration must each return a stable, generic, client-safe error code/message — the real diagnostic detail is retained only in server-side logs (which must themselves remain subject to this codebase's existing sanitisation discipline — no SQL text, DB URLs, driver exceptions, filesystem paths, or credential material, even server-side, beyond what is already an accepted logging pattern elsewhere in this codebase). No passwords, session cookies, CSRF tokens, OAuth codes/states, or raw SQL exception text may ever appear in an HTTP response header or body.

**Required tests** (additive to §6.A.11, §6.A′, and §12):
- Regression proof that unauthenticated, CSRF-rejected, Origin-rejected, and session-store-unavailable requests each receive a correlation ID (`X-Correlation-Id` header present) and generate the expected sanitised completion log entry — not merely that the rejection itself still occurs correctly.
- Adversarial tests injecting synthetic exception messages containing identifiable, unique sensitive markers (e.g. a fake SQL fragment, a fake DB host/credential-shaped string) into the session-repository failure path, proving those exact markers are **absent** from the HTTP response (status, headers, and body) while a generic, stable error code/message is returned instead.
- Re-confirmation that the existing sabotage-tested HTTP-level fail-closed tests (`a951efc302ea27f7c162938f9debd269b080d5fe`) and the §6.A′ GUI-CSRF-browser-proof/Origin-verification/password-reset tests all remain passing, unchanged in their own pass/fail meaning.
- Full login/logout/cookie/session-expiry/CSRF-rejection/complete-OAuth-compatibility re-confirmation under the corrected code.

No production access, deployment, database mutation, or authentication activation is authorised by this correction. `BAGMAN_AUTH_ENABLED` remains disabled by default throughout. No new PID, authentication architecture, service, user model, or implementation Work Order is authorised. This correction does not reopen any settled WO-117 architecture decision, nor the GUI CSRF/Origin-validation/password-reset corrections already accepted under §6.A′.

### 6.B — Stage B: Repository verification (FORGE, pre-audit)

1. Run the new domain tests plus the full existing relevant suite (to prove no regression to Xero/Needs-You/evidence/mailbox routes now sitting behind the new middleware).
2. Run `gitleaks detect` against the branch.
3. Confirm via `git diff --stat` that only the authorised files changed.
4. Confirm the Argon2id dependency actually imports and a real hash/verify round-trip succeeds in the test environment (not just "it installed").
5. Confirm the public-route allow-list is exhaustive against the real router table — enumerate every registered route and classify each as public or protected explicitly in the return.
6. Confirm no change to WO-114's scheduler artifacts, classification logic, or Xero write-behaviour.

No production contact is authorised in Stage B.

### 6.C — Stage C: PR / Independent Audit / Architect Acceptance / Merge (repository implementation)

Repository implementation must complete `Implementer → Independent Audit (repository content) → PR → Architect Acceptance → Merge → post-merge Security GREEN` before any production deployment. No production deployment may be sourced from an unmerged branch or an un-accepted SHA.

### 6.D — Stage D: Production deploy, disabled/inert (production executor)

After Stage C closes GREEN:

1. Deploy the merged `bagman-api` image containing the new auth code, but with the new middleware **feature-flagged off** (e.g. an explicit `BAGMAN_AUTH_ENABLED` environment toggle, defaulting to `false`) — the application runs with the new code present but inert, exactly mirroring WO-114's own "installed but unloaded" discipline.
2. Separately, read-only investigate and document the **exact** HTTPS routing change needed (new Caddy site block, new `pf` rule) **without applying it yet** — per PID §117.D's explicit instruction not to assume the existing Caddy container can be widened without consequence. Verify real hostname/DNS/interface/certificate facts live before drafting any deployment command.
3. No recurring/live authentication enforcement occurs in Stage D.

### 6.E — Stage E: Pre-activation safety proof (production executor, read-only + the one scoped HTTPS/firewall change)

Before flipping `BAGMAN_AUTH_ENABLED` on:

1. Prove the Argon2id CLI bootstrap script genuinely works on the production host (create a real operator credential in a disposable/test fashion first if practical, or prove it via the Stage B test environment's own already-passing round-trip — FORGE/the executor must not guess).
2. Apply the additive Caddy site block + `pf` rule extension for the new HTTPS port (per §117.D) — this is the one Stage E change permitted to touch live infrastructure, and must have a **tested rollback plan** (revert the Caddy config addition, revert the `pf` rule addition — both purely additive, so rollback is a clean removal, not a restore-from-backup).
3. Verify the existing Xero-OAuth Caddy leg (`:443`, `127.0.0.1:8543`) is completely unaffected — same config file, same hostname/path/connectivity contract, re-tested.
4. Verify HTTPS reachability and certificate trust from the operator's actual device over the new port, on the restricted subnet.
5. Confirm `bagman-api`'s direct HTTP port remains the *only* currently-working path (auth still disabled) — this is the last point at which the old access path is still open, by design, before Stage F flips enforcement on.

Any failed gate: **STOP — RED. Do not proceed to Stage F.**

### 6.F — Stage F: Explicit enablement (production executor)

Only after every Stage E gate is GREEN: flip `BAGMAN_AUTH_ENABLED` to `true` (a config/env change + scoped `bagman-api` recreate, mirroring WO-114's own proven T_ACT-remediation pattern exactly — same image, no rebuild, no unrelated container touched). Immediately verify login/logout work end-to-end over the new HTTPS path. Only once this is confirmed stable: narrow `bagman-api`'s direct HTTP port exposure (per §117.D) so it is no longer directly reachable from operator devices — this is its own explicit, individually-recorded sub-step with its own tested rollback (widen the `pf` rule back, or re-publish the port) if anything breaks.

### 6.G — Stage G: Bounded observation (production executor + Delivery Controller)

Observe real operator login/logout/session-expiry behaviour over a bounded window. Prove: existing Xero/Gmail/Microsoft OAuth reconnection flows still work unauthenticated through the allow-list; existing evidence/Needs-You/Documents GUI workflows still work once logged in; WO-114's scheduler continues firing on its normal cadence, completely unaffected by any of this (a live, direct check, not an assumption); no unintended production mutation beyond exactly what Stages E/F name.

---

## 7. Explicit exclusions

This Work Order authorises **NO**: users table, role/permission framework, multi-operator capability; public signup; SSO/LDAP/OAuth-login-provider integration for operator identity; organisation/workspace/tenant membership tables; per-user entity-permission matrices; any change to Xero/Microsoft/Gmail OAuth *provider-connection* flows beyond preserving their existing callback contracts; any Banking/Invoicing/Tax domain implementation (separately governed); any interruption to WO-114's live scheduler; any interruption to NoustAI accounting discovery/reconciliation planning; any change to classification/evidence domain logic beyond the new `entity_context.py` generalisation at §6.A.2; Redis/Celery/Kafka or any broker infrastructure; a general-purpose identity platform.

If any stage's own verification would require one of these actions: **STOP and return to Architect.**

---

## 8. Read-only query discipline

Any production database query outside the explicitly-authorised Stage D/E/F mutations must be demonstrably read-only (`SELECT`/schema inspection/`READ ONLY` transaction mode). No DDL/DML outside this WO's own one authorised migration (§6.A.7) and the scoped container recreates already named.

---

## 9. Evidence preservation

Capture sufficient evidence to support every finding without exposing secrets. Never commit: the operator's plaintext password, password hash, session tokens (raw or digest need not be secret but should not be gratuitously logged), OAuth tokens, API keys, or any other credential. Use IDs, timestamps, hashes, counts, redacted excerpts, command outputs with secrets removed. Run `gitleaks` before any PR this work produces.

---

## 10. Deliverables

### 10.1 Repository implementation (Stage A)
Exactly the files enumerated at §6.A — new auth/entity-context modules, routes, login page, migration, CLI script, dependency addition, middleware, tests.

### 10.2 Activation closure document (after Stage G)
`work-orders/WO-BAGMAN-117-SINGLE-OPERATOR-AUTH-CLOSURE.md`, containing: WO identifier; canonical repository-implementation merge SHA; production authority proof; Stage D/E/F/G commands/checks performed; credential-bootstrap verification; HTTPS/certificate verification; the hard "no regression" proof for Xero/Gmail/Microsoft/evidence/Needs-You/WO-114-scheduler, including the full provider OAuth browser-lifecycle regression at §12 criterion 21; rollback-plan verification (that it was tested, not merely described); deviations; evidence references; verdict; and an explicit, named recommendation for the separate, immediately-following governed Work Order that delivers the global GUI entity selector and its integration (per PID §117.H's clarified scope split and §12 criterion 22) — this closure must not declare the Entity Context work "done" without naming that dependency explicitly.

No secrets in either deliverable.

---

## 11. Independent Audit requirements

### 11.1 Repository-implementation audit (before Stage C's PR)
A fresh Auditor, no inherited conclusions, must independently verify: correct parent PID (§117, canonical); exact base SHA; scope limited to the enumerated files; Argon2id (never bcrypt) actually used; session-token digest (never raw token) actually persisted; CSRF + Origin-verification both present; fail-closed behaviour on session-store unavailability; the public-route allow-list is genuinely exhaustive against the real router table; no change to WO-114/classification/Xero-write code; `gitleaks` clean; full test suite passes including the new regression tests.

**Extended by §6.A′ (2026-10-14 Architect correction)**: the Auditor must additionally independently verify: every real GUI write path (`api.js`/`mailbox-api.js`/`intake-upload.js`/`documents.js`) genuinely supplies the CSRF header via the actual shipped client-side code (not merely asserted); a real logged-in GUI write workflow succeeds end-to-end under `BAGMAN_AUTH_ENABLED=true` and is rejected when the CSRF header is withheld; `_origin_is_trusted` genuinely validates scheme+host+port (not netloc/host alone), with each required negative case independently reproduced; `scripts/set_operator_password.py` never reports success on an incomplete reset, independently reproduced for session-store-unavailable, file-write-failure, and other incomplete-reset conditions; the previously sabotage-tested fail-closed HTTP tests remain passing and effective; full login/logout/cookie/session-expiry/CSRF-rejection/OAuth-compatibility behaviour is unchanged.

**Extended by §6.A″ (2026-10-15 Architect correction)**: the Auditor must additionally independently verify, by direct inspection of actual middleware execution order (not file/registration order alone, e.g. via `app.user_middleware` introspection or an equivalent live-request proof) and real request execution: that every 401/403/503 rejection genuinely carries `X-Correlation-Id` and produces the existing sanitised completion log entry; and, via an adversarial test injecting a synthetic, uniquely-identifiable sensitive marker into the session-repository failure path, that no authentication-related 5xx response ever exposes that marker (or any SQL/DB/credential/filesystem detail) in its status, headers, or body.

### 11.2 Production-activation audit (before the activation-closure PR)
A second fresh Auditor, no inherited conclusions, dispatched against the real production evidence after Stage G. Must independently repeat, read-only wherever possible: a real login/logout round-trip over the new HTTPS path; session persistence across a routine `bagman-api` recreate; password-reset session invalidation; CSRF rejection; the Xero-OAuth Caddy leg's continued, unchanged operation; WO-114's scheduler continuing to fire normally; the narrowed direct-HTTP-port exposure; absence of any other production mutation. Must explicitly answer: does the evidence support "a secure single-operator session now gates BAGMAN, with zero regression to existing autonomous operations or OAuth connectivity"?

Both audits must receive the full §0 doctrine inline and must bind their verdict to the exact commit/evidence-set audited.

---

## 12. Acceptance criteria

GREEN only if **all** of the following hold:

1. Repository implementation scoped exactly to the enumerated files; no WO-114/classification/Xero-write code touched.
2. Argon2id used; raw session tokens never persisted, only SHA-256 digests.
3. Session cookie is `Secure`/`HttpOnly`/`SameSite=Lax`/`Path=/`, no `Domain` attribute, `__Host-` prefix where practical.
4. CSRF + Origin verification both present and independently tested.
5. Fail-closed on session-store unavailability, independently tested.
6. Public-route allow-list exhaustive; no OAuth callback path broken.
7. Repository-implementation Independent Audit GREEN; PR Architect-Accepted; true merge; post-merge Security GREEN.
8. Stage D deploys with auth inert; no live enforcement before Stage F.
9. The existing Xero-OAuth Caddy leg is provably unchanged and still working after the new HTTPS block is added.
10. New HTTPS entry point reachable with a device-trusted certificate (no routine browser warning).
11. `pf` firewall extension mirrors the existing restriction's subnet scope exactly.
12. `bagman-api`'s direct HTTP port is no longer directly reachable from operator devices once Stage F completes, with a tested, proven rollback path.
13. Credential bootstrap/reset CLI tested; reset provably revokes all existing sessions.
14. WO-114's scheduler independently confirmed firing normally, unaffected, during and after this delivery.
15. NoustAI accounting discovery/reconciliation work independently confirmed uninterrupted.
16. Production-activation Independent Audit GREEN, explicitly answering the §11.2 question affirmatively.
17. Activation-closure PR Architect-Accepted; true merge; post-merge Security GREEN.
18. No production mutation occurred beyond exactly the named Stage D/E/F actions.
19. **Positive access tests** (each independently run, not assumed): `GET /login.html` and its enumerated essential static assets return 200 with **no** session cookie present; `POST /auth/login` with correct credentials succeeds without a prior session; `GET /auth/status` is reachable unauthenticated.
20. **Negative access tests**: every other static resource and every protected API route (evidence downloads, internal APIs, Needs-You, Documents, Xero account-suggestion/assignment routes, Activity, Connections) returns an authentication-required response with **no** session cookie present — enumerated against the real, full router table, not a sample.
21. **Full provider OAuth browser-lifecycle regression, end-to-end, for all three providers**: a real (or realistically simulated) Xero/Microsoft/Gmail connect → external redirect → `.../oauth/callback` → `.../oauth/result` round-trip completes successfully **with no session cookie present throughout**, proving the new middleware does not insert itself into any leg of the external redirect chain; additionally, for Xero specifically, the `GET /oauth/pending-selection/{selection_id}` page and its `POST .../resolve` submission are exercised end-to-end and independently proven to retain their existing `selection_id`-scoped state, expiry, replay, and multi-tenant candidate-selection (`_eligible_candidates`) protections completely unchanged — not merely reachable, but functionally identical to pre-WO-117 behaviour.
22. The PID §117.H scope split is honoured: no entity-selector GUI, per-tab persistence, or stale-response guard exists anywhere in this WO's own delivered code; the activation-closure document (§10.2) explicitly names the separate, immediately-following Work Order required to deliver those, and records it as this WO's own required dependency output, not an optional afterthought.

**Added by §6.A′ (2026-10-14 Architect correction):**

23. Every real GUI write path (`api.js`/`mailbox-api.js`/`intake-upload.js`/`documents.js`) supplies the session-bound CSRF header on authenticated state-changing requests via the actual shipped client-side code; a real logged-in GUI write workflow (not a bare Python HTTP client bypassing the JS) is independently proven to succeed with the header present and to be rejected without it, with existing content types, multipart boundaries, and business logic unchanged.
24. `_origin_is_trusted` validates the full expected origin (scheme, hostname, effective port); independently tested negative cases cover HTTP-against-HTTPS, wrong hostname, wrong port, missing/malformed Origin, and an attempted untrusted-forwarded-header substitution; the independent CSRF-token requirement is retained unchanged.
25. `scripts/set_operator_password.py` never reports a successful reset unless session invalidation has actually completed; the password-hash file is written atomically with restrictive permissions from creation; session-store-unavailable, file-write-failure, and incomplete-reset conditions are each independently tested and proven to surface as failure, never a false success.

**Added by §6.A″ (2026-10-15 Architect correction):**

26. `correlation_id_middleware` genuinely wraps `auth_enforcement_middleware` in actual request-handling order (not merely in file/registration order); every unauthenticated, CSRF-rejected, Origin-rejected, and session-store-unavailable response independently proven to carry `X-Correlation-Id` and produce the existing sanitised, path-only completion log entry; existing passing-request correlation/logging behaviour, fail-closed semantics, and the full public-route/OAuth allow-list remain unchanged.
27. No authentication-related `5xx` HTTP response (session-store failure during enforcement, session-creation failure at login, missing/unreadable operator-credential configuration) ever exposes SQL text, database connection details, driver exception content, filesystem paths, or credential material in its status, headers, or body; each such case independently proven, via an adversarial test injecting a synthetic exception carrying a unique sensitive marker, to return only a stable, generic, client-safe error code/message with the marker verifiably absent from the full HTTP response.

If any material claim cannot be independently supported, return `RED`/`HOLD` with the exact unsupported claim, and return to the Architect — never rewrite evidence to make it fit.
