// features/documents/documents-api.js — the `/internal/documents/*`
// HTTP surface (CD-6 Slice 5 WI-5 §5). Owned by the `documents`
// feature, same "feature owns its own endpoint map" pattern
// `features/ai/ai-api.js`/`features/needs-you/needs-you-api.js` both
// already establish — `shared/api.js` only knows the pre-classification
// canonical endpoints.
//
// Every function here is a thin wrapper: build the URL, call `apiGet`,
// hand back `{ok, status, body}` untouched. No business logic — the
// caller decides what a response means (WI-5 §5 — this is a read
// projection, never a client-side classification authority, §51).
import { apiGet } from "../../shared/api.js";

export const DOCUMENTS_API = {
  list: "/internal/documents",
  one: (evidenceId) => `/internal/documents/${encodeURIComponent(evidenceId)}`,
};

/** `GET /internal/documents` — the bounded, evidence-first Documents
 * list projection (WI-5 §5/§6/§8/§9). `filters` may carry any of
 * `entityId`/`documentType`/`classificationStatus`/`classificationSource`/
 * `reviewRequired`/`receivedAtFrom`/`receivedAtTo` — all optional,
 * omitted filters match every document. */
export function listDocuments({
  entityId, documentType, classificationStatus, classificationSource, reviewRequired,
  receivedAtFrom, receivedAtTo, limit = 25, offset = 0,
} = {}) {
  const params = new URLSearchParams({ limit: String(limit), offset: String(offset) });
  if (entityId) params.set("entity_id", entityId);
  if (documentType) params.set("document_type", documentType);
  if (classificationStatus) params.set("classification_status", classificationStatus);
  if (classificationSource) params.set("classification_source", classificationSource);
  if (reviewRequired !== undefined && reviewRequired !== null) params.set("review_required", String(reviewRequired));
  if (receivedAtFrom) params.set("received_at_from", receivedAtFrom);
  if (receivedAtTo) params.set("received_at_to", receivedAtTo);
  return apiGet(`${DOCUMENTS_API.list}?${params.toString()}`);
}

/** `GET /internal/documents/{evidenceId}` — the single-document
 * projection (same row shape as one `listDocuments()` item). */
export function getDocument(evidenceId) {
  return apiGet(DOCUMENTS_API.one(evidenceId));
}
