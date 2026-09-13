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
      headers: { "Content-Type": "application/json", Accept: "application/json" },
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
