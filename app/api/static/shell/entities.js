// shell/entities.js — a small client-side cache in front of `GET
// /internal/entities` (CD-6 Slice 1, PID §98.3). Every place the GUI
// needs to populate a "Company" dropdown (the global upload modal, the
// Needs You review drawer) goes through THIS module, never a
// hardcoded list of entity names — the whole point of PID §98.3's "map
// to canonical entity IDs, never hardcode the labels as business
// truth". No business logic here: this is a pure read-through cache
// over one existing endpoint, re-fetched on demand by `refresh()`.
import { apiGet } from "../shared/api.js";

let _cache = null; // Array<{entity_id, canonical_name, display_name, ...}> | null
let _inFlight = null;

/** Returns the cached entity list, fetching it the first time (or
 * after `refresh()`). Never throws — on a fetch failure returns `[]`
 * so a caller's dropdown renders empty rather than crashing the whole
 * surface; the caller decides whether/how to surface that failure
 * (e.g. `shared/notify.js`). */
export async function listEntities() {
  if (_cache) return _cache;
  if (!_inFlight) {
    _inFlight = apiGet("/internal/entities").then(({ ok, body }) => {
      _cache = ok && body && Array.isArray(body.items) ? body.items : [];
      _inFlight = null;
      return _cache;
    });
  }
  return _inFlight;
}

export function refresh() {
  _cache = null;
  _inFlight = null;
  return listEntities();
}
