// features/ai/ai-api.js — the `/internal/ai/*` + `/internal/operator/*`
// HTTP surface (CD-5 WI-2/WI-3 backend, WI-4 GUI client). Owned by the
// `ai` feature, not `shared/api.js` (which only knows the pre-AI
// canonical endpoints) — feature ownership, per this WI's own
// modularisation goal.
//
// Every function here is a thin wrapper: build the URL/body, call
// `apiGet`/`apiPost`, hand back `{ok, status, body}` untouched. No
// business logic — the caller (features/documents/detail.js,
// features/ai/ask-bagman.js, features/overview/overview.js) decides
// what a given response means and how to render it.

import { apiGet, apiPost } from "../../shared/api.js";

export const AI_API = {
  tasks: "/internal/ai/tasks",
  invocations: "/internal/ai/invocations",
  invocationOne: (id) => `/internal/ai/invocations/${encodeURIComponent(id)}`,
  health: "/internal/ai/health",
  operatorChat: "/internal/operator/chat",
};

//: The three CD-5 BACKGROUND tasks judged relevant to a Documents AI
//: panel (PID §45's dispatch names `DOCUMENT_TYPE_PROPOSAL`/
//: `DOCUMENT_SUMMARY` explicitly and leaves `ENTITY_PROPOSAL` to this
//: WI's own judgment — included here: an entity-ownership hint is
//: exactly the kind of thing an operator reviewing one document wants
//: to see alongside its type/summary, and it shares the same
//: `{evidence_id}` input shape as the other two).
// CD-6 Slice 5 WI-5 §21/§63 — `DOCUMENT_TYPE_PROPOSAL` v1 removed from
// this list: canonical document classification now goes through the
// governed WI-3 orchestrator (`features/documents/classification-api.js
// ::classifyWithBagman`, "Classify with BAGMAN" on the detail panel),
// never a new "Run analysis: Document type" v1 button. `DOCUMENT_SUMMARY`
// and `ENTITY_PROPOSAL` are unchanged — the generic AI panel/Ask BAGMAN
// keep working exactly as before, and historical v1 invocation CARDS
// still render fine (driven by real AIInvocation history via
// `listInvocationsForEvidence`, not this array).
export const DOCUMENT_BACKGROUND_TASKS = [
  { task_id: "DOCUMENT_SUMMARY", task_version: 1, label: "Summary" },
  { task_id: "ENTITY_PROPOSAL", task_version: 1, label: "Entity hint" },
];

/** `GET /internal/ai/invocations/{id}` — the full AIInvocation record
 * (WI-5 §17's "Why BAGMAN thinks this": `output.signals`/
 * `output.warnings`/`output.confidence`/`capability_alias`/
 * `provider_model`). */
export function getInvocation(aiInvocationId) {
  return apiGet(AI_API.invocationOne(aiInvocationId));
}

/** `POST /internal/ai/tasks` — dispatch one BACKGROUND task against
 * `evidence_id`. Synchronous (the fetch IS the "Running…" duration —
 * PID §38's own "never a fabricated multi-step progress animation"
 * discipline). Returns the raw `{ok, status, body}` — 200 is always a
 * terminal `AIInvocation` (SUCCEEDED or FAILED); 409 means an
 * ActiveInvocationConflictError (an analysis for this exact task is
 * already in flight); 404/422 are unexpected here in ordinary GUI use
 * (unknown task / bad input shape) but rendered honestly if they ever
 * occur. */
export function runBackgroundTask({ taskId, taskVersion, evidenceId, actorId }) {
  return apiPost(AI_API.tasks, {
    task_id: taskId,
    task_version: taskVersion,
    input_references: { evidence_id: evidenceId },
    actor_type: "USER",
    actor_id: actorId,
  });
}

/** `GET /internal/ai/invocations?primary_input_reference=<evidenceId>`
 * — every invocation (any task, any status, terminal or not) whose
 * canonical subject is this document (WI-4's own `primary_input_reference`
 * filter addition — see `ai/invocation.py`). Ordered newest-started
 * first by the server. */
export function listInvocationsForEvidence(evidenceId, { limit = 50 } = {}) {
  const params = new URLSearchParams({ primary_input_reference: evidenceId, limit: String(limit) });
  return apiGet(`${AI_API.invocations}?${params.toString()}`);
}

/** `GET /internal/ai/invocations?limit=N` — most recent invocations
 * across the whole system (Overview's "recent activity", PID §46). */
export function listRecentInvocations({ limit = 5 } = {}) {
  const params = new URLSearchParams({ limit: String(limit) });
  return apiGet(`${AI_API.invocations}?${params.toString()}`);
}

/** A best-effort "how many invocations are currently pending" count
 * (Overview, PID §46) — two small queries (REQUESTED + RUNNING), each
 * capped at the server's own max page size, summed. Deliberately NOT
 * an exact unbounded total (the API's own `count` field is "how many
 * came back on this page", not a separate total-count field — no
 * combined filter/count endpoint exists, and this WI's own dispatch
 * says two small queries is an acceptable, honestly-scoped answer
 * here). The caller renders "200+" (etc.) when a page came back full,
 * rather than silently presenting a truncated number as exact. */
export async function countPendingInvocations() {
  const CAP = 200; // ai.py's own _MAX_PAGE_SIZE
  const [requested, running] = await Promise.all([
    apiGet(`${AI_API.invocations}?${new URLSearchParams({ status: "REQUESTED", limit: String(CAP) })}`),
    apiGet(`${AI_API.invocations}?${new URLSearchParams({ status: "RUNNING", limit: String(CAP) })}`),
  ]);
  if (!requested.ok || !running.ok) return { ok: false, count: null, capped: false };
  const requestedCount = requested.body?.count ?? 0;
  const runningCount = running.body?.count ?? 0;
  const capped = requestedCount >= CAP || runningCount >= CAP;
  return { ok: true, count: requestedCount + runningCount, capped };
}

/** `GET /internal/ai/health` — Claude / bagman-fast / bagman-core
 * reachability (PID §46-48). CD-6 §103 Inference Architecture Ruling:
 * `bagman-deep` is retired; Trinity overflow (`trinity-core`) is
 * backlog/maintenance-mode only and deliberately NOT one of this
 * endpoint's live-health-checked keys — see its own `checks` response
 * shape and separate `trinity_core_overflow` status note. */
export function getAiHealth() {
  return apiGet(AI_API.health);
}

/** `POST /internal/operator/chat` — Ask BAGMAN (PID §42-44). `context`
 * is whichever of `evidence_id`/`intake_id`/`entity_id` is attached (at
 * most one is normally set from the GUI's own "Ask BAGMAN about this"
 * entry point). `conversationId`/`source` (CD-6 reliability delta, PID
 * §98/§100): `conversationId` is the GUI-generated "this open Ask
 * BAGMAN drawer session" id (see features/ai/ask-bagman.js) — the
 * backend's own conversation-scoped fallback subject when none of
 * `evidence_id`/`intake_id`/`entity_id` is attached, which is exactly
 * what fixed the previously-real, previously-documented "general chat
 * has no evidence_id" 422 for a bare "hi bagman" message (see
 * `ai.invocation.derive_primary_input_reference`'s own module
 * docstring for the full history). `source` records which UI surface
 * this call came from. */
export function sendOperatorChat({ message, actorId, evidenceId, intakeId, entityId, correlationId, conversationId, source }) {
  return apiPost(AI_API.operatorChat, {
    message,
    actor_type: "USER",
    actor_id: actorId,
    correlation_id: correlationId || null,
    evidence_id: evidenceId || null,
    intake_id: intakeId || null,
    entity_id: entityId || null,
    conversation_id: conversationId || null,
    source: source || null,
  });
}
