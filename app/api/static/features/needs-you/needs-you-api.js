// features/needs-you/needs-you-api.js — the `/internal/needs-you/*`
// HTTP surface (CD-6 Slice 1, PID §98.2/§98.5). Owned by the
// `needs-you` feature, same "feature owns its own endpoint map" pattern
// `features/ai/ai-api.js` already established — never folded into
// `shared/api.js`, which only knows the pre-AI/pre-Needs-You canonical
// endpoints.
import { apiGet, apiPost } from "../../shared/api.js";

export const NEEDS_YOU_API = {
  list: "/internal/needs-you",
  one: (id) => `/internal/needs-you/${encodeURIComponent(id)}`,
  resolve: (id) => `/internal/needs-you/${encodeURIComponent(id)}/resolve`,
};

export function listNeedsYou({ status, itemType, domain, limit = 50, offset = 0 } = {}) {
  const params = new URLSearchParams({ limit: String(limit), offset: String(offset) });
  if (status) params.set("status", status);
  if (itemType) params.set("item_type", itemType);
  if (domain) params.set("domain", domain);
  return apiGet(`${NEEDS_YOU_API.list}?${params.toString()}`);
}

export function getNeedsYouItem(itemId) {
  return apiGet(NEEDS_YOU_API.one(itemId));
}

/** `resolution` is the free-form answer payload (Slice 1's own
 * `{entity_id, what, why}` shape for `COMPANY_WHAT_WHY` items).
 * `newStatus` defaults to `"RESOLVED"`; pass `"DISMISSED"` for the
 * explicit decline-to-answer path (PID §98.5). */
export function resolveNeedsYouItem(itemId, { newStatus = "RESOLVED", resolution, actorId }) {
  return apiPost(NEEDS_YOU_API.resolve(itemId), {
    new_status: newStatus,
    resolution: resolution || null,
    actor_type: "USER",
    actor_id: actorId,
  });
}
