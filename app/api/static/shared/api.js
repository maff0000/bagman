// shared/api.js — the canonical (pre-AI) BAGMAN HTTP endpoint map, plus
// the generic fetch/error-shape helpers every feature module builds
// on. CD-4 WI-4's `API`/`apiGet`/`errorMessage`, relocated unchanged by
// CD-5 WI-4's modularisation, plus one small, generic addition
// (`apiPost`) the new AI surfaces need (features/ai/ai-api.js is the
// only caller of it in this WI).
//
// This module deliberately knows nothing about `/internal/ai/*` or
// `/internal/operator/*` — those endpoint paths/wrappers live in
// `features/ai/ai-api.js`, which owns that surface (feature ownership,
// per this WI's own modularisation goal). Nothing here decides
// evidence business logic (PID §40) — it only shapes HTTP calls and
// renders whatever the server said.

export const API = {
  health: "/health",
  ready: "/ready",
  version: "/version",
  intake: "/internal/intake",
  intakeOne: (id) => `/internal/intake/${encodeURIComponent(id)}`,
  intakeEvidence: "/internal/intake/evidence",
  evidence: "/internal/evidence",
  evidenceOne: (id) => `/internal/evidence/${encodeURIComponent(id)}`,
  evidenceContent: (id) => `/internal/evidence/${encodeURIComponent(id)}/content`,
  provenance: (subjectType, subjectId) =>
    `/internal/provenance/${encodeURIComponent(subjectType)}/${encodeURIComponent(subjectId)}`,
};

export async function apiGet(path) {
  const res = await fetch(path, { headers: { Accept: "application/json" } });
  let body = null;
  try {
    body = await res.json();
  } catch {
    body = null;
  }
  return { ok: res.ok, status: res.status, body };
}

//: WO-BAGMAN-117 §6.A'.12 (Architect correction 2026-10-14) — the ONE
//: shared CSRF-header-construction helper every state-changing GUI
//: fetch() call in this codebase must spread into its own `headers`
//: object, so the double-submit check `app/api/main.py`'s
//: `auth_enforcement_middleware` already enforces server-side (PID
//: §117.E) is actually satisfied by the real shipped client, not just
//: by a Python test client supplying the header directly.
//:
//: Reads the CSRF value from the same-origin, deliberately
//: NOT-`HttpOnly` `__Host-bagman_csrf` cookie `app/api/routers/
//: auth.py::_set_session_cookies` sets at login — a plain, same-
//: document `document.cookie` read, never a cross-origin fetch/XHR of
//: any kind, so the value never leaves this same origin by anything
//: this helper does. `apiPost` below (and, via it, every feature
//: module that already imports `apiPost` — mailbox/needs-you/xero/ai)
//: picks this up automatically; `features/mailbox/mailbox-api.js`'s own
//: local `apiPut`, `shell/intake-upload.js`'s multipart upload, and
//: `features/documents/documents.js`'s own multipart upload each spread
//: this same helper's result into their own `fetch()` headers directly
//: (see those files' own call sites) — one implementation, reused
//: everywhere, per this codebase's existing no-build-step convention.
function _readCsrfCookie() {
  if (typeof document === "undefined" || !document.cookie) return "";
  const cookies = document.cookie.split("; ");
  for (const cookie of cookies) {
    const eq = cookie.indexOf("=");
    if (eq === -1) continue;
    if (cookie.slice(0, eq) === "__Host-bagman_csrf") {
      try {
        return decodeURIComponent(cookie.slice(eq + 1));
      } catch {
        return cookie.slice(eq + 1);
      }
    }
  }
  return "";
}

/** Returns `{"X-Bagman-Csrf-Token": "<value>"}` when the CSRF cookie is
 * present, or `{}` (never a header with an empty value) when it is not
 * — pre-login, or with auth disabled, a request is shaped EXACTLY as it
 * was before this correction; the server-side middleware is the one
 * and only place that decides whether a missing header is rejected
 * (PID §117.E), this helper never fabricates a value to satisfy it.
 * Always safe to spread into a `headers` object: `{...csrfHeaders()}`. */
export function csrfHeaders() {
  const token = _readCsrfCookie();
  return token ? { "X-Bagman-Csrf-Token": token } : {};
}

/** Generic JSON POST — used by features/ai/ai-api.js (`POST
 * /internal/ai/tasks`, `POST /internal/operator/chat`). Never throws
 * on a non-2xx response (mirrors `apiGet`'s own contract): callers
 * always get back `{ok, status, body}` and decide what to render,
 * including the honest in-flight/failure states PID §38/§76 require. */
export async function apiPost(path, jsonBody) {
  let res;
  try {
    res = await fetch(path, {
      method: "POST",
      headers: { "Content-Type": "application/json", Accept: "application/json", ...csrfHeaders() },
      body: JSON.stringify(jsonBody),
    });
  } catch (networkErr) {
    return { ok: false, status: 0, body: null, networkError: networkErr.message };
  }
  let body = null;
  try {
    body = await res.json();
  } catch {
    body = null;
  }
  return { ok: res.ok, status: res.status, body };
}

/** Extract a human error message from whatever shape main.py's error
 * handling actually returns (see app/api/main.py's module docstring):
 * a BagmanError-mapped response is {error_code, message[, correlation_id]};
 * a bare FastAPI HTTPException (e.g. malformed multipart 'metadata')
 * is {detail: "..."}; anything else falls back to the raw HTTP status. */
export function errorMessage(status, body) {
  if (body && typeof body === "object") {
    if (body.message) {
      const corr = body.correlation_id ? ` (correlation_id: ${body.correlation_id})` : "";
      return `${body.error_code || "ERROR"}: ${body.message}${corr}`;
    }
    if (body.detail) return String(body.detail);
  }
  return `HTTP ${status}`;
}
