// features/xero/xero-api.js — the `/internal/xero/*` HTTP surface
// (CD-6 Slice 2, PID §98.4, architect spec §1-24). Owned by the `xero`
// feature, same "feature owns its own endpoint map" pattern
// `features/needs-you/needs-you-api.js`/`features/ai/ai-api.js` already
// establish — never folded into `shared/api.js`.
import { apiGet, apiPost } from "../../shared/api.js";

export const XERO_API = {
  connect: "/internal/xero/connect",
  status: (entityId) => `/internal/xero/${encodeURIComponent(entityId)}`,
  accounts: (entityId, eligibleOnly = true) =>
    `/internal/xero/${encodeURIComponent(entityId)}/accounts?eligible_only=${eligibleOnly ? "true" : "false"}`,
  syncs: (entityId) => `/internal/xero/${encodeURIComponent(entityId)}/syncs`,
  syncNow: (entityId) => `/internal/xero/${encodeURIComponent(entityId)}/sync`,
  disconnect: (entityId) => `/internal/xero/${encodeURIComponent(entityId)}/disconnect`,
  suggestAccount: (entityId, evidenceId) =>
    `/internal/xero/${encodeURIComponent(entityId)}/evidence/${encodeURIComponent(evidenceId)}/suggest-account`,
  getSuggestion: (entityId, evidenceId) =>
    `/internal/xero/${encodeURIComponent(entityId)}/evidence/${encodeURIComponent(evidenceId)}/suggestion`,
};

export function getXeroStatus(entityId) {
  return apiGet(XERO_API.status(entityId));
}

/** `eligibleOnly` defaults to `true` — the ordinary coding-dropdown
 * case (architect spec §5's server-side eligibility policy). The
 * Settings/Connections tab's own account-COUNT display passes `false`
 * to show every synced account regardless of default eligibility. */
export function getXeroAccounts(entityId, eligibleOnly = true) {
  return apiGet(XERO_API.accounts(entityId, eligibleOnly));
}

export function getXeroSyncs(entityId) {
  return apiGet(XERO_API.syncs(entityId));
}

export function connectXero(entityId, actorId) {
  return apiPost(XERO_API.connect, { entity_id: entityId, actor_type: "USER", actor_id: actorId });
}

export function syncXeroNow(entityId, actorId) {
  return apiPost(XERO_API.syncNow(entityId), { actor_type: "USER", actor_id: actorId });
}

export function disconnectXero(entityId, actorId) {
  return apiPost(XERO_API.disconnect(entityId), { actor_type: "USER", actor_id: actorId });
}

/** Trigger the Xero Account Suggestion Producer for one evidence item
 * (`xero/account-suggestion-producer` WO). Not called by the ordinary
 * review drawer flow today (the producer is invoked server-side by
 * whatever pipeline first raises the XERO_ACCOUNT_REQUIRED item) —
 * exposed here for any future manual "suggest again" trigger. */
export function suggestXeroAccount(entityId, evidenceId, actorId) {
  return apiPost(XERO_API.suggestAccount(entityId, evidenceId), {
    actor_type: "USER",
    actor_id: actorId,
  });
}

/** The current suggestion/assignment for one evidence item, if any. */
export function getXeroAccountSuggestion(entityId, evidenceId) {
  return apiGet(XERO_API.getSuggestion(entityId, evidenceId));
}
