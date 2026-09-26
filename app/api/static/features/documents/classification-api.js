// features/documents/classification-api.js — the classification-action
// HTTP surface used by the Documents detail panel
// (features/documents/classification-panel.js) and the Needs You
// classification-review drawer
// (features/needs-you/classification-review.js) (CD-6 Slice 5 WI-5).
// Thin wrappers only — every governed decision happens server-side
// (WI-5 §51: this GUI invents no client-side classification/rule
// logic of its own).
import { apiGet, apiPost } from "../../shared/api.js";

export const CLASSIFICATION_API = {
  history: (evidenceId) => `/internal/evidence/${encodeURIComponent(evidenceId)}/classifications`,
  orchestrated: (evidenceId) => `/internal/evidence/${encodeURIComponent(evidenceId)}/classifications/orchestrated`,
  rulePreview: "/internal/evidence-classification/rules/preview",
  ruleOne: (ruleId) => `/internal/evidence-classification/rules/${encodeURIComponent(ruleId)}`,
};

/** `GET /internal/evidence-classification/rules/{ruleId}` — the
 * learned-rule detail (WI-5 §18's deterministic explanation: sender
 * scope, subject predicate). */
export function getClassificationRule(ruleId) {
  return apiGet(CLASSIFICATION_API.ruleOne(ruleId));
}

/** `GET /internal/evidence/{evidenceId}/classifications` — the full
 * DOCUMENT_TYPE classification lineage, oldest first, each item
 * carrying its own `is_current` flag (WI-5 §15). */
export function getClassificationHistory(evidenceId) {
  return apiGet(CLASSIFICATION_API.history(evidenceId));
}

/** WI-5 §20 — "Classify with BAGMAN": calls the governed, persistent
 * WI-3 orchestrator (deterministic-first, AI-fallback) directly. NEVER
 * calls generic `DOCUMENT_TYPE_PROPOSAL` v1 (WI-5 §20/§21). */
export function classifyWithBagman(evidenceId, { actorId, correlationId } = {}) {
  return apiPost(CLASSIFICATION_API.orchestrated(evidenceId), {
    actor_type: "USER",
    actor_id: actorId,
    correlation_id: correlationId || null,
  });
}

/** `POST /internal/evidence-classification/rules/preview` (WI-2, reused
 * verbatim by WI-5 §32's mandatory rule-teaching preview) — read-only,
 * never persists, never emits an audit event. */
export function previewClassificationRule({
  senderScopeType, senderScopeValue, subjectPredicateType, subjectPredicateValue, documentType,
}) {
  return apiPost(CLASSIFICATION_API.rulePreview, {
    sender_scope_type: senderScopeType,
    sender_scope_value: senderScopeValue,
    subject_predicate_type: subjectPredicateType,
    subject_predicate_value: subjectPredicateValue,
    document_type: documentType,
  });
}
