// features/activity/activity-api.js — the `/internal/activity` HTTP
// surface (CD-6 Slice 1, PID §98.8). Owned by the `activity` feature,
// same pattern as `features/ai/ai-api.js`/`features/needs-you/needs-you-api.js`.
import { apiGet } from "../../shared/api.js";

export const ACTIVITY_API = { list: "/internal/activity" };

export function listActivity({ limit = 25, before, eventTypePrefix } = {}) {
  const params = new URLSearchParams({ limit: String(limit) });
  if (before) params.set("before", before);
  if (eventTypePrefix) params.set("event_type_prefix", eventTypePrefix);
  return apiGet(`${ACTIVITY_API.list}?${params.toString()}`);
}
